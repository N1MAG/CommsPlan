"""Canonical scheduling permission and conservative legacy-pause adoption.

Connection helpers never create schema or commit. Their owner must hold the
configuration write transaction; read helpers are safe in a detached review.
Permission belongs to the model. Operator intent belongs to the existing durable
authorization pause, which is cleared only by a token-matched explicit Resume.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Mapping
from uuid import uuid4

PERMISSION_VERSION = 1


def audit_change(conn, *, source, property, old, new, radio_id=None, model_id=None, correlation=None):
    """A bounded durable log: rolls back with the change, not a pre-commit claim."""
    revision = conn.execute('SELECT revision FROM _fio_schedule_revision WHERE id=1').fetchone()[0]
    conn.execute('''INSERT INTO scheduler_permission_audit
        (at_utc,source,radio_profile_id,operating_profile_id,property,old_value,new_value,configuration_revision,correlation_id)
        VALUES(?,?,?,?,?,?,?,?,?)''',
        (datetime.now(timezone.utc).isoformat(), source, radio_id, model_id, property, str(old), str(new), revision, correlation or uuid4().hex))
    conn.execute('DELETE FROM scheduler_permission_audit WHERE id <= (SELECT id FROM scheduler_permission_audit ORDER BY id DESC LIMIT 1 OFFSET 1000)')


def assigned_model(conn, radio_id: int) -> dict:
    from freqinout.core.multi_radio_store import _effective_assignment_for_device, _record_by_id
    assignment = _effective_assignment_for_device(conn, int(radio_id))
    if not assignment:
        return {}
    return _record_by_id(conn, 'operating_profiles', int(assignment['operating_profile_id'])) or {}


def needs_reconciliation(conn, radio_id: int, model: Mapping) -> bool:
    row = conn.execute('SELECT scheduler_enabled,scheduler_permission_version FROM runtime_policies WHERE radio_profile_id=?',
                       (int(radio_id),)).fetchone()
    return bool(row is not None and (int(row[1]) < PERMISSION_VERSION or bool_value(row[0]) != model_allows_scheduling(model)))


def legacy_scheduler_output(conn, submitted):
    """A single modeled radio publishes compatibility permission, never imports it."""
    radios = conn.execute('SELECT id FROM device_profiles WHERE runtime_active=1 LIMIT 2').fetchall()
    if len(radios) == 1:
        model = assigned_model(conn, int(radios[0][0]))
        if model:
            allowed = model_allows_scheduling(model)
            if bool_value(submitted) != allowed:
                audit_change(conn, source='legacy settings write rejected', property='compatibility permission',
                             old=submitted, new=allowed, radio_id=int(radios[0][0]), model_id=model.get('id'))
            return allowed
    return submitted


def bool_value(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def model_allows_scheduling(model: Mapping) -> bool:
    return bool(model and bool_value(model.get('enabled', 1), True)
                and str(model.get('status') or 'saved') == 'saved'
                and bool_value(model.get('scheduler_enabled', 0)))


def permission_blocker(model: Mapping) -> str:
    if not model:
        return 'No Operating Model is assigned. Choose an Operating Model that allows scheduling.'
    name = str(model.get('name') or 'Assigned Operating Model')
    if not bool_value(model.get('enabled', 1), True) or str(model.get('status') or 'saved') != 'saved':
        return f'Operating Model “{name}” is disabled or unfinished. Choose a saved, enabled model.'
    if not bool_value(model.get('scheduler_enabled', 0)):
        return f'Operating Model “{name}” does not allow scheduling. Change its scheduling permission or choose another model.'
    return ''


def pause_state(conn, radio_id: int) -> dict:
    from freqinout.core.multi_radio_store import _fetchone_dict
    row = _fetchone_dict(conn.execute('SELECT * FROM operator_schedule_authorizations WHERE radio_id=?', (int(radio_id),)))
    policy = _fetchone_dict(conn.execute('SELECT * FROM runtime_policies WHERE radio_profile_id=?', (int(radio_id),)))
    return pause_from_rows(row or {}, policy or {})


def pause_from_rows(row: Mapping, policy: Mapping) -> dict:
    """Shared interpretation for live transactions and detached catalogues."""
    pending = bool(policy and int(policy.get('scheduler_permission_version') or 0) < PERMISSION_VERSION
                   and not bool_value(policy.get('scheduler_enabled')))
    paused = bool(row and not row.get('enabled') and row.get('reason') == 'operator disabled')
    try:
        origin = json.loads(row.get('source_evidence_json') or '{}').get('_scheduler_pause_source', '') if paused else ''
    except (ValueError, TypeError, AttributeError):
        origin = ''
    return dict(operator_paused=paused or pending,
                operator_pause_token=str(row.get('approval_token') or '') if paused else '',
                legacy_pause_pending=pending,
                legacy_pause=pending or origin == 'legacy runtime policy adoption')


def record_pause(conn, radio_id: int, *, source: str) -> None:
    """Persist a new explicit pause, including before first automatic approval."""
    now, token = datetime.now(timezone.utc).isoformat(), uuid4().hex
    previous = pause_state(conn, radio_id)
    conn.execute('''INSERT INTO operator_schedule_authorizations
        (radio_id,fingerprint,approval_token,enabled,reviewed_utc,reason,source_evidence_json)
        VALUES(?,?,?,0,?,'operator disabled',?) ON CONFLICT(radio_id) DO UPDATE SET
        enabled=0,approval_token=excluded.approval_token,reviewed_utc=excluded.reviewed_utc,
        reason='operator disabled',source_evidence_json=excluded.source_evidence_json ''',
                 (int(radio_id), '', token, now, json.dumps({'_scheduler_pause_source': source})))
    audit_change(conn, source=source, property='operator pause', old=previous['operator_paused'], new=True,
                 radio_id=radio_id, model_id=assigned_model(conn, radio_id).get('id'), correlation=token)


def reconcile_permission(conn, radio_id: int, model: Mapping, *, source: str) -> bool:
    """Adopt an ambiguous legacy false as paused before deriving policy.

    Seeded v1 rows never manufacture operator intent when the model later changes.
    Returns whether tracked configuration changed; repeated cache hits write nothing.
    """
    row = conn.execute('SELECT scheduler_enabled,scheduler_permission_version FROM runtime_policies WHERE radio_profile_id=?',
                       (int(radio_id),)).fetchone()
    if row is None:
        return False
    allowed = int(model_allows_scheduling(model))
    if int(row[1]) < PERMISSION_VERSION and not bool_value(row[0]):
        current = pause_state(conn, radio_id)
        if not current['operator_pause_token']:
            record_pause(conn, radio_id, source='legacy runtime policy adoption')
    if int(row[0]) == allowed and int(row[1]) >= PERMISSION_VERSION:
        return False
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('UPDATE runtime_policies SET scheduler_enabled=?,scheduler_permission_version=?,updated_utc=? WHERE radio_profile_id=?',
                 (allowed, PERMISSION_VERSION, now, int(radio_id)))
    audit_change(conn, source=source, property='runtime permission projection', old=row[0], new=allowed,
                 radio_id=radio_id, model_id=model.get('id'))
    return True
