"""Factual, cache-only wording for retained outgoing NCS attempts.

Stored outcomes remain untouched. A successful API send is not RF or delivery
confirmation; connection exceptions cannot establish a definite rejection.
"""
from collections.abc import Mapping


def activity_action(row):
    action = row.get('action') or {}
    return action if isinstance(action, Mapping) else {}


def submission_outcome_label(row):
    outcome = str(row.get('outcome') or '').lower()
    if outcome == 'accepted':
        return 'Submitted to JS8Call'
    if outcome == 'blocked':
        return 'Not submitted'
    if outcome == 'attempting':
        return 'Submitting…'
    if outcome == 'failed' and activity_action(row).get('api_rejection_confirmed') is True:
        return 'Submission failed'
    return 'Submission uncertain'


def activity_kind(row):
    action = activity_action(row)
    kind = str(action.get('kind') or row.get('kind') or '')
    if kind == 'ack':
        return 'ACK group' if str(action.get('destination') or '').startswith('@') else 'ACK station'
    if kind == 'utility':
        content = str(action.get('content') or '').strip().upper()
        return 'QUERY MSGS' if content.endswith('QUERY MSGS') else 'SNR?' if content.endswith('SNR?') else 'Utility query'
    return {'query':'Expect query', 'custom':'Text message', 'saved_expect':'Saved form'}.get(kind, kind or 'Message')


def activity_content(row):
    return str(activity_action(row).get('content') or row.get('content') or '')


def activity_destination(row):
    return str(activity_action(row).get('destination') or row.get('destination') or '')
