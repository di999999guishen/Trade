"""Frozen ten-year inverse-volatility control with identical S2 execution constraints."""
import argparse
import json
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
from quant_research.engine import independent_audit, run_strategy
from quant_research.research import metrics

PROJECT = Path(__file__).resolve().parents[1]
SNAPSHOT = PROJECT / "data/ten_year/snapshot_20260918T080154884269Z_e0e281cc5c354cbb8476b16f3f2c4c41"
CONFIG = PROJECT / "outputs/tenyear_inputs_97901b32c1d44ad1b567d3839e6eb765/config.json"
STRATEGIES = ["s2_multi_factor", "s2_equal_allocation", "s2_inverse_volatility"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    env = provenance(PROJECT)
    baseline_path = PROJECT / "outputs/cmp_c8d6cb7b5b9e42719366d0e6a596cda3"
    verify_files(baseline_path, json.loads((baseline_path / "manifest.json").read_text(encoding="utf-8")))
    baseline = json.loads((baseline_path / "results.json").read_text(encoding="utf-8"))
    contract = {"snapshot": str(SNAPSHOT), "config": str(CONFIG), "config_hash": file_hash(CONFIG),
                "baseline_results_hash": file_hash(baseline_path / "results.json"),
                "snapshot_manifest_hash": file_hash(SNAPSHOT / "manifest.json"),
                "source_hash": env["source_hash"], "lock_hash": env["lock_hash"], "runner_hash": file_hash(__file__),
                "strategies": STRATEGIES, "start": "2016-09-19", "cost_bps": [5, 10, 25],
                "new_control": "all eligible assets, inverse max(vol63,0.05); no trend/momentum/top-k",
                "shared": "S2 caps, covariance shrinkage, 10% forecast volatility scaling, deadband and turnover",
                "trial_count": 9, "holdout": "unavailable_previously_seen_history"}
    previous = None
    if args.replay:
        previous = args.replay.resolve()
        verify_files(previous, json.loads((previous / "manifest.json").read_text(encoding="utf-8")))
        frozen_contract = json.loads((previous / "contract.json").read_text(encoding="utf-8"))
        # Engine/source, parameters, inputs and dependencies remain exact. Record
        # runner-only revisions separately and still require identical outputs.
        if ({k: v for k, v in contract.items() if k != "runner_hash"}
                != {k: v for k, v in frozen_contract.items() if k != "runner_hash"}):
            raise ValueError("replay contract/source differs; use archived source")
    config = load_config(CONFIG)
    identifier = run_id("s2_control_replay" if previous else "s2_control")
    with publication(PROJECT / "outputs", identifier) as stage:
        write_json(stage / "contract.json", contract)
        frames, quality = normalize(SNAPSHOT, config)
        write_json(stage / "data_quality.json", quality)
        summary, audits, hashes = {}, {}, {}
        for strategy in STRATEGIES:
            for cost in contract["cost_bps"]:
                key = f"{strategy}_{cost}bps"
                result = run_strategy(frames, config, strategy, cost, start=pd.Timestamp(contract["start"]))
                audits[key] = independent_audit(result, frames, config.portfolio.initial_capital_usd)
                summary[key] = metrics(result["nav"])
                hashes[key] = digest(result)
                for kind in ("nav", "fills", "targets", "holdings"):
                    pd.DataFrame(result[kind]).to_parquet(stage / f"{key}_{kind}.parquet", index=False)
                print(key, summary[key]["cagr"], flush=True)
        result_hash = digest({"metrics": summary, "audits": audits, "hashes": hashes})
        write_json(stage / "metrics.json", summary)
        write_json(stage / "audits.json", audits)
        write_json(stage / "result_hash.json", {"result_hash": result_hash})
        if previous:
            if result_hash != json.loads((previous / "result_hash.json").read_text(encoding="utf-8"))["result_hash"]:
                raise AssertionError("result hash mismatch")
            write_json(stage / "replay.json", {"passed": True, "source_run": str(previous), "result_hash": result_hash,
                                               "original_runner_hash": frozen_contract["runner_hash"],
                                               "replay_runner_hash": contract["runner_hash"]})
        unchanged = {}
        for key, scenario_hash in hashes.items():
            if key.startswith("s2_inverse_volatility"):
                continue
            unchanged[key] = scenario_hash == baseline["scenario_hashes"]["full__"+key]
        if not all(unchanged.values()):
            raise AssertionError("existing S2 strategies changed versus published baseline")
        write_json(stage / "baseline_unchanged.json", unchanged)
        write_json(stage / "environment.json", env)
        with ZipFile(stage / "reproduction_source.zip", "x", ZIP_DEFLATED) as archive:
            for relative in [*env["source_files"], "uv.lock", "pyproject.toml", "scripts/run_s2_control.py"]:
                archive.write(PROJECT / relative, relative)
        write_json(stage / "manifest.json", {"files": {p.name: file_hash(p) for p in sorted(stage.iterdir())}})
    print(json.dumps({"output": str(PROJECT / "outputs" / identifier), "result_hash": result_hash}))


if __name__ == "__main__":
    main()
