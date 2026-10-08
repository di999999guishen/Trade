"""S4 real financial evidence + S8 fixed candidate engineering run (no return search)."""
import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd

from quant_research.artifacts import (
    digest,
    file_hash,
    provenance,
    publication,
    run_id,
    verify_files,
    write_json,
)
from quant_research.config import load_config
from quant_research.data import normalize
from quant_research.factor_dsl import TrialLedger, evaluate
from quant_research.factors import compute
from quant_research.sec_evidence import extract_facts

PROJECT = Path(__file__).resolve().parents[1]
PROBE = PROJECT / "outputs/strategy_inputs_20260918T093152521773Z_97181a07d18c40ed8583beadca6241dd"
SNAPSHOT = PROJECT / "data/ten_year/snapshot_20260918T080154884269Z_e0e281cc5c354cbb8476b16f3f2c4c41"
CONFIG = PROJECT / "outputs/tenyear_inputs_97901b32c1d44ad1b567d3839e6eb765/config.json"
EXPRESSIONS = ["rank(mom12_1) - rank(vol63)", "rank(mom20) * rank(volume_ratio20)",
               "rank(reversal5) - rank(vol20)"]


def feature_inputs(contract):
    if file_hash(contract["config"]) != contract["config_hash"]:
        raise ValueError("frozen configuration changed")
    if file_hash(Path(contract["snapshot"]) / "manifest.json") != contract["snapshot_manifest_hash"]:
        raise ValueError("snapshot manifest changed")
    frames, _ = normalize(Path(contract["snapshot"]), load_config(contract["config"]))
    cutoff = contract["development_end"]
    frames = {s: f.loc[:cutoff].copy() for s, f in frames.items()}
    factors = {s: compute(f).where(f.eligible, axis=0) for s, f in frames.items()}
    return {name: pd.DataFrame({s: f[name] for s, f in sorted(factors.items())}) for name in next(iter(factors.values()))}


def panel_hash(frame):
    return digest(frame.to_json(orient="split", date_format="iso", double_precision=15))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--recover", type=Path)
    args = parser.parse_args()
    env = provenance(PROJECT)
    if args.recover:
        previous = args.recover.resolve()
        if not previous.is_relative_to(PROJECT / "outputs") or ".partial-" not in previous.name:
            raise ValueError("recovery only accepts unpublished local output bundles")
        manifest = json.loads((previous / "manifest.json").read_text(encoding="utf-8"))
        verify_files(previous, manifest)
        contract = json.loads((previous / "contract.json").read_text(encoding="utf-8"))
        if contract["source_hash"] != env["source_hash"] or contract["lock_hash"] != env["lock_hash"]:
            raise ValueError("recovery cannot change the calculation source or dependencies")
        panels = feature_inputs(contract)
        hashes = {expr: panel_hash(evaluate(expr, panels)) for expr in contract["expressions"]}
        if hashes != json.loads((previous / "s8_panel_hashes.json").read_text(encoding="utf-8")):
            raise AssertionError("recovery calculation mismatch")
        identifier = run_id("strategy_inputs_build")
        with publication(PROJECT / "outputs", identifier) as stage:
            for relative in manifest["files"]:
                if relative not in {"contract.json", "environment.json", "reproduction_source.zip"}:
                    shutil.copyfile(previous / relative, stage / relative)
            contract.update({"original_runner_hash": contract["runner_hash"], "runner_hash": file_hash(__file__),
                             "recovered_from": str(previous), "recovery_reason": "SQLite backup connection held Windows directory open",
                             "recovery_recomputed_factors_match": True, "recovery_additional_trials": 0})
            write_json(stage / "contract.json", contract)
            write_json(stage / "environment.json", env)
            with ZipFile(stage / "reproduction_source.zip", "x", ZIP_DEFLATED) as archive:
                for relative in [*env["source_files"], "uv.lock", "pyproject.toml", "scripts/build_strategy_inputs.py"]:
                    archive.write(PROJECT / relative, relative)
            write_json(stage / "manifest.json", {"files": {p.name: file_hash(p) for p in sorted(stage.iterdir())}})
        print(PROJECT / "outputs" / identifier)
        return
    if args.replay:
        previous = args.replay.resolve()
        verify_files(previous, json.loads((previous / "manifest.json").read_text(encoding="utf-8")))
        contract = json.loads((previous / "contract.json").read_text(encoding="utf-8"))
        if env["source_hash"] != contract["source_hash"] or env["lock_hash"] != contract["lock_hash"]:
            raise ValueError("source or lock changed; use archived source")
        if file_hash(__file__) != contract["runner_hash"]:
            raise ValueError("runner changed")
        panels = feature_inputs(contract)
        actual = {expr: panel_hash(evaluate(expr, panels)) for expr in contract["expressions"]}
        expected = json.loads((previous / "s8_panel_hashes.json").read_text(encoding="utf-8"))
        if actual != expected:
            raise AssertionError("factor replay mismatch")
        identifier = run_id("strategy_inputs_replay")
        with publication(PROJECT / "outputs", identifier) as stage:
            write_json(stage / "replay.json", {"passed": True, "source_run": str(previous),
                                                "panel_hashes": actual, "consumes_new_trials": False})
        print(PROJECT / "outputs" / identifier)
        return
    verify_files(PROBE, json.loads((PROBE / "manifest.json").read_text(encoding="utf-8")))
    contract = {"source_hash": env["source_hash"], "lock_hash": env["lock_hash"],
                "runner_hash": file_hash(__file__), "snapshot": str(SNAPSHOT), "config": str(CONFIG),
                "config_hash": file_hash(CONFIG), "snapshot_manifest_hash": file_hash(SNAPSHOT / "manifest.json"),
                "probe": str(PROBE), "probe_manifest_hash": file_hash(PROBE / "manifest.json"),
                "development_end": "2022-12-30", "expressions": EXPRESSIONS,
                "campaign": "s8_fixed_candidates_v1", "budget": 30, "holdout": "unavailable_history_previously_seen",
                "purpose": "factor_engineering_only_no_return_selection", "correlation_threshold": .90}
    identifier = run_id("strategy_inputs_build")
    with publication(PROJECT / "outputs", identifier) as stage:
        write_json(stage / "contract.json", contract)
        write_json(stage / "environment.json", env)
        facts = json.loads((PROBE / "sec_aapl_facts.raw").read_bytes())
        submissions = json.loads((PROBE / "sec_aapl_submissions.raw").read_bytes())
        financials = extract_facts(facts, submissions, 320193)
        financials.to_parquet(stage / "s4_sec_fact_versions.parquet", index=False)
        eastmoney = json.loads((PROBE / "eastmoney_aapl_financials.raw").read_bytes())
        if not eastmoney.get("success"):
            raise ValueError("Eastmoney response was not successful")
        rows = pd.DataFrame(eastmoney["result"]["data"])
        if set(rows.SECURITY_CODE) != {"AAPL"}:
            raise ValueError("Eastmoney ticker mismatch")
        rows.to_parquet(stage / "eastmoney_financial_rows.parquet", index=False)
        groups = financials.groupby(["metric", "period_start", "period_end", "unit"], dropna=False)
        versions = groups.revision_id.nunique()
        s4 = {"entity": facts["entityName"], "cik": 320193, "fact_rows": len(financials),
              "metric_coverage": financials.groupby("metric").size().to_dict(),
              "periods_with_multiple_accessions": int((versions > 1).sum()),
              "recent_acceptance_timestamp_matches": int(financials.acceptance_timestamp_as_returned.notna().sum()),
              "historical_arrival_verified_rows": 0, "earliest_period": str(financials.period_end.min()),
              "latest_period": str(financials.period_end.max()), "eastmoney_rows": len(rows),
              "eastmoney_earliest_period": str(rows.REPORT_DATE.min()),
              "eastmoney_latest_period": str(rows.REPORT_DATE.max()),
              "ready_for_s4_backtest": False,
              "missing": ["PIT stock universe including delistings and sector/size history",
                          "verified historical availability and full filing acceptance history",
                          "quarterly/TTM mapping, debt-tag de-duplication, accounting comparability",
                          "raw stock execution/corporate-action ledger and sector-constrained portfolio"]}
        write_json(stage / "s4_readiness.json", s4)
        write_json(stage / "s5_readiness.json", {"ready_for_s5_backtest": False,
                   "implemented": ["versioned input gate", "60-minute consensus freeze", "prior-eight-quarter SUE",
                                   "252-session historical event percentile", "18ET/next-open/20-session schedule"],
                   "missing": ["historical median analyst EPS and pre-release snapshot/arrival timestamps",
                               "original EPS release and parser timestamps", "common GAAP/share basis",
                               "event portfolio with corporate actions and delistings"],
                   "eastmoney_eps_columns": [c for c in rows.columns if "EPS" in c],
                   "accepted_historical_consensus_records": 0})
        panels = feature_inputs(contract)
        ledger_path = PROJECT / "data/s8_fixed_candidates_v1.sqlite"
        ledger = TrialLedger(ledger_path, {"campaign": contract["campaign"], "budget": 30,
                                          "snapshot_hash": contract["snapshot_manifest_hash"],
                                          "development_end": contract["development_end"], "expressions": EXPRESSIONS,
                                          "source_hash": env["source_hash"], "holdout": contract["holdout"]})
        results, values, hashes = [], {}, {}
        for index, expression in enumerate(EXPRESSIONS, 1):
            result, frame = ledger.attempt(expression, panels)
            results.append(dict(result, expression=expression))
            if frame is None:
                raise ValueError("candidate already attempted or rejected; inspect durable ledger, do not reset it")
            frame.to_parquet(stage / f"s8_factor_{index}.parquet")
            values[f"F{index}"] = frame
            hashes[expression] = panel_hash(frame)
        flattened = pd.concat({name: value.stack(future_stack=True) for name, value in values.items()}, axis=1)
        correlations = flattened.corr(method="spearman", min_periods=100)
        selected = []
        for name in values:
            if all(pd.notna(correlations.loc[name, other]) and abs(correlations.loc[name, other]) <= .90 for other in selected):
                selected.append(name)
        ensemble = sum(values[name].rank(axis=1, pct=True) for name in selected)/len(selected)
        ensemble.to_parquet(stage / "s8_equal_rank_ensemble.parquet")
        write_json(stage / "s8_trials.json", results)
        write_json(stage / "s8_panel_hashes.json", hashes)
        write_json(stage / "s8_diagnostics.json", {"selected_in_registration_order": selected,
                   "development_spearman": correlations.to_dict(), "returns_evaluated": False,
                   "trial_attempts_in_this_run": 3, "development_end": contract["development_end"],
                   "holdout_available": False, "operating_system_sandbox_implemented": False,
                   "ensemble_finite_values": int(ensemble.notna().sum().sum())})
        with closing(sqlite3.connect(ledger_path)) as source, closing(sqlite3.connect(stage / "s8_trials.sqlite")) as target:
            source.backup(target)
        with ZipFile(stage / "reproduction_source.zip", "x", ZIP_DEFLATED) as archive:
            for relative in [*env["source_files"], "uv.lock", "pyproject.toml", "scripts/build_strategy_inputs.py"]:
                archive.write(PROJECT / relative, relative)
        shutil.copyfile(PROJECT / "scripts/probe_strategy_inputs.py", stage / "probe_strategy_inputs.py")
        write_json(stage / "manifest.json", {"files": {p.name: file_hash(p) for p in sorted(stage.iterdir())}})
    print(PROJECT / "outputs" / identifier)
    print(json.dumps(s4))


if __name__ == "__main__":
    main()
