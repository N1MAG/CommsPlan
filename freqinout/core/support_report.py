"""Bounded, allowlisted diagnostics from cached state; no I/O or raw secrets."""
from __future__ import annotations

import math
from collections import Counter
from itertools import islice
from typing import Mapping, Iterable

MAX_REPORT_ROWS = 100
_ALLOWED_STATES = frozenset({
    'ok', 'info', 'warning', 'danger', 'error', 'failed', 'stalled', 'connected',
    'disconnected', 'missing', 'unknown', 'unavailable', 'ready', 'idle', 'paused',
    'active', 'inactive', 'healthy', 'degraded', 'recovering', 'not connected',
    'needs attention', 'shared endpoint', 'blocked', 'stale', 'disabled',
})


def _state(value: object) -> str:
    if not isinstance(value, str):
        return 'unknown'
    token = value[:64].strip().lower()
    return token if token in _ALLOWED_STATES else 'unknown'


def _count(value: object) -> int:
    try:
        return max(0, min(1_000_000_000, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _category(value: object) -> str:
    # Raw error strings are inspected in a bounded buffer, never exported.
    text = value[:512].lower() if isinstance(value, str) else ''
    for category, tokens in (
        ('timeout', ('timeout', 'timed out')),
        ('permission', ('permission', 'denied')),
        ('connection', ('connect', 'socket', 'endpoint')),
        ('configuration', ('config', 'missing', 'invalid')),
    ):
        if any(token in text for token in tokens):
            return category
    return 'other' if text else 'none'


def _family(value: object) -> str:
    text = value[:256].lower() if isinstance(value, str) else ''
    for token, label in (
        ('js8', 'JS8Call'), ('meshcore', 'MeshCore'), ('meshtastic', 'Meshtastic'),
        ('varac', 'VarAC'), ('fldigi', 'FLDigi'), ('flrig', 'FLRig'),
        ('flmsg', 'FLMsg'), ('flamp', 'FLAmp'), ('scheduler', 'Scheduler'),
        ('map', 'Map'),
    ):
        if token in text:
            return label
    return 'Other'


def build_support_report(
    summary: Mapping[str, object],
    sources: Iterable[Mapping[str, object]] = (),
    *,
    version: str = '',
    snapshot_age_s: float | None = None,
) -> str:
    """Export only schema-defined tokens/counts; arbitrary labels stay private."""
    import re
    safe_version = version if re.fullmatch(r'\d+(?:\.\d+){1,3}(?:[a-z0-9.-]{0,24})?', version) else 'unknown'
    age = 'unknown'
    if isinstance(snapshot_age_s, (int, float)) and math.isfinite(snapshot_age_s):
        age = f'{max(0, int(snapshot_age_s))} seconds'
    lines = [
        'CommsPlan support report',
        f'Version: {safe_version}',
        'Profile: current station (identity withheld)',
        f'Cached snapshot age: {age}',
        f'Station state: {_state(summary.get("severity"))}',
        f'Reported issues: {_count(summary.get("issue_count"))}',
        'Only cached evidence is included; this report runs no probes.',
        '', 'Dependencies (anonymous aliases):',
    ]
    items = summary.get('items', ())
    if not isinstance(items, (list, tuple)):
        items = ()
    categories: Counter[str] = Counter()
    for index, item in enumerate(islice(items, MAX_REPORT_ROWS), 1):
        if not isinstance(item, Mapping):
            continue
        category = _category(item.get('last_issue'))
        categories[category] += 1
        lines.append(f'Dependency {index}: {_state(item.get("state"))}; family: {_family(item.get("dependency"))}; issue category: {category}')
    if not items:
        lines.append('No cached dependency rows.')
    if len(items) > MAX_REPORT_ROWS:
        lines.append('Dependency list truncated to 100 rows.')
    lines.extend(['', 'Runtime sources (anonymous aliases):'])
    source_rows = list(islice(sources, MAX_REPORT_ROWS + 1))
    for index, item in enumerate(source_rows[:MAX_REPORT_ROWS], 1):
        if not isinstance(item, Mapping):
            continue
        lines.append(f'Source {index}: {_state(item.get("state"))}; family: {_family(item.get("source_kind"))}; records: {_count(item.get("message_count"))}')
    if not source_rows:
        lines.append('Runtime source evidence unavailable.')
    if len(source_rows) > MAX_REPORT_ROWS:
        lines.append('Runtime source list truncated to 100 rows.')
    events = summary.get('recent_scheduler_events', ())
    if not isinstance(events, (list, tuple)):
        events = ()
    decisions = Counter()
    for event in islice(events, MAX_REPORT_ROWS):
        if isinstance(event, Mapping):
            decisions[_state(event.get('event_type'))] += 1
    lines.extend(['', 'Recent scheduler decisions (counts only):'])
    lines.extend(f'{key}: {value}' for key, value in sorted(decisions.items()))
    if not decisions:
        lines.append('No cached scheduler events.')
    lines.extend([
        '', 'Map and Mesh details are included only when represented by cached health sources.',
        'No callsigns, paths, endpoints, credentials, keys, payloads or raw errors are exported.',
    ])
    return '\n'.join(lines)
