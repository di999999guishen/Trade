import numpy as np
import pandas as pd

from quant_research.config import load_config
from quant_research.portfolio import desired_weights


def test_inverse_volatility_ignores_prediction_and_trend_inputs():
    dates = pd.bdate_range("2020-01-01", periods=260)
    symbols = ["SPY", "QQQ", "GLD", "IEF", "TLT", "IWM"]
    rng = np.random.default_rng(5)
    frames = {s: pd.DataFrame({"close": 100*np.cumprod(1+rng.normal(0, .01, len(dates))),
                               "eligible": True}, index=dates) for s in symbols}
    factors = {s: pd.DataFrame({"vol63": .10+i*.03, "trend200": -1., "mom6_1": -1.,
                                "mom126": -1., "mom252": -1., "mom12_1": -1., "down63": .1}, index=dates)
               for i, s in enumerate(symbols)}
    cfg = load_config()
    weights, _, _, _, _ = desired_weights("s2_inverse_volatility", dates[-1], frames, factors, {}, cfg)
    assert all(w > 0 for w in weights.values())
    assert max(weights.values()) <= .25
    assert sum(weights.values()) <= 1
    changed = {s: f.assign(trend200=99., mom6_1=99., mom126=99., mom252=99., mom12_1=99., down63=99.)
               for s, f in factors.items()}
    again = desired_weights("s2_inverse_volatility", dates[-1], frames, changed, {}, cfg)[0]
    assert weights == again
    gated = desired_weights("s2_multi_factor", dates[-1], frames, factors, {}, cfg)[0]
    assert sum(gated.values()) == 0
