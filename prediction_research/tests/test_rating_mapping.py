from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from prediction_research import tradingagents_adapter as adapter
from prediction_research.store import connect, record_agent_analysis


@pytest.mark.parametrize("text,rating,intent,direction", [
    ("BUY", "Buy", "enter_or_add", "up"),
    ("Overweight", "Overweight", "increase_exposure", "unclassified"),
    ("Hold", "Hold", "maintain_or_wait", "neutral"),
    ("UNDERWEIGHT", "Underweight", "reduce_exposure", "unclassified"),
    ("Sell", "Sell", "exit_or_avoid", "down"),
    ("买入", "Buy", "enter_or_add", "up"),
    ("增持", "Overweight", "increase_exposure", "unclassified"),
    ("建议观望", "Hold", "maintain_or_wait", "neutral"),
    ("持有", "Hold", "maintain_or_wait", "neutral"),
    ("减持", "Underweight", "reduce_exposure", "unclassified"),
    ("卖出", "Sell", "exit_or_avoid", "down"),
    ("**Rating**: **Underweight**", "Underweight", "reduce_exposure", "unclassified"),
    ("**Rating:** Overweight", "Overweight", "increase_exposure", "unclassified"),
    ("最终评级：建议增持。", "Overweight", "increase_exposure", "unclassified"),
    ("Final decision: SELL", "Sell", "exit_or_avoid", "down"),
])
def test_explicit_ratings_preserve_exposure_intent(text, rating, intent, direction):
    assert adapter.decision_semantics(text) == {
        "rating": rating, "action_intent": intent, "direction": direction, "parse_status": "parsed",
    }
    assert adapter.decision_direction(text) == direction


@pytest.mark.parametrize("text", [
    None, "", "  ", "insufficient evidence", "BUYBACK", "SELLING", "shareholder",
    "Buy / Hold / Sell", "Buy and Sell", "Hold or Underweight", "建议买入或减持",
    "Do not BUY", "不建议买入", "等待卖出信号，不要买入", "看多", "看空",
    "Buy if the price breaks resistance", "Buybacks support the price; hold your decision.",
    "Rating: Buy if conditions improve", "Rating: Unknown\nRecommendation: Buy",
])
def test_unknown_or_narrative_text_never_invents_hold_or_direction(text):
    result = adapter.decision_semantics(text)
    assert result["rating"] is None
    assert result["action_intent"] == "unknown"
    assert result["direction"] == "unclassified"
    assert result["parse_status"] in {"missing", "unrecognized"}
    assert adapter.decision_direction(text) == "unclassified"


def test_explicit_field_is_distinct_from_words_in_explanation():
    text = "**Rating**: Underweight\nRationale: BUY was considered; do not SELL everything."
    assert adapter.decision_semantics(text)["rating"] == "Underweight"
    assert adapter.decision_direction(text) == "unclassified"


@pytest.mark.parametrize("text", [
    "Rating: Buy\nRating: Sell",
    "Final decision: Hold\nFinal decision: Underweight",
    "Recommendation: Buy\nAction: Sell",
])
def test_conflicting_explicit_fields_are_ambiguous(text):
    result = adapter.decision_semantics(text)
    assert result["parse_status"] == "ambiguous"
    assert result["rating"] is None and result["direction"] == "unclassified"


def test_stdout_extraction_does_not_choose_first_conflicting_rating():
    stdout = "Research log: Final decision: Buy\nFinal decision: Hold\nFinal decision: Sell\n"
    assert adapter.decision_semantics(adapter._decision(stdout))["parse_status"] == "ambiguous"
    assert adapter._decision("Final decision:\nBUY\n") is None
    assert adapter._decision("Final decision: Underweight\n") == "Underweight"


@pytest.mark.parametrize("returncode", [0, 1])
def test_batch_report_keeps_semantics_and_original_decision(tmp_path, monkeypatch, returncode):
    project = tmp_path / "project"
    interpreter = project / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    (tmp_path / "runs").mkdir()
    cfg = {"_project_dir": str(tmp_path), "runs_dir": "runs", "state_db": "state.db",
           "tradingagents": {"reports_dir": "runs/agents", "timeout_seconds": 1}}
    plan = {"project_available": True, "project_dir": str(project), "trade_date": "2026-01-01",
            "screen_report": "test-screen.json", "candidates": [{"ticker": "A.SS", "symbol": "A"}]}
    monkeypatch.setattr(adapter, "build_plan", lambda *_: plan)
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=returncode, stdout="Final decision: Underweight\n", stderr=""))
    path, report = adapter.run_tradingagents(cfg, 1)
    item = report["items"][0]
    assert path.exists()
    assert item["decision"] == "Underweight"
    assert item["decision_semantics"]["action_intent"] == ("reduce_exposure" if returncode == 0 else "unknown")
    assert item["decision_semantics"]["parse_status"] == ("parsed" if returncode == 0 else "missing")
    assert item["decision_semantics"]["direction"] == "unclassified"
    with connect(tmp_path / "state.db") as connection:
        assert connection.execute("SELECT decision_text FROM agent_analyses").fetchone()[0] == "Underweight"


def test_settlement_does_not_score_exposure_change_as_price_prediction_or_rewrite_history(tmp_path, monkeypatch):
    cfg = {"_project_dir": str(tmp_path), "state_db": "state.db", "market_data_dir": "market", "horizons": [5]}
    ratings = ["Buy", "Overweight", "Hold", "Underweight", "Sell", "unrecognized"]
    db_path = tmp_path / "state.db"
    with connect(db_path) as connection:
        for index, rating in enumerate(ratings):
            record_agent_analysis(connection, "test-screen", {
                "ticker": f"{index}.SS", "symbol": str(index), "status": "ok", "decision": rating,
                "report_dir": "test-report",
            }, "2026-01-01")
        old_id = record_agent_analysis(connection, "old-screen", {
            "ticker": "OLD.SS", "symbol": "OLD", "status": "ok", "decision": "Underweight",
            "report_dir": "old-report",
        }, "2026-01-01")
        old = ("old-outcome", old_id, 5, "down", "2026-01-06", -0.1, 0, 1, "2026-01-06T18:00:00Z")
        connection.execute("INSERT INTO agent_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", old)
        connection.commit()
    sample = SimpleNamespace(feature_date=date(2026, 1, 1), target_end_date=date(2026, 1, 6),
                             target_return=-0.1, target_up=0)
    monkeypatch.setattr(adapter, "load_cache", lambda *_: object())
    monkeypatch.setattr(adapter, "build_samples", lambda *_: [sample])
    report = adapter.settle_agent_analyses(cfg)
    assert report["settled"] == len(ratings)
    by_rating = {item["decision_semantics"]["rating"]: item for item in report["items"]}
    assert by_rating["Buy"]["direction_correct"] == 0
    assert by_rating["Sell"]["direction_correct"] == 1
    for rating in ("Overweight", "Underweight", "Hold", None):
        assert by_rating[rating]["direction_correct"] is None
    with connect(db_path) as connection:
        assert tuple(connection.execute("SELECT * FROM agent_outcomes WHERE outcome_id='old-outcome'").fetchone()) == old
        new_changes = connection.execute(
            "SELECT direction,direction_correct FROM agent_outcomes o JOIN agent_analyses a USING (analysis_id) "
            "WHERE a.decision_text IN ('Overweight','Underweight') AND a.symbol != 'OLD'"
        ).fetchall()
        assert [tuple(row) for row in new_changes] == [("unclassified", None), ("unclassified", None)]
    assert adapter.settle_agent_analyses(cfg)["settled"] == 0
