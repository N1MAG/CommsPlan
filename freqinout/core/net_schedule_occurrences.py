"""Bounded, I/O-free occurrence lookup for NCS preparation."""
from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Optional

from freqinout.core.schedule_projection import DAY_NAMES, _parse_hhmm, _row_applies_on_day, _target_days


def next_net_occurrence(row: Mapping[str, Any], now: dt.datetime) -> Optional[dict]:
    """Return the current or next schedule occurrence using projection recurrence rules."""
    if now.tzinfo is not None:
        now = now.astimezone(dt.timezone.utc)
    start = _parse_hhmm(row.get("start_utc"))
    end = _parse_hhmm(row.get("end_utc"))
    recurrence = str(row.get("recurrence") or "Weekly").strip().upper()
    day_value = str(row.get("day_utc") or "").strip()
    if start is None or end is None or recurrence not in {"WEEKLY", "DAILY", "PERIODIC", "MONTHLY", "BI-WEEKLY"}:
        return None
    if recurrence != "DAILY" and day_value.upper() not in {"ALL", "DAILY", "EVERYDAY"} and not any(day_value.upper().startswith(day[:3].upper()) for day in DAY_NAMES):
        return None
    try:
        early = int(row.get("early_checkin") or 0)
        if not 0 <= early <= 24 * 60:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    targets = _target_days(day_value, recurrence)
    # One prior day covers overnight sessions; one year covers sparse monthly rows.
    for offset in range(-1, 367):
        day = now.date() + dt.timedelta(days=offset)
        day_name = DAY_NAMES[(day.weekday() + 1) % 7]
        week_start = day - dt.timedelta(days=(day.weekday() + 1) % 7)
        if day_name not in targets or not _row_applies_on_day(row, day_name, week_start):
            continue
        start_dt = dt.datetime.combine(day, dt.time(), tzinfo=now.tzinfo) + dt.timedelta(minutes=start)
        end_dt = dt.datetime.combine(day, dt.time(), tzinfo=now.tzinfo) + dt.timedelta(minutes=end)
        if end_dt <= start_dt:
            end_dt += dt.timedelta(days=1)
        if end_dt <= now:
            continue
        window_start = start_dt - dt.timedelta(minutes=early)
        return {"start_dt": start_dt, "end_dt": end_dt, "window_start": window_start,
                "active": window_start <= now < end_dt, "row": row}
    return None
