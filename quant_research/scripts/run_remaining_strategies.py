"""Register and run S6 hypothetical scenarios and S7 illustrative expiry payoffs."""
import argparse
import json
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
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
from quant_research.data import normalize, read_snapshot
from quant_research.option_payoffs import VanillaLeg, expiry_pnl
from quant_research.research import metrics
from quant_research.reversal import VARIANTS, audit, signals, simulate, target_schedule

PROJECT = Path(__file__).resolve().parents[1]
SNAPSHOT = PROJECT / "data/ten_year/snapshot_20260918T080154884269Z_e0e281cc5c354cbb8476b16f3f2c4c41"
CONFIG = PROJECT / "outputs/tenyear_inputs_97901b32c1d44ad1b567d3839e6eb765/config.json"
SCENARIOS = [(0, 0)] + [(bps, borrow) for bps in (5, 10, 25) for borrow in (0, .02, .05, .10)]


def run(stage, contract):
    frames, quality = normalize(Path(contract["snapshot"]), load_config(contract["config"]))
    write_json(stage / "data_quality.json", quality)
    panel = signals(frames)
    panel.to_parquet(stage / "signals.parquet", index=False)
    dates = frames["SPY"].index[frames["SPY"].index >= "2016-09-19"]
    summary, audits, hashes = {}, {}, {}
    for variant in VARIANTS:
        schedule = target_schedule(panel, dates, variant)
        write_json(stage / f"{variant}_targets.json", [dict(r, session=str(r["session"].date())) for r in schedule])
        for bps, borrow in SCENARIOS:
            key = f"{variant}_{bps}bps_borrow{borrow:g}"
            result = simulate(frames, schedule, bps, borrow, contract["initial_capital"])
            audits[key] = audit(result, frames, bps, borrow, contract["initial_capital"])
            summary[key] = metrics(result["nav"])
            nav = pd.DataFrame(result["nav"]).set_index("session")
            strategy_returns = nav.nav.pct_change().iloc[1:].to_numpy()
            market = frames["SPY"].loc[dates, "close"].pct_change().iloc[1:].to_numpy()
            summary[key].update({"borrow_cost_usd": float(nav.borrow_cost.sum()),
                                 "average_gross_exposure": float(nav.gross_exposure.mean()),
                                 "max_close_gross_exposure": float(nav.gross_exposure.max()),
                                 "max_close_abs_net_exposure": float(nav.net_exposure.abs().max()),
                                 "realized_spy_beta": float(np.cov(strategy_returns, market)[0, 1]/np.var(market, ddof=1)),
                                 "trade_dates": len({f["session"] for f in result["fills"]}),
                                 "actual_borrow_evidence_dates": 0})
            hashes[key] = digest(result)
            for name, rows in result.items():
                pd.DataFrame(rows).to_parquet(stage / f"{key}_{name}.parquet", index=False)
            print(f"{key}: CAGR={summary[key]['cagr']:.6%}; ledger passed", flush=True)
    # All inputs here are invented pedagogical values, not real option quotes.
    def leg(kind, strike, count, bid, ask):
        return VanillaLeg(kind, strike, count, 100, 100, bid, ask, .65, "illustrative_common_expiry")
    structures = {"collar": [leg("put", 95, 1, 2.8, 3.0), leg("call", 105, -1, 2.0, 2.2)],
                  "put_spread": [leg("put", 95, 1, 2.8, 3.0), leg("put", 85, -1, 1.0, 1.2)],
                  "covered_call": [leg("call", 105, -1, 2.0, 2.2)]}
    option_rows = [{"variant": variant, "spot_start": 100, "spot_end": terminal,
                    **expiry_pnl(100, terminal, 100, legs, variant)}
                   for variant, legs in structures.items() for terminal in (60, 80, 85, 90, 95, 100, 105, 110, 120, 140)]
    write_json(stage / "s7_inputs.json", {v: [x.__dict__ for x in legs] for v, legs in structures.items()})
    write_json(stage / "s7_scenarios.json", option_rows)
    write_json(stage / "metrics.json", summary)
    write_json(stage / "audits.json", audits)
    result_hash = digest({"metrics": summary, "audits": audits, "scenarios": hashes, "options": option_rows})
    write_json(stage / "result_hash.json", {"result_hash": result_hash, "scenario_hashes": hashes})
    return result_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    env = provenance(PROJECT)
    if args.replay:
        previous = args.replay.resolve()
        manifest = json.loads((previous / "manifest.json").read_text(encoding="utf-8"))
        verify_files(previous, manifest)
        contract = json.loads((previous / "contract.json").read_text(encoding="utf-8"))
        if env["source_hash"] != contract["source_hash"] or env["lock_hash"] != contract["lock_hash"]:
            raise ValueError("source/lock differs: use the archived reproduction source")
        if file_hash(__file__) != contract["runner_hash"]:
            raise ValueError("runner differs")
    else:
        contract = {"status": "exploratory_hypothetical_not_executable", "snapshot": str(SNAPSHOT),
                    "snapshot_hash": read_snapshot(SNAPSHOT)["content_hash"], "config": str(CONFIG),
                    "config_hash": file_hash(CONFIG), "initial_capital": 100000.0,
                    "source_hash": env["source_hash"], "lock_hash": env["lock_hash"],
                    "runner_hash": file_hash(__file__), "start": "2016-09-19", "variants": list(VARIANTS),
                    "cost_scenarios": SCENARIOS, "trials": 39, "holdout": "none_previously_observed_history",
                    "assumptions": ["All eligible shorts always borrowable; no recalls",
                                    "Adjusted total-return proxy, fractional units; no raw dividend-payment ledger",
                                    "Next-open exact weights, every-day rebalance; no 2pp deadband",
                                    "Zero cash interest/rebate; illustrative collateral=150% current short value",
                                    "Borrow fee on previous-close short value, actual calendar days/365.25",
                                    "Five held sessions then compulsory one-session flat for that asset",
                                    "Optimizer maximizes retained gross; no return-based parameter selection",
                                    "Training residual sample std ddof=1; epsilon=1e-12",
                                    "Raw reversal control uses negative compounded 5-day return, SPY-only risk beta",
                                    "Target constraints hold at rebalance; intraday/overnight drift is reported",
                                    "End NAV is marked, no forced terminal liquidation or beyond-end borrow charge",
                                    "S7 invented expiry examples only; no quote qualification or portfolio sizing"]}
    if read_snapshot(Path(contract["snapshot"]))["content_hash"] != contract["snapshot_hash"]:
        raise ValueError("snapshot differs")
    if file_hash(contract["config"]) != contract["config_hash"]:
        raise ValueError("config differs")
    identifier = run_id("s6s7_replay" if args.replay else "s6s7")
    with publication(PROJECT / "outputs", identifier) as stage:
        write_json(stage / "contract.json", contract)  # before any performance computation
        write_json(stage / "environment.json", env)
        with ZipFile(stage / "reproduction_source.zip", "x", ZIP_DEFLATED) as archive:
            for relative in [*env["source_files"], "uv.lock", "pyproject.toml", "scripts/run_remaining_strategies.py"]:
                archive.write(PROJECT / relative, relative)
        result_hash = run(stage, contract)
        if args.replay:
            expected = json.loads((previous / "result_hash.json").read_text(encoding="utf-8"))["result_hash"]
            if result_hash != expected:
                raise AssertionError("independent-process result replay mismatch")
            write_json(stage / "replay_check.json", {"passed": True, "source_run": str(previous),
                                                      "result_hash": result_hash})
        write_json(stage / "manifest.json", {"files": {p.name: file_hash(p) for p in sorted(stage.iterdir())},
                                               "result_hash": result_hash})
    print(json.dumps({"output": str(PROJECT / "outputs" / identifier), "result_hash": result_hash}))


if __name__ == "__main__":
    sys.exit(main())
