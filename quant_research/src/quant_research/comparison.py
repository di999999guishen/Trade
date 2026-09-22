"""Registered quarterly S3 reconstruction and same-window controlled comparisons."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .artifacts import digest, file_hash, provenance, publication, run_id, verify_files, write_json
from .calendar import calendar, decision_at
from .data import normalize, read_snapshot
from .engine import independent_audit, run_strategy
from .factors import compute, registry
from .model import train_fold
from .research import STRATEGIES, metrics
from .validation import TimeSplit, labels

RULE_CONTROLS = ["single_momentum", "s2_equal_allocation"]
MODEL_STRATEGIES = {"s3_lightgbm": "lightgbm", "s3_ridge": "ridge"}


def dataset(frames):
    """Include live-edge features without inventing terminal returns."""
    cal = calendar()
    tables, records = [], []
    for symbol, frame in frames.items():
        table = compute(frame).loc[frame.eligible.fillna(False)]
        for session in table.index:
            entry, end = cal.next_session(session), cal.session_offset(session, 5)
            records.append({"sample_id": f"{symbol}:{session.date()}:h5", "symbol": symbol,
                            "session": session, "decision_at": decision_at(session).tz_convert("UTC"),
                            "entry_at": cal.session_open(entry), "exit_at": cal.session_close(end),
                            "label_available_at": decision_at(end).tz_convert("UTC"), "horizon": 5})
        table.index = [f"{symbol}:{s.date()}:h5" for s in table.index]
        table.index.name = "sample_id"
        tables.append(table)
    samples = pd.DataFrame(records)
    if samples.empty:
        raise ValueError("insufficient eligible feature history")
    mature = labels(frames, 5).set_index("sample_id")
    samples["return_value"] = samples.sample_id.map(mature.return_value)
    # Retain actual availability wherever a mature label exists.
    actual = samples.sample_id.map(mature.label_available_at)
    samples["label_available_at"] = actual.fillna(samples.label_available_at)
    return pd.concat(tables).sort_index(), samples.sort_values(["session", "symbol"]).reset_index(drop=True)


def quarterly_splits(samples):
    start, end = samples.session.min(), samples.session.max()
    folds = []
    for quarter in pd.date_range(start.to_period("Q").start_time, end, freq="QS"):
        train_start = quarter - pd.DateOffset(years=6)
        valid_start = quarter - pd.DateOffset(years=1)
        test_end = min(quarter + pd.DateOffset(months=3), end + pd.Timedelta(days=1))
        if train_start.year < calendar().first_session.year:
            continue
        spec = TimeSplit(str(train_start.date()), str(valid_start.date()), str(quarter.date()),
                         str(test_end.date()), embargo_sessions=5)
        fold = spec.select(samples)
        if fold["status"] == "ready":
            fold["refit_policy"] = "quarterly_5_year_train_1_year_validation_next_quarter_test"
            folds.append(fold)
    if not folds:
        raise ValueError("insufficient history for original 5-year train + 1-year validation policy")
    return folds


def fit_panels(features, samples, folds, stage, seed):
    panels, models = {}, []
    lookup = samples.set_index("sample_id")
    for strategy, estimator in MODEL_STRATEGIES.items():
        pieces = []
        for fold in folds:
            result, predictions = train_fold(features, samples, fold, stage / "models", seed, estimator)
            joined = predictions.set_index("sample_id").join(lookup[["session", "symbol"]])
            pieces.append(joined.reset_index())
            models.append({"strategy": strategy, "boundaries": fold["boundaries"], **result})
            print(f"Fitted {strategy} {fold['boundaries'][2]}: {len(predictions)} predictions", file=sys.stderr, flush=True)
        predictions = pd.concat(pieces, ignore_index=True)
        if predictions.sample_id.duplicated().any():
            raise ValueError("overlapping quarterly test predictions")
        predictions.to_parquet(stage / f"{strategy}_predictions.parquet", index=False)
        panels[strategy] = predictions.pivot(index="session", columns="symbol", values="score").sort_index()
    return panels, models


def calculate_comparisons(frames, config, panels, stage, full_start=None):
    summary, signatures, audits, nav_tables = {}, {}, {}, []
    model_start = max(panel.index.min() for panel in panels.values())
    # Full-history controls and independent cash-start portfolios on the S3 window.
    plans = [("full", full_start, STRATEGIES + RULE_CONTROLS),
             ("model_window", model_start, STRATEGIES + RULE_CONTROLS + list(MODEL_STRATEGIES))]
    for window, start, strategies in plans:
        for bps in [config.costs.base_one_way_bps, *config.costs.stress_one_way_bps]:
            for strategy in strategies:
                key = f"{window}__{strategy}_{bps:g}bps"
                result = run_strategy(frames, config, strategy, bps, start=start, score_panel=panels.get(strategy))
                audits[key] = independent_audit(result, frames, config.portfolio.initial_capital_usd)
                summary[key] = {**metrics(result["nav"]), "start": result["nav"][0]["session"],
                                "end": result["nav"][-1]["session"], "window": window}
                signatures[key] = digest(result)
                if stage is not None:
                    target = stage / "scenarios" / key
                    target.mkdir(parents=True)
                    for kind in ("nav", "targets", "fills", "holdings"):
                        pd.DataFrame(result[kind]).to_parquet(target / f"{kind}.parquet", index=False)
                nav_tables.append(pd.DataFrame(result["nav"]).assign(scenario=key))
                print(f"Audited {key}: {summary[key]['cumulative_net_return']:.4%} net", file=sys.stderr, flush=True)
    return {"metrics": summary, "scenario_hashes": signatures, "audits": audits}, pd.concat(nav_tables, ignore_index=True)


def plot_comparison(nav, path, bps):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(13, 10), constrained_layout=True)
    for ax, window in zip(axes, ("full", "model_window")):
        for scenario, group in nav.groupby("scenario", sort=True):
            if not scenario.startswith(f"{window}__") or not scenario.endswith(f"_{bps:g}bps"):
                continue
            name = scenario.split("__")[1].rsplit("_", 1)[0]
            ax.plot(pd.to_datetime(group.session), group.nav / group.nav.iloc[0], label=name, linewidth=1.3)
        ax.set_title(f"{window}: independent cash start, {bps:g} bps per side")
        ax.set_ylabel("Net wealth / initial capital")
        ax.grid(alpha=.2)
        ax.legend(fontsize=8, ncol=3)
    fig.suptitle("US-listed ETFs | exploratory reconstruction | no untouched holdout")
    fig.savefig(path, dpi=140)
    plt.close(fig)


def compare(snapshot, config, project, model_test_start=None, portfolio_start=None):
    project, snapshot = Path(project), Path(snapshot).resolve()
    frames, quality = normalize(snapshot, config)
    features, samples = dataset(frames)
    folds = quarterly_splits(samples)
    if model_test_start is not None:
        folds = [fold for fold in folds if pd.Timestamp(fold["boundaries"][2]) >= pd.Timestamp(model_test_start)]
    if not folds or len(folds) * 2 > config.validation.model_trials_max:
        raise ValueError("no eligible folds or model fitting budget exceeded; register a bounded model test window")
    identifier = "cmp_" + run_id("comparison").rsplit("_", 1)[1]
    env = provenance(project)
    with publication(project / "outputs", identifier) as stage:
        contract = {"config": config.model_dump(), "source_snapshot": str(snapshot),
                    "model_test_start": model_test_start, "portfolio_start": portfolio_start,
                    "snapshot_hash": read_snapshot(snapshot)["content_hash"], "folds": folds,
                    "features": registry(), "horizon": 5, "estimator_trials_per_fold": 2,
                    "training_trial_budget": len(folds) * 2, "portfolio_trial_budget": 54,
                    "ridge": "alpha=1; train-only medians and standard scaling; train-only coefficients",
                    "lightgbm": "fixed model.PARAMETERS; validation early stopping only",
                    "controls": {"single_momentum": "mom252 ranking; S1 trend filters, top5 and identical constraints",
                                 "s2_equal_allocation": "S2 scores/top8/filters/covariance risk target; equal initial allocation"},
                    "test_policy": "quarterly nonoverlapping inference, including unlabeled terminal sessions",
                    "comparison_policy": "same date range and independent cash start; registered costs; no search",
                    "holdout_status": "not_untouched; test era previously inspected in S1/S2",
                    "promotion": "NO-GO: no untouched 24-month holdout or independent full-history verified prices"}
        write_json(stage / "contract.json", contract)
        try:
            features.to_parquet(stage / "features.parquet")
            samples.to_parquet(stage / "samples.parquet", index=False)
            panels, models = fit_panels(features, samples, folds, stage, config.validation.seed)
            payload, nav = calculate_comparisons(frames, config, panels, stage, full_start=portfolio_start)
            write_json(stage / "models.json", models)
            write_json(stage / "results.json", payload)
            write_json(stage / "quality.json", quality)
            nav.to_parquet(stage / "nav.parquet", index=False)
            plot_comparison(nav, stage / "comparison.png", config.costs.base_one_way_bps)
            report = ["# US ETF extended comparison — research only", "",
                      "Five-year train + one-year validation, quarterly refits, five-session embargo.",
                      "All model features are fixed before fitting; test labels are never fitted.",
                      "Use actual window dates below. Partial years and full-history CAGR must not be confused.",
                      f"Source: {config.data.provider}; sample-checked total-return proxy. Cash earns zero; USD, pre-tax.",
                      "Cross-vendor extension, if used, is explicit in snapshot row-level lineage; volume discrepancies remain provisional.",
                      "No untouched holdout, independent full-history action ledger or live validation. Promotion: NO-GO.", "",
                      "| Window | Scenario | Dates | Net return | CAGR* | Max drawdown | Volatility | Turnover/year |",
                      "|---|---|---|---:|---:|---:|---:|---:|"]
            for key, m in payload["metrics"].items():
                report.append(f"| {m['window']} | {key.split('__')[1]} | {m['start']}–{m['end']} | "
                              f"{m['cumulative_net_return']:.2%} | {m['cagr']:.2%} | {m['max_drawdown']:.2%} | "
                              f"{m['annualized_volatility']:.2%} | {m['annualized_one_way_turnover_risky']:.2f} |")
            report += ["", "*Compare model-window total returns first; do not compare its CAGR against the full-history CAGR.",
                       "Every scenario has an independent cash/fill/NAV ledger audit.", "", "![Comparison](comparison.png)"]
            (stage / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
            status = "research_only"
        except Exception as exc:  # noqa: BLE001 -- publish durable failed trials
            write_json(stage / "failure.json", {"type": type(exc).__name__, "reason": str(exc)})
            payload, status = None, "failed"
        manifest = {"status": status, "run_id": identifier, "provenance": env,
                    "source_manifest_hash": file_hash(snapshot / "manifest.json"),
                    "result_hash": digest(payload) if payload else None,
                    "files": {p.relative_to(stage).as_posix(): file_hash(p) for p in stage.rglob("*") if p.is_file()}}
        write_json(stage / "manifest.json", manifest)
    if status == "failed":
        raise ValueError(f"comparison failed; see {project / 'outputs' / identifier / 'failure.json'}")
    return {"status": status, "directory": str(project / "outputs" / identifier), "result_hash": manifest["result_hash"]}


def replay_comparison(manifest_path, project):
    """Recompute factors, frozen-model predictions, portfolios, and ledger audits offline."""
    from .config import ResearchConfig
    root = Path(manifest_path).parent
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    verify_files(root, manifest)
    contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
    current = provenance(Path(project))
    if any(current[k] != manifest["provenance"][k] for k in ("source_hash", "lock_hash")):
        raise ValueError("source/lock changed; use frozen implementation")
    snapshot = Path(contract["source_snapshot"])
    if file_hash(snapshot / "manifest.json") != manifest["source_manifest_hash"]:
        raise ValueError("snapshot changed")
    config = ResearchConfig.model_validate(contract["config"])
    frames, _ = normalize(snapshot, config)
    features, samples = dataset(frames)
    pd.testing.assert_frame_equal(features, pd.read_parquet(root / "features.parquet"))
    pd.testing.assert_frame_equal(samples, pd.read_parquet(root / "samples.parquet"))
    folds = quarterly_splits(samples)
    if contract.get("model_test_start") is not None:
        folds = [fold for fold in folds if pd.Timestamp(fold["boundaries"][2]) >= pd.Timestamp(contract["model_test_start"])]
    if folds != contract["folds"]:
        raise ValueError("quarterly splits changed")
    pieces = {strategy: [] for strategy in MODEL_STRATEGIES}
    for record in json.loads((root / "models.json").read_text(encoding="utf-8")):
        model_root = root / "models" / record["model_id"]
        spec = json.loads((model_root / "contract.json").read_text(encoding="utf-8"))
        x = features.loc[spec["split"]["test"], spec["feature_columns"]]
        x = x.replace([np.inf, -np.inf], np.nan).fillna(pd.Series(spec["preprocessor"]["medians"]))
        if spec["estimator"] == "lightgbm":
            import lightgbm as lgb
            score = lgb.Booster(model_file=str(model_root / "model.txt")).predict(x, num_threads=1)
        else:
            fitted = json.loads((model_root / "ridge.json").read_text(encoding="utf-8"))
            score = ((x.to_numpy() - fitted["mean"]) / fitted["scale"]) @ fitted["coef"] + fitted["intercept"]
        saved = pd.read_parquet(model_root / "predictions.parquet")
        np.testing.assert_allclose(score, saved.score, rtol=1e-13, atol=1e-15)
        # Tiny BLAS serialization differences cannot affect the frozen score contract.
        saved = saved.set_index("sample_id").join(samples.set_index("sample_id")[["session", "symbol"]])
        pieces[record["strategy"]].append(saved)
    panels = {key: pd.concat(value).pivot(index="session", columns="symbol", values="score").sort_index()
              for key, value in pieces.items()}
    payload, _ = calculate_comparisons(frames, config, panels, None, full_start=contract.get("portfolio_start"))
    if digest(payload) != manifest["result_hash"]:
        raise ValueError("portfolio replay differs")
    return {"status": "reproduced_offline_from_frozen_models", "result_hash": digest(payload),
            "training_repeated": False}
