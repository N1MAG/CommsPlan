"""Worker-owned, reviewed Plan Builder assignments and durable live-plan drafts.

No endpoint I/O. Review mutates only a private SQLite snapshot. Commit compares
the reviewed configuration and source evidence, then writes all selected radios
and any deliberate model changes in one configuration-store transaction.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from freqinout.core.multi_radio_store import (
    MultiRadioStore, EFFECTIVE_ASSIGNMENT_STATES, SOURCE_ONLY_FREQUENCY_PLAN_CATEGORIES,
    _GuidedTransactionConnection, _effective_assigned_plan_for_device,
    _set_assigned_plan_conn, _schedule_assignment_validation_status_conn,
)
from freqinout.core.operator_schedule_authorization import review_schedule
from freqinout.core.scheduler_permission import assigned_model, model_allows_scheduling, pause_state
from freqinout.core.schedule_approval_sources import source_tokens, tokens_current
from freqinout.core.schedule_approval_sources import TABLE as SOURCE_REVISION_TABLE
from freqinout.core.schedule_source_inputs import ScheduleSourceError
from freqinout.core.schedule_configuration_cache import ConfigurationChanged
from freqinout.core.sqlite_utils import connect_sqlite_readonly


MAX_RADIOS = 100
MAX_PLANS = 2000
MAX_CONFIG_BYTES = 128 * 1024 * 1024


class _SourceProblem(ScheduleSourceError):
    def __init__(self, message, *, plan_id=0, source_ref=''):
        super().__init__(message)
        self.plan_id, self.source_ref = plan_id, source_ref


class _PendingWindowMismatch(ScheduleSourceError):
    def __init__(self, finding, plan, projected):
        super().__init__(finding['message'])
        self.finding = finding
        self.plan, self.projected = deepcopy(plan), deepcopy(projected)


class _AssignmentBlocked(ValueError):
    def __init__(self, message, *, target='assignment_choices', radio_id=0):
        super().__init__(message)
        self.target, self.radio_id = target, radio_id


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _rows(conn, table):
    return [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]


def _clean(row):
    return {k: v for k, v in dict(row or {}).items()
            if not k.startswith('_catalog_') and k not in {'created_utc', 'updated_utc'}}


def _plan_ref(plan):
    return {'id': int(plan['id']), 'name': str(plan.get('name') or 'Plan')} if plan else None


def _pending_window_issue(settings, store, plan, projected, radio_ids):
    from freqinout.core.plan_conflict_issues import issue, repair, source_ref_for_window
    from freqinout.core.schedule_source_sets import (
        HF_DAILY_SOURCE_SETS_KEY, HF_NET_SOURCE_SETS_KEY, NO_NET_SOURCE_SET_ID,
        _refs_equivalent, source_set_row_by_id,
    )

    source_labels = {}
    kinds = {'hf_daily_schedule': ('HF Daily', HF_DAILY_SOURCE_SETS_KEY),
             'hf_net_schedule': ('HF Nets', HF_NET_SOURCE_SETS_KEY),
             'sop_schedule_plan': ('SOP plan', None), 'sop_schedule_layer': ('SOP', None)}
    old_rows = json.loads(plan['schedule_refs_json'] or '[]')
    new_rows = projected['schedule_refs']
    for ref in json.loads(plan['source_refs_json'] or '[]'):
        category, _, identity = ref.partition(':')
        if category not in kinds or not identity or identity == NO_NET_SOURCE_SET_ID:
            continue
        kind, key = kinds[category]
        source = None
        if identity.startswith('plan:') and identity[5:].isdigit():
            source = store.get_frequency_plan(int(identity[5:]))
        elif category == 'sop_schedule_plan' and identity.isdigit():
            source = store.get_frequency_plan(int(identity))
        elif key:
            source = source_set_row_by_id(settings, key, identity)
        name = (source or {}).get('name')
        if category == 'sop_schedule_layer':
            name = next((row['profile_name'] for row in new_rows + old_rows
                         if row.get('profile_name') and source_ref_for_window(plan, row) == ref), None)
        source_labels[ref] = f'{kind} “{name}”' if name else f'{kind} source'

    def describe(row):
        ref = source_ref_for_window(plan, row)
        if row.get('source') == 'SOP' and ref not in source_labels:
            sop_plans = [key for key in source_labels if key.startswith('sop_schedule_plan:')]
            if len(sop_plans) == 1:
                ref = sop_plans[0]
        source = source_labels.get(ref) or {'HF': 'Daily layer', 'NET': 'Net layer', 'SOP': 'SOP layer'}.get(row.get('source'), 'Plan window')
        day = row.get('day_utc') or 'ALL'
        day = 'Every day' if day == 'ALL' else day
        time = f"{day} {row.get('start_utc') or '?'}–{row.get('end_utc') or '?'} UTC"
        values = [source, time, row.get('recurrence'), row.get('net_name') or row.get('group_name'),
                  row.get('band'), f"{row['frequency']} MHz" if row.get('frequency') else '', row.get('mode'), row.get('vfo'),
                  row.get('occurrence_start_utc'), row.get('occurrence_end_utc')]
        return ref, ' · '.join(str(value) for value in values if value)

    pending = dict.fromkeys(map(describe, old_rows))
    current = dict.fromkeys(map(describe, new_rows))
    differences = []
    affected = set()
    for label, left, right in [('Only in pending changes', pending, current),
                               ('Only in current sources', current, pending)]:
        rows = [(ref, text) for ref, text in left if (ref, text) not in right]
        affected.update(ref for ref, _ in rows if ref in source_labels)
        differences.extend(f'{label} — {text}' for _, text in rows[:8])
        if len(rows) > 8:
            differences.append(f'{label} — {len(rows) - 8} more windows')
    if not differences and not _refs_equivalent(old_rows, new_rows):
        differences.append('The displayed windows match. Stored window metadata or ordering differs from the current projection.')
    for field, label in [('frequency_refs', 'Frequency references'), ('group_refs', 'Group references')]:
        old_values = json.loads(plan[f'{field}_json'] or '[]')
        new_values = projected[field]
        if not _refs_equivalent(old_values, new_values):
            pending_values = ', '.join(map(str, old_values[:8])) or 'None'
            current_values = ', '.join(map(str, new_values[:8])) or 'None'
            differences.append(f'{label} — pending: {pending_values} · current: {current_values}')
    sources = {ref: name for ref, name in source_labels.items() if not affected or ref in affected}
    instructions = ('Compare the pending windows with the current source windows below. Open a named source if its schedule needs correcting. '
                    'If the current sources are correct, return to Plan Builder and use Review changes to replace the pending version with your intended edits. '
                    'Use Discard pending changes if you no longer need those edits.')
    return issue('source.pending_windows_differ', 'source',
                 f'Pending changes for “{plan["name"]}” do not match the linked schedules.',
                 plan_id=plan['id'], scope_label=plan['name'], instructions=instructions,
                 related_radios=[_plan_ref(store.get_device_profile(radio_id)) for radio_id in radio_ids],
                 affected_sources=list(sources.values()), window_differences=differences,
                 remedies=[repair(f'Open {name}…', 'source', plan_id=plan['id'], source_ref=ref, source_label=name)
                           for ref, name in sources.items()])


class _ReviewStore(MultiRadioStore):
    def __init__(self, connection, db_path):
        self.connection = connection
        self.db_path = Path(db_path)
        self._transaction_local = threading.local()
        self._transaction_local.connection = connection

    def _connect(self):
        return _GuidedTransactionConnection(self.connection)

    _connect_readonly = _connect

    def stage_assignment(self, radio_id, plan_id, values):
        # Explanatory post-state only. This adapter cannot stage into a file DB;
        # real commit always calls the guarded canonical store writer instead.
        if any(row[2] for row in self.connection.execute('PRAGMA database_list')):
            raise RuntimeError('Assignment previews require an isolated memory database')
        self.connection.execute("UPDATE assigned_plans SET assignment_state='superseded' WHERE device_profile_id=? AND assignment_state IN ('active','temporary_override')", (radio_id,))
        fields = ('assignment_state','assignment_category','scheduler_mode','starts_utc','ends_utc','reason','created_by')
        self.connection.execute('INSERT INTO assigned_plans(device_profile_id,frequency_plan_id,' + ','.join(fields) +
            ',validation_status_json,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (radio_id,plan_id,*(values[field] for field in fields),'{}',datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat()))


class PlanAssignmentService:
    def __init__(self, config_dir, *, fixed_root=False):
        self.config_dir = Path(config_dir)
        self.db_path = self.config_dir / 'freqinout.db'
        self.fixed_root = bool(fixed_root)
        self._owner = threading.get_ident()
        self._reviews = OrderedDict()

    def _check(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError('Plan assignment service used outside its serial worker')
        if not self.fixed_root:
            from freqinout.core.config_paths import get_config_dir
            if get_config_dir() / 'config' != self.config_dir:
                raise ConfigurationChanged('The active profile changed. Reopen Plan Builder before assigning.')

    def _snapshot(self):
        self._check()
        if not self.db_path.is_file() or self.db_path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError('Configuration is unavailable or exceeds the supported review size.')
        copy = sqlite3.connect(':memory:')
        copy.row_factory = sqlite3.Row
        try:
            with connect_sqlite_readonly(self.db_path, row_factory=sqlite3.Row) as source:
                source.execute('BEGIN')
                source.backup(copy)
            if copy.execute('SELECT count(*) FROM device_profiles').fetchone()[0] > MAX_RADIOS:
                raise ValueError('Too many radios for one assignment review.')
            if copy.execute('SELECT count(*) FROM frequency_plans').fetchone()[0] > MAX_PLANS:
                raise ValueError('Too many plans for one assignment review.')
            return copy
        except Exception:
            copy.close()
            raise

    def _source_stamp(self, conn):
        def stamp(connection):
            rows = connection.execute(f'SELECT source_key,revision FROM {SOURCE_REVISION_TABLE} ORDER BY source_key').fetchmany(250001)
            if len(rows) > 250000:
                raise ValueError('Too many source revisions for one assignment review.')
            return _hash([tuple(row) for row in rows])
        identity = self.db_path.stat()
        result = {'settings': [identity.st_dev, identity.st_ino, stamp(conn)]}
        path = self.db_path.with_name('freqinout_nets.db')
        if not path.is_file():
            result['nets'] = None
        else:
            stat = path.stat()
            with connect_sqlite_readonly(path) as sources:
                sources.execute('BEGIN')
                result['nets'] = [stat.st_dev, stat.st_ino, stamp(sources)]
        return result

    def _live_source_stamp(self):
        with connect_sqlite_readonly(self.db_path) as conn:
            conn.execute('BEGIN')
            return self._source_stamp(conn)

    def _validate_sources(self, store, plan):
        from freqinout.core.schedule_source_sets import (
            _dependency_set_id, _rows_for_dependency, _sop_sources_for_plan,
            HF_DAILY_SOURCE_CATEGORY, HF_NET_SOURCE_CATEGORY,
        )
        from freqinout.core.planner_catalog_model import DetachedSettings
        with store.connect_readonly() as conn:
            settings = DetachedSettings({row['key']: json.loads(row['value']) for row in conn.execute('SELECT key,value FROM kv')}, self.config_dir)
        refs = json.loads(plan.get('source_refs_json') or '[]')
        for category in (HF_DAILY_SOURCE_CATEGORY, HF_NET_SOURCE_CATEGORY):
            ident = _dependency_set_id(refs, category)
            if ident:
                if ident.startswith('plan:'):
                    source = store.get_frequency_plan(int(ident.split(':', 1)[1]))
                    if not source or not source.get('enabled') or source.get('status') != 'saved':
                        raise _SourceProblem(f'Declared source is disabled, unfinished or unavailable: {category}:{ident}', plan_id=plan['id'], source_ref=f'{category}:{ident}')
                try:
                    _rows_for_dependency(settings, category, ident, store=store)
                except ScheduleSourceError as exc:
                    raise _SourceProblem(str(exc), plan_id=plan['id'], source_ref=f'{category}:{ident}') from exc
        for ref in refs:
            if isinstance(ref, str) and ref.startswith('sop_schedule_plan:'):
                source = store.get_frequency_plan(int(ref.split(':', 1)[1]))
                if not source or source.get('status') != 'saved' or not source.get('enabled'):
                    raise _SourceProblem(f'Declared SOP plan source is unavailable: {ref}', plan_id=plan['id'], source_ref=ref)
        try:
            _sop_sources_for_plan(settings, plan, refs)
        except ScheduleSourceError as exc:
            ref = next((ref for ref in refs if isinstance(ref, str) and ref.startswith('sop_')), '')
            raise _SourceProblem(str(exc), plan_id=plan['id'], source_ref=ref) from exc
        return settings

    def _refresh_review_sources(self, conn, *, skip_plan_id=0):
        """Materialize current source rows only in the private review copy."""
        from freqinout.core.schedule_source_sets import _source_backed_plan_payload
        store = _ReviewStore(conn, self.db_path)
        errors, refresh_required = {}, set()
        for plan in store.list_frequency_plans():
            if plan['id'] == skip_plan_id:
                continue
            try:
                settings = self._validate_sources(store, plan)
                updated = _source_backed_plan_payload(settings, plan, store=store)
                if updated:
                    store.save_frequency_plan(updated)
                    refresh_required.add(int(plan['id']))
            except (ScheduleSourceError, ValueError, sqlite3.Error) as exc:
                errors[int(plan['id'])] = exc
        return errors, refresh_required

    @staticmethod
    def _fingerprint(conn):
        # Internal proof refreshes, traffic and bookkeeping do not invalidate an
        # operator review. Explicit pause changes and all real config inputs do.
        tables = ('device_profiles', 'operating_profiles', 'operating_profile_assignments',
                  'frequency_plans', 'assigned_plans', 'frequency_plan_drafts',
                  'runtime_policies', 'scheduler_manual_control_states',
                  'js8_instances', 'fast_light_configs', 'radio_software_identity_records')
        data = {table: [_clean(row) for row in _rows(conn, table)] for table in tables}
        data['operator_pauses'] = [_clean(dict(row)) for row in conn.execute(
            "SELECT radio_id,reason,approval_token FROM operator_schedule_authorizations WHERE enabled=0 AND reason='operator disabled' ORDER BY radio_id")]
        data['settings'] = [dict(row) for row in conn.execute(
            "SELECT key,value FROM kv WHERE key IN ('operating_groups','radio_operator_controls','sop_schedule_layer_enabled','sop_frequency_variant_references') ORDER BY key")]
        return _hash(data)

    @staticmethod
    def _draft(conn, plan_id):
        row = conn.execute('SELECT * FROM frequency_plan_drafts WHERE plan_id=?', (int(plan_id or 0),)).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _dependent_plans(conn, plan_id):
        plans = {int(row['id']): row for row in _rows(conn, 'frequency_plans')}
        affected = {int(plan_id)}
        changed = True
        while changed:
            changed = False
            for ident, plan in plans.items():
                refs = json.loads(plan.get('source_refs_json') or '[]')
                deps = {int(str(ref).rsplit(':', 1)[1]) for ref in refs
                        if isinstance(ref, str) and ref.startswith(('hf_daily_schedule:plan:', 'hf_net_schedule:plan:', 'sop_schedule_plan:'))
                        and str(ref).rsplit(':', 1)[1].isdigit()}
                if ident not in affected and deps & affected:
                    affected.add(ident)
                    changed = True
        return affected

    def context(self, *, plan_id=0, device_profile_id=0, operation='assign'):
        conn = self._snapshot()
        try:
            store = _ReviewStore(conn, self.db_path)
            devices = _rows(conn, 'device_profiles')
            plans = {int(p['id']): p for p in _rows(conn, 'frequency_plans')}
            current = {int(d['id']): _effective_assigned_plan_for_device(conn, int(d['id'])) or {} for d in devices}
            if not plan_id and int(device_profile_id or 0) in current:
                plan_id = int(current[int(device_profile_id)].get('frequency_plan_id') or 0)
            plan = plans.get(int(plan_id or 0))
            radios = []
            for device in devices:
                ident = int(device['id'])
                assignment = current[ident]
                model = assigned_model(conn, ident)
                pause = pause_state(conn, ident)
                hold = conn.execute('SELECT * FROM scheduler_manual_control_states WHERE radio_profile_id=?', (ident,)).fetchone()
                hold_data = dict(hold) if hold is not None else {}
                held = str(hold_data.get('state') or '') not in {'', 'following', 'follow', 'on_schedule', 'idle'}
                status = ('Paused — operator' if pause['operator_paused'] else 'Manual hold retained' if held
                          else 'Radio stopped' if not device.get('runtime_active') else 'Runtime safety checks required')
                radios.append({'id': ident, 'name': device.get('name') or f'Radio {ident}',
                               'current_plan': _plan_ref(plans.get(int(assignment.get('frequency_plan_id') or 0))),
                               'current_assignment': assignment, 'model': model, 'status': status,
                               'receive_only': device.get('device_class') == 'observer'})
            consumers = {}
            for radio in radios:
                mid = str((radio.get('model') or {}).get('id') or '')
                consumers.setdefault(mid, []).append({k: radio[k] for k in ('id', 'name', 'current_plan', 'status')})
            for radio in radios:
                radio['model_consumers'] = consumers.get(str((radio['model'] or {}).get('id') or ''), [])
            targets = [int(device_profile_id)] if int(device_profile_id or 0) > 0 else []
            if not targets and operation == 'assign' and plan:
                existing = [ident for ident, item in current.items() if int(item.get('frequency_plan_id') or 0) == int(plan_id)]
                if len(existing) == 1:
                    targets = existing
            draft = self._draft(conn, int(plan_id or 0))
            if operation == 'apply' and draft:
                dependent = self._dependent_plans(conn, int(plan_id))
                targets = [ident for ident, a in current.items() if int(a.get('frequency_plan_id') or 0) in dependent]
            return {'plan': _plan_ref(plan), 'plans': [_plan_ref(p) for p in plans.values()
                    if p.get('status') == 'saved' and p.get('category') not in SOURCE_ONLY_FREQUENCY_PLAN_CATEGORIES],
                    'radios': radios, 'models': _rows(conn, 'operating_profiles'), 'model_consumers': consumers,
                    'preselected_radio_ids': targets, 'operation': operation, 'plan_draft': bool(draft), 'plan_unsaved': not bool(plan),
                    'draft': draft, 'revision': self._fingerprint(conn)}
        finally:
            conn.close()

    @staticmethod
    def _intent(request):
        operation = str(request.get('operation') or 'assign')
        if operation not in {'assign', 'swap', 'remove', 'apply'}:
            raise ValueError('Choose Assign, Swap, Remove or Apply changes.')
        ids = [int(value) for value in request.get('selected_radio_ids', ())]
        if not ids or len(ids) > MAX_RADIOS or len(ids) != len(set(ids)) or any(value <= 0 for value in ids):
            raise ValueError('Choose the radios affected by this assignment.')
        if operation == 'swap' and len(ids) != 2:
            raise ValueError('Choose exactly two radios to swap their current plans.')
        intent = {'operation': operation, 'plan_id': int(request.get('plan_id') or 0),
                  'selected_radio_ids': sorted(ids), 'remedies': deepcopy(request.get('remedies') or {})}
        for key in ('assignment_state', 'reason', 'ends_utc'):
            if key in request:
                intent[key] = str(request[key] or '')
        if intent.get('assignment_state', 'active') not in EFFECTIVE_ASSIGNMENT_STATES:
            raise ValueError('Choose Active or Temporary override.')
        if intent.get('assignment_state') == 'active':
            intent['ends_utc'] = ''  # Explicit No end date, not the prior assignment's timestamp.
        if intent.get('ends_utc'):
            try:
                end = datetime.fromisoformat(intent['ends_utc'].replace('Z', '+00:00'))
                if end.tzinfo is None or end.utcoffset().total_seconds() != 0 or end <= datetime.now(timezone.utc):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError('Enter a future end time in ISO UTC, including Z or +00:00.') from None
        return intent

    @staticmethod
    def _issue(message, *, radio_id=0, severity='blocked', target='radio', code='configuration', **extra):
        from freqinout.core.plan_conflict_issues import issue, repair
        actions = {'operating_model':'Choose or review Operating Model…','source':'Open affected source…',
            'rf_guard':'Review radio RF configuration…','radio':'Review radio setup…',
            'assignment_choices':'Edit assignment choices…','retry_review':'Recheck assignment…',
            'refresh_sources':'Review source window refresh…'}
        instructions = {'source':'Open the affected source, correct the named entry and save. Return here and recheck.',
            'rf_guard':'Verify this radio’s real RF resource configuration. Do not disable RF Guard or reduce protection to clear a blocker.',
            'radio':'Open the named radio, correct its saved setup and return to recheck.',
            'assignment_choices':'Choose a compatible plan, radio or Operating Model below, then recheck.',
            'retry_review':'Recheck current inputs. A stale or incomplete review cannot authorize operation.',
            'refresh_sources':'Review every affected plan and radio before refreshing source-derived windows, then recheck this assignment.'}.get(target,'Review the named setting, then return and recheck.' if target else '')
        category = {'source':'source','refresh_sources':'source','rf_guard':'rf_guard',
                    'assignment_choices':'assignment','retry_review':'review','':'operational_wait'}.get(target,'configuration')
        label=extra.pop('action_label',actions.get(target,'Review…'))
        plan_id=int(extra.pop('frequency_plan_id',0) or 0)
        remedies=[repair(label,target,radio_id=radio_id,plan_id=plan_id,instructions=instructions,
                    **{key:value for key,value in extra.items() if key in {'source_ref','refresh_plan_ids','operating_profile_id','window'}})] if target else []
        if target=='source' and extra.get('source_ref'):
            remedies.append(repair('Choose a replacement source…','assignment_choices',radio_id=radio_id,plan_id=plan_id,
                field='source',source_ref=extra['source_ref'],
                instructions='Choose a valid replacement layer in Plan Builder and save the plan or draft. Apply saved draft changes through their impact review, then recheck this assignment.'))
        return issue(code,category,message,severity=severity,radio_id=radio_id,plan_id=plan_id,
            instructions=instructions,remedies=remedies,target=target,action_label=label,**extra)

    def _propose(self, conn, intent, *, preview=False):
        if preview and any(row[2] for row in conn.execute('PRAGMA database_list')):
            raise RuntimeError('Assignment previews require an isolated memory database')
        store = _ReviewStore(conn, self.db_path)
        ids = intent['selected_radio_ids']
        operation, plan_id = intent['operation'], intent['plan_id']
        before = {ident: store.get_effective_assigned_plan_for_device(ident) or {} for ident in ids}
        for ident in ids:
            if store.get_device_profile(ident) is None:
                raise ValueError('A selected radio no longer exists. Refresh the assignment review.')
        changed_models, copied_models = set(), {}
        for raw_id, remedy in intent['remedies'].items():
            ident = int(raw_id)
            if ident not in ids or operation == 'remove':
                raise ValueError('A permission remedy refers to a radio outside this assignment.')
            current_model = assigned_model(conn, ident)
            kind = remedy.get('kind')
            if kind == 'allow':
                mid = int(remedy.get('model_id') or 0)
                if not current_model or current_model['id'] != mid:
                    raise ConfigurationChanged('The assigned Operating Model changed; review again.')
                store.save_operating_profile({'id': mid, 'scheduler_enabled': 1})
                changed_models.add(mid)
            elif kind in {'model', 'copy'}:
                model = store.get_operating_profile(int(remedy.get('model_id') or 0))
                if not model:
                    raise _AssignmentBlocked('Choose an existing Operating Model.', radio_id=ident)
                if kind == 'copy':
                    name = str(remedy.get('copy_name') or '').strip()
                    if not name:
                        raise _AssignmentBlocked('Name the reusable Operating Model copy before reviewing.', radio_id=ident)
                    if conn.execute('SELECT 1 FROM operating_profiles WHERE lower(name)=lower(?)', (name,)).fetchone():
                        raise _AssignmentBlocked('An Operating Model already uses that name. Choose it or use a different name.', radio_id=ident)
                    payload = {k: v for k, v in model.items() if k not in {'id', 'created_utc', 'updated_utc'}}
                    payload.update(name=name, system_key='model_'+uuid4().hex, scheduler_enabled=1)
                    model = store.save_operating_profile(payload)
                    copied_models[ident] = model['id']
                store.set_device_operating_profile(ident, model['id'], reason='Selected in Plan Builder assignment review')
            else:
                raise ValueError('Choose Allow scheduling, an existing model, or Create reusable copy.')
        plan_updates = []
        if operation == 'apply':
            draft = self._draft(conn, plan_id)
            current_plan = store.get_frequency_plan(plan_id)
            if not draft or not current_plan:
                raise ValueError('Save draft changes before reviewing their application.')
            if _clean(current_plan) != json.loads(draft['expected_plan_json']):
                raise ConfigurationChanged('The live plan changed since this draft was saved. Reopen and review the draft.')
            saved = store.save_frequency_plan(json.loads(draft['payload_json']))
            plan_updates.append(saved)
            from freqinout.core.schedule_source_sets import _source_backed_plan_payload
            from freqinout.core.planner_catalog_model import DetachedSettings
            settings = DetachedSettings({row['key']: json.loads(row['value']) for row in conn.execute('SELECT key,value FROM kv')}, self.config_dir)
            projected = _source_backed_plan_payload(settings, saved, store=store)
            if projected is not None:
                raise _PendingWindowMismatch(_pending_window_issue(settings, store, saved, projected, ids), saved, projected)
            dependent = self._dependent_plans(conn, plan_id)
            for dependent_id in sorted(dependent - {plan_id}):
                plan = store.get_frequency_plan(dependent_id)
                updated = _source_backed_plan_payload(settings, plan, store=store)
                if updated is not None:
                    plan_updates.append(store.save_frequency_plan(updated))
                elif any(str(ref).startswith('sop_schedule_plan:') for ref in json.loads(plan.get('source_refs_json') or '[]')):
                    raise ValueError(f'Plan “{plan["name"]}” depends on an SOP plan source. Edit its canonical SOP layers in SOP Builder, then review this plan again.')
            all_consumers = {int(row['device_profile_id']) for row in store.list_effective_assigned_plans()
                             if int(row.get('frequency_plan_id') or 0) in dependent}
            if set(ids) != all_consumers:
                raise ConfigurationChanged('The radios affected by this draft changed. Review all affected radios again.')
        overrides = {}
        if operation == 'swap':
            if not all(before.values()):
                raise ValueError('Both radios need saved plan assignments before swapping.')
            overrides = {ids[0]: int(before[ids[1]]['frequency_plan_id']), ids[1]: int(before[ids[0]]['frequency_plan_id'])}
            if overrides[ids[0]] == overrides[ids[1]]:
                raise ValueError('Both radios already use the same plan.')
        elif operation == 'assign':
            if not store.get_frequency_plan(plan_id):
                raise ValueError('Choose a saved Frequency Plan before assigning radios.')
            overrides = {ident: plan_id for ident in ids}
        elif operation == 'remove':
            overrides = {ident: 0 for ident in ids}
        for ident, desired_plan in overrides.items():
            if desired_plan == 0:
                conn.execute("UPDATE assigned_plans SET assignment_state='superseded',reason='Removed in Plan Builder',updated_utc=? WHERE device_profile_id=? AND assignment_state IN ('active','temporary_override')", (datetime.now(timezone.utc).isoformat(), ident))
                continue
            plan = store.get_frequency_plan(desired_plan)
            if not plan or plan.get('status') != 'saved':
                raise ValueError('Only saved, enabled Frequency Plans can be assigned.')
            old = before[ident]
            values=dict(assignment_state=intent.get('assignment_state') or old.get('assignment_state') or 'active',
                assignment_category=old.get('assignment_category') or 'normal', scheduler_mode=old.get('scheduler_mode') or 'full',
                starts_utc=old.get('starts_utc') or '', ends_utc=intent.get('ends_utc',old.get('ends_utc') or ''),
                reason=intent.get('reason',old.get('reason') or 'Assigned in Plan Builder'),created_by='plan_builder')
            if preview:
                store.stage_assignment(ident,desired_plan,values)
            else:
                _set_assigned_plan_conn(store._connect(),ident,desired_plan,**values,assignment_plan_overrides=overrides,commit=False)
        affected = set(ids)
        if changed_models:
            affected.update(int(row['id']) for row in _rows(conn, 'device_profiles')
                            if assigned_model(conn, int(row['id'])).get('id') in changed_models)
        return store, before, sorted(affected), plan_updates

    @staticmethod
    def _event_display(value):
        if not value:
            return ''
        try:
            event = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if event.tzinfo is None:
                event = event.replace(tzinfo=timezone.utc)
            local = event.astimezone()
            return local.strftime('%a %d %b %H:%M %Z') + ' · ' + event.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        except (ValueError, TypeError):
            return str(value) + ' UTC'

    def _radio_review_context(self, conn, intent):
        store = _ReviewStore(conn, self.db_path)
        plan_id = int(intent.get('plan_id') or 0)
        plan = store.get_frequency_plan(plan_id) or {}
        assignments = {int(row['device_profile_id']): int(row['frequency_plan_id'])
                       for row in store.list_effective_assigned_plans()}
        live = {radio_id for radio_id, value in assignments.items() if value == plan_id}
        proposed = set(live)
        chosen = intent['selected_radio_ids']
        if intent['operation'] == 'assign':
            proposed.update(chosen)
        elif intent['operation'] == 'remove':
            proposed.difference_update(chosen)
        elif intent['operation'] == 'swap' and len(chosen) == 2:
            first, second = chosen
            if assignments.get(second) == plan_id: proposed.add(first)
            else: proposed.discard(first)
            if assignments.get(first) == plan_id: proposed.add(second)
            else: proposed.discard(second)
        def named(values):
            return [_plan_ref(device) for ident in sorted(values)
                    if (device := store.get_device_profile(ident))]
        draft = self._draft(conn, plan_id) if intent['operation'] == 'apply' else None
        return {'plan_name': str(plan.get('name') or 'Plan'),
                'version_label': f"Pending version {draft['version']}" if draft else 'Saved plan',
                'live_radios': named(live), 'proposed_radios': named(proposed)}

    def _comparison(self, conn, intent, left_plan, right_plan, kind, token):
        from freqinout.core.plan_window_comparison import compare_windows
        from freqinout.core.planner_catalog_model import DetachedSettings
        settings = DetachedSettings({row['key']: json.loads(row['value']) for row in conn.execute('SELECT key,value FROM kv')}, self.config_dir)
        store = _ReviewStore(conn, self.db_path)
        plan = right_plan or left_plan
        rows, full = compare_windows(settings, store, plan, left_plan, right_plan,
                                     kind=kind, radio_id=intent['selected_radio_ids'][0])
        size = 100
        result = {'kind':kind,
                  'left_label':'Pending changes' if kind == 'source_mismatch' else 'In use now',
                  'right_label': ('Current sources' if kind == 'source_mismatch'
                                  else 'After Apply' if intent['operation'] == 'apply' else 'Saved plan'),
                  'rows':rows[:size], 'total':len(rows), 'complete':len(rows) <= size,
                  'next_offset':size if len(rows) > size else None,
                  'full_schedule':full[:size], 'full_schedule_total':len(full),
                  'full_schedule_complete':len(full) <= size,
                  'full_schedule_next_offset':size if len(full) > size else None,
                  'page_token':token}
        return result, full, rows

    def schedule_page(self, request):
        self._check()
        token = str(request.get('review_token') or '')
        review = self._reviews.get(token)
        if not review or int(request.get('plan_id') or 0) != int(review['intent']['plan_id']):
            raise ConfigurationChanged('The schedule review is no longer current. Review again.')
        try:
            offset, limit = int(request.get('offset') or 0), int(request.get('limit') or 100)
        except (TypeError, ValueError):
            raise ValueError('Invalid schedule page.') from None
        if offset < 0 or limit < 1 or limit > 500:
            raise ValueError('Invalid schedule page.')
        conn = self._snapshot()
        try:
            if self._fingerprint(conn) != review['fingerprint'] or self._source_stamp(conn) != review['source_stamp']:
                raise ConfigurationChanged('The plan or its sources changed. Review again.')
        finally:
            conn.close()
        view = str(request.get('view') or 'full_schedule')
        if view not in {'full_schedule', 'differences'}:
            raise ValueError('Invalid comparison view.')
        rows = review.get('difference_rows' if view == 'differences' else 'full_schedule') or []
        end = min(offset + limit, len(rows))
        return {'rows':deepcopy(rows[offset:end]), 'total':len(rows), 'complete':end >= len(rows),
                'next_offset':end if end < len(rows) else None, 'review_token':token, 'view':view}

    def review(self, request):
        self._check()
        intent = self._intent(request)
        conn = self._snapshot()
        try:
            baseline = self._fingerprint(conn)
            source_stamp = self._source_stamp(conn)
            radio_context = self._radio_review_context(conn, intent)
            live_plan = _ReviewStore(conn, self.db_path).get_frequency_plan(intent['plan_id']) or {}
            source_errors, refresh_required = self._refresh_review_sources(conn, skip_plan_id=int(intent['plan_id']) if intent['operation'] == 'apply' else 0)
            before_plan = _ReviewStore(conn, self.db_path).get_frequency_plan(intent['plan_id']) or {}
            store, before, affected, _updates = self._propose(conn, intent, preview=True)
            issues, impacts, evidence = [], [], {}
            changed_permissions = set()
            for remedy in intent['remedies'].values():
                if remedy.get('kind') == 'allow':
                    model_id = int(remedy.get('model_id') or 0)
                    if model_id not in changed_permissions:
                        changed_permissions.add(model_id)
                        model = store.get_operating_profile(model_id) or {}
                        issues.append(self._issue('This save updates reusable Operating Model “' + str(model.get('name') or 'Model') +
                            '” to allow scheduling for all its consumers shown above. Radios with valid assigned plans may begin following them when existing pauses, holds and safety gates permit.',
                            severity='info', target='', code='model_change'))
            for ident in affected:
                radio = store.get_device_profile(ident)
                assignment = store.get_effective_assigned_plan_for_device(ident) or {}
                plan = store.get_frequency_plan(int(assignment.get('frequency_plan_id') or 0)) or {}
                model = assigned_model(conn, ident)
                old = before.get(ident, assignment)
                old_plan = store.get_frequency_plan(int(old.get('frequency_plan_id') or 0)) or {}
                status, next_event = 'Checking after save; existing holds and safety gates remain', ''
                rf_guard_checked = False
                if intent['operation'] == 'remove' and ident in intent['selected_radio_ids']:
                    status = 'No assigned plan; current tuning and holds remain'
                elif not plan:
                    status = 'No assigned plan; model change alone does not start scheduling'
                else:
                    try:
                        relevant = {int(plan['id'])}
                        for peer in store.list_device_profiles():
                            if peer['id'] == ident:
                                continue
                            if any(radio.get(key) and radio.get(key) == peer.get(key) for key in ('band_overlap_guard_group', 'advanced_frequency_guard_group')):
                                peer_assignment = store.get_effective_assigned_plan_for_device(peer['id']) or {}
                                peer_plan_id = int(peer_assignment.get('frequency_plan_id') or 0)
                                if peer_plan_id:
                                    relevant.add(peer_plan_id)
                                    if peer_plan_id in source_errors:
                                        problem = source_errors[peer_plan_id]
                                        raise _SourceProblem(f'RF review cannot verify {peer["name"]}: {problem}', plan_id=peer_plan_id,
                                                             source_ref=getattr(problem, 'source_ref', ''))
                        pending_refresh = relevant & refresh_required
                        if pending_refresh:
                            issues.append(self._issue('Saved source-derived windows are out of date. Refresh source windows, then review this assignment again. Assignments and model permissions have not changed.',
                                radio_id=ident, target='refresh_sources', code='source_refresh',
                                action_label='Review source window refresh…', refresh_plan_ids=sorted(pending_refresh)))
                        if int(plan['id']) in source_errors:
                            raise source_errors[int(plan['id'])]
                        self._validate_sources(store, plan)
                        review = review_schedule(store, ident)
                        next_event = review.get('next_event') or ''
                        if review.get('operator_paused'):
                            status = 'Paused — retained; use the radio’s explicit Resume when appropriate'
                        elif not radio.get('runtime_active'):
                            status = 'Radio stopped; assignment will remain saved'
                        rf_guard_checked = bool(review.get('valid')) and not pending_refresh
                        structured = review.get('structured_issues') or []
                        issues.extend(structured)
                        if not review.get('valid') and not any(item.get('severity') == 'blocked' for item in structured):
                            issues.append(self._issue('Validation could not provide complete issue details. Recheck current inputs before assigning.',radio_id=ident,frequency_plan_id=plan.get('id'),target='retry_review',code='review.incomplete',evidence={'validator_messages':review.get('issues') or []}))
                        evidence[str(ident)] = review['source_evidence']
                        issues.append(self._issue('An active event may apply after save when connection, identity, PTT and RF checks permit. Existing pauses and holds remain.',
                                                  radio_id=ident, severity='info', target='', code='execution'))
                    except (ValueError, ScheduleSourceError, sqlite3.Error) as exc:
                        issues.append(self._issue(str(exc), radio_id=ident, target='source',
                            frequency_plan_id=getattr(exc, 'plan_id', 0) or plan.get('id'), source_ref=getattr(exc, 'source_ref', '')))
                impacts.append({'radio_id': ident, 'radio_name': radio['name'], 'current_plan': _plan_ref(old_plan),
                                'proposed_plan': _plan_ref(plan), 'model_name': model.get('name') or 'No assigned model',
                                'status': status, 'next_event': next_event, 'next_event_display': self._event_display(next_event), 'assignment_state': assignment.get('assignment_state'),
                                'ends_utc': assignment.get('ends_utc') or '', 'rf_guard_checked': rf_guard_checked})
            # De-duplicate validator messages while retaining each direct repair.
            issues = list({_json(item): item for item in issues}.values())
            if self._live_source_stamp() != source_stamp:
                raise ConfigurationChanged('Schedule sources changed while this review was being prepared. Review current inputs again.')
            for finding in issues:
                target_radio=store.get_device_profile(int(finding.get('radio_id') or 0)) or {}
                target_plan=store.get_frequency_plan(int(finding.get('frequency_plan_id') or 0)) or {}
                finding['scope_label']=' · '.join(str(value) for value in (target_radio.get('name'),target_plan.get('name')) if value)
                finding['revisions']={'configuration':baseline,'sources':deepcopy(source_stamp)}
            can_commit = not any(item['severity'] == 'blocked' for item in issues)
            token = uuid4().hex
            result = {'token': token, 'can_commit': can_commit, 'impacts': impacts, 'issues': issues,
                      'operation': intent['operation'], 'rf_guard': ('Configured RF Guard rules checked; this does not verify physical RF isolation.' if can_commit
                          else 'Review blocked or incomplete. Resolve the named issues; RF protection has not been certified.'),
                      'next_event': min((i['next_event'] for i in impacts if i['next_event']), default='')}
            result['next_event_display'] = self._event_display(result['next_event'])
            result['radio_context'] = radio_context
            result['comparison'], full_schedule, difference_rows = self._comparison(conn, intent, live_plan, store.get_frequency_plan(intent['plan_id']) or live_plan, 'apply', token)
            if intent['operation'] == 'apply':
                after_plan = store.get_frequency_plan(intent['plan_id']) or {}
                result['plan_changes'] = {'old_name': before_plan.get('name'), 'new_name': after_plan.get('name'),
                    'old_windows': len(json.loads(before_plan.get('schedule_refs_json') or '[]')),
                    'new_windows': len(json.loads(after_plan.get('schedule_refs_json') or '[]')),
                    'old_frequencies': json.loads(before_plan.get('frequency_refs_json') or '[]'),
                    'new_frequencies': json.loads(after_plan.get('frequency_refs_json') or '[]')}
            self._reviews[token] = {'intent': intent, 'fingerprint': baseline, 'source_stamp': source_stamp,
                                    'evidence': evidence, 'result': deepcopy(result), 'full_schedule': full_schedule, 'difference_rows': difference_rows}
            while len(self._reviews) > 8:
                self._reviews.popitem(last=False)
            return result
        except _PendingWindowMismatch as exc:
            if self._live_source_stamp() != source_stamp:
                raise ConfigurationChanged('Schedule sources changed during comparison. Review again.')
            page_token = uuid4().hex
            comparison, full_schedule, difference_rows = self._comparison(conn, intent, exc.plan, exc.projected, 'source_mismatch', page_token)
            result = {'token': '', 'can_commit': False, 'operation': intent['operation'], 'impacts': [],
                      'issues': [exc.finding], 'rf_guard': 'Review blocked by a schedule source mismatch.',
                      'radio_context': radio_context, 'comparison': comparison}
            self._reviews[page_token] = {'kind':'comparison_only', 'intent':intent, 'fingerprint':baseline,
                'source_stamp':source_stamp, 'result':deepcopy(result), 'full_schedule':full_schedule, 'difference_rows':difference_rows}
            while len(self._reviews) > 8:
                self._reviews.popitem(last=False)
            return result
        except (ValueError, ConfigurationChanged, ScheduleSourceError, sqlite3.Error) as exc:
            return {'token': '', 'can_commit': False, 'operation': intent['operation'], 'impacts': [],
                    'issues': [self._issue(str(exc), radio_id=getattr(exc, 'radio_id', 0) or int(intent['selected_radio_ids'][0]),
                        target='source' if isinstance(exc, ScheduleSourceError) else 'retry_review' if isinstance(exc,ConfigurationChanged) else getattr(exc,'target','assignment_choices'),
                        frequency_plan_id=getattr(exc, 'plan_id', 0) or intent['plan_id'], source_ref=getattr(exc, 'source_ref', ''))],
                    'rf_guard': 'Review needs changes. No settings or assignments were written.',
                    'radio_context': locals().get('radio_context', {}), 'comparison': {}}
        finally:
            conn.close()

    def commit(self, request):
        self._check()
        token = str(request.get('review_token') or '')
        reviewed = self._reviews.pop(token, None)
        intent = self._intent(request)
        if not reviewed or reviewed.get('kind') == 'refresh' or reviewed['intent'] != intent or not reviewed['result']['can_commit']:
            raise ConfigurationChanged('Review this exact assignment before saving. No changes were made.')
        store = MultiRadioStore(self.db_path)
        with store.configuration_save_transaction() as transaction:
            with store.connect() as conn:
                if self._fingerprint(conn) != reviewed['fingerprint']:
                    raise ConfigurationChanged('Settings or affected radios changed. Review updated results before saving.')
                if self._source_stamp(conn) != reviewed['source_stamp']:
                    raise ConfigurationChanged('Schedule sources changed. Review current inputs before saving.')
                if not all(tokens_current(self.db_path, evidence) for evidence in reviewed['evidence'].values()):
                    raise ConfigurationChanged('Schedule sources changed. Review the current plan before saving.')
                _view, _before, affected, _updates = self._propose(conn, intent)
                for ident in affected:
                    assignment = _effective_assigned_plan_for_device(conn, ident)
                    if not assignment:
                        continue
                    radio = store.get_device_profile(ident)
                    plan = store.get_frequency_plan(int(assignment['frequency_plan_id']))
                    validation = _schedule_assignment_validation_status_conn(conn, radio, plan, emit_events=False)
                    if validation['state'] == 'blocked':
                        raise ValueError((validation.get('blocked') or ['RF Guard blocked this change.'])[0])
                if intent['operation'] == 'apply':
                    conn.execute('DELETE FROM frequency_plan_drafts WHERE plan_id=?', (intent['plan_id'],))
                names = [str(store.get_device_profile(ident).get('name') or f'Radio {ident}') for ident in affected]
                if self._source_stamp(conn)['nets'] != reviewed['source_stamp']['nets']:
                    raise ConfigurationChanged('Schedule sources changed during save. No assignment changes were committed.')
                self._check()
                identity = self.db_path.stat()
                if [identity.st_dev, identity.st_ino] != reviewed['source_stamp']['settings'][:2]:
                    raise ConfigurationChanged('The configuration file was replaced. Reopen and review before saving.')
            transaction.complete()
        return {'saved': True, 'operation': intent['operation'], 'radio_ids': affected, 'radio_names': names,
                'message': f'Changes saved for {", ".join(names)}. Assignments are checking current inputs; existing pauses, holds and live safety checks remain.'}

    def save_draft(self, payload, *, expected=None, expected_draft_version=None):
        self._check()
        plan_id = int(payload.get('id') or 0)
        store = MultiRadioStore(self.db_path)
        clean = {k: deepcopy(v) for k, v in payload.items() if not k.startswith('_catalog_')}
        if not plan_id or not str(clean.get('name') or '').strip():
            raise ValueError('Choose an existing plan and give the draft a clear name.')
        if not isinstance(clean.get('schedule_refs', []), list):
            raise ValueError('Draft schedule windows must be a list.')
        with store.configuration_save_transaction() as transaction:
            with store.connect() as conn:
                current = store.get_frequency_plan(plan_id)
                if not current or expected is not None and _clean(current) != _clean(expected):
                    raise ConfigurationChanged('The live plan changed. Review current inputs before saving the draft.')
                prior = self._draft(conn, plan_id)
                if expected_draft_version is not None and int((prior or {}).get('version') or 0) != int(expected_draft_version):
                    raise ConfigurationChanged('Another saved draft changed. Reload it before saving your edits.')
                if prior and json.loads(prior['expected_plan_json']) != _clean(current):
                    raise ConfigurationChanged('This draft is based on an older live plan. Review or discard it first.')
                conn.execute('''INSERT INTO frequency_plan_drafts(plan_id,version,payload_json,expected_plan_json,saved_utc)
                    VALUES(?,1,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET version=version+1,
                    payload_json=excluded.payload_json,saved_utc=excluded.saved_utc''',
                    (plan_id, _json(clean), _json(_clean(current)), datetime.now(timezone.utc).isoformat()))
                result = self._draft(conn, plan_id)
            transaction.complete()
        return result

    def relink_context(self, plan_id):
        """Explicit named source choices; opening this workflow never writes."""
        from freqinout.core.schedule_source_sets import (
            source_sets_for_category, HF_DAILY_SOURCE_CATEGORY, HF_NET_SOURCE_CATEGORY,
            HF_DAILY_SOURCE_SETS_KEY, HF_NET_SOURCE_SETS_KEY,
            describe_duplicate_schedule_choices,
        )
        conn = self._snapshot()
        try:
            store = _ReviewStore(conn, self.db_path)
            plan = store.get_frequency_plan(int(plan_id))
            if not plan or plan.get('category') != 'normal' or plan.get('status') != 'saved':
                raise ValueError('Select a saved Frequency Plan before linking source schedules.')
            from freqinout.core.planner_catalog_model import DetachedSettings
            settings = DetachedSettings({row['key']: json.loads(row['value']) for row in conn.execute('SELECT key,value FROM kv')}, self.config_dir)
            choices = {}
            for key, category, legacy in [('daily', HF_DAILY_SOURCE_CATEGORY, HF_DAILY_SOURCE_SETS_KEY),
                                          ('nets', HF_NET_SOURCE_CATEGORY, HF_NET_SOURCE_SETS_KEY)]:
                rows = source_sets_for_category(settings, legacy, category, strict=True, store=store)
                rows = describe_duplicate_schedule_choices(rows, category, store.list_frequency_plans(),
                    store.list_effective_assigned_plans(), store.list_device_profiles())
                choices[key] = [{'label': 'No Daily schedule' if key == 'daily' else 'No Net schedule', 'ref': ''}]
                for row in rows:
                    source = store.get_frequency_plan(int(row['db_id'])) if row.get('db_id') else None
                    if source and (not source.get('enabled') or source.get('status') != 'saved'):
                        continue
                    choices[key].append({'label': row.get('choice_label') or row['name'],
                                         'tooltip': row.get('choice_tooltip') or row['name'],
                                         'ref': f"{category}:{row['id']}"})
            return {'plan': _plan_ref(plan), 'source_choices': choices}
        finally:
            conn.close()

    def preview_relink(self, request):
        refs = request.get('source_refs')
        return self.preview_refresh([int(request['plan_id'])], relink_refs=[] if refs is None else refs)

    def preview_refresh(self, plan_ids, *, relink_refs=None):
        """Review scoped derived-window changes without touching live state."""
        self._check()
        from freqinout.core.schedule_source_sets import _source_backed_plan_payload
        ids = sorted({int(value) for value in plan_ids})
        if not ids or len(ids) > MAX_PLANS or any(value <= 0 for value in ids):
            raise ValueError('Choose the named plans whose source windows need refreshing.')
        conn = self._snapshot()
        try:
            baseline, stamp = self._fingerprint(conn), self._source_stamp(conn)
            store = _ReviewStore(conn, self.db_path)
            updates, changes = [], []
            for ident in ids:
                old = store.get_frequency_plan(ident)
                if not old:
                    raise ConfigurationChanged('A named plan is no longer available. Review the assignment again.')
                candidate = old
                if relink_refs is not None:
                    from freqinout.core.schedule_source_sets import HF_DAILY_SOURCE_CATEGORY, HF_NET_SOURCE_CATEGORY
                    if len(ids) != 1 or old.get('category') != 'normal' or old.get('status') != 'saved':
                        raise ValueError('Only a saved Frequency Plan can be linked here.')
                    if not isinstance(relink_refs, list) or len(relink_refs) > 2 or not all(isinstance(ref, str) for ref in relink_refs):
                        raise ValueError('Choose one Daily schedule and one Net schedule, or explicitly choose none.')
                    refs = [ref for ref in relink_refs if ref]
                    prefixes = (HF_DAILY_SOURCE_CATEGORY + ':', HF_NET_SOURCE_CATEGORY + ':')
                    if not refs or any(not ref.startswith(prefixes) for ref in refs) or any(sum(ref.startswith(prefix) for ref in refs) > 1 for prefix in prefixes):
                        raise ValueError('Choose at least one named source schedule. Only one source of each kind is allowed.')
                    available = {choice['ref'] for rows in self.relink_context(ident)['source_choices'].values() for choice in rows}
                    if any(ref not in available for ref in refs):
                        raise ConfigurationChanged('A selected source is no longer available. Reopen Link to source schedules and choose again.')
                    if self._dependent_plans(conn, ident) != {ident}:
                        raise ValueError('Other plans depend on this plan. Keep this snapshot and review those source links before replacing its windows.')
                    retained = [ref for ref in json.loads(old.get('source_refs_json') or '[]')
                                if not ref.startswith(prefixes) and ref not in {'hf_daily', 'hf_nets'}]
                    candidate = {**old, 'source_refs_json': _json(retained + refs)}
                settings = self._validate_sources(store, candidate)
                payload = _source_backed_plan_payload(settings, candidate, store=store)
                if relink_refs is not None:
                    payload = payload or dict(candidate)
                    payload['source_refs'] = json.loads(candidate['source_refs_json'])
                    windows = payload.get('schedule_refs', json.loads(payload.get('schedule_refs_json') or '[]'))
                    if not windows:
                        raise ValueError('The selected sources contain no schedule windows. Add and save source entries, then review the links again. The current plan has been kept.')
                if payload:
                    # Window refresh is not an authoring edit of the plan notes.
                    payload['notes'] = old.get('notes') or ''
                    new = store.save_frequency_plan(payload)
                    updates.append({'old': old, 'payload': payload})
                    changes.append({'plan_id': ident, 'plan_name': new['name'],
                        'old_windows': len(json.loads(old['schedule_refs_json'] or '[]')),
                        'new_windows': len(json.loads(new['schedule_refs_json'] or '[]')),
                        'old_frequencies': json.loads(old['frequency_refs_json'] or '[]'),
                        'new_frequencies': json.loads(new['frequency_refs_json'] or '[]'),
                        'old_source_refs': json.loads(old['source_refs_json'] or '[]'),
                        'new_source_refs': json.loads(new['source_refs_json'] or '[]'),
                        'old_schedule_refs': json.loads(old['schedule_refs_json'] or '[]'),
                        'new_schedule_refs': json.loads(new['schedule_refs_json'] or '[]'),
                        'draft_retained': bool(self._draft(conn, ident))})
            changed = {item['old']['id'] for item in updates}
            impacts, issues = [], []
            for radio in store.list_device_profiles():
                assignment = store.get_effective_assigned_plan_for_device(radio['id']) or {}
                if assignment.get('frequency_plan_id') not in changed:
                    continue
                plan = store.get_frequency_plan(assignment['frequency_plan_id'])
                # Check every configured RF peer against committed source windows.
                peer_refresh_required = False
                for peer in store.list_device_profiles():
                    if peer['id'] == radio['id'] or not any(radio.get(k) and radio.get(k) == peer.get(k)
                            for k in ('band_overlap_guard_group', 'advanced_frequency_guard_group')):
                        continue
                    peer_assignment = store.get_effective_assigned_plan_for_device(peer['id']) or {}
                    peer_plan = store.get_frequency_plan(int(peer_assignment.get('frequency_plan_id') or 0))
                    if peer_plan:
                        settings = self._validate_sources(store, peer_plan)
                        if _source_backed_plan_payload(settings, peer_plan, store=store):
                            peer_refresh_required = True
                            issues.append(self._issue(f'RF review needs current source windows for {peer["name"]}. Return to assignment review and include this plan in the refresh.',
                                radio_id=peer['id'], target='refresh_sources', refresh_plan_ids=sorted(set(ids) | {peer_plan['id']}),
                                action_label='Review source window refresh…'))
                validation = _schedule_assignment_validation_status_conn(conn, radio, plan, emit_events=False, include_issues=True)
                issues.extend(validation.get('issues') or [])
                if validation.get('state') == 'blocked' and not any(i.get('severity') == 'blocked' for i in validation.get('issues') or []):
                    issues.append(self._issue('RF Guard blocked this refresh. Recheck current inputs for complete details.',radio_id=radio['id'],frequency_plan_id=plan.get('id'),target='retry_review',code='review.incomplete'))
                impacts.append({'radio_id': radio['id'], 'radio_name': radio['name'],
                    'current_plan': _plan_ref(plan), 'proposed_plan': _plan_ref(plan),
                    'model_name': assigned_model(conn, radio['id']).get('name') or 'No assigned model',
                    'status': 'Current operation may change after refresh when existing safety gates permit. Pauses and holds remain.',
                    'next_event': '',
                    'rf_guard_checked': (not peer_refresh_required and validation.get('state') in {'ok', 'warning'}
                                         and validation.get('rf_guard_validation') == 'enforced')})
            if self._live_source_stamp() != stamp:
                raise ConfigurationChanged('Schedule sources changed during refresh review. Review again.')
            result = {'operation': 'relink_sources' if relink_refs is not None else 'refresh_sources', 'token': uuid4().hex,
                'refresh_plan_ids': ids, 'can_commit': bool(updates) and not any(i['severity'] == 'blocked' for i in issues),
                'impacts': impacts, 'issues': issues, 'source_refresh_changes': changes,
                'rf_guard': 'Configured RF Guard checked; physical RF isolation and live connection/PTT remain runtime checks.'}
            self._reviews[result['token']] = {'kind': 'refresh', 'relink': relink_refs is not None, 'fingerprint': baseline, 'source_stamp': stamp,
                'updates': updates, 'result': deepcopy(result)}
            while len(self._reviews) > 8:
                self._reviews.popitem(last=False)
            return result
        except (ValueError, ConfigurationChanged, ScheduleSourceError, sqlite3.Error) as exc:
            return {'operation': 'refresh_sources', 'token': '', 'can_commit': False,
                'refresh_plan_ids': ids, 'impacts': [], 'source_refresh_changes': [],
                'issues': [self._issue(str(exc), target='source',
                    frequency_plan_id=getattr(exc, 'plan_id', 0) or ids[0], source_ref=getattr(exc, 'source_ref', ''))],
                'rf_guard': 'Refresh review is blocked or incomplete. No live plan changes were saved.'}
        finally:
            conn.close()

    def commit_refresh(self, request):
        self._check()
        reviewed = self._reviews.pop(str(request.get('refresh_token') or ''), None)
        if not reviewed or reviewed.get('kind') != 'refresh' or not reviewed['result']['can_commit']:
            raise ConfigurationChanged('Review the source window refresh before saving.')
        if sorted(request.get('refresh_plan_ids') or []) != reviewed['result']['refresh_plan_ids']:
            raise ConfigurationChanged('The refresh scope changed. Review again.')
        store = MultiRadioStore(self.db_path)
        with store.configuration_save_transaction() as transaction:
            with store.connect() as conn:
                if self._fingerprint(conn) != reviewed['fingerprint'] or self._source_stamp(conn) != reviewed['source_stamp']:
                    raise ConfigurationChanged('Plans, radios or sources changed. Review this refresh again.')
                for update in reviewed['updates']:
                    old = update['old']
                    new = store.save_frequency_plan(update['payload'], expected=old)
                    draft = self._draft(conn, old['id'])
                    if draft and json.loads(draft['expected_plan_json']) == _clean(old):
                        # Keep authoring content verbatim; invalidate old editor/review versions.
                        conn.execute('UPDATE frequency_plan_drafts SET expected_plan_json=?,version=version+1 WHERE plan_id=?',
                                     (_json(_clean(new)), old['id']))
                for impact in reviewed['result']['impacts']:
                    radio = store.get_device_profile(impact['radio_id'])
                    assignment = store.get_effective_assigned_plan_for_device(radio['id'])
                    plan = store.get_frequency_plan(assignment['frequency_plan_id'])
                    validation = _schedule_assignment_validation_status_conn(conn, radio, plan, emit_events=False)
                    if validation['state'] == 'blocked':
                        raise ValueError((validation.get('blocked') or ['RF Guard blocked this refresh.'])[0])
                if self._source_stamp(conn)['nets'] != reviewed['source_stamp']['nets']:
                    raise ConfigurationChanged('Sources changed during refresh. No changes were committed.')
                self._check()
                identity = self.db_path.stat()
                if [identity.st_dev, identity.st_ino] != reviewed['source_stamp']['settings'][:2]:
                    raise ConfigurationChanged('The configuration file changed. Reopen Plan Builder.')
            transaction.complete()
        self._reviews.clear()
        names = [item['plan_name'] for item in reviewed['result']['source_refresh_changes']]
        if reviewed.get('relink'):
            pending = any(item['draft_retained'] for item in reviewed['result']['source_refresh_changes'])
            message = 'Source links saved to the live plan: ' + ', '.join(names) + '. No further save is needed.'
            if pending:
                message += ' Earlier pending changes were kept separately and have not been applied. Review them only if you want to replace the saved plan.'
        else:
            message = 'Source windows refreshed for ' + ', '.join(names) + '. Saved draft content was retained. Review the assignment again; existing pauses and safety gates remain.'
        return {'saved': True, 'relinked': reviewed.get('relink', False), 'message': message}

    def discard_draft(self, plan_id, *, expected_version):
        self._check()
        store = MultiRadioStore(self.db_path)
        with store.configuration_save_transaction() as transaction:
            with store.connect() as conn:
                draft = self._draft(conn, int(plan_id))
                if not draft or int(draft['version']) != int(expected_version):
                    raise ConfigurationChanged('The saved draft changed. Reload it before discarding.')
                conn.execute('DELETE FROM frequency_plan_drafts WHERE plan_id=?', (int(plan_id),))
            transaction.complete()
        self._reviews.clear()
        return {'plan_id': int(plan_id), 'message': 'Draft changes discarded. The live plan and radio assignments are unchanged.'}
