"""CP-owned reusable messages and per-net explicit submission service.

No selection method transmits. Call storage/dispatch methods on a worker. Saved
forms are references to static CP Expect entries and use shared Compose codecs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
import threading
import uuid
from typing import Callable, Mapping

from freqinout.core.ncs_workbench_store import NcsWorkbenchStore, StaleWorkbenchWrite, _json, _required
from freqinout.core.js8_send_service import normalize_js8_target
from freqinout.core.sqlite_utils import connect_sqlite_readonly
from freqinout.core.logger import log

MESSAGE_TYPES = frozenset({'custom', 'query', 'saved_expect'})


def ensure_net_messages_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ncs_net_messages (
        entry_id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
        definition_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        deleted INTEGER NOT NULL DEFAULT 0, updated_ts REAL NOT NULL)''')


class NetMessagesStore:
    """Reusable definitions belong to freqinout.db; sent history belongs to nets."""
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    @contextmanager
    def _write(self):
        conn = sqlite3.connect(f'{self.db_path.expanduser().resolve().as_uri()}?mode=rw', uri=True, timeout=0.25)
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save(self, *, name: str, kind: str, definition: Mapping,
             entry_id: str | None = None, expected_version: int = 0) -> str:
        name = _required(name, 'Message name')
        if kind not in MESSAGE_TYPES:
            raise ValueError('Unknown Net Message type')
        value = dict(definition)
        if kind == 'custom' and not str(value.get('text') or '').strip():
            raise ValueError('Enter message text')
        if kind == 'query':
            from freqinout.core.js8_spotter_forms import normalize_form_code
            if not normalize_form_code(value.get('form_code')):
                raise ValueError('Select a form')
            value['form_code'] = normalize_form_code(value['form_code'])
        if kind == 'saved_expect' and int(value.get('expect_entry_id') or 0) <= 0:
            raise ValueError('Select a locally saved Expect response')
        encoded = _json(value)
        ident = _required(entry_id or uuid.uuid4().hex, 'Message identity')
        with self._write() as conn:
            if expected_version == 0:
                conn.execute('INSERT INTO ncs_net_messages VALUES(?,?,?,?,1,0,?)',
                             (ident, name, kind, encoded, time.time()))
            else:
                changed = conn.execute('''UPDATE ncs_net_messages SET name=?,kind=?,definition_json=?,
                    version=version+1,updated_ts=? WHERE entry_id=? AND version=? AND deleted=0''',
                    (name, kind, encoded, time.time(), ident, expected_version))
                if changed.rowcount != 1:
                    raise StaleWorkbenchWrite('Message changed; draft retained')
        return ident

    def delete(self, entry_id: str, *, expected_version: int):
        with self._write() as conn:
            changed = conn.execute('UPDATE ncs_net_messages SET deleted=1,version=version+1,updated_ts=? WHERE entry_id=? AND version=? AND deleted=0',
                                   (time.time(), entry_id, expected_version))
            if changed.rowcount != 1:
                raise StaleWorkbenchWrite('Message changed; reload before deleting')

    def list(self, *, after: str = '', limit: int = 200) -> list[dict]:
        conn = connect_sqlite_readonly(self.db_path, row_factory=sqlite3.Row)
        try:
            rows = conn.execute('SELECT * FROM ncs_net_messages WHERE deleted=0 AND entry_id>? ORDER BY entry_id LIMIT ?',
                                (after, min(200, max(1, int(limit)))))
            result = []
            for row in rows:
                item = dict(row)
                item['definition'] = json.loads(item.pop('definition_json'))
                result.append(item)
            return result
        finally:
            conn.close()


    def search(self, text: str = '', *, limit: int = 200) -> list[dict]:
        """Search the entire active library independently of admin pagination."""
        needle = str(text).strip()[:512].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        conn = connect_sqlite_readonly(self.db_path, row_factory=sqlite3.Row)
        try:
            rows = conn.execute("""SELECT * FROM ncs_net_messages WHERE deleted=0
                AND (name LIKE ? ESCAPE '\\' OR json_extract(definition_json,'$.text') LIKE ? ESCAPE '\\' OR json_extract(definition_json,'$.form_code') LIKE ? ESCAPE '\\')
                ORDER BY name COLLATE NOCASE,entry_id LIMIT ?""",
                ('%' + needle + '%', '%' + needle + '%', '%' + needle + '%', min(201, max(1, int(limit)))))
            result = []
            for row in rows:
                item = dict(row)
                item['definition'] = json.loads(item.pop('definition_json'))
                result.append(item)
            return result
        finally:
            conn.close()


    def get(self, entry_id: str) -> dict | None:
        conn = connect_sqlite_readonly(self.db_path, row_factory=sqlite3.Row)
        try:
            row = conn.execute('SELECT * FROM ncs_net_messages WHERE entry_id=? AND deleted=0', (entry_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['definition'] = json.loads(result.pop('definition_json'))
            return result
        finally:
            conn.close()


def form_query_command(operator_call: str, destination: str, form_code: str) -> str:
    from freqinout.core.js8_spotter_forms import normalize_form_code
    code = normalize_form_code(form_code)
    target = normalize_js8_target(destination)
    call = str(operator_call or '').strip().upper()
    if not call or not target or not code or any(c.isspace() for c in call + target):
        raise ValueError('Select an operator, destination and specific form')
    return f'{call}: {target} E? {code}'


def saved_expect_compose_intent(entry: Mapping, *, destination: str, radio_id: int,
                                context: Mapping) -> dict:
    """Reference stored values; never replace them with a blank catalog form."""
    code = str(entry.get('expect_key') or '').strip().upper()
    if not code.startswith('F!') or not str(entry.get('response_text') or '').strip() or int(entry.get('id') or 0) <= 0:
        raise ValueError('Select a static, locally saved Spotter response')
    target = normalize_js8_target(destination)
    if not target or radio_id <= 0:
        raise ValueError('Select a destination and pinned radio')
    return {'source': 'js8_ncs', 'source_label': 'JS8 NCS', 'mode': 'spotter', 'transport': 'spotter',
            'expect_entry_id': int(entry['id']), 'expect_key': code, 'spotter_form_code': code,
            'expect_view': False, 'recipient_callsign': target, 'radio_id': radio_id,
            'ncs_context': dict(context), 'ncs_expect_entry': dict(entry), 'ncs_target': target}


@dataclass(frozen=True)
class NetSubmissionOutcome:
    outcome: str
    detail: str
    result: object = None


def _submit_net_message_once(store: NcsWorkbenchStore, *, run_id: str, intent_id: str,
                       action: Mapping, preflight: Callable[[], tuple[bool, str]],
                       submit: Callable[[str], object]) -> NetSubmissionOutcome:
    """One durable explicit intent, one guarded submit, never a retry.

    ``preflight`` verifies pinned source/session and RF Guard on the worker;
    ``submit`` is the shared endpoint-serialized JS8 send service. Both are
    mandatory. A lost/ambiguous send reply is recorded uncertain, never replayed.
    """
    snapshot = json.loads(_json(action))
    content = str(snapshot.get('content') or '').strip()
    if not content or not callable(preflight) or not callable(submit):
        raise ValueError('An explicit message and guarded source are required')
    snapshot['content'] = content
    store.begin_submission(run_id, intent_id=intent_id, action=snapshot)
    try:
        allowed, detail = preflight()
    except Exception as exc:
        allowed, detail = False, f'Source/guard unavailable: {exc}'
    if not allowed:
        store.finish_submission(run_id, intent_id, outcome='blocked', detail=detail)
        return NetSubmissionOutcome('blocked', detail)
    try:
        result = submit(content)
        issues = getattr(getattr(result, 'preflight', None), 'issues', ())
        codes = {str(getattr(issue, 'code', '')) for issue in issues}
        outcome = 'accepted' if bool(getattr(result, 'sent', False)) else 'uncertain' if result is None or 'send_failed' in codes else 'blocked'
        detail = str(getattr(result, 'detail', '') or 'Submission outcome not confirmed')
    except Exception as exc:
        outcome, detail, result = 'uncertain', f'Submission outcome unknown: {exc}', None
    # If this write fails, the durable intent remains attempting. Startup recovery
    # exposes it as uncertain; a storage exception cannot authorize another send.
    store.finish_submission(run_id, intent_id, outcome=outcome, detail=detail)
    return NetSubmissionOutcome(outcome, detail, result)


_ACTIVE_LOCK = threading.Lock()
_ACTIVE_SUBMISSIONS: set[tuple[str, str]] = set()
_ENDING_RUNS: set[tuple[str, str]] = set()


def net_submission_inflight(db_path, run_id):
    with _ACTIVE_LOCK:
        return (str(db_path), run_id) in _ACTIVE_SUBMISSIONS


def claim_net_end(db_path, run_id):
    key = (str(db_path), run_id)
    with _ACTIVE_LOCK:
        if key in _ACTIVE_SUBMISSIONS or key in _ENDING_RUNS:
            return False
        _ENDING_RUNS.add(key)
        return True


def release_net_end(db_path, run_id):
    with _ACTIVE_LOCK:
        _ENDING_RUNS.discard((str(db_path), run_id))


def submit_net_message(store: NcsWorkbenchStore, *, run_id: str, intent_id: str,
                       action: Mapping, preflight: Callable[[], tuple[bool, str]],
                       submit: Callable[[str], object]) -> NetSubmissionOutcome:
    key = (str(store.db_path), run_id)
    with _ACTIVE_LOCK:
        if key in _ACTIVE_SUBMISSIONS or key in _ENDING_RUNS:
            raise StaleWorkbenchWrite('Another send or End is pending; draft retained')
        _ACTIVE_SUBMISSIONS.add(key)
    try:
        return _submit_net_message_once(store, run_id=run_id, intent_id=intent_id,
                                        action=action, preflight=preflight, submit=submit)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_SUBMISSIONS.discard(key)



def resolve_ncs_js8_source(config_path: str | Path, radio_id: int) -> dict:
    """Pin a new regular NCS run to its selected canonical radio, worker only."""
    from freqinout.core.multi_radio_store import _resolve_device_profile_links_conn
    from freqinout.core.js8_send_service import js8_endpoint_from_radio_profile, js8_profile_allows_transmit
    conn = connect_sqlite_readonly(config_path, row_factory=sqlite3.Row)
    try:
        row = conn.execute('SELECT * FROM device_profiles WHERE id=?', (radio_id,)).fetchone()
        if row is None:
            raise ValueError('Selected NCS radio is unavailable; review net setup')
        if row['js8_instance_id'] is not None:
            linked = conn.execute('SELECT * FROM js8_instances WHERE id=?', (row['js8_instance_id'],)).fetchone()
            if linked is None or not dict(linked).get('enabled', 1):
                raise ValueError('Selected JS8 instance is unavailable; review net setup')
        profile = _resolve_device_profile_links_conn(conn, dict(row))
        if (not profile.get('enabled', 1) or not profile.get('runtime_active', 0)
                or not profile.get('use_js8call', False) or not js8_profile_allows_transmit(profile)
                or profile.get('receive_only', False)):
            raise ValueError('Selected radio is not available for JS8 transmission')
        endpoint = js8_endpoint_from_radio_profile(profile).normalized()
        return {'instance_id': str(profile.get('js8_instance_id') or profile['id']),
                'host': endpoint.host, 'port': endpoint.port,
                'radio_name': str(profile.get('name') or f'Radio {radio_id}')}
    finally:
        conn.close()


def ncs_source_preflight(store: NcsWorkbenchStore, run_id: str, runtime_manager):
    """Revalidate current configured ownership and existing station RF Guard.

    Worker only. The immutable run source, not a mutable Compose selector or
    runtime-primary default, supplies the endpoint. No schema initialization.
    """
    from freqinout.core.multi_radio_store import _resolve_device_profile_links_conn
    from freqinout.core.js8_send_service import js8_endpoint_from_radio_profile, js8_profile_allows_transmit
    from freqinout.core.js8_expect_runtime import build_expect_rf_guard_preflight
    from freqinout.core.net_preparation_inputs import PreparationReader
    from freqinout.radio_interface.js8_api_client import JS8ApiEndpoint
    run = store.get_run(run_id)
    if not run or run['state'] != 'active':
        return False, 'This net is no longer active'
    if json.loads(run['draft_json']).get('end_pending'):
        return False, 'End is pending; draft retained'
    source = json.loads(run['source_json'])
    endpoint = JS8ApiEndpoint(str(source.get('host') or ''), int(source.get('port') or 0)).normalized()
    if not source.get('host') or not source.get('port'):
        return False, 'Pinned JS8 source is unavailable; review net setup'
    if runtime_manager is None:
        return False, 'Station RF Guard is unavailable'
    conn = connect_sqlite_readonly(store.db_path.with_name('freqinout.db'), row_factory=sqlite3.Row)
    try:
        row = conn.execute('SELECT * FROM device_profiles WHERE id=?', (run['radio_id'],)).fetchone()
        if row is None:
            return False, 'Pinned radio was removed'
        if row['js8_instance_id'] is not None:
            linked = conn.execute('SELECT * FROM js8_instances WHERE id=?', (row['js8_instance_id'],)).fetchone()
            if linked is None or not dict(linked).get('enabled', 1):
                return False, 'Pinned JS8 instance is unavailable'
        profile = _resolve_device_profile_links_conn(conn, dict(row))
        if (not profile.get('enabled', 1) or not profile.get('runtime_active', 0)
                or not profile.get('use_js8call', False) or not js8_profile_allows_transmit(profile)
                or profile.get('receive_only', False)):
            return False, 'Pinned radio is not available for JS8 transmission'
        instance = str(profile.get('js8_instance_id') or profile['id'])
        if instance != run['instance_id'] or js8_endpoint_from_radio_profile(profile).normalized() != endpoint:
            return False, 'JS8 source ownership changed; end and review net setup'
        policy = conn.execute('SELECT * FROM runtime_policies WHERE radio_profile_id=?', (run['radio_id'],)).fetchone()
        if policy and (not policy['net_control_enabled'] or policy['operator_suppressed']):
            return False, 'Net control is disabled for this radio'
        for peer in conn.execute('SELECT * FROM device_profiles WHERE enabled=1 AND runtime_active=1 AND id<>?', (run['radio_id'],)):
            resolved = _resolve_device_profile_links_conn(conn, dict(peer))
            if resolved.get('use_js8call'):
                other = js8_endpoint_from_radio_profile(resolved).normalized()
                if PreparationReader._same_endpoint((endpoint.host, endpoint.port), (other.host, other.port)):
                    return False, 'JS8 endpoint ownership is ambiguous'
    finally:
        conn.close()
    snapshots = list(runtime_manager.get_runtime_snapshots(force=True))
    source_runtime = next((s for s in snapshots if int(getattr(s, 'device_profile_id', 0)) == run['radio_id']), None)
    if source_runtime is None or not source_runtime.runtime_active:
        return False, 'Pinned radio runtime is unavailable'
    if source_runtime.ptt_active or source_runtime.shared_ptt_blocked:
        return False, 'Radio or shared PTT is busy; draft retained'
    # Reuse the existing policy evaluator and the just-observed detached snapshot.
    manager_view = type('NcsGuardSnapshot', (), {'get_runtime_snapshots': lambda self, **kw: snapshots})()
    result = build_expect_rf_guard_preflight(manager_view)(str(run['radio_id']), run['instance_id'], profile)
    return bool(result.ok), str(result.reason or '').replace('Expect auto-reply', 'NCS send').replace('Expect source', 'NCS source')


def send_ncs_command(store: NcsWorkbenchStore, *, run_id: str, intent_id: str,
                     action: Mapping, runtime_manager) -> NetSubmissionOutcome:
    """Production adapter for the same guarded service used by NCS and Compose."""
    from freqinout.core.js8_send_service import send_js8_addressed_message_guarded
    from freqinout.radio_interface.js8_api_client import JS8ApiClientRegistry, JS8ApiEndpoint
    run = store.get_run(run_id)
    if not run:
        raise StaleWorkbenchWrite('Net record is unavailable')
    source = json.loads(run['source_json'])
    def submit(content):
        endpoint = JS8ApiEndpoint(source['host'], int(source['port']))
        client = JS8ApiClientRegistry.get(endpoint, timeout_s=1.0, auto_reconnect=True)
        return send_js8_addressed_message_guarded(client, content, timeout_s=0.6,
            source_preflight=lambda: ncs_source_preflight(store, run_id, runtime_manager))
    log.info('NCS_SEND|attempt|run=%s|intent=%s|radio=%s|instance=%s|endpoint=%s:%s',
             run_id, intent_id, run['radio_id'], run['instance_id'], source.get('host'), source.get('port'))
    outcome = submit_net_message(store, run_id=run_id, intent_id=intent_id, action=action,
                                 preflight=lambda: ncs_source_preflight(store, run_id, runtime_manager), submit=submit)
    log.info('NCS_SEND|outcome=%s|run=%s|intent=%s|detail=%s',
             outcome.outcome, run_id, intent_id, outcome.detail)
    return outcome


def expand_net_message(text: str, *, target_group: str) -> str:
    """Expand explicit CP tokens on a detached outgoing copy, not the library."""
    body = str(text or '').strip()
    if '{TARGET GROUP}' in body:
        group = str(target_group or '').strip().lstrip('@')
        if not group:
            raise ValueError('Choose a group for {TARGET GROUP}')
        body = body.replace('{TARGET GROUP}', group)
    return body
