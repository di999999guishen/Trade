import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from quant_research.config import ResearchConfig
from quant_research.fixtures import create_fixture
from quant_research.research import replay, research


def test_multi_asset_replay_across_python_hash_seeds(tmp_path):
    from quant_research.data import DataRequest, snapshot
    from quant_research.fixtures import FixtureProvider
    symbols = ("SPY", "QQQ", "IWM", "MDY", "GLD", "TLT", "IEF", "XLV")
    path = snapshot(DataRequest(symbols, date(2022, 1, 3), date(2023, 2, 1)),
                    FixtureProvider(), tmp_path, attempts=1, pause=0)
    code = """
import sys
from copy import deepcopy
from quant_research.artifacts import digest
from quant_research.config import DEFAULTS, ResearchConfig
from quant_research.data import normalize
from quant_research.engine import run_strategy
p = deepcopy(DEFAULTS)
p['universe']['symbols'] = ['SPY','QQQ','IWM','MDY','GLD','TLT','IEF','XLV']
c = ResearchConfig.model_validate(p)
frames, _ = normalize(sys.argv[1], c)
print(digest({s: run_strategy(frames, c, s, 5) for s in ['eligible_pool_equal_weight','s1_etf_momentum','s2_multi_factor']}))
"""
    hashes = []
    for seed in ("1", "97"):
        result = subprocess.run([sys.executable, "-c", code, str(path)], env={**os.environ, "PYTHONHASHSEED": seed},
                                check=True, capture_output=True, text=True, timeout=60)
        hashes.append(result.stdout.strip().splitlines()[-1])
    assert hashes[0] == hashes[1]


def test_full_bundle_offline_replay_and_tamper(tmp_path):
    project = Path(__file__).parents[1]
    config = ResearchConfig.model_validate_json((project / "configs" / "fixture.json").read_text())
    path = create_fixture(tmp_path / "snapshots")["snapshot"]
    result = research(path, config, tmp_path)
    directory = Path(result["directory"])
    assert (directory / "overview.png").is_file()
    output = replay(directory / "manifest.json", tmp_path)
    assert output["status"] == "reproduced_offline"
    assert output["result_hash"] == result["result_hash"]
    data = json.loads((directory / "results.json").read_text())
    assert len(data["results"]) == 18
    assert all(a["status"] == "passed" for a in data["audits"].values())
    with (directory / "metrics.json").open("a") as handle:
        handle.write("changed")
    with pytest.raises(ValueError, match="integrity"):
        replay(directory / "manifest.json", tmp_path)
