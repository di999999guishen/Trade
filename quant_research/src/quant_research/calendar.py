"""Exchange sessions, rather than weekdays or the host's local date."""
from datetime import datetime
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd


def calendar(start="2000-01-01", end="2030-12-31"):
    return xcals.get_calendar("XNYS", start=start, end=end)


def decision_at(session):
    day = pd.Timestamp(session).date()
    return pd.Timestamp(datetime.combine(day, datetime.min.time().replace(hour=18), ZoneInfo("America/New_York")))


def next_open(session, cal=None):
    cal = cal or calendar()
    return cal.session_open(cal.next_session(pd.Timestamp(session)))


def latest_completed(now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    cal = calendar()
    day = cal.date_to_session(now.tz_convert("America/New_York").date(), direction="previous")
    # At 18:00 both completed bars and the daily decision cut are available.
    while decision_at(day) > now or cal.session_close(day) > now:
        day = cal.previous_session(day)
    return day
