"""Descriptive overlapping calendar-window returns for a frozen comparison."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant_research.artifacts import file_hash, publication, run_id, verify_files, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("comparison", type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.comparison / "manifest.json").read_text(encoding="utf-8"))
    verify_files(args.comparison, manifest)
    nav = pd.read_parquet(args.comparison / "nav.parquet")
    summary = {}
    for key, group in nav.groupby("scenario"):
        if not key.endswith("_5bps"):
            continue
        group = group.set_index(pd.to_datetime(group.session))
        record = {}
        for years in (3, 5):
            values = []
            for end, row in group.iterrows():
                start = end - pd.DateOffset(years=years)
                if start < group.index[0]:
                    continue
                position = group.index.searchsorted(start)
                actual_start = group.index[position]
                days = (end - actual_start).days
                value = float((row.nav / group.nav.iloc[position]) ** (365.25 / days) - 1)
                values.append(value)
            if values:
                record[str(years)] = {"windows": len(values), "min_cagr": min(values),
                                      "median_cagr": float(np.median(values)), "max_cagr": max(values),
                                      "fraction_positive": float(np.mean(np.array(values) > 0))}
            else:
                record[str(years)] = {"status": "insufficient_history"}
        summary[key] = record
    identifier = "rolling_" + run_id().rsplit("_", 1)[1]
    with publication(args.comparison.parent, identifier) as stage:
        write_json(stage / "summary.json", {"status": "posthoc_descriptive_only", "source_manifest_hash": file_hash(args.comparison / "manifest.json"),
                                             "caveat": "Overlapping daily windows of existing NAV, not independent trials or newly cash-started portfolios",
                                             "results": summary})
        (stage / "analysis_source.py").write_bytes(Path(__file__).read_bytes())
        write_json(stage / "manifest.json", {"files": {p.name: file_hash(p) for p in stage.iterdir()}})
    print(args.comparison.parent / identifier)


if __name__ == "__main__":
    main()
