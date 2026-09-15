import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from prediction_research import tradingagents_adapter as adapter
from prediction_research.screening import select_etfs, screen_etfs
from prediction_research.tests.test_cycle_remediation import flow_row, snapshot
from prediction_research.tests.test_evidence import cfg
from tradingagents.evidence_context import render_screen_evidence


def test_relative_strength_uses_valid_peers_and_excludes_missing(cfg):
    rules = cfg["etf_market"]["screen"]
    rows = [flow_row(str(i)) | {"change_pct": change} for i, change in enumerate([-2, 0, 3])]
    rows += [flow_row("invalid") | {"change_pct": 100, "price": float("nan")}]
    eligible, _ = select_etfs(rows, rules)
    by_symbol = {row["symbol"]: row for row in eligible}
    assert "invalid" not in by_symbol
    assert by_symbol["0"]["screen_components"]["relative_strength"] == -0.4
    assert by_symbol["2"]["screen_components"]["relative_strength"] == 0.6
    assert by_symbol["2"]["relative_strength_context"]["peer_count"] == 3
    missing, _ = select_etfs([flow_row() | {"change_pct": None}], rules)
    assert missing[0]["screen_components"]["momentum"] is None
    assert missing[0]["screen_components"]["relative_strength"] is None
    assert missing[0]["screen_score"] == pytest.approx(sum(v for v in missing[0]["screen_contributions"].values() if v is not None))


def test_flow_to_cap_has_signed_and_bounded_contribution(cfg):
    rules = cfg["etf_market"]["screen"]
    rules["score_weights"] = {"main_flow": 0, "momentum": 0, "liquidity": 0,
                              "order_divergence": 0, "flow_to_cap": 1}
    rows, _ = select_etfs([flow_row("in", net=50_000_000), flow_row("out", net=-50_000_000)], rules)
    assert [(row["symbol"], row["screen_score"]) for row in rows] == [("in", 1), ("out", -1)]


def test_frozen_screen_reaches_actual_subprocess_argument(cfg, monkeypatch):
    snapshot(cfg)
    _, frozen = screen_etfs(cfg, decision_at_utc="2024-08-17T12:00:00Z")
    project = Path(cfg["_project_dir"]) / "agent_project"
    interpreter = project / ".venv/Scripts/python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    (project / "main.py").touch()
    cfg["tradingagents"] = {"project_dir": str(project), "reports_dir": "runs/agents", "timeout_seconds": 1}
    captured = []
    def run(command, **kwargs):
        payload = json.loads(Path(command[command.index("--screen-evidence") + 1]).read_text(encoding="utf-8"))
        text = render_screen_evidence(payload, command[2], command[4])
        assert "screen_components" in text
        assert payload["source_snapshot"] == frozen["source_snapshot"]
        assert payload["decision_at_utc"] == frozen["decision_at_utc"]
        captured.append(payload)
        return SimpleNamespace(returncode=0, stdout="Final decision: Overweight\n", stderr="")
    monkeypatch.setattr(adapter.subprocess, "run", run)
    _, report = adapter.run_tradingagents(cfg, 1)
    assert len(captured) == 1
    assert report["items"][0]["decision_semantics"]["rating"] == "Overweight"
    assert report["items"][0]["evidence_handoff_status"] == "passed_to_cli"


@pytest.mark.parametrize("ticker,day", [("other.SS", "2024-08-17"), ("518880.SS", "2024-08-18")])
def test_evidence_rejects_other_instrument_or_date(ticker, day):
    with pytest.raises(ValueError, match="does not match"):
        render_screen_evidence({"schema_version": 1, "ticker": "518880.SS", "trade_date": "2024-08-17"}, ticker, day)


@pytest.mark.parametrize("action", ["Overweight", "Underweight"])
def test_trader_retains_intermediate_rating(action):
    from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
    proposal = TraderProposal(action=action, reasoning="Evidence supports gradual exposure adjustment")
    assert action.upper() in render_trader_proposal(proposal)


def test_graph_injects_evidence_before_any_agent_runs_and_changes_checkpoint():
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
    graph.selected_analysts = ["market"]
    old_signature = graph._run_signature("stock")
    graph.config["screen_evidence_context"] = " frozen factor evidence"
    assert graph._run_signature("stock") != old_signature
    graph.memory_log = SimpleNamespace(get_past_context=lambda _, **kwargs: "")
    graph.resolve_instrument_context = lambda *_: "ETF identity"
    captured = {}
    def initial(*args, **kwargs):
        captured.update(kwargs)
        raise StopIteration("stop before graph execution")
    graph.propagator = SimpleNamespace(create_initial_state=initial)
    with pytest.raises(StopIteration):
        graph._run_graph("518880.SS", "2024-08-17")
    assert captured["instrument_context"] == "ETF identity frozen factor evidence"
