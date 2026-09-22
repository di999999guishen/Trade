"""One immutable experiment bundle, all fixed cost scenarios, offline replay."""
import html
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from .artifacts import digest, file_hash, provenance, publication, run_id, utc_now, verify_files, write_json
from .config import ResearchConfig
from .data import normalize, read_snapshot
from .engine import independent_audit, run_strategy
from .factors import registry

STRATEGIES = ["buy_hold_spy", "buy_hold_qqq", "eligible_pool_equal_weight", "simple_trend",
              "s1_etf_momentum", "s2_multi_factor"]


def metrics(rows):
    table = pd.DataFrame(rows).set_index("session")
    nav = table.nav
    if nav.isna().any() or (nav <= 0).any():
        raise ValueError("missing or nonpositive NAV")
    returns = nav.pct_change(fill_method=None).iloc[1:]
    days = (pd.Timestamp(nav.index[-1]) - pd.Timestamp(nav.index[0])).days
    std = float(returns.std(ddof=1))
    cagr = float((nav.iloc[-1] / nav.iloc[0]) ** (365.25 / days) - 1) if days else None
    annual = {}
    for year, r in returns.groupby(pd.to_datetime(returns.index).year):
        annual[str(year)] = float((1 + r).prod() - 1)
    return {"cumulative_net_return": float(nav.iloc[-1] / nav.iloc[0] - 1), "cagr": cagr,
            "annual_returns": annual, "max_drawdown": float((1 - nav / nav.cummax()).max()),
            "annualized_volatility": std * np.sqrt(252) if np.isfinite(std) else None,
            "sharpe_zero_rf": float(np.sqrt(252) * returns.mean() / std) if np.isfinite(std) and std > 1e-15 else None,
            "total_cost_usd": float(table.cost.sum()), "average_cash_weight": float(table.cash_weight.mean()),
            "annualized_one_way_turnover_risky": float(table.one_way_turnover_risky.iloc[1:].mean() * 252),
            "regression_alpha": None, "alpha_unavailable_reason": "risk_factor_dataset_not_integrated",
            "sessions": len(nav), "calendar_days": days}


def calculate(snapshot_path, config):
    frames, quality = normalize(snapshot_path, config)
    results, summary, audits = {}, {}, {}
    for bps in [config.costs.base_one_way_bps, *config.costs.stress_one_way_bps]:
        for strategy in STRATEGIES:
            key = f"{strategy}_{bps:g}bps"
            result = run_strategy(frames, config, strategy, bps)
            audits[key] = independent_audit(result, frames, config.portfolio.initial_capital_usd)
            results[key] = result
            summary[key] = metrics(result["nav"])
            print(f"Completed {key}: {len(result['nav'])} sessions; ledger audit passed", file=sys.stderr, flush=True)
        for strategy in STRATEGIES:
            key = f"{strategy}_{bps:g}bps"
            for benchmark in ("spy", "qqq"):
                base_key = f"buy_hold_{benchmark}_{bps:g}bps"
                summary[key][f"cagr_difference_vs_{benchmark}"] = summary[key]["cagr"] - summary[base_key]["cagr"]
    return {"results": results, "metrics": summary, "audits": audits, "quality": quality,
            "factor_registry": registry()}


def research(snapshot_path, config, project):
    project = Path(project)
    snapshot_path = Path(snapshot_path).resolve()
    snapshot_manifest = read_snapshot(snapshot_path)
    identifier = run_id("research")
    started = utc_now()
    env = provenance(project)
    # Register before execution: failed experiments remain in the registry.
    with publication(project / "outputs", identifier) as stage:
        contract = {"hypothesis": "Frozen S1/S2 rules versus four controls; no parameter search",
                    "config": config.model_dump(), "cost_scenarios": [config.costs.base_one_way_bps, *config.costs.stress_one_way_bps],
                    "strategies": STRATEGIES, "snapshot_hash": snapshot_manifest["content_hash"],
                    "source_snapshot": str(snapshot_path), "seed": config.validation.seed,
                    "started_at": started, "trial_budget": 18,
                    "holdout_status": "not_reserved_development_or_synthetic_only"}
        write_json(stage / "experiment_contract.json", contract)
        try:
            payload = calculate(snapshot_path, config)
        except Exception as exc:  # noqa: BLE001 -- failed trials must be preserved
            write_json(stage / "failure.json", {"status": "failed", "error_type": type(exc).__name__, "reason": str(exc)})
            # Publication still completes, but is explicitly a failed experiment.
            failure = str(exc)
            payload = None
        generated = utc_now()
        if payload is not None:
            from .plots import plot_report
            plot_report(payload, stage / "overview.png", config.costs.base_one_way_bps,
                        snapshot_manifest["provider"].startswith("synthetic"))
            write_json(stage / "results.json", payload)
            write_json(stage / "metrics.json", payload["metrics"])
            write_json(stage / "data_quality_report.json", payload["quality"])
            for kind in ("nav", "targets", "fills", "holdings"):
                rows = [dict(row, scenario=key) for key, result in payload["results"].items() for row in result[kind]]
                if kind == "targets":
                    rows = [dict(row, run_id=identifier, generated_at=generated, generation_mode=config.mode,
                                 data_snapshot_id=snapshot_manifest["snapshot_id"], strategy_version="spec_20260917_v1",
                                 factor_version=payload["factor_registry"]["version_hash"], model_version=None,
                                 risk_config_hash=digest(config.portfolio.model_dump())) for row in rows]
                pd.DataFrame(rows).to_parquet(stage / f"{kind}.parquet", index=False)
            report = ["# research_only — synthetic fixture" if snapshot_manifest["provider"].startswith("synthetic") else "# research_only",
                      "", "USD / pre-tax / adjusted_total_return_proxy / normalized fractional units.",
                      "No real orders. Dividend effects are embedded in prices; no second cash dividend.",
                      "Fixed present-day ETF universe; this is not a point-in-time full-market sample.",
                      ("Engineering fixture results are not evidence of investment performance."
                       if snapshot_manifest["provider"].startswith("synthetic") else
                       "Exploratory historical reconstruction, not untouched OOS or a forecast of future returns."),
                      f"Source: {snapshot_manifest['provider']}. Window: " +
                      f"{next(iter(payload['results'].values()))['nav'][0]['session']} to " +
                      f"{next(iter(payload['results'].values()))['nav'][-1]['session']}.",
                      "Sina basis checks are samples only; independent full-history verification remains incomplete."
                      if config.data.provider == "sina_us_daily" else "", "",
                      "| Scenario | Net return | CAGR | Max drawdown | Average cash | Cost USD |",
                      "|---|---:|---:|---:|---:|---:|"]
            for key, m in payload["metrics"].items():
                report.append(f"| {key} | {m['cumulative_net_return']:.4%} | {m['cagr']:.4%} | "
                              f"{m['max_drawdown']:.4%} | {m['average_cash_weight']:.2%} | {m['total_cost_usd']:.4f} |")
            report += ["", "Engineering: emitted Qlib fills independently reconciled to cash and NAV.",
                       "Investment promotion: NO-GO; real verified data, untouched OOS and LEAN raw audit remain required.",
                       "Unverified: raw action/payment ledger, real open-auction capacity, factor regression alpha, live broker.",
                       "S2 diagnostics and model training are separate from this fixed-rule comparison.",
                       "", "![Research overview](overview.png)"]
        else:
            report = ["# no_go — experiment failed", "", failure, "No performance conclusion was generated."]
        (stage / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        (stage / "report.html").write_text("<!doctype html><meta charset='utf-8'><title>ETF research</title><pre>" +
                                           html.escape("\n".join(report)) + "</pre>" +
                                           ("<img src='overview.png' alt='Research overview' style='max-width:100%'>" if payload is not None else ""), encoding="utf-8")
        manifest = {"schema_version": 1, "run_id": identifier, "status": "research_only" if payload is not None else "failed",
                    "started_at": started, "completed_at": generated, "config": config.model_dump(),
                    "config_hash": digest(config.model_dump()), "provenance": env,
                    "source_snapshot": str(snapshot_path), "source_snapshot_hash": snapshot_manifest["content_hash"],
                    "source_manifest_hash": file_hash(snapshot_path / "manifest.json"),
                    "price_mode": config.data.price_mode, "generation_mode": config.mode,
                    "result_hash": digest(payload) if payload is not None else None,
                    "files": {p.name: file_hash(p) for p in stage.iterdir() if p.is_file()}}
        write_json(stage / "manifest.json", manifest)
    # DuckDB is a local index only; immutable manifests remain the source of truth.
    (project / "state").mkdir(exist_ok=True)
    with duckdb.connect(str(project / "state" / "research.duckdb")) as db:
        db.execute("CREATE TABLE IF NOT EXISTS experiments (run_id VARCHAR PRIMARY KEY, status VARCHAR, manifest VARCHAR)")
        db.execute("INSERT INTO experiments VALUES (?, ?, ?)", [identifier, manifest["status"], str(project / "outputs" / identifier / "manifest.json")])
    if payload is None:
        raise ValueError(f"experiment failed; preserved at {project / 'outputs' / identifier}: {failure}")
    return {"status": manifest["status"], "run_id": identifier, "directory": str(project / "outputs" / identifier),
            "result_hash": manifest["result_hash"]}


def replay(manifest_path, project):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_files(manifest_path.parent, manifest)
    if manifest["status"] != "research_only":
        raise ValueError("cannot replay a failed experiment as a successful one")
    snapshot_path = Path(manifest["source_snapshot"])
    if file_hash(snapshot_path / "manifest.json") != manifest["source_manifest_hash"]:
        raise ValueError("source snapshot manifest changed")
    current = provenance(project)
    if current["source_hash"] != manifest["provenance"]["source_hash"] or current["lock_hash"] != manifest["provenance"]["lock_hash"]:
        raise ValueError("source/lock changed: reproduce using the recorded code version")
    config = ResearchConfig.model_validate(manifest["config"])
    if digest(config.model_dump()) != manifest["config_hash"]:
        raise ValueError("config hash mismatch")
    payload = calculate(snapshot_path, config)
    if digest(payload) != manifest["result_hash"]:
        raise ValueError("offline replay did not reproduce the frozen outputs")
    return {"status": "reproduced_offline", "result_hash": digest(payload), "run_id": manifest["run_id"]}
