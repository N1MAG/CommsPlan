"""Pure, bounded operator information for saved schedule assignment previews."""
from __future__ import annotations

import json
import math
from typing import Any, Mapping

DAYS = ('SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY')


def _rows(plan: Mapping[str, Any]):
    rows=plan.get('schedule_refs',plan.get('schedule_refs_json',[]))
    if isinstance(rows,str):
        try: rows=json.loads(rows)
        except (TypeError,ValueError): return []
    return [row for row in rows if isinstance(row,dict) and str(row.get('enabled',1)).lower() not in {'0','false','none',''}] if isinstance(rows,list) else []


def _minute(value):
    try:
        hour,minute=map(int,str(value).split(':'))
        return hour*60+minute if 0<=hour<24 and 0<=minute<60 else None
    except (TypeError,ValueError): return None


def frequency_plan_schedule_issues(plan: Mapping[str, Any]) -> list[str]:
    """Obvious entry problems; RF Guard remains the assignment safety owner."""
    issues=[]
    for index,row in enumerate(_rows(plan),1):
        if row.get('implicit_continuation') or row.get('_resolution_implicit_continuation'):
            continue
        if not str(row.get('mode') or '').strip():
            issues.append(f'Entry {index} needs an operating mode.')
        if _minute(row.get('start_utc')) is None or _minute(row.get('end_utc')) is None:
            issues.append(f'Entry {index} needs valid start and end UTC times.')
        try:
            frequency=float(row.get('frequency'))
            if not math.isfinite(frequency) or not .001<=frequency<=300000: raise ValueError
        except (TypeError,ValueError):
            issues.append(f'Entry {index} needs a valid frequency in MHz.')
    return issues


def frequency_plan_schedule_information(plan: Mapping[str, Any]) -> list[str]:
    """Identify uncovered explicit weekly time without treating gaps as errors.

    Implied continuations in compiled Daily plans are not explicit scheduled
    changes. Net/SOP layers may be absent; that alone is never an issue.
    """
    rows=_rows(plan)
    if not rows:
        return ['No timed entries are saved. Current tuning is kept until a scheduled change is available.']
    intervals=[]
    for row in rows:
        if row.get('implicit_continuation') or row.get('_resolution_implicit_continuation'):
            continue
        start,end=_minute(row.get('start_utc')),_minute(row.get('end_utc'))
        if start is None or end is None: continue
        day=str(row.get('day_utc') or 'ALL').upper()
        daily=str(row.get('recurrence') or '').lower()=='daily'
        indices=range(7) if day=='ALL' or daily else [DAYS.index(day)] if day in DAYS else []
        duration=end-start if end>start else end-start+1440
        for index in indices:
            left=index*1440+start;right=left+duration
            intervals.append((left,min(right,10080)))
            if right>10080: intervals.append((0,right-10080))
    cursor=0
    for left,right in sorted(intervals):
        if left>cursor: break
        cursor=max(cursor,right)
    if cursor<10080:
        return ['Schedule gaps keep the current frequency and mode until the next scheduled change.']
    return []
