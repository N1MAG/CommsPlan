"""Saved scheduling approval in the existing configuration DB.

Review/enable are worker operations. Dispatch checks one indexed approval row;
rendering and clock evaluation never read the DB. Additive SQL triggers suspend
approval in the same transaction as a material change, including other writers.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
from uuid import uuid4

TABLE = 'operator_schedule_authorizations'
RADIO_FIELDS = ('enabled','device_class','control_backend','rig_host','rig_port','flrig_host','flrig_port',
    'js8_host','js8_port','js8_instance_id','js8_profile_path','fast_light_config_id',
    'fldigi_host','fldigi_port','use_js8call','use_flrig','use_fldigi',
    'antenna_group','antenna_supported_bands_json','antenna_band_guard_mode','rf_conflict_group',
    'frontend_group','amplifier_group','band_overlap_guard_group','advanced_frequency_guard_group','advanced_frequency_guard_window_hz',
    'freq_enforcement_mode','fldigi_enforcement_mode','js8_enforcement_mode','freq_prompt_interval','fldigi_prompt_interval','js8_prompt_interval',
    'ptt_group','band_overlap_guard_mode','advanced_frequency_guard_mode','sdr_control_enabled',
    'sdr_adapter','sdr_target','sdr_host','sdr_port')
MODEL_FIELDS = ('enabled','status','scheduler_enabled','scheduler_mode','receive_only')
PLAN_FIELDS = ('enabled','status','receive_only','source_refs_json','schedule_refs_json','frequency_refs_json','group_refs_json')
SHARED_FIELDS = ('antenna_group','frontend_group','amplifier_group','ptt_group','band_overlap_guard_group','advanced_frequency_guard_group')


def ensure_schedule_authorization_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        radio_id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, approval_token TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0, reviewed_utc TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', source_evidence_json TEXT NOT NULL DEFAULT '{{}}')''')
    if 'source_evidence_json' not in {row[1] for row in conn.execute(f'PRAGMA table_info({TABLE})')}:
        conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN source_evidence_json TEXT NOT NULL DEFAULT '{{}}'")
    if 'binding_evidence_json' not in {row[1] for row in conn.execute(f'PRAGMA table_info({TABLE})')}:
        conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN binding_evidence_json TEXT NOT NULL DEFAULT '{{}}'")
    def triggers(table, fields, predicate):
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        fields = tuple(field for field in fields if field in columns)
        if not fields:
            return
        changed = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields)
        for event in ('UPDATE','DELETE'):
            when = f' WHEN {changed}' if event == 'UPDATE' else ''
            affected = predicate
            if table in {'assigned_plans','operating_profile_assignments'}:
                old_effective = "OLD.assignment_state IN ('active','temporary_override')"
                active = f"({old_effective} OR NEW.assignment_state IN ('active','temporary_override'))" if event == 'UPDATE' else old_effective
                affected = f"({predicate}) AND {active}"
                if event == 'UPDATE':
                    affected = f"radio_id IN (OLD.device_profile_id, NEW.device_profile_id) AND {active}"
            name=f'ocs_schedule_{table}_{event.lower()}'
            definition=f'''CREATE TRIGGER {name}
                AFTER {event} ON {table}{when} BEGIN
                UPDATE {TABLE} SET enabled=0,reason='configuration changed'
                WHERE enabled=1 AND ({affected}); END'''
            existing=conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()
            if existing is not None and existing[0] != definition:
                conn.execute(f'DROP TRIGGER {name}')
                existing=None
            if existing is None:
                conn.execute(definition)
    shared = ' OR '.join(f"(COALESCE(OLD.{field},'')<>'' AND d.{field}=OLD.{field}) OR (COALESCE(NEW.{field},'')<>'' AND d.{field}=NEW.{field})" for field in SHARED_FIELDS)
    # DELETE has no NEW row; field-specific predicates are selected per event.
    shared_old = ' OR '.join(f"(COALESCE(OLD.{field},'')<>'' AND d.{field}=OLD.{field})" for field in SHARED_FIELDS)
    triggers('device_profiles', RADIO_FIELDS, 'radio_id=OLD.id')
    # A shared resource change also fences consumers of that actual resource.
    # Keep this separate from ordinary endpoint/profile updates on one radio.
    device_columns={row[1] for row in conn.execute('PRAGMA table_info(device_profiles)')}
    changed=' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in RADIO_FIELDS if field in device_columns)
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS ocs_schedule_shared_resource_update AFTER UPDATE ON device_profiles
        WHEN {changed} BEGIN UPDATE {TABLE} SET enabled=0,reason='shared configuration changed'
        WHERE enabled=1 AND radio_id IN (SELECT d.id FROM device_profiles d WHERE d.id=OLD.id OR {shared}); END''')
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS ocs_schedule_shared_resource_delete AFTER DELETE ON device_profiles
        BEGIN UPDATE {TABLE} SET enabled=0,reason='shared configuration changed'
        WHERE enabled=1 AND radio_id IN (SELECT d.id FROM device_profiles d WHERE {shared_old}); END''')
    shared_new=shared_old.replace('OLD.','NEW.')
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS ocs_schedule_shared_resource_insert AFTER INSERT ON device_profiles
        BEGIN UPDATE {TABLE} SET enabled=0,reason='shared configuration changed'
        WHERE enabled=1 AND radio_id IN (SELECT d.id FROM device_profiles d WHERE d.id<>NEW.id AND ({shared_new})); END''')
    triggers('operating_profiles', MODEL_FIELDS,
        "radio_id IN (SELECT device_profile_id FROM operating_profile_assignments WHERE operating_profile_id=OLD.id AND assignment_state IN ('active','temporary_override'))")
    triggers('frequency_plans', PLAN_FIELDS,
        "radio_id IN (SELECT device_profile_id FROM assigned_plans WHERE frequency_plan_id=OLD.id AND assignment_state IN ('active','temporary_override'))")
    for table,fields in (
        ('assigned_plans',('device_profile_id','frequency_plan_id','assignment_state','scheduler_mode','starts_utc','ends_utc')),
        ('operating_profile_assignments',('device_profile_id','operating_profile_id','assignment_state','starts_utc','ends_utc'))):
        triggers(table,fields,'radio_id=OLD.device_profile_id')
        name=f'ocs_schedule_{table}_insert'
        existing=conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()
        if existing is not None and 'WHEN NEW.assignment_state' not in existing[0]:
            conn.execute(f'DROP TRIGGER {name}')
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS ocs_schedule_{table}_insert AFTER INSERT ON {table}
            WHEN NEW.assignment_state IN ('active','temporary_override') BEGIN UPDATE {TABLE} SET enabled=0,reason='configuration changed'
            WHERE enabled=1 AND radio_id=NEW.device_profile_id; END''')
    triggers('js8_instances',('enabled','host','port','install_path','profile_path','rig_name','offset_hz'),
        'radio_id IN (SELECT id FROM device_profiles WHERE js8_instance_id=OLD.id AND use_js8call=1)')
    triggers('fast_light_configs',('enabled','flrig_host','flrig_port','fldigi_host','fldigi_port','flrig_path','fldigi_path'),
        'radio_id IN (SELECT id FROM device_profiles WHERE fast_light_config_id=OLD.id AND (use_flrig=1 OR use_fldigi=1))')
    for event in ('INSERT','UPDATE','DELETE'):
        ref='OLD' if event=='DELETE' else 'NEW'
        changed = ''
        if event == 'UPDATE':
            changed = " AND json_extract(CASE WHEN json_valid(OLD.value) THEN OLD.value ELSE '{}' END, '$.\"' || radio_id || '\"') IS NOT json_extract(CASE WHEN json_valid(NEW.value) THEN NEW.value ELSE '{}' END, '$.\"' || radio_id || '\"')"
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS ocs_schedule_operator_controls_{event.lower()}
            AFTER {event} ON kv WHEN {ref}.key='radio_operator_controls' BEGIN
            UPDATE {TABLE} SET enabled=0,reason='configuration changed' WHERE enabled=1{changed}; END""")


def _data(value, default):
    if isinstance(value, type(default)):
        return value
    try:
        return json.loads(value or json.dumps(default))
    except (TypeError, ValueError):
        raise ValueError('Saved schedule references are invalid')


def _selected(row, fields):
    return {field:row.get(field) for field in fields}


def _truth(value):
    return str(value).lower() not in {'0','false','none',''}


def _binding_evidence(conn,radio_id):
    """Only command/native identity facts, never CP labels or verification age."""
    result={}
    for family,encoded in conn.execute("SELECT family_key,record_json FROM radio_software_identity_records WHERE radio_profile_id=? AND family_key IN ('js8call','fast_light') ORDER BY family_key",(radio_id,)):
        record=json.loads(encoded)
        parts=[]
        for part in record.get('components') or []:
            if part.get('component_id') not in {'js8call','flrig','fldigi'}:
                continue
            parts.append({field:part.get(field) for field in ('component_id','argv','cwd','env','dependencies')})
        result[family]=dict(paths=record.get('paths'),endpoints=record.get('endpoints'),components=parts,
                            bindings=record.get('bindings'))
    return result


def _saved_inputs(store, radio_id):
    profile = store.get_device_profile(radio_id)
    if not profile:
        raise ValueError('The selected radio no longer exists')
    model_assignment = store.get_effective_assignment_for_device(radio_id) or {}
    model = store.get_operating_profile(int(model_assignment.get('operating_profile_id') or 0)) or {}
    assignment = store.get_effective_assigned_plan_for_device(radio_id) or {}
    plan = store.get_frequency_plan(int(assignment.get('frequency_plan_id') or 0)) or {}
    with store.connect_readonly() as conn:
        approval = conn.execute(f'SELECT * FROM {TABLE} WHERE radio_id=?',(radio_id,)).fetchone()
        js8 = conn.execute('SELECT * FROM js8_instances WHERE id=?',(profile.get('js8_instance_id'),)).fetchone()
        fast = conn.execute('SELECT * FROM fast_light_configs WHERE id=?',(profile.get('fast_light_config_id'),)).fetchone()
        bindings = _binding_evidence(conn,radio_id)
    evidence = dict(profile=_selected(profile,RADIO_FIELDS),model=_selected(model,MODEL_FIELDS),
        model_id=model.get('id'),plan_id=plan.get('id'),plan=_selected(plan,PLAN_FIELDS),
        assignment=_selected(assignment,('frequency_plan_id','assignment_state','scheduler_mode','starts_utc','ends_utc')),
        js8=_selected(dict(js8 or {}),('enabled','host','port','install_path','profile_path','rig_name','offset_hz')) if profile.get('use_js8call') else {},
        fast=_selected(dict(fast or {}),('enabled','flrig_host','flrig_port','fldigi_host','fldigi_port','flrig_path','fldigi_path')) if profile.get('use_flrig') or profile.get('use_fldigi') else {})
    peers = [peer for peer in store.list_device_profiles() if peer['id'] != profile['id'] and any(profile.get(field) and profile.get(field)==peer.get(field) for field in SHARED_FIELDS)]
    evidence['shared_consumers'] = [_selected(peer,('id',*RADIO_FIELDS)) for peer in peers]
    evidence['native_bindings'] = bindings
    fingerprint=hashlib.sha256(json.dumps(evidence,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return profile,model,assignment,plan,dict(approval or {}),fingerprint


def review_schedule(store, radio_id, *, now=None):
    """Pure validation of one saved snapshot; no probe, launch or native writes."""
    now = now or datetime.now(timezone.utc)
    if getattr(store._transaction_local, 'connection', None) is None:
        _prepare_source_revisions(store)
    with store.configuration_read_transaction():
        profile,model,assignment,plan,approval,fingerprint = _saved_inputs(store,int(radio_id))
        from freqinout.core.schedule_approval_sources import source_tokens, tokens_current
        with store.connect_readonly() as conn:
            settings_data={row['key']:json.loads(row['value']) for row in conn.execute("SELECT key,value FROM kv WHERE key IN ('operating_groups','radio_operator_controls','sop_schedule_layer_enabled','sop_frequency_variant_references')")}
        from freqinout.core.radio_operator_controls import radio_choices
        choices=radio_choices(settings_data,int(radio_id))
        sources=source_tokens(store.db_path,plan,choices)
        fingerprint=hashlib.sha256((fingerprint+json.dumps(sources,sort_keys=True)+json.dumps(choices,sort_keys=True)).encode()).hexdigest()
        issues=[]
        warnings=[]
        next_events=[]
        if not plan:
            issues.append('Choose and assign a saved schedule.')
        if not model or str(model.get('status') or 'saved') != 'saved' or not _truth(model.get('enabled',1)) or not _truth(model.get('scheduler_enabled',0)):
            issues.append('Choose an operating model that permits CP scheduling.')
        if plan and (not _truth(plan.get('enabled',1)) or str(plan.get('status') or 'saved') != 'saved'):
            issues.append('The assigned schedule is disabled or unfinished.')
        rows = _data(plan.get('schedule_refs_json'),[]) if plan else []
        from types import SimpleNamespace
        from freqinout.core.schedule_source_inputs import load_sop_projection_sources
        refs=_data(plan.get('source_refs_json'),[]) if plan else []
        declared={int(ref.split(':',1)[1]) for ref in refs if isinstance(ref,str) and ref.startswith('sop_schedule_layer:')}
        nets_path=Path(store.db_path).with_name('freqinout_nets.db')
        if choices.get('sop_tuning_enabled') is not False and nets_path.is_file():
            from freqinout.core.sqlite_utils import connect_sqlite_readonly
            with connect_sqlite_readonly(nets_path) as conn:
                tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                profile_ids=declared or ({int(row[0]) for row in conn.execute('SELECT id FROM sop_profiles WHERE active=1')} if 'sop_profiles' in tables else set())
            if profile_ids:
                class Snapshot(dict):
                    pass
                snapshot=Snapshot(settings_data)
                snapshot.db_path=store.db_path
                live_sop,_policies=load_sop_projection_sources(snapshot,profile_ids)
                rows=[row for row in rows if str(row.get('source') or '').upper()!='SOP']+live_sop
        enabled = [row for row in rows if isinstance(row,dict) and _truth(row.get('enabled',1))
                   and not row.get('implicit_continuation') and not row.get('_resolution_implicit_continuation')]
        if plan and not enabled:
            issues.append('The assigned schedule has no enabled entries.')
        # Reuse the scheduler's recurrence evaluator without constructing a
        # runtime, timer, SettingsManager or endpoint. All persisted time fields
        # are canonical UTC; unsupported bases are rejected rather than guessed.
        from freqinout.core.scheduler_engine import SchedulerEngine, _parse_hhmm_to_minutes
        engine=SchedulerEngine.__new__(SchedulerEngine)
        engine.settings=SimpleNamespace(all=lambda:settings_data)
        for index,row in enumerate(enabled,1):
            prefix=f'Entry {index}: '
            group_issue=engine._operating_group_resolution_error(row)
            if group_issue:
                issues.append(prefix+group_issue)
                continue
            if str(row.get('time_basis') or 'UTC').upper() != 'UTC' or row.get('timezone') not in (None,'','UTC','Etc/UTC'):
                issues.append(prefix+'save the schedule in supported UTC time.')
                continue
            start=_parse_hhmm_to_minutes(str(row.get('start_utc') or ''))
            end=_parse_hhmm_to_minutes(str(row.get('end_utc') or ''))
            if start is None or end is None:
                issues.append(prefix+'enter valid start/end UTC times.')
                continue
            recurrence=str(row.get('recurrence') or 'Weekly')
            if recurrence not in {'Daily','Weekly','Bi-Weekly','Periodic','Monthly'}:
                issues.append(prefix+'unsupported recurrence; choose Daily, Weekly, Bi-Weekly or Periodic.')
                continue
            day=str(row.get('day_utc') or 'ALL').upper()
            if day not in {'ALL','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY','SUNDAY'}:
                issues.append(prefix+'choose a valid day.')
                continue
            if row.get('date') or row.get('start_date') or row.get('end_date'):
                issues.append(prefix+'dated occurrences are not supported by this schedule runtime.')
                continue
            if recurrence in {'Periodic','Monthly'} and row.get('month_weeks'):
                tokens=str(row['month_weeks']).split(',')
                if any(token.strip() not in {'1','2','3','4','5'} for token in tokens):
                    issues.append(prefix+'choose month weeks 1 through 5.')
                    continue
            freq=engine._parse_freq_hz(str(row.get('frequency') or ''))
            if freq is None or not 1000 <= freq <= 300_000_000_000:
                issues.append(prefix+'enter a valid frequency in MHz.')
                continue
            if not str(row.get('mode') or '').strip():
                issues.append(prefix+'choose an operating mode.')
                continue
            # Every supported recurrence repeats within four calendar months, including a
            # fifth weekday missing in the current month. This is a semantic
            # bound for these supported recurrences, not a today-only test.
            candidates=engine._row_start_candidates(row,now_utc=now,horizon_days=124 if recurrence in {"Periodic","Monthly"} else 15 if recurrence == "Bi-Weekly" else 8)
            if candidates:
                next_events.append(min(candidates))
            else:
                issues.append(prefix+'no remaining supported occurrence.')
        if enabled:
            try:
                hf=[row for row in enabled if str(row.get('source') or 'HF').upper()=='HF']
                active=engine._find_active_hf_entry(now,hf) or engine._find_active_net_entry(now,enabled)
                if active:
                    warnings.append('An event is active now. Assigning this schedule may apply it when connection and safety checks permit.')
            except Exception as exc:
                issues.append('Resolve the schedule conflict: '+str(exc))
        warnings.append('CP scheduling requires current connection, identity and RF/PTT checks. Application startup and existing holds are separate choices.')
        if plan:
            validation=store.validate_frequency_plan_for_device(int(radio_id),plan)
            issues.extend(validation.get('blocked') or [])
            warnings.extend(validation.get('warnings') or [])
        backend=str(profile.get('control_backend') or 'manual').lower()
        if backend not in {'manual','none'}:
            host,port = (('js8_host','js8_port') if backend=='js8call' else ('flrig_host','flrig_port') if backend=='flrig' else ('rig_host','rig_port'))
            try:
                if not profile.get(host) or not 1 <= int(profile.get(port) or 0) <= 65535:
                    raise ValueError
            except (ValueError,TypeError):
                issues.append('Choose a complete saved control endpoint for this radio.')
        for use,link,table in (('use_js8call','js8_instance_id','js8_instances'),('use_flrig','fast_light_config_id','fast_light_configs'),('use_fldigi','fast_light_config_id','fast_light_configs')):
            if profile.get(use):
                with store.connect_readonly() as conn:
                    app=conn.execute(f'SELECT enabled FROM {table} WHERE id=?',(profile.get(link),)).fetchone()
                if app is None or not app[0]:
                    issues.append('A required application is disabled or has no saved binding: '+use.replace('use_',''))
        if not tokens_current(store.db_path,sources):
            issues.append('Schedule sources changed during review. Check again.')
        valid=not issues
        approved=bool(valid and approval.get('enabled') and approval.get('fingerprint')==fingerprint and tokens_current(store.db_path,json.loads(approval.get('source_evidence_json') or '{}')))
        operator_paused=bool(approval.get('reason') == 'operator disabled' and not approval.get('enabled'))
        state='operator_paused' if operator_paused else 'enabled' if approved else 'checking' if valid else 'needs_attention'
        label=('Scheduling paused — operator' if operator_paused else 'Following assigned schedule' if approved
               else 'Assigned schedule — checking inputs' if valid else 'No assigned schedule' if not plan
               else 'Schedule blocked — '+str(issues[0] if issues else 'check the assigned plan'))
        next_event=min(next_events).isoformat() if next_events else ''
        from freqinout.core.schedule_assignment_information import frequency_plan_schedule_information
        information=frequency_plan_schedule_information(plan) if plan else []
        return dict(state=state,label=label,fingerprint=fingerprint,valid=valid,information=information,issues=list(dict.fromkeys(issues)),warnings=warnings,
            next_event=next_event,plan_name=plan.get('name') or '',time_basis='UTC (fixed; no DST shift)',enabled=approved,
            approval_token=approval.get('approval_token') if approved else '',radio_name=profile.get('name') or '',
            operator_paused=operator_paused,operator_pause_token=approval.get('approval_token') if operator_paused else '',
            source_evidence=sources,binding_evidence=_binding_evidence(store._transaction_local.connection,int(radio_id)),launch_summaries=launch_summaries(store,int(radio_id)))


def launch_summaries(store,radio_id):
    out=[]
    for identity in store.list_radio_software_identity_records(radio_id):
        for component in identity.components:
            launch=component.launch
            selector=str(launch.get('profile_selector') or '')
            out.append(f'{identity.family_key} / {component.component_id}: Launches as {selector or "Default"} · '
                       + ('I start the application' if launch.get('operator_starts',True) else 'CommsPlan launches')
                       + (' · when CP opens' if launch.get('launch_at_startup') else ''))
    return out


def _prepare_source_revisions(store):
    from freqinout.core.schedule_approval_sources import ensure_source_database
    ensure_source_database(store.db_path)
    ensure_source_database(Path(store.db_path).with_name('freqinout_nets.db'))


def set_schedule_authorization(store,radio_id,*,enable,expected_fingerprint=''):
    if enable:
        _prepare_source_revisions(store)
    if not enable:
        with store.configuration_save_transaction() as tx:
            with store.connect() as conn:
                conn.execute(f"UPDATE {TABLE} SET enabled=0,approval_token=?,reason='operator disabled' WHERE radio_id=?",(uuid4().hex,int(radio_id)))
            tx.complete()
        return dict(enabled=False,state='off',label='Scheduling off',radio_id=int(radio_id))
    with store.configuration_save_transaction() as tx:
        review=review_schedule(store,int(radio_id))
        if enable and (not review['valid'] or review['fingerprint']!=expected_fingerprint):
            raise ValueError('Saved configuration changed or needs attention. Review it before enabling scheduling.')
        with store.connect() as conn:
            conn.execute(f'''INSERT INTO {TABLE}(radio_id,fingerprint,approval_token,enabled,reviewed_utc,reason,source_evidence_json,binding_evidence_json)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(radio_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                approval_token=excluded.approval_token,enabled=excluded.enabled,reviewed_utc=excluded.reviewed_utc,reason=excluded.reason,source_evidence_json=excluded.source_evidence_json,binding_evidence_json=excluded.binding_evidence_json''',
                (int(radio_id),review['fingerprint'],uuid4().hex,int(bool(enable)),datetime.now(timezone.utc).isoformat(),
                 '' if enable else 'operator disabled',json.dumps(review['source_evidence'],sort_keys=True),json.dumps(review['binding_evidence'],sort_keys=True)))
        tx.complete()
    return review_schedule(store,int(radio_id))


def _configuration_revision(conn):
    exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='_fio_schedule_revision'").fetchone()
    return int(conn.execute("SELECT revision FROM _fio_schedule_revision WHERE id=1").fetchone()[0]) if exists else 0


def ensure_assigned_schedule_authority(store, radio_id, *, sources_prepared=False):
    """Worker-only validation proof for assignment intent; preserve operator pause.

    Return the detached review and exact owned configuration-revision delta.
    Configuration/source revocation and dispatch token checks remain mandatory.
    """
    if not sources_prepared:
        _prepare_source_revisions(store)
    # Most refreshes only confirm an existing proof. Do not reserve SQLite's
    # writer for recurrence/RF validation and cross-database source reads.
    # Any mutation still re-reviews under the original atomic write boundary.
    with store.configuration_read_transaction():
        review = review_schedule(store, int(radio_id))
        with store.connect_readonly() as conn:
            current = conn.execute(f'SELECT enabled FROM {TABLE} WHERE radio_id=?', (int(radio_id),)).fetchone()
        if (review.get('operator_paused') or review.get('enabled')
                or (not review['valid'] and not (current and current[0]))):
            return review, 0
    with store.configuration_save_transaction() as transaction:
        review=review_schedule(store,int(radio_id))
        with store.connect() as conn:
            before=_configuration_revision(conn)
            current=conn.execute(f'SELECT enabled,reason FROM {TABLE} WHERE radio_id=?',(int(radio_id),)).fetchone()
            if review.get('operator_paused') or review.get('enabled'):
                transaction.complete()
                return review,0
            if review['valid']:
                token=uuid4().hex
                conn.execute(f"""INSERT INTO {TABLE}(radio_id,fingerprint,approval_token,enabled,reviewed_utc,reason,source_evidence_json,binding_evidence_json)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(radio_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                    approval_token=excluded.approval_token,enabled=excluded.enabled,reviewed_utc=excluded.reviewed_utc,
                    reason=excluded.reason,source_evidence_json=excluded.source_evidence_json,binding_evidence_json=excluded.binding_evidence_json""",
                    (int(radio_id),review['fingerprint'],token,1,datetime.now(timezone.utc).isoformat(),
                     'automatic validation',json.dumps(review['source_evidence'],sort_keys=True),json.dumps(review['binding_evidence'],sort_keys=True)))
            elif current and current[0]:
                conn.execute(f"UPDATE {TABLE} SET enabled=0,reason='validation failed',approval_token=? WHERE radio_id=?",
                             (uuid4().hex,int(radio_id)))
            delta=_configuration_revision(conn)-before
        transaction.complete()
    if review['valid']:
        review.update(enabled=True,state='enabled',label='Following assigned schedule',approval_token=token)
    return review,delta


def clear_legacy_schedule_pause(store, radio_id, expected_token):
    """Only an explicit Resume may clear an exact legacy disabled intent."""
    if not expected_token:
        return 0
    with store.configuration_save_transaction() as transaction:
        with store.connect() as conn:
            before=_configuration_revision(conn)
            conn.execute(f"UPDATE {TABLE} SET reason='resume requested',approval_token=? WHERE radio_id=? AND enabled=0 AND reason='operator disabled' AND approval_token=?",
                         (uuid4().hex,int(radio_id),str(expected_token)))
            delta=_configuration_revision(conn)-before
        transaction.complete()
    return delta


def dispatch_approval_current(db_path,radio_id,token):
    """Command-worker check only: one indexed row, no schema work or scans."""
    if not token or not radio_id:
        return False
    try:
        from freqinout.core.sqlite_utils import connect_sqlite_readonly
        with connect_sqlite_readonly(Path(db_path),timeout=0.25,busy_timeout_ms=250) as conn:
            row=conn.execute(f'SELECT source_evidence_json,binding_evidence_json FROM {TABLE} WHERE radio_id=? AND approval_token=? AND enabled=1',
                                (int(radio_id),str(token))).fetchone()
            if row is None:
                return False
            if _binding_evidence(conn,int(radio_id)) != json.loads(row[1] or '{}'):
                return False
            from freqinout.core.schedule_approval_sources import tokens_current
            return tokens_current(db_path,json.loads(row[0] or '{}'))
    except (OSError,sqlite3.Error,ValueError,TypeError):
        return False


def review_station_setup(store):
    _prepare_source_revisions(store)
    from freqinout.core.schedule_source_inputs import ScheduleSourceError
    for profile in store.list_device_profiles():
        try:
            ensure_assigned_schedule_authority(store,int(profile['id']))
        except (ValueError, ScheduleSourceError):
            pass  # The read-only per-radio review below publishes its error.
    with store.configuration_read_transaction():
        with store.connect_readonly() as conn:
            kv={row['key']:_data(row['value'],{}) if row['key']=='operating_groups' else row['value'] for row in conn.execute(
                "SELECT key,value FROM kv WHERE key IN ('operator_callsign','callsign','operator_name','timezone','operating_groups')")}
        # Settings strings may themselves be JSON-encoded; retain ordinary text.
        def text(value):
            try:
                decoded=json.loads(value or '""')
                return decoded if isinstance(decoded,str) else str(decoded)
            except (ValueError,TypeError):
                return str(value or '')
        groups=kv.get('operating_groups') or []
        radios=[]
        for profile in store.list_device_profiles():
            assignment=store.get_effective_assignment_for_device(profile['id']) or {}
            model=store.get_operating_profile(int(assignment.get('operating_profile_id') or 0)) or {}
            schedule_required=bool(model.get('scheduler_enabled'))
            try:
                review=review_schedule(store,profile['id'])
            except Exception as exc:
                review=dict(enabled=False,valid=False,state='needs_attention',label='Scheduling off — needs attention',issues=[str(exc)],fingerprint='')
            if not schedule_required:
                review.update(label='Scheduling off — not required for this operating behavior',valid=False,not_required=True)
            radios.append(dict(id=profile['id'],name=profile.get('name') or 'Radio',profile=profile,
                configuration_complete=bool(profile.get('name')),
                behavior_complete=bool(model),schedule_required=schedule_required,review=review))
        return dict(station=dict(callsign=text(kv.get('operator_callsign') or kv.get('callsign')),name=text(kv.get('operator_name')),timezone=text(kv.get('timezone'))),
                    groups=groups,radios=radios,
                    behavior_state='complete' if radios and all(row['behavior_complete'] for row in radios) else 'needs_attention',
                    schedule_state='not required' if radios and not any(row['schedule_required'] for row in radios)
                        else 'complete' if radios and all(not row['schedule_required'] or row['review'].get('valid') for row in radios) else 'needs_attention')
