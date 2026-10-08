from pathlib import Path

import pandas as pd

from .artifacts import file_hash, publication, run_id, write_json
from .data import normalize
from .diagnostics import factor_diagnostics
from .factors import compute, registry
from .validation import annual_splits, labels


def factor_run(snapshot, config, project, horizon=5):
    frames, quality = normalize(snapshot, config)
    samples = labels(frames, horizon)
    tables = []
    for symbol, frame in frames.items():
        table = compute(frame)
        table = table[frame.eligible.fillna(False)]
        table.index = [f"{symbol}:{s.date()}:h{horizon}" for s in table.index]
        table.index.name = "sample_id"
        tables.append(table)
    features = pd.concat(tables)
    if samples.empty:
        raise ValueError("insufficient_data: no mature labels")
    samples = samples[samples.sample_id.isin(features.index)]
    if samples.empty:
        raise ValueError("insufficient_data: no eligible labeled factors")
    identifier = run_id("factors")
    with publication(Path(project) / "outputs", identifier) as stage:
        features.to_parquet(stage / "features.parquet")
        samples.to_parquet(stage / "labels.parquet", index=False)
        diagnostics = factor_diagnostics(features, samples, config.validation.seed)
        split = annual_splits(samples, horizon)
        write_json(stage / "diagnostics.json", diagnostics)
        write_json(stage / "splits.json", split)
        write_json(stage / "registry.json", registry())
        write_json(stage / "manifest.json", {"run_id": identifier, "status": "research_only",
                                             "quality": quality, "horizon": horizon,
                                             "split_status": split["status"],
                                             "files": {p.name: file_hash(p) for p in stage.iterdir()}})
    return {"run_id": identifier, "directory": str(Path(project) / "outputs" / identifier),
            "split_status": split["status"], "diagnostic_status": "descriptive_only_no_factor_selection"}
