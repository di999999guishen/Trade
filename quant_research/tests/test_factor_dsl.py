import sqlite3

import numpy as np
import pandas as pd
import pytest

from quant_research.factor_dsl import TrialLedger, evaluate


def features():
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2020-01-01", periods=50)
    return {name: pd.DataFrame(rng.normal(size=(50, 3)), index=dates, columns=list("ABC"))
            for name in ["mom12_1", "vol63", "reversal5"]}


@pytest.mark.parametrize("expression", ["__import__('os').system('echo bad')", "mom12_1.shift(-1)",
    "lag(mom12_1,-1)", "rolling_mean(mom12_1,7)", "rank(target)", "mom12_1 ** 1000",
    "zscore(zscore(zscore(zscore(zscore(mom12_1)))))", "rank(mom12_1,axis=0)"])
def test_unsafe_or_unregistered_expressions_rejected(expression):
    with pytest.raises((ValueError, SyntaxError)):
        evaluate(expression, features())


def test_temporal_prefix_and_cross_sectional_normalization():
    data = features()
    expression = "rank(rolling_mean(mom12_1,5)) - rank(vol63)"
    result = evaluate(expression, data)
    prefix = evaluate(expression, {k: v.iloc[:30] for k, v in data.items()})
    pd.testing.assert_frame_equal(prefix, result.iloc[:30])
    assert evaluate("safe_div(mom12_1,0)", data).isna().all().all()
    assert np.allclose(evaluate("zscore(mom12_1)", data).mean(axis=1), 0, atol=1e-14)


def test_persistent_budget_counts_invalid_and_duplicate_attempts(tmp_path):
    path = tmp_path / "trials.sqlite"
    contract = {"snapshot": "fixed", "holdout": "unavailable"}
    ledger = TrialLedger(path, contract)
    assert ledger.attempt("rank(target)", features())[0]["status"] == "rejected"
    assert ledger.attempt("rank(mom12_1)", features())[0]["status"] == "evaluated"
    assert ledger.attempt("rank( mom12_1 )", features())[0]["status"] == "duplicate"
    reopened = TrialLedger(path, contract)
    for _ in range(27):
        reopened.attempt("rank(mom12_1)", features())
    with pytest.raises(ValueError, match="budget exhausted"):
        reopened.attempt("rank(vol63)", features())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 30
    with pytest.raises(ValueError, match="cannot be reset"):
        TrialLedger(path, {"snapshot": "changed"})
