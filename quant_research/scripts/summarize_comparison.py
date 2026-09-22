"""Supplement a frozen comparison with descriptive risk and factor redundancy checks."""
import argparse
import json
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from quant_research.artifacts import file_hash, publication, run_id, verify_files, write_json


def risk_summary(nav):
    output = {}
    series = {key: group.set_index("session").nav for key, group in nav.groupby("scenario")}
    for key, wealth in series.items():
        returns = wealth.pct_change().iloc[1:]
        window, scenario = key.split("__")
        cost = scenario.rsplit("_", 1)[1]
        item = {}
        downside = float(np.sqrt(np.mean(np.minimum(returns, 0) ** 2)))
        item["sortino_zero_target"] = float(np.sqrt(252) * returns.mean() / downside) if downside else None
        threshold = returns.quantile(.05)
        item["daily_worst_5pct_mean_return"] = float(returns[returns <= threshold].mean())
        drawdown = 1 - wealth / wealth.cummax()
        longest, current = 0, 0
        for underwater in drawdown > 1e-12:
            current = current + 1 if underwater else 0
            longest = max(longest, current)
        item["longest_underwater_sessions_including_unrecovered_tail"] = longest
        for benchmark in ("spy", "qqq"):
            baseline = series[f"{window}__buy_hold_{benchmark}_{cost}"].pct_change().iloc[1:]
            pd.testing.assert_index_equal(returns.index, baseline.index)
            active = returns - baseline
            tracking = float(active.std() * np.sqrt(252))
            rng = np.random.default_rng(42)
            values, block = active.to_numpy(), 20
            draws = []
            for _ in range(2000):
                starts = rng.integers(0, len(values) - block + 1, size=int(np.ceil(len(values) / block)))
                sampled = np.concatenate([values[s:s + block] for s in starts])[:len(values)]
                draws.append(float(sampled.mean() * 252))
            item[benchmark] = {"beta_ols_descriptive": float(returns.cov(baseline) / baseline.var()),
                               "tracking_error_annualized": tracking,
                               "information_ratio": float(active.mean() * 252 / tracking) if tracking > 1e-15 else None,
                               "annualized_arithmetic_active_mean": float(active.mean() * 252),
                               "active_mean_20_session_block_bootstrap_95pct": np.quantile(draws, [.025, .975]).tolist()}
        output[key] = item
    return output


def redundancy(features, train_ids):
    # Fixed first training fold only. These results never select model features.
    correlation = features.loc[train_ids].corr(method="spearman")
    edges = []
    groups = [{name} for name in correlation.columns]
    for i, left in enumerate(correlation.columns):
        for right in correlation.columns[i + 1:]:
            value = float(correlation.at[left, right])
            if abs(value) >= .90:
                edges.append({"left": left, "right": right, "spearman": value})
                matches = [g for g in groups if left in g or right in g]
                groups = [g for g in groups if g not in matches] + [set.union(*matches)]
    return {"method": "pooled training-fold Spearman; absolute threshold .90; connected components",
            "feature_selection_performed": False, "high_correlation_pairs": edges,
            "components": sorted([sorted(g) for g in groups]), "correlation": correlation.to_dict()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--factors", type=Path, required=True)
    args = parser.parse_args()
    manifests = {}
    for name, root in (("comparison", args.comparison), ("factors", args.factors)):
        manifests[name] = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        verify_files(root, manifests[name])
    contract = json.loads((args.comparison / "contract.json").read_text(encoding="utf-8"))
    identifier = "diag_" + run_id().rsplit("_", 1)[1]
    with publication(args.comparison.parent, identifier) as stage:
        write_json(stage / "contract.json", {"kind": "posthoc_descriptive_no_strategy_change", "seed": 42,
                                             "bootstrap_draws": 2000, "block_sessions": 20,
                                             "multiple_comparisons_adjusted": False,
                                             "warning": "Intervals are descriptive, not alpha or a selection/promotion test",
                                             "inputs": {k: str(v.resolve()) for k, v in {"comparison": args.comparison, "factors": args.factors}.items()},
                                             "input_manifests": {k: file_hash(v / "manifest.json") for k, v in {"comparison": args.comparison, "factors": args.factors}.items()}})
        write_json(stage / "risk.json", risk_summary(pd.read_parquet(args.comparison / "nav.parquet")))
        features = pd.read_parquet(args.comparison / "features.parquet")
        write_json(stage / "redundancy.json", redundancy(features, contract["folds"][0]["train"]))
        diagnostics = json.loads((args.factors / "diagnostics.json").read_text(encoding="utf-8"))
        compact = {k: {name: value for name, value in v.items() if name != "daily_rank_ic"}
                   for k, v in diagnostics["factors"].items()}
        write_json(stage / "factor_summary.json", compact)
        (stage / "analysis_source.py").write_bytes(Path(__file__).read_bytes())
        project = args.comparison.parent.parent
        source = manifests["comparison"]["provenance"]
        with zipfile.ZipFile(stage / "reproduction_source.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, expected in source["source_files"].items():
                path = (project / relative).resolve()
                if not path.is_relative_to(project.resolve()) or file_hash(path) != expected:
                    raise ValueError(f"cannot archive changed source: {relative}")
                archive.write(path, relative)
            if file_hash(project / "uv.lock") != source["lock_hash"]:
                raise ValueError("cannot archive changed lock")
            for name in ("uv.lock", "pyproject.toml"):
                archive.write(project / name, name)
            archive.writestr("recorded_provenance.json", json.dumps(source, indent=2))
        write_json(stage / "manifest.json", {"status": "descriptive_only", "files": {
            p.name: file_hash(p) for p in stage.iterdir() if p.is_file()}})
    print(args.comparison.parent / identifier)


if __name__ == "__main__":
    main()
