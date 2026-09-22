import hashlib
import json
from datetime import datetime

import pytest

from prediction_research.screening import select_etfs
from prediction_research.weight_audit import audit


def test_audit_respects_decision_and_snapshot_hash(tmp_path):
    decision = datetime.fromisoformat("2026-09-17T02:00:00+00:00")
    rules = {"asset_classes": ["equity"], "min_amount": 5e6, "min_market_cap": 5e7,
             "top_n": None, "max_per_group": 1,
             "score_weights": {"main_flow": .30, "liquidity": .25, "momentum": .10,
                               "order_divergence": .10, "flow_to_cap": .10, "relative_strength": .15}}
    rows = [{"symbol": str(510000 + i), "name": name, "price": 1., "amount": 1e8,
             "market_cap": 1e9, "main_net_inflow": 1e6 * (i + 1), "main_net_inflow_pct": i + 1.,
             "change_pct": float(i), "quote_epoch": decision.timestamp() - 60}
            for i, name in enumerate(["沪深300ETF", "黄金股ETF", "煤炭ETF"])]
    eligible, selected = select_etfs(rows, rules)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"records": rows + [{**rows[0], "symbol": "future", "quote_epoch": decision.timestamp() + 1}]}), encoding="utf-8")
    screen = tmp_path / "screen.json"
    screen.write_text(json.dumps({"rules": rules, "selected": selected, "eligible_records": len(eligible),
                                  "decision_at_utc": decision.isoformat(),
                                  "source_snapshot": {"path": str(snapshot), "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()}}), encoding="utf-8")
    before = snapshot.read_bytes(), screen.read_bytes()
    result = audit(screen)
    assert result["eligible"] == 3
    assert result["factor_statistics"]["order_divergence"]["missing"] == 3
    assert result["replay_matches_frozen"]
    assert (snapshot.read_bytes(), screen.read_bytes()) == before
    snapshot.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        audit(screen)
