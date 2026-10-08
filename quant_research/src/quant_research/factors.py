"""Causal trailing factors. No fitting, backfill, or labels in this module."""
import numpy as np
import pandas as pd

from .artifacts import digest

SPEC = {
    "mom20": (21, "close/close.shift(20)-1", "trend persistence", 1),
    "mom60": (61, "close/close.shift(60)-1", "trend persistence", 1),
    "mom120": (121, "close/close.shift(120)-1", "trend persistence", 1),
    "mom126": (127, "close/close.shift(126)-1", "trend persistence", 1),
    "mom252": (253, "close/close.shift(252)-1", "trend persistence", 1),
    "mom12_1": (253, "close.shift(21)/close.shift(252)-1", "skip recent reversal", 1),
    "mom6_1": (127, "close.shift(21)/close.shift(126)-1", "skip recent reversal", 1),
    "trend200": (200, "close/close.rolling(200).mean()-1", "absolute trend", 1),
    "vol20": (21, "std(simple_return,20,ddof=1)*sqrt(252)", "risk", -1),
    "vol60": (61, "std(simple_return,60,ddof=1)*sqrt(252)", "risk", -1),
    "vol63": (64, "std(log_return,63,ddof=1)*sqrt(252)", "risk budget", -1),
    "down63": (64, "sqrt(mean(min(simple_return,0)^2,63))*sqrt(252)", "downside risk", -1),
    "drawdown60": (60, "close/close.rolling(60).max()-1", "trend damage", 1),
    "reversal5": (6, "-(close/close.shift(5)-1)", "short horizon reversal", 1),
    "range20": (20, "mean((high-low)/close,20)", "range risk", -1),
    "volume_ratio20": (20, "volume/volume.rolling(20).mean()", "activity", 0),
}


def registry():
    records = [{"id": name, "window": values[0], "expression": values[1],
                "hypothesis": values[2], "direction": values[3], "version": "1",
                "missing_rule": "no_imputation", "availability": "completed_session_at_18_ET"}
               for name, values in SPEC.items()]
    return {"factors": records, "version_hash": digest(records), "candidate_count": len(records)}


def compute(frame):
    c, v = frame.close, frame.volume
    r = c.pct_change(fill_method=None)
    f = pd.DataFrame(index=frame.index)
    for n in (20, 60, 120, 126, 252):
        f[f"mom{n}"] = c / c.shift(n) - 1
    f["mom12_1"] = c.shift(21) / c.shift(252) - 1
    f["mom6_1"] = c.shift(21) / c.shift(126) - 1
    f["trend200"] = c / c.rolling(200).mean() - 1
    for n in (20, 60):
        f[f"vol{n}"] = r.rolling(n).std(ddof=1) * np.sqrt(252)
    f["vol63"] = np.log(c / c.shift(1)).rolling(63).std(ddof=1) * np.sqrt(252)
    f["down63"] = np.sqrt(r.clip(upper=0).pow(2).rolling(63).mean() * 252)
    f["drawdown60"] = c / c.rolling(60).max() - 1
    f["reversal5"] = -(c / c.shift(5) - 1)
    f["range20"] = ((frame.high - frame.low) / c).rolling(20).mean()
    f["volume_ratio20"] = v / v.rolling(20).mean().replace(0, np.nan)
    return f.replace([np.inf, -np.inf], np.nan)
