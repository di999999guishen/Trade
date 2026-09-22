"""Frozen three-factor S8 prototype; historical development evaluation, no untouched OOS claim."""
import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd

from quant_research.artifacts import digest, file_hash, provenance, publication, verify_files, write_json
from quant_research.config import load_config
from quant_research.data import normalize
from quant_research.engine import independent_audit, run_strategy
from quant_research.factors import SPEC, compute
from quant_research.research import metrics

PROJECT = Path(__file__).resolve().parents[1]
FROZEN = PROJECT / "outputs/strategy_inputs_build_20260918T094304068462Z_e2f73419bdc34b57acf1b2d8ab0fc06d"
BASELINE = PROJECT / "outputs/cmp_c8d6cb7b5b9e42719366d0e6a596cda3"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--ten-year", action="store_true", help="Frozen exploratory 2016-09-19 window")
    args = parser.parse_args()
    # Fixed factor list loaded from the previous registered run; no new search.
    verify_files(FROZEN, json.loads((FROZEN / "manifest.json").read_text(encoding="utf-8")))
    verify_files(BASELINE, json.loads((BASELINE / "manifest.json").read_text(encoding="utf-8")))
    frozen = json.loads((FROZEN / "contract.json").read_text(encoding="utf-8"))
    env = provenance(PROJECT)
    identifier = "s8p_"+uuid4().hex
    with publication(PROJECT / "outputs", identifier) as stage:
        contract = {"factor_registration": str(FROZEN), "factor_manifest_hash": file_hash(FROZEN / "manifest.json"),
                    "expressions": frozen["expressions"], "expression_trials_used": 3, "new_expression_trials": 0,
                    "snapshot": frozen["snapshot"], "snapshot_manifest_hash": frozen["snapshot_manifest_hash"],
                    "config": frozen["config"], "config_hash": frozen["config_hash"],
                    "source_hash": env["source_hash"], "lock_hash": env["lock_hash"],
                    "runner_hash": file_hash(__file__), "worker_hash": file_hash(PROJECT / "scripts/factor_worker.py"),
                    "portfolio": "S3 trend/momentum eligibility + top5 + same position/turnover/cost caps",
                    "start": "2016-09-19" if args.ten_year else "2023-01-03", "cost_bps": [5, 10, 25], "portfolio_trial_count": 3,
                    "historical_status": "previously_seen_history_exploratory_not_untouched_holdout",
                    "cash_return": 0, "process_timeout_seconds": 60, "os_access_sandbox": False}
        write_json(stage / "contract.json", contract)
        # Prospective registration only. Future prices do not exist in this run;
        # collecting them and maintaining access separation are not yet automated.
        holdout = PROJECT / "data/s8_prospective_holdout_v1.json"
        if not args.replay and not holdout.exists():
            write_json(holdout, {"status": "registered_not_collected_or_evaluated",
                       "start_inclusive": "2026-09-21", "end_inclusive": "2028-09-20",
                       "final_evaluation_not_before": "2028-09-21T22:00:00Z", "final_access_count": 0,
                       "expressions": contract["expressions"], "source_hash": env["source_hash"],
                       "portfolio_contract_hash": digest(contract), "registered_before_result_run": identifier,
                       "costs_bps": [5, 10, 25], "no_parameter_changes_within_registered_version": True,
                       "isolation_status": "plan_only_no_collector_or_os_access_boundary"})
        if args.replay:
            previous = args.replay.resolve()
            verify_files(previous, json.loads((previous / "manifest.json").read_text(encoding="utf-8")))
            if contract != json.loads((previous / "contract.json").read_text(encoding="utf-8")):
                raise ValueError("replay source/contract changed; use archived reproduction source")
        if file_hash(Path(contract["snapshot"]) / "manifest.json") != contract["snapshot_manifest_hash"]:
            raise ValueError("snapshot changed")
        if file_hash(contract["config"]) != contract["config_hash"]:
            raise ValueError("config changed")
        config = load_config(contract["config"])
        frames, quality = normalize(Path(contract["snapshot"]), config)
        write_json(stage / "quality.json", quality)
        factors = {s: compute(f).where(f.eligible, axis=0) for s, f in frames.items()}
        names = sorted({n.id for expr in contract["expressions"] for n in ast.walk(ast.parse(expr, mode="eval"))
                        if isinstance(n, ast.Name) and n.id in SPEC})
        # Only explicit feature panels enter the worker, no labels/models/API keys.
        worker_dir = stage / "worker"
        worker_dir.mkdir()
        for name in names:
            pd.DataFrame({s: f[name] for s, f in sorted(factors.items())}).to_parquet(worker_dir / f"{name}.parquet")
        write_json(worker_dir / "job.json", {"expressions": contract["expressions"],
                   "inputs": {n: file_hash(worker_dir / f"{n}.parquet") for n in names}})
        child_env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        child_env["PYTHONHASHSEED"] = "97"
        worker = PROJECT / "scripts/factor_worker.py"
        subprocess.run([sys.executable, "-I", str(worker), str(worker_dir)], cwd=worker_dir,
                       env=child_env, timeout=60, check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        scores = pd.read_parquet(worker_dir / "scores.parquet")
        summary, audits, hashes = {}, {}, {}
        for bps in contract["cost_bps"]:
            key = f"s8_fixed_ensemble_{bps}bps"
            result = run_strategy(frames, config, "s8_fixed_ensemble", bps, start=pd.Timestamp(contract["start"]), score_panel=scores)
            audits[key] = independent_audit(result, frames, config.portfolio.initial_capital_usd)
            summary[key] = metrics(result["nav"])
            hashes[key] = digest(result)
            for kind in ("nav", "targets", "fills", "holdings"):
                pd.DataFrame(result[kind]).to_parquet(stage / f"{key}_{kind}.parquet", index=False)
            print(key, summary[key]["cagr"], flush=True)
        baseline = json.loads((BASELINE / "results.json").read_text(encoding="utf-8"))
        prefix = "full__" if args.ten_year else "model_window__"
        controls = {k: v for k, v in baseline["metrics"].items() if k.startswith(prefix)}
        if args.ten_year:
            # Recompute matched controls and require exact agreement with frozen ledgers.
            for strategy in ("s1_etf_momentum", "s2_multi_factor"):
                for bps in contract["cost_bps"]:
                    key = f"{strategy}_{bps}bps"
                    result = run_strategy(frames, config, strategy, bps, start=pd.Timestamp(contract["start"]))
                    audits[key] = independent_audit(result, frames, config.portfolio.initial_capital_usd)
                    if digest(result) != baseline["scenario_hashes"][prefix+key]:
                        raise AssertionError(f"frozen control changed: {key}")
                    summary[key] = metrics(result["nav"])
                    hashes[key] = digest(result)
                    for kind in ("nav", "targets", "fills", "holdings"):
                        pd.DataFrame(result[kind]).to_parquet(stage / f"{key}_{kind}.parquet", index=False)
                    print(key, summary[key]["cagr"], flush=True)
        write_json(stage / "baseline_metrics.json", controls)
        write_json(stage / "metrics.json", summary)
        write_json(stage / "audits.json", audits)
        result_hash = digest({"metrics": summary, "audits": audits, "hashes": hashes})
        write_json(stage / "result_hash.json", {"result_hash": result_hash,
                                               "scenario_hashes": hashes})
        if args.replay:
            if result_hash != json.loads((previous / "result_hash.json").read_text(encoding="utf-8"))["result_hash"]:
                raise AssertionError("S8 portfolio replay mismatch")
            write_json(stage / "replay.json", {"passed": True, "source_run": str(previous), "result_hash": result_hash})
        write_json(stage / "environment.json", env)
        if provenance(PROJECT)["source_hash"] != env["source_hash"]:
            raise ValueError("source changed during run; rerun from stable source before publication")
        with ZipFile(stage / "reproduction_source.zip", "x", ZIP_DEFLATED) as archive:
            for relative in [*env["source_files"], "uv.lock", "pyproject.toml", "scripts/run_s8_portfolio.py", "scripts/factor_worker.py"]:
                archive.write(PROJECT / relative, relative)
        write_json(stage / "manifest.json", {"files": {p.relative_to(stage).as_posix(): file_hash(p)
                   for p in sorted(stage.rglob("*")) if p.is_file()}})
    print(PROJECT / "outputs" / identifier)


if __name__ == "__main__":
    main()
