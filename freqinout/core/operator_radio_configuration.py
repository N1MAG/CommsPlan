"""Atomic operator radio edits, using the existing typed stores and draft store."""
from copy import deepcopy
from uuid import uuid4
import json
from freqinout.core.configuration_edit_session import (
    ConfigEditSession, ConfigurationConflict, load_unfinished_configuration, save_configuration_edit, retain_configuration_edit,
)
from freqinout.core.multi_radio_store import COMMON_AMATEUR_BANDS, _normalize_band_token, SUPPORTED_DEVICE_CONTROL_BACKENDS, SUPPORTED_DEVICE_CLASSES
from freqinout.core.software_identity_bundle import build_guided_identity_records
from freqinout.core.operator_application_configuration import (
    application_issues, load_operator_application_context, save_operator_application, LINKS,
    validate_js8_native_profile_claims,
    launch_configuration_token,
)

RADIO_FIELDS = frozenset(('name','radio_catalog_id','radio_manufacturer','radio_model','device_class','control_backend','flrig_host','flrig_port',
    'rig_host','rig_port',
    'antenna_group','antenna_supported_bands','notes','rf_conflict_group','ptt_group','sdr_host','sdr_port',
    'sdr_application','sdr_adapter','sdr_target','sdr_control_enabled',
    'sdr_verification_state','sdr_verification_json',
    'use_flrig','use_fldigi','use_flmsg','use_flamp','use_js8call','use_varac','use_js8spotter',
    'use_external_js8spotter','use_commstat'))


def receiver_launch_selection(bundle):
    """Project the editor-owned fields without adopting any runtime authority."""
    row = next((i for i in bundle.get('items', ()) if i.get('instance_key') == 'receiver:sdrpp'), {})
    return dict(name='SDR++', enabled=bool(row.get('enabled', False)),
                startup=bool(row.get('launch_at_startup', False)),
                launch_path_override=str(row.get('path_override') or ''),
                launch_command_override=str(row.get('command_override') or ''))


def _save_receiver_launch(conn, store, radio_id, selection, *, enable_startup=False):
    # Update only the canonical row; peer slots and extension policy are untouched.
    from freqinout.core.receiver_software_stack import build_receiver_launch_items
    item = build_receiver_launch_items({'device_class': 'observer'}, [selection])[0]
    conn.execute("""INSERT INTO radio_launch_bundles
        (radio_profile_id,schema_version,launch_enabled,migrated_from_legacy,updated_utc)
        VALUES (?,1,?,0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(radio_profile_id) DO UPDATE SET
        launch_enabled=CASE WHEN excluded.launch_enabled=1 THEN 1 ELSE launch_enabled END,
        updated_utc=excluded.updated_utc""", (radio_id, int(enable_startup)))
    row = conn.execute("SELECT readiness_json FROM radio_launch_bundle_items WHERE radio_profile_id=? AND instance_key='receiver:sdrpp'", (radio_id,)).fetchone()
    readiness = json.loads(row['readiness_json'] or '{}') if row else {}
    readiness.update(execution_scope='receive_only')
    readiness.setdefault('readiness', 'process')
    conn.execute("""INSERT INTO radio_launch_bundle_items
        (radio_profile_id,instance_key,app_name,display_order,enabled,launch_at_startup,
         monitor_health,command_override,path_override,dependencies_json,readiness_json,updated_utc)
        VALUES (?,'receiver:sdrpp','SDR++',
        (SELECT COALESCE(MAX(display_order),-1)+1 FROM radio_launch_bundle_items WHERE radio_profile_id=?),
        ?,?,1,?,?,'[]',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(radio_profile_id,instance_key) DO UPDATE SET
        enabled=excluded.enabled,launch_at_startup=excluded.launch_at_startup,
        command_override=excluded.command_override,path_override=excluded.path_override,
        readiness_json=excluded.readiness_json,updated_utc=excluded.updated_utc""",
        (radio_id,radio_id,int(item['enabled']),int(item['startup']),
         item['launch_command_override'],item['launch_path_override'],json.dumps(readiness)))
    if receiver_launch_selection(store.get_radio_launch_bundle(radio_id)) != selection:
        raise ValueError('SDR++ launch settings readback did not match. Radio changes were not applied.')


def normalize_supported_band(value):
    token = _normalize_band_token(value)
    return token + 'M' if token + 'M' in COMMON_AMATEUR_BANDS else token


def load_operator_radio_context(store, radio_id=None):
    """Worker-side bounded coherent load; never adopts saved unfinished work."""
    with store.configuration_read_transaction():
        profile = store.get_device_profile(int(radio_id)) if radio_id else {}
        if radio_id and profile is None:
            raise ConfigurationConflict('The selected radio no longer exists.')
        profile = dict(profile or {})
        apps = {}
        for family in LINKS:
            if radio_id:
                loaded = load_operator_application_context(store,{'radio_id':radio_id,'family_key':family})
                apps[family] = {**loaded['current_values'],**loaded['context']}
                apps[family]['_unfinished_application'] = load_unfinished_configuration(
                    store, f'application:{radio_id}:{family}')
        assignment = store.get_effective_assignment_for_device(int(radio_id)) if radio_id else None
        plan = store.get_effective_assigned_plan_for_device(int(radio_id)) if radio_id else None
        profile['antenna_supported_bands'] = ', '.join(json.loads(profile.get('antenna_supported_bands_json') or '[]'))
        profile['_operating_profile_id'] = int((assignment or {}).get('operating_profile_id') or 0)
        profile['_schedule_plan_id'] = int((plan or {}).get('frequency_plan_id') or 0)
        launch_bundle = store.get_radio_launch_bundle(int(radio_id)) if radio_id else {}
        profile['_launch_snapshot'] = launch_configuration_token(launch_bundle) if radio_id else {}
        profile['_receiver_launch'] = receiver_launch_selection(launch_bundle)
        profile['_identity_generation'] = store.radio_software_identity_generation(int(radio_id)) if radio_id else 0
        scope = f'radio:{radio_id or "new"}'
        response = dict(profile=profile,application_seeds=apps,operating_profiles=store.list_operating_profiles(),
                        schedule_plans=store.list_frequency_plans(),unfinished=load_unfinished_configuration(store,scope))
    return response


def save_operator_radio(store,payload):
    # Runtime BBS/vault snapshots are not radio editor fields. Duplicating them
    # in base/proposed can exhaust the bounded unfinished-edit envelope.
    context_fields = {'id', *LINKS.values(), '_operating_profile_id', '_schedule_plan_id',
                      '_identity_generation', '_launch_snapshot', '_receiver_launch'}
    base = {key: deepcopy(value) for key, value in dict(payload.get('base') or {}).items()
            if key in RADIO_FIELDS or key in context_fields}
    if base.get('id') and '_launch_snapshot' in base:
        base['_launch_snapshot'] = launch_configuration_token(base['_launch_snapshot'])
    radio_id = int(base.get('id') or 0) or None
    raw_values = deepcopy(dict(payload.get('values') or {}))
    proposed = {**deepcopy(base), **{key:value for key,value in raw_values.items() if key in RADIO_FIELDS}}
    if isinstance(raw_values.get('__edited_fields__'), (list, tuple)):
        # Draft-only provenance: canonical writes remain RADIO_FIELDS-only.
        proposed['__edited_fields__'] = list(raw_values['__edited_fields__'])
    for field in ('name', 'notes'):
        if field in proposed:
            proposed[field] = str(proposed[field] or '').strip()
    if 'sdr_verification_json' in proposed:
        from freqinout.core.multi_radio_store import _coerce_json_object_text
        proposed['sdr_verification_json'] = _coerce_json_object_text(proposed['sdr_verification_json'])
    for field in ('flrig_port','rig_port','sdr_port'):
        if proposed.get(field) not in ('',None):
            try:
                proposed[field] = int(proposed[field])
            except (ValueError,TypeError):
                pass
    for field in ('flrig_host','rig_host','sdr_host','flrig_port','rig_port','sdr_port'):
        if proposed.get(field) == '' and base.get(field) in ('',None):
            if field in base:
                proposed[field] = base[field]
            else:
                proposed.pop(field,None)
    band_raw = str(proposed.get('antenna_supported_bands') or '')
    band_tokens = list(dict.fromkeys(normalize_supported_band(item) for item in band_raw.split(',') if item.strip()))
    invalid_bands = [band for band in band_tokens if band not in COMMON_AMATEUR_BANDS] if band_raw != str(base.get('antenna_supported_bands') or '') else []
    if not invalid_bands and 'antenna_supported_bands' in proposed:
        proposed['antenna_supported_bands'] = ', '.join(band_tokens)
    proposed['_operating_profile_id'] = int(payload.get('operating_profile_id',base.get('_operating_profile_id',0)) or 0)
    proposed['_schedule_plan_id'] = int(payload.get('schedule_plan_id',base.get('_schedule_plan_id',0)) or 0)
    proposed['_identity_generation'] = base.get('_identity_generation',0)
    selection = payload.get('receiver_launch')
    if selection is not None:
        proposed['_receiver_launch'] = deepcopy(selection)
    drafts = deepcopy(dict(payload.get('application_drafts') or {}))
    if (proposed.get('control_backend') == 'flrig' and proposed.get('use_flrig')
            and not proposed.get('fast_light_config_id') and 'fast_light' not in drafts
            and (radio_id is None or proposed.get('control_backend') != base.get('control_backend')
                 or proposed.get('use_flrig') != base.get('use_flrig'))
            and not any(proposed.get(f'use_{name}') for name in ('fldigi', 'flmsg', 'flamp'))):
        # The Radio page already reviewed this control endpoint. FLRig may be
        # operator-started without an executable/launcher or a sibling app.
        drafts['fast_light'] = {'operator_base_values': {}, 'operator_values': {
            'flrig_enabled': True, 'fldigi_enabled': False, 'flmsg_enabled': False, 'flamp_enabled': False,
            'host': str(raw_values.get('flrig_host') or '') if 'flrig_host' in raw_values else str(proposed.get('flrig_host', '127.0.0.1')),
            'port': str(raw_values.get('flrig_port') or '') if 'flrig_port' in raw_values else str(proposed.get('flrig_port', 12345)),
            'flrig_launch_control': 'operator', 'flrig_launch_at_startup': False,
            'settings_identity_mode': 'default'}}
    base['_application_drafts'] = {}
    proposed['_application_drafts'] = drafts
    scope = f'radio:{radio_id or "new"}'
    envelope = load_unfinished_configuration(store,scope)
    session = ConfigEditSession(base,draft_revision=envelope['revision'],watch_fields=('_identity_generation','_launch_snapshot'))
    session.session_id = str(payload.get('operator_session_id') or uuid4().hex)
    session.recovered_from = payload.get('operator_recovered_from')
    replacement = payload.get('replace_saved_draft')
    if replacement:
        previous = envelope.get('payload') or {}
        if (not isinstance(replacement, dict) or replacement.get('revision') != envelope['revision']
                or replacement.get('session_id') != previous.get('session_id')):
            raise ConfigurationConflict('The previous draft changed. Review it before replacing it.')
        session.recovered_from = previous.get('session_id')
    if payload.get('operator_draft_revision') is not None and not replacement:
        session.draft_revision = payload['operator_draft_revision']
    session.values = proposed
    if payload.get('keep_draft_only'):
        return {'result': retain_configuration_edit(store, scope, session.snapshot()), 'profile': None}
    published = False
    saved_profile = None

    def read(conn):
        if published:
            return {**proposed,**{key:saved_profile.get(key) for key in RADIO_FIELDS if key in proposed},
                    '_receiver_launch':receiver_launch_selection(store.get_radio_launch_bundle(radio_id))}
        if radio_id is None:
            return deepcopy(base)
        current = store.get_device_profile(radio_id)
        if not current:
            raise ConfigurationConflict('The radio was removed; retain this draft and review the target.')
        current['antenna_supported_bands'] = ', '.join(json.loads(current.get('antenna_supported_bands_json') or '[]'))
        assignment=store.get_effective_assignment_for_device(radio_id)
        plan=store.get_effective_assigned_plan_for_device(radio_id)
        return {**base,**current,
                '_operating_profile_id':int((assignment or {}).get('operating_profile_id') or 0),
                '_schedule_plan_id':int((plan or {}).get('frequency_plan_id') or 0),
                '_identity_generation':store.radio_software_identity_generation(radio_id),
                '_launch_snapshot':launch_configuration_token(store.get_radio_launch_bundle(radio_id)),
                '_receiver_launch':receiver_launch_selection(store.get_radio_launch_bundle(radio_id))}

    corrections = []
    def validate(values):
        issues=[]
        corrections.clear()
        def radio_issue(message, field):
            issues.append(message)
            corrections.append({'family':'radio','field':field,'message':message})
        if selection is not None:
            try:
                from freqinout.core.receiver_software_stack import build_receiver_launch_items
                if not isinstance(selection, dict) or selection.get('name') != 'SDR++':
                    raise ValueError('Review the SDR++ launch settings.')
                if selection.get('enabled') and not (selection.get('launch_path_override') or selection.get('launch_command_override')):
                    raise ValueError('Choose an SDR++ application path or launch command, or turn off inclusion in Launch Control.')
                if selection.get('startup') and not selection.get('enabled'):
                    raise ValueError('Include SDR++ in Launch Control before selecting startup.')
                build_receiver_launch_items(values, [selection])
            except (ValueError, TypeError) as exc:
                radio_issue(str(exc), '_receiver_launch')
        if invalid_bands:
            radio_issue('Review supported antenna bands on the Radio step. Unrecognized saved values: ' + ', '.join(invalid_bands), 'antenna_supported_bands')
        if values.get('control_backend','manual') not in SUPPORTED_DEVICE_CONTROL_BACKENDS:
            issues.append('Choose a supported radio control application.')
        if values.get('device_class','tx_rx') not in SUPPORTED_DEVICE_CLASSES:
            issues.append('Choose a supported radio role.')
        if values.get('device_class') == 'observer' and (values.get('use_varac') or values.get('use_flrig')):
            issues.append('Observer radios cannot enable VarAC or FLRig transmit control.')
        if not str(values.get('name') or '').strip():
            radio_issue('Enter a radio name.', 'name')
        for field in ('flrig_port','rig_port','sdr_port'):
            if values.get(field) not in ('',None):
                try:
                    if not 1 <= int(values[field]) <= 65535:
                        raise ValueError
                except (ValueError,TypeError):
                    radio_issue(f'Enter a valid {field.replace("_"," ")}.', field)
        for family,link in LINKS.items():
            selected = bool(values.get('use_js8call' if family == 'js8call' else 'use_varac')) if family != 'fast_light' else any(values.get(f'use_{key}') for key in ('flrig','fldigi','flmsg','flamp'))
            draft = drafts.get(family)
            if draft:
                if radio_id:
                    unfinished = load_unfinished_configuration(store, f'application:{radio_id}:{family}')
                    previous = unfinished.get('payload') or {}
                    replacement = draft.get('replace_saved_draft') or {}
                    resolves_previous = (previous.get('session_id') in
                                         {draft.get('operator_session_id'), draft.get('operator_recovered_from')}
                                         or (replacement.get('revision') == unfinished['revision']
                                             and replacement.get('session_id') == previous.get('session_id')))
                    if previous and not resolves_previous:
                        message = ('Previous unfinished application entries are available. Open this application, '
                                   'then choose Recover or Use current entries instead before saving the radio.')
                        issues.append(message)
                        corrections.append({'family':family,'field':None,'message':message})
                family_corrections = []
                issues.extend(application_issues(family,draft['operator_values'], corrections=family_corrections))
                corrections.extend({'family': family, **item} for item in family_corrections)
            elif selected and not values.get(link) and (radio_id is None or not any(base.get(flag) for flag in (('use_flrig','use_fldigi','use_flmsg','use_flamp') if family == 'fast_light' else ('use_js8call',) if family == 'js8call' else ('use_varac',)))):
                issues.append(f'Choose Edit {family.replace("_"," ")} to enter its application settings.')
        return tuple(issues)

    def publish(conn,values):
        nonlocal saved_profile,published,radio_id
        changes={key:value for key,value in values.items() if key in RADIO_FIELDS and (key not in base or value != base[key])}
        # Save only reviewed fields; resolved app proxies and startup/model
        # snapshots must not replay into typed radio/app columns.
        if radio_id:
            changes['id']=radio_id
        else:
            changes.update(system_key=f'operator_radio_{uuid4().hex}',scheduler_enabled=0,runtime_active=0)
        if 'antenna_supported_bands' in changes:
            changes['antenna_supported_bands'] = [item.strip() for item in str(changes['antenna_supported_bands']).split(',') if item.strip()]
        saved_profile=store.save_device_profile(changes)
        radio_id=int(saved_profile['id'])
        old_model=int(base.get('_operating_profile_id') or 0)
        model=int(values['_operating_profile_id'])
        if model != old_model:
            if model:
                store.set_device_operating_profile(radio_id,model)
            else:
                conn.execute("UPDATE operating_profile_assignments SET assignment_state='superseded' WHERE device_profile_id=? AND assignment_state IN ('active','temporary_override')",(radio_id,))
        old_plan=int(base.get('_schedule_plan_id') or 0)
        plan=int(values['_schedule_plan_id'])
        if plan != old_plan:
            if plan:
                store.set_assigned_plan(radio_id,plan,reason='Selected explicitly in radio setup; scheduling requires Review & enable.')
            else:
                conn.execute("UPDATE assigned_plans SET assignment_state='superseded' WHERE device_profile_id=? AND assignment_state IN ('active','temporary_override')",(radio_id,))
        for family,draft in drafts.items():
            request={**draft,'radio_id':radio_id,'family_key':family}
            # Other family writes in this one outer transaction increment the
            # set generation. The outer edit's watch already fenced all of it.
            request['expected_identity_generation']=store.radio_software_identity_generation(radio_id)
            # The complete outer launch snapshot was compared under this write
            # transaction before any family wrote. Earlier siblings legitimately
            # change it, so use that now-current inner snapshot without dropping CAS.
            request['operator_launch_snapshot'] = launch_configuration_token(store.get_radio_launch_bundle(radio_id))
            result=save_operator_application(store,request)
            if result.state != 'saved':
                detail = ' '.join(result.issues) or 'Review the application entries.'
                corrections.append({'family':family,'field':None,'message':detail})
                raise ValueError(f'{family.replace("_", " ")}: {detail} Radio changes were not applied.')
        # Explicit radio Use selections remain independent of app/startup state.
        flags={key:int(bool(values[key])) for key in RADIO_FIELDS if key.startswith('use_') and key in values}
        if flags:
            store.save_device_profile({'id':radio_id, **flags})
        # Preserve configured application identities independently of Use flags.
        # The explicit flags below own runtime/launch availability.
        effective=store.get_device_profile(radio_id)
        if effective.get('use_js8call') and (not base.get('use_js8call') or effective.get('js8_instance_id') != base.get('js8_instance_id')):
            loaded = load_operator_application_context(store, {'radio_id':radio_id, 'family_key':'js8call'})
            validate_js8_native_profile_claims(store, conn, radio_id, loaded['current_values'])
        retained={record.family_key:record for record in store.list_radio_software_identity_records(radio_id)}
        selected={'js8call':bool(effective.get('use_js8call')),
                  'fast_light':any(effective.get(f'use_{name}') for name in ('flrig','fldigi','flmsg','flamp')),
                  'varac':bool(effective.get('use_varac')),'fio_spotter':bool(effective.get('use_js8spotter')),
                  'commstat':bool(effective.get('use_commstat')),
                  'external_js8spotter':bool(effective.get('use_external_js8spotter'))}
        for family,enabled in selected.items():
            if not enabled:
                # Stored application identity survives an availability toggle.
                if family not in LINKS:
                    retained.pop(family,None)
            elif family not in retained:
                if family in LINKS and effective.get(LINKS[family]):
                    loaded=load_operator_application_context(store,{'radio_id':radio_id,'family_key':family})
                    with store.connect() as app_conn:
                        from freqinout.core.operator_application_configuration import TABLES
                        row=dict(app_conn.execute(f'SELECT * FROM {TABLES[family]} WHERE id=?',(effective[LINKS[family]],)).fetchone())
                    manifest=store.get_software_instance_manifest(f"{family}:{row['system_key']}") or {}
                    recipe=(manifest.get('evidence') or {}).get('launch_recipe',{})
                    draft={**loaded['current_values'],'instance_key':f"{family}:{row['system_key']}",'mode':'manual','launch_recipe':recipe}
                    retained[family]=build_guided_identity_records(effective,{family:draft},(family,))[0]
                elif family not in LINKS:
                    retained[family]=build_guided_identity_records(effective,{},(family,))[0]
        current_records=store.list_radio_software_identity_records(radio_id)
        if {record.family_key:record for record in current_records} != retained:
            store.save_radio_software_identity_records(radio_id,tuple(retained.values()),
                expected_generation=store.radio_software_identity_generation(radio_id))
        component_flags={'JS8Call':'use_js8call','VarAC':'use_varac','FLRig':'use_flrig','FLDigi':'use_fldigi','FLMsg':'use_flmsg','FLAmp':'use_flamp','JS8Spotter':'use_external_js8spotter','CommStat':'use_commstat'}
        for name,flag in component_flags.items():
            family = 'js8call' if name == 'JS8Call' else 'varac' if name == 'VarAC' else 'fast_light' if name in {'FLRig','FLDigi','FLMsg','FLAmp'} else None
            if (family not in drafts and bool(effective.get(flag)) == bool(base.get(flag))
                    and (family not in LINKS or effective.get(LINKS[family]) == base.get(LINKS[family]))):
                continue
            record = retained.get(family)
            if record is not None:
                prefix = record.bundle_id + ':'
                conn.execute('''UPDATE radio_launch_bundle_items
                    SET enabled=CASE WHEN substr(instance_key,1,?)=? THEN ? ELSE 0 END,
                        launch_at_startup=CASE WHEN substr(instance_key,1,?)=? THEN launch_at_startup ELSE 0 END
                    WHERE radio_profile_id=? AND app_name=?''',
                             (len(prefix),prefix,int(bool(effective.get(flag))),len(prefix),prefix,radio_id,name))
            else:
                conn.execute('UPDATE radio_launch_bundle_items SET enabled=? WHERE radio_profile_id=? AND app_name=?',(int(bool(effective.get(flag))),radio_id,name))
        if selection is not None:
            _save_receiver_launch(conn, store, radio_id, values['_receiver_launch'],
                                  enable_startup=bool(selection.get('startup') and not (base.get('_receiver_launch') or {}).get('startup')))
        # A radio save never opts into scheduling. Material changes revoke its
        # intent; the separate Review & enable transaction owns reenabling.
        if (set(changes) - {'id','name','notes','radio_catalog_id','radio_manufacturer','radio_model'}) or drafts or model != old_model or plan != old_plan:
            conn.execute('UPDATE device_profiles SET scheduler_enabled=0 WHERE id=?',(radio_id,))
        saved_profile=store.get_device_profile(radio_id)
        saved_profile['antenna_supported_bands'] = ', '.join(json.loads(saved_profile.get('antenna_supported_bands_json') or '[]'))
        actual_model=store.get_effective_assignment_for_device(radio_id)
        actual_plan=store.get_effective_assigned_plan_for_device(radio_id)
        if int((actual_model or {}).get('operating_profile_id') or 0) != model or int((actual_plan or {}).get('frequency_plan_id') or 0) != plan:
            raise ValueError('Saved operating model or schedule readback did not match.')
        published=True

    original_radio_id = radio_id
    try:
        result=save_configuration_edit(store,scope,session.snapshot(),read_current=read,validate=validate,publish=publish)
    except ConfigurationConflict:
        raise
    except ValueError as exc:
        # The failed atomic publication has rolled back. Retain the reviewed
        # raw draft in a separate transaction, never a provisional radio ID.
        radio_id, saved_profile, published = original_radio_id, None, False
        issues = (str(exc),)
        result=save_configuration_edit(store,scope,session.snapshot(),read_current=read,
                                       validate=lambda _values: issues,publish=publish)
    return {'result':result,'corrections':corrections,'profile':saved_profile if result.state == 'saved'
            else (store.get_device_profile(original_radio_id) if original_radio_id else None)}
