"""Factor pre-screen: conservative retention and the top-n contract."""

import daily_predict
from tradingagents.factors import prefilter


def _series(start, daily_rate, days=30):
    closes = [start * (1 + daily_rate) ** index for index in range(days)]
    amounts = [5e7 + index * 1e6 for index in range(days)]
    return closes, amounts


def _install_history(monkeypatch, mapping):
    def load(ticker, config=None):
        try:
            return mapping[ticker]
        except KeyError as exc:
            raise prefilter.ScreeningDataError(f"no history for {ticker}") from exc

    monkeypatch.setattr(prefilter, "_load_history", load)


def test_holdings_and_mandatory_kept_even_when_not_rankable(monkeypatch):
    # The pool excludes the base, so these two can never be scored or dropped.
    _install_history(monkeypatch, {
        "AAA.SZ": _series(10, 0.01),
        "BBB.SZ": _series(10, 0.002),
    })

    selected = prefilter.select_tickers(
        ["HOLD.SZ"], ["MAND.SZ"], ["AAA.SZ", "BBB.SZ"], top_n=1, config={}
    )

    assert selected == ["HOLD.SZ", "MAND.SZ", "AAA.SZ"]
    assert "BBB.SZ" not in selected


def test_env_candidates_are_screened_and_added(monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_CANDIDATES", "510300.SS,159792.SZ")
    monkeypatch.setenv("TRADINGAGENTS_SCREEN_TOP_N", "1")
    seen = []

    def fake_rank(candidates, config=None):
        seen.extend(candidates)
        return [
            ("510300.SS", 0.82,
             "中期+4.10% 短期+1.20% 流动性分位86% 20日均额12.30亿"),
            ("159792.SZ", -1.0,
             "中期+1.00% 短期+0.50% 流动性分位50% 20日均额5.00亿 "
             "跨境溢价+6.10% >5%，不可选"),
        ]

    monkeypatch.setattr(prefilter, "rank_candidates", fake_rank)

    assert daily_predict._candidates_from_env() == ["510300.SS", "159792.SZ"]
    selected = daily_predict.build_daily_tickers(
        None, daily_predict._candidates_from_env()
    )

    assert seen == ["510300.SS", "159792.SZ"]
    assert {"510050.SS", "515220.SS", "GLD"} <= set(selected)
    # Only the rankable winner is added; the premium-vetoed candidate is not.
    assert set(selected) == {"510050.SS", "515220.SS", "GLD", "510300.SS"}
    assert len(selected) == 1 + 3

    out = capsys.readouterr().out
    assert "[prefilter] 510300.SS score=0.82 中期+4.10%" in out
    assert "[prefilter] 159792.SZ score=-1.00" in out
    assert "不可选" in out
    assert "[prefilter] 已选:" in out


def test_candidate_env_unset_keeps_old_behavior(monkeypatch, capsys):
    monkeypatch.delenv("TRADINGAGENTS_CANDIDATES", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_SCREEN_TOP_N", raising=False)

    assert daily_predict._candidates_from_env() is None
    assert daily_predict.build_daily_tickers(None, None) == [
        "510050.SS", "515220.SS", "GLD"
    ]
    assert daily_predict.build_daily_tickers(None) == [
        "510050.SS", "515220.SS", "GLD"
    ]
    assert capsys.readouterr().out == ""


def test_top_n_unset_keeps_old_build_daily_tickers_behavior(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_SCREEN_TOP_N", raising=False)

    old = daily_predict.build_daily_tickers("510050.SS,515220.SS")
    new = daily_predict.build_daily_tickers(
        "510050.SS,515220.SS", candidates=["AAA.SZ", "BBB.SZ"]
    )

    assert new == old == ["510050.SS", "515220.SS", "GLD"]


def test_data_failure_conservatively_keeps_every_candidate(monkeypatch):
    def boom(ticker, config=None):
        raise OSError("network down")

    monkeypatch.setattr(prefilter, "_load_history", boom)

    selected = prefilter.select_tickers(
        ["HOLD.SZ"], ["MAND.SZ"], ["AAA.SZ", "BBB.SZ"], top_n=1, config={}
    )

    assert selected == ["HOLD.SZ", "MAND.SZ", "AAA.SZ", "BBB.SZ"]


def test_top_n_never_increases_the_result_set(monkeypatch):
    mapping = {
        "AAA.SZ": _series(10, 0.010),
        "BBB.SZ": _series(10, 0.001),
        "CCC.SZ": _series(10, -0.001),
    }
    _install_history(monkeypatch, mapping)
    monkeypatch.setenv("TRADINGAGENTS_SCREEN_TOP_N", "2")

    base = daily_predict.build_daily_tickers("510050.SS,515220.SS")
    universe_size = len(base) + len(mapping)
    selected = daily_predict.build_daily_tickers(
        "510050.SS,515220.SS", candidates=list(mapping)
    )

    assert len(selected) <= universe_size
    assert set(selected) <= set(base) | set(mapping)
    assert set(base) <= set(selected)
    assert len(selected) == len(base) + 2


def test_cross_border_premium_above_five_percent_is_unselectable(monkeypatch):
    _install_history(monkeypatch, {
        "159999.SZ": _series(1.0, 0.02),
        "AAA.SZ": _series(10, 0.001),
    })
    monkeypatch.setattr(
        prefilter, "_cross_premium",
        lambda ticker, close: 0.062 if ticker == "159999.SZ" else None,
    )

    ranked = prefilter.rank_candidates(["159999.SZ", "AAA.SZ"], {})
    scores = {ticker: (score, reason) for ticker, score, reason in ranked}
    assert scores["159999.SZ"][0] == prefilter.UNSELECTABLE_SCORE
    assert "不可选" in scores["159999.SZ"][1]

    selected = prefilter.select_tickers(
        [], [], ["159999.SZ", "AAA.SZ"], top_n=2, config={}
    )
    assert "159999.SZ" not in selected
    assert "AAA.SZ" in selected
