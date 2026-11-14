"""规则闸门（C）回归：2026-11-09 用户定版 —— 缠论降权、流动性硬否决。"""

from prediction_research.tradingagents_adapter import rule_gate_rating


def _candidate(amount=1.0e8, pct=20.0, change=1.0, chanlun="neutral", divergence=None):
    return {
        "screen_evidence": {"amount": amount, "main_net_inflow_pct": pct, "change_pct": change},
        "chanlun_signal": {"signal": chanlun},
        "order_divergence": {"signal": divergence or "no_data", "status": "missing_data"},
    }


def test_thin_turnover_vetoed_even_with_chanlun_buy():
    """561680 实测：0.147 亿成交额、净流入占比 51.29%、缠论 buy —— 旧版照升，新版必须否决。"""
    g = rule_gate_rating(_candidate(amount=14_679_846, pct=51.29, chanlun="buy"), "Hold")
    assert g["rule_override"] is False
    assert g["rule_gated_rating"] == "Hold"
    assert g["reason"] == "liquidity_veto"


def test_missing_amount_is_conservatively_vetoed():
    """新浪回退时 K 线无成交额，amount 为 0/None ⇒ 不得当成流动性达标。"""
    for value in (None, 0, -1):
        g = rule_gate_rating(_candidate(amount=value), "Hold")
        assert g["reason"] == "liquidity_unavailable", value
        assert g["rule_override"] is False


def test_chanlun_no_longer_required():
    """缠论 neutral / sell 只要流动性+资金+非追高达标仍可升级 —— 这是「不添加缠论」。"""
    for signal in ("neutral", "sell"):
        g = rule_gate_rating(_candidate(chanlun=signal), "Hold")
        assert g["rule_override"] is True, signal
        assert g["rule_gated_rating"] == "Overweight"
        assert g["reason"] == "cash_confirm+liquidity_ok+not_chasing"


def test_chanlun_can_be_downweighted_not_decisive():
    """比重降低口径：缠论只作加分票，单独不能触发；与资金确认同时满足才够分。"""
    c = _candidate(pct=0.0, chanlun="buy")
    solo = rule_gate_rating(c, "Hold", chanlun_weight=0.5, min_confirm=1.5)
    assert solo["rule_override"] is False
    assert solo["reason"] == "no_confirmation"
    both = rule_gate_rating(_candidate(pct=20.0, chanlun="buy"), "Hold",
                            chanlun_weight=0.5, min_confirm=1.5)
    assert both["rule_override"] is True
    assert "chanlun_weighted" in both["reason"]


def test_chasing_and_no_cash_confirmation_veto():
    assert rule_gate_rating(_candidate(change=7.5), "Hold")["reason"] == "chasing"
    assert rule_gate_rating(_candidate(pct=-1.0), "Hold")["reason"] == "no_confirmation"


def test_llm_directional_rating_untouched():
    for rating in ("Buy", "Sell"):
        g = rule_gate_rating(_candidate(), rating)
        assert g == {"rule_gated_rating": rating, "rule_override": False,
                     "reason": "llm_directional_kept"}


def test_divergence_branch_still_needs_liquidity():
    thin = _candidate(amount=1.0e7, pct=0.0, divergence="bullish_divergence")
    assert rule_gate_rating(thin, "Hold", allow_divergence=True)["reason"] == "liquidity_veto"
    thick = _candidate(amount=1.0e8, pct=0.0, divergence="bullish_divergence")
    g = rule_gate_rating(thick, "Hold", allow_divergence=True)
    assert g["rule_override"] is True
    assert "order_divergence_bullish" in g["reason"]


def test_min_amount_is_configurable():
    c = _candidate(amount=3.0e7)
    assert rule_gate_rating(c, "Hold")["reason"] == "liquidity_veto"
    assert rule_gate_rating(c, "Hold", min_amount=2.0e7)["rule_override"] is True
