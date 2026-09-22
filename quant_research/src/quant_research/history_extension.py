"""Explicit, row-traceable cross-vendor extension. Never overwrite source bars."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .artifacts import file_hash, verify_files
from .calendar import calendar
from .sina_prepare import parse_adjustments, total_return_proxy

FIELDS = ["Open", "High", "Low", "Close", "Volume"]
REPAIRS = {"XLB": ["2018-11-15"], "XLE": ["2018-11-15"], "XLRE": ["2017-07-11"]}
EARLY_GAPS = ["2015-10-14", "2015-11-02", "2015-11-25", "2015-11-27", "2015-12-07", "2016-01-25", "2016-02-16"]
FIRST_TRADE = {"XLRE": "2015-10-08", "XLC": "2018-06-19"}


def invalid_bars(frame):
    return ((frame.High < frame[["Open", "Low", "Close"]].max(axis=1))
            | (frame.Low > frame[["Open", "High", "Close"]].min(axis=1))
            | (frame[FIELDS[:4]] <= 0).any(axis=1) | (frame.Volume < 0)
            | ~np.isfinite(frame[FIELDS]).all(axis=1))


def parse_tencent(payload, symbol, market):
    key = f"us{symbol}.{market}"
    if payload.get("code") != 0 or key not in payload.get("data", {}):
        raise ValueError("wrong Tencent instrument or response status")
    node = payload["data"][key]
    if not node.get("day") or "qfqday" in node or "hfqday" in node:
        raise ValueError("requires explicit unadjusted day bars, not adjusted prices")
    table = pd.DataFrame([row[:6] for row in node["day"]], columns=["date", "Open", "Close", "High", "Low", "Volume"])
    table.index = pd.to_datetime(table.pop("date"), format="%Y-%m-%d", errors="raise")
    table = table[FIELDS].astype(float)
    table.index.name = "session"
    if table.index.has_duplicates or not table.index.is_monotonic_increasing or invalid_bars(table).any():
        raise ValueError("invalid Tencent OHLCV")
    if not table.index.isin(calendar().sessions).all():
        raise ValueError("Tencent bars outside XNYS calendar")
    return table


def merge_registered(base, other, symbol, expected):
    """Only registered invalid rows or missing dates may change; overlap is audited."""
    verification_base = base
    base = base.reindex(base.index.intersection(expected)).copy()
    bad = base.index[invalid_bars(base)]
    if set(bad.strftime("%Y-%m-%d")) != set(REPAIRS.get(symbol, [])):
        raise ValueError(f"{symbol}: invalid rows differ from predeclared repair list")
    missing = expected.difference(base.index)
    allowed_gaps = pd.DatetimeIndex(EARLY_GAPS if symbol == "XLRE" else [])
    needed = missing.difference(allowed_gaps).union(bad)
    if len(needed) and (other is None or not needed.isin(other.index).all()):
        raise ValueError(f"{symbol}: missing independently sourced repair bars")
    overlap = {}
    if other is not None:
        common = verification_base.index.intersection(other.index)
        common = common.difference(verification_base.index[invalid_bars(verification_base)])
        if len(common) < 15:
            raise ValueError("insufficient cross-vendor overlap")
        delta = (verification_base.loc[common, FIELDS[:4]] - other.loc[common, FIELDS[:4]]).abs()
        price_limit = .120001 if symbol == "XLRE" else .030001
        if float(delta.max().max()) > price_limit:
            raise ValueError("unreviewed cross-vendor price discrepancy")
        ratios = other.loc[common, "Volume"] / verification_base.loc[common, "Volume"].replace(0, np.nan)
        overlap = {"sessions": len(common), "max_abs_price_difference": delta.max().to_dict(),
                   "volume_ratio_min": float(ratios.min()), "volume_ratio_max": float(ratios.max()),
                   "price_tolerance_usd": price_limit,
                   "volume_status": "share_units_only_not_independently_verified_consolidated_volume"}
    merged = base.reindex(base.index.union(needed))
    patches = []
    for day in needed:
        original = base.loc[day].to_dict() if day in base.index else None
        merged.loc[day] = other.loc[day, FIELDS]
        patches.append({"session": str(day.date()), "reason": "invalid_ohlc" if day in bad else "missing_history",
                        "before": original, "after": merged.loc[day].to_dict(), "source": "tencent_unadjusted_day"})
    merged = merged.sort_index()
    if invalid_bars(merged).any() or not merged.index.difference(expected).empty:
        raise ValueError("invalid merged bars")
    remaining = expected.difference(merged.index)
    if not remaining.equals(allowed_gaps.intersection(expected)):
        raise ValueError("unregistered missing sessions")
    for day in remaining:
        if (merged.index < day).sum() >= 253:
            raise ValueError("missing bar occurs after possible strategy eligibility")
    return merged, {"patches": patches, "overlap": overlap,
                    "unfilled_pre_warmup_gaps": remaining.strftime("%Y-%m-%d").tolist()}


class ExtendedFrozenProvider:
    name = "sina_tencent_us_daily"

    def __init__(self, catalog_path):
        self.catalog_path = Path(catalog_path)
        self.catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        self.roots = {k: Path(v) for k, v in self.catalog["sources"].items()}
        for key, root in self.roots.items():
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            if key != "decoded":
                verify_files(root, manifest)
        self.decoded_manifest = json.loads((self.roots["decoded"] / "manifest.json").read_text(encoding="utf-8"))

    def fetch(self, symbol, request):
        if str(request.start) != self.catalog["window_start"] or str(request.end_exclusive) != self.catalog["end_exclusive"]:
            raise ValueError("extension window differs from frozen contract")
        raw_path = self.roots["candidate"] / f"{symbol}.response.json"
        source = pd.DataFrame(json.loads(raw_path.read_text(encoding="utf-8")))
        source.index = pd.to_datetime(source.d, format="%Y-%m-%d")
        base = source.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})[FIELDS].astype(float)
        if base.index.has_duplicates or not base.index.is_monotonic_increasing:
            raise ValueError("duplicate or unordered Sina bars")
        first = max(pd.Timestamp(request.start), pd.Timestamp(FIRST_TRADE.get(symbol, request.start)))
        expected = calendar().sessions_in_range(first, pd.Timestamp(request.end_exclusive) - pd.Timedelta(days=1))
        # Decode provenance: compare both Sina endpoints across every preserved bar.
        decoded_path = self.roots["decoded"] / f"{symbol}.decoded.json"
        entry = self.decoded_manifest["assets"][symbol]
        if file_hash(decoded_path) != entry["decoded_sha256"] or file_hash(self.roots["supplement"] / f"{symbol}.static.response") != entry["source_sha256"]:
            raise ValueError("static decoder integrity mismatch")
        decoded = pd.DataFrame(json.loads(decoded_path.read_text(encoding="utf-8")))
        decoded.index = pd.to_datetime(decoded.date, utc=True).dt.tz_localize(None)
        decoded = decoded.rename(columns={f.lower(): f for f in FIELDS})[FIELDS].astype(float)
        common = base.index.intersection(expected)
        other_sina = decoded.reindex(common)
        if not np.isfinite(other_sina.to_numpy()).all():
            raise ValueError("Sina static endpoint lacks a preserved JSON bar")
        if (base.loc[common, FIELDS[:4]] - other_sina[FIELDS[:4]]).abs().max().max() > .005001:
            raise ValueError("Sina endpoint prices disagree")
        other, supplement_hash = None, None
        if symbol in self.catalog["tencent_symbols"]:
            root_key, market = self.catalog["tencent_symbols"][symbol]
            supplement_path = self.roots[root_key] / f"{symbol}.response"
            other = parse_tencent(json.loads(supplement_path.read_text(encoding="utf-8")), symbol, market)
            supplement_hash = file_hash(supplement_path)
        merged, review = merge_registered(base, other, symbol, expected)
        factor_path = self.roots["candidate"] / f"{symbol}.adjustment.response"
        if not factor_path.exists():
            factor_path = self.roots["supplement"] / f"{symbol}.adjustment.response"
        adjustments = parse_adjustments(factor_path.read_text(encoding="utf-8"), symbol)
        frame, actions = total_return_proxy(merged, adjustments)
        splits = {str(day.date()): float(row.split) for day, row in actions.iterrows() if abs(row.split - 1) > 1e-6}
        if splits != self.catalog["split_samples"].get(symbol, {}):
            raise ValueError("unreviewed split events")
        for sample in self.catalog["dividend_samples"].get(symbol, []):
            if abs(actions.loc[sample["date"], "dividend_old_share"] - sample["amount"]) > 1e-5:
                raise ValueError("dividend sample differs")
        product = self.catalog["products"][symbol]
        if pd.Timestamp(product["inception"]) > merged.index.min():
            raise ValueError("price before product inception")
        evidence = {"passed": True, "scope": "explicit_cross_vendor_exploratory_extension",
                    "independent_full_history_verified": False, "catalog_sha256": file_hash(self.catalog_path),
                    "sina_raw_sha256": file_hash(raw_path), "tencent_raw_sha256": supplement_hash,
                    "sina_factors_sha256": file_hash(factor_path), "row_review": review,
                    "observed_splits": splits, "action_events": [
                        {"session": str(day.date()), "split": float(row.split), "dividend_old_share": float(row.dividend_old_share)}
                        for day, row in actions.iterrows() if row.split != 1 or row.dividend_old_share > 0],
                    "special_distribution_proxy": self.catalog["special_distribution_proxy"] if symbol == "XLF" else None,
                    "volume_warning": "Providers disagree on some historical volumes; ADV is provisional",
                    "first_trade": FIRST_TRADE.get(symbol), "normalization": "causal_ex_dividend_previous_close_proxy_v1"}
        return frame, {"price_basis": "raw_sample_checked", "volume_basis": "raw_sample_checked",
                       "inception_verified": True, "inception": product["inception"],
                       "product_source": product["source"], "raw_execution_ready": False,
                       "research_basis_evidence": evidence}
