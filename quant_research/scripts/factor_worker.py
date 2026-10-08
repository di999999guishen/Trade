"""Dedicated S8 computation process: only staged input panels and fixed DSL.

No credentials are passed by the parent. This is process/data separation with
a parent timeout, NOT an OS security sandbox or a network/filesystem deny rule.
"""
import json
import sys
from pathlib import Path

import pandas as pd

from quant_research.artifacts import digest, file_hash, write_json
from quant_research.factor_dsl import evaluate


def main():
    root = Path(sys.argv[1]).resolve()
    job = json.loads((root / "job.json").read_text(encoding="utf-8"))
    inputs = {}
    for name, expected in job["inputs"].items():
        if not name.isidentifier():
            raise ValueError("unsafe input identifier")
        path = root / f"{name}.parquet"
        if file_hash(path) != expected:
            raise ValueError("input hash mismatch")
        frame = pd.read_parquet(path)
        if len(frame) > 20000 or len(frame.columns) > 200:
            raise ValueError("worker input limit exceeded")
        inputs[name] = frame
    values = [evaluate(expression, inputs) for expression in job["expressions"]]
    if not 1 <= len(values) <= 5:
        raise ValueError("one to five frozen factors required")
    ensemble = sum(v.rank(axis=1, pct=True) for v in values)/len(values)
    ensemble.to_parquet(root / "scores.parquet")
    write_json(root / "worker_result.json", {"expressions_hash": digest(job["expressions"]),
               "scores_hash": file_hash(root / "scores.parquet"), "rows": len(ensemble),
               "first_session": str(ensemble.index.min().date()), "last_session": str(ensemble.index.max().date()),
               "process_isolated": True, "os_access_sandbox": False})


if __name__ == "__main__":
    main()
