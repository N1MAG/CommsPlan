"""Shared recurrence predicates for schedule materialization and evaluation."""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping


def biweekly_week_is_eligible(row: Mapping[str, Any], date_value: dt.date) -> bool:
    """Return whether a Bi-Weekly row is eligible on an ISO calendar week.

    The offset and parity deliberately match the existing SOP recurrence
    behavior.  Invalid offsets retain the historical default of zero.
    """
    try:
        offset = int(row.get("biweekly_offset_weeks") or 0)
    except Exception:
        offset = 0
    return ((int(date_value.isocalendar()[1]) - offset) % 2) == 0
