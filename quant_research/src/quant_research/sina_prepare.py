"""Causal dividend/split proxy for a predeclared, complete Sina research window.

This is a sample-checked exploratory source, NOT a fully independently verified
history. Raw OHLCV remains unchanged; the affine qfq price is never used as NAV.
"""
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .artifacts import file_hash, verify_files
from .calendar import calendar
from .domestic import parse_bars


def parse_adjustments(text, symbol):
    match = re.match(r"\s*var\s+" + re.escape(symbol) + r"_qfq\s*=\s*", text)
    if match is None:
        raise ValueError("unexpected adjustment symbol/wrapper")
    payload, stop = json.JSONDecoder().raw_decode(text[match.end():])
    tail = text[match.end() + stop:].strip().lstrip(";").strip()
    if tail and not re.fullmatch(r"/\*.*\*/", tail, flags=re.DOTALL):
        raise ValueError("unexpected adjustment suffix")
    table = pd.DataFrame(payload["data"])
    if not {"d", "f", "c"}.issubset(table) or table.empty:
        raise ValueError("missing adjustment fields")
    table.index = pd.to_datetime(table.pop("d"), format="%Y-%m-%d", errors="raise")
    table = table[["f", "c"]].astype(float).sort_index()
    if table.index.has_duplicates or not np.isfinite(table.to_numpy()).all() or (table.f <= 0).any():
        raise ValueError("invalid adjustment factors")
    table["split"] = table.f / table.f.shift()
    table["dividend_old_share"] = (table.c - table.c.shift()) / table.f.shift()
    return table


def total_return_proxy(raw, adjustments):
    """Forward scale changes use ex-date actions and PRIOR close, never same-day close.

    Affine vendor continuity gives split=f_new/f_old and D=(c_new-c_old)/f_old.
    Scale_t / scale_prev = split / (1-D/close_prev), the ex-dividend price proxy.
    This assumes immediate reinvestment; it is not a payment-date cash ledger.
    """
    if adjustments.index.min() >= raw.index.min():
        raise ValueError("adjustment history must precede first price")
    events = adjustments.loc[(adjustments.index >= raw.index.min()) & (adjustments.index <= raw.index.max())]
    if not events.index.isin(raw.index).all():
        raise ValueError("corporate action on missing/non-trading session")
    if (events.dividend_old_share < -1e-6).any() or events[["split", "dividend_old_share"]].isna().any().any():
        raise ValueError("negative or unknown distribution")
    actions = events[["split", "dividend_old_share"]].reindex(raw.index)
    actions["split"] = actions.split.fillna(1.0)
    actions["dividend_old_share"] = actions.dividend_old_share.fillna(0.0).clip(lower=0)
    if actions.iloc[0].split != 1 or actions.iloc[0].dividend_old_share != 0:
        raise ValueError("first bar corporate action needs a previous close")
    denominator = 1 - actions.dividend_old_share / raw.Close.shift()
    denominator.iloc[0] = 1.0
    if (denominator <= 0).any() or not np.isfinite(denominator).all():
        raise ValueError("invalid dividend adjustment")
    scale = (actions.split / denominator).cumprod()
    frame = raw.copy()
    frame["Adj Close"] = frame.Close * scale
    frame["Stock Splits"] = actions.split
    frame["Dividends"] = actions.dividend_old_share
    return frame, actions


class SinaFrozenProvider:
    name = "sina_us_daily"

    def __init__(self, candidate, supplement, decoded, catalog):
        self.candidate, self.supplement, self.decoded = map(Path, (candidate, supplement, decoded))
        for path in (self.candidate, self.supplement):
            verify_files(path, json.loads((path / "manifest.json").read_text()))
        self.decoded_manifest = json.loads((self.decoded / "manifest.json").read_text())
        self.catalog = json.loads(Path(catalog).read_text(encoding="utf-8"))
        self.catalog_hash = file_hash(catalog)

    def fetch(self, symbol, request):
        if str(request.start) != self.catalog["window_start"] or str(request.end_exclusive) != self.catalog["end_exclusive"]:
            raise ValueError("Sina pilot requires the frozen audited window; no silent extension")
        raw_path = self.candidate / f"{symbol}.response.json"
        raw = parse_bars(json.loads(raw_path.read_text()), request)
        expected = calendar().sessions_in_range(request.start, pd.Timestamp(request.end_exclusive) - pd.Timedelta(days=1))
        if not raw.index.equals(expected):
            raise ValueError("missing or unexpected sessions in frozen pilot window")
        decoded_path = self.decoded / f"{symbol}.decoded.json"
        entry = self.decoded_manifest["assets"][symbol]
        if file_hash(decoded_path) != entry["decoded_sha256"] or file_hash(
                self.supplement / f"{symbol}.static.response") != entry["source_sha256"]:
            raise ValueError("decoded/static integrity mismatch")
        other = pd.DataFrame(json.loads(decoded_path.read_text()))
        other.index = pd.to_datetime(other.date, utc=True).dt.tz_localize(None)
        other = other.rename(columns={k.lower(): k for k in raw.columns})
        if other.index.has_duplicates:
            raise ValueError("duplicate static sessions")
        other = other.reindex(raw.index)[list(raw.columns)].astype(float)
        if not np.isfinite(other.to_numpy()).all():
            raise ValueError("static history incomplete")
        price_difference = float((raw.drop(columns="Volume") - other.drop(columns="Volume")).abs().max().max())
        volume_difference = float(((raw.Volume - other.Volume).abs() / raw.Volume.clip(lower=1)).max())
        if price_difference > .005001 or volume_difference > .001:
            raise ValueError("two Sina endpoints disagree beyond frozen tolerance")
        factor_path = self.candidate / f"{symbol}.adjustment.response"
        if not factor_path.exists():
            factor_path = self.supplement / f"{symbol}.adjustment.response"
        factors = parse_adjustments(factor_path.read_text(), symbol)
        frame, actions = total_return_proxy(raw, factors)
        observed_splits = {str(d.date()): float(row.split) for d, row in actions.iterrows() if abs(row.split - 1) > 1e-6}
        expected_splits = self.catalog["split_samples"].get(symbol, {})
        if observed_splits != expected_splits:
            raise ValueError("split events differ from reviewed issuer schedule")
        for sample in self.catalog["dividend_samples"].get(symbol, []):
            if abs(actions.loc[sample["date"], "dividend_old_share"] - sample["amount"]) > 1e-5:
                raise ValueError("issuer dividend sample mismatch")
        product = self.catalog["products"][symbol]
        if pd.Timestamp(product["inception"]) > raw.index.min():
            raise ValueError("prices precede reviewed inception")
        evidence = {"passed": True, "scope": "vendor_schema_dual_endpoint_and_issuer_action_samples_only",
                    "independent_full_history_verified": False,
                    "price_max_abs_difference": price_difference, "volume_max_relative_difference": volume_difference,
                    "catalog_sha256": self.catalog_hash,
                    "raw_response_sha256": file_hash(raw_path), "factor_response_sha256": file_hash(factor_path),
                    "static_response_sha256": entry["source_sha256"],
                    "decoder_sha256": self.decoded_manifest["decoder_sha256"],
                    "observed_splits": observed_splits,
                    "action_events": [{"session": str(d.date()), "split": float(row.split),
                                       "dividend_old_share": float(row.dividend_old_share)}
                                      for d, row in actions.iterrows() if row.split != 1 or row.dividend_old_share > 0],
                    "window_selection": self.catalog["window_selection"],
                    "normalization": "causal_ex_dividend_previous_close_proxy_v1"}
        return frame, {"price_basis": "raw_sample_checked", "volume_basis": "raw_sample_checked",
                       "inception_verified": True, "inception": product["inception"],
                       "product_source": product["source"], "raw_execution_ready": False,
                       "research_basis_evidence": evidence}
