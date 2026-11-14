"""回归守卫：main.py 不得再硬编码辩论轮次，必须听 .env。

背景（2026-11-08）：main.py 里 `config["max_debate_rounds"] = 1` 无条件覆盖，
使 default_config.py::_apply_env_overrides 折叠进来的 env 值失效，
.env 设几轮都只跑 1 轮，表现为「统一主流程不跑多 agent 辩论」。
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class _FakeGraph:
    """只捕获 config，不建图、不调 LLM。"""

    last_config: dict = {}

    def __init__(self, debug=False, config=None):
        _FakeGraph.last_config = dict(config or {})

    def propagate(self, ticker, trade_date):
        return {}, "HOLD"

    def save_reports(self, state, ticker, save_path=None):
        return Path(save_path)


def test_main_honours_env_rounds(monkeypatch, tmp_path):
    if str(ROOT) not in sys.path:
        monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "5")
    monkeypatch.setenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "4")

    import tradingagents.default_config as default_config

    importlib.reload(default_config)  # DEFAULT_CONFIG 在导入期折叠 env，必须重载

    import main as main_mod

    importlib.reload(main_mod)
    monkeypatch.setattr(main_mod, "DEFAULT_CONFIG", default_config.DEFAULT_CONFIG)
    monkeypatch.setattr(main_mod, "TradingAgentsGraph", _FakeGraph)
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "510050.SS", "--date", "2026-11-06", "--quiet",
         "--reports-dir", str(tmp_path)],
    )

    assert main_mod.main() == 0
    assert _FakeGraph.last_config["max_debate_rounds"] == 5
    assert _FakeGraph.last_config["max_risk_discuss_rounds"] == 4


def test_main_clamps_rounds_to_at_least_one(monkeypatch, tmp_path):
    """rounds=0 会让 ConditionalLogic 的 count >= 2*rounds 立即成立，必须钳到 1。"""
    if str(ROOT) not in sys.path:
        monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "0")
    monkeypatch.setenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "0")

    import tradingagents.default_config as default_config

    importlib.reload(default_config)

    import main as main_mod

    importlib.reload(main_mod)
    monkeypatch.setattr(main_mod, "DEFAULT_CONFIG", default_config.DEFAULT_CONFIG)
    monkeypatch.setattr(main_mod, "TradingAgentsGraph", _FakeGraph)
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "510050.SS", "--date", "2026-11-06", "--quiet",
         "--reports-dir", str(tmp_path)],
    )

    assert main_mod.main() == 0
    assert _FakeGraph.last_config["max_debate_rounds"] == 1
    assert _FakeGraph.last_config["max_risk_discuss_rounds"] == 1
