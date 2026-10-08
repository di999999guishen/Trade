"""XNYS labels and date-grouped, purged rolling split manifests."""
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd

from .calendar import calendar, decision_at


def labels(frames, horizon=5):
    if horizon not in (5, 20):
        raise ValueError("only registered 5/20-session labels are supported")
    cal = calendar()
    records = []
    for symbol, frame in frames.items():
        sessions = cal.sessions_in_range(frame.index.min(), frame.index.max())
        frame = frame.reindex(sessions)
        for i in range(len(sessions) - horizon):
            signal, entry, exit_day = sessions[i], sessions[i + 1], sessions[i + horizon]
            op, close = frame.at[entry, "open"], frame.at[exit_day, "close"]
            if not np.isfinite(op) or not np.isfinite(close) or op <= 0 or close <= 0:
                continue
            # A gap anywhere in the holding interval remains an unresolved label.
            if frame.loc[entry:exit_day, "close"].isna().any():
                continue
            records.append({"sample_id": f"{symbol}:{signal.date()}:h{horizon}", "symbol": symbol,
                            "session": signal, "decision_at": decision_at(signal).tz_convert("UTC"),
                            "entry_at": cal.session_open(entry), "exit_at": cal.session_close(exit_day),
                            "label_available_at": max(pd.Timestamp(frame.at[exit_day, "available_at"]), cal.session_close(exit_day)),
                            "return_value": float(close / op - 1), "horizon": horizon})
    return pd.DataFrame(records)


@dataclass(frozen=True)
class TimeSplit:
    train_start: str
    validation_start: str
    test_start: str
    test_end: str
    embargo_sessions: int = 20

    def select(self, samples):
        cal = calendar()
        starts = [pd.Timestamp(v) for v in (self.train_start, self.validation_start, self.test_start, self.test_end)]
        if not all(a < b for a, b in pairwise(starts)) or self.embargo_sessions < 0:
            raise ValueError("invalid temporal split")
        if samples.empty:
            return {"status": "insufficient_data", "train": [], "validation": [], "test": [], "purged": []}
        if (samples.session.min() > cal.date_to_session(starts[0], direction="next")
                or samples.session.max() < cal.date_to_session(starts[3] - pd.Timedelta(days=1), direction="previous")):
            return {"status": "insufficient_data", "train": [], "validation": [], "test": [], "purged": []}
        ids, purged = {}, []
        for name, lower, upper in zip(("train", "validation", "test"), starts, starts[1:]):
            group = samples[(samples.session >= lower) & (samples.session < upper)]
            if name != "test":
                boundary_day = cal.date_to_session(upper, direction="next")
                cutoff = decision_at(boundary_day).tz_convert("UTC")
                embargo_start = cal.session_offset(boundary_day, -self.embargo_sessions)
                keep = ((group.label_available_at < cutoff) & (group.exit_at < cal.session_open(boundary_day))
                        & (group.session < embargo_start))
                purged += group.loc[~keep, "sample_id"].tolist()
                group = group[keep]
            ids[name] = group.sample_id.tolist()
        return {"status": "ready" if all(ids.values()) else "insufficient_data", **ids, "purged": purged,
                "grouping": "all_assets_by_signal_session", "embargo_sessions": self.embargo_sessions,
                "boundaries": [str(s.date()) for s in starts]}


def annual_splits(samples, horizon=20):
    if samples.empty:
        return {"status": "insufficient_data", "folds": []}
    start_year = samples.session.min().year + 1
    end_year = samples.session.max().year
    folds = []
    for year in range(start_year + 6, end_year):
        spec = TimeSplit(f"{year-6}-01-01", f"{year-1}-01-01", f"{year}-01-01", f"{year+1}-01-01", horizon)
        fold = spec.select(samples)
        if fold["status"] == "ready":
            folds.append(fold)
    return {"status": "ready" if folds else "insufficient_data", "folds": folds,
            "policy": "5_year_train_1_year_validation_1_year_test_purged_with_embargo"}
