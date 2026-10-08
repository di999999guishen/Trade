"""Offline row-level diagnostics for a frozen Sina candidate, without repairing data."""
import argparse
import json
from pathlib import Path

import pandas as pd

from quant_research.artifacts import file_hash, publication, run_id, verify_files, write_json
from quant_research.calendar import calendar


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.candidate / "manifest.json").read_text())
    verify_files(args.candidate, manifest)
    reports = {}
    for symbol in manifest["request"]["symbols"]:
        frame = pd.DataFrame(json.loads((args.candidate / f"{symbol}.response.json").read_text()))
        frame = frame.loc[(frame.d >= manifest["request"]["start"]) &
                          (frame.d < manifest["request"]["end_exclusive"])].copy()
        numeric = frame[["o", "h", "l", "c", "v"]].apply(pd.to_numeric, errors="raise")
        bad = (numeric.h < numeric[["o", "c", "l"]].max(axis=1)) | (
            numeric.l > numeric[["o", "c", "h"]].min(axis=1))
        dates = pd.DatetimeIndex(frame.d)
        expected = calendar().sessions_in_range(dates.min(), dates.max())
        reports[symbol] = {"rows": len(frame), "first": frame.d.iloc[0], "last": frame.d.iloc[-1],
                           "invalid_ohlc_rows": frame.loc[bad].to_dict("records"),
                           "non_session_dates": dates.difference(expected).strftime("%Y-%m-%d").tolist(),
                           "missing_sessions_within_coverage": expected.difference(dates).strftime("%Y-%m-%d").tolist()}
    root = Path(__file__).resolve().parents[1] / "outputs"
    identifier = run_id("sina_audit")
    with publication(root, identifier) as stage:
        write_json(stage / "quality.json", {"candidate": str(args.candidate.resolve()),
                                            "manifest_hash": file_hash(args.candidate / "manifest.json"),
                                            "status": "not_backtest_ready", "assets": reports})
    print(root / identifier)
    for symbol, report in reports.items():
        print(symbol, report["rows"], "bad_ohlc", len(report["invalid_ohlc_rows"]),
              "missing_sessions", len(report["missing_sessions_within_coverage"]),
              "non_sessions", len(report["non_session_dates"]))


if __name__ == "__main__":
    main()
