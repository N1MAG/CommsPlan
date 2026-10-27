"""Operator-selected application settings. CP database only; no native writes.

Invoked on a worker-owned store. Existing transactions/identity projection remain
sole runtime authority; unfinished raw values use the shared edit-session store.
"""
from __future__ import annotations
from copy import deepcopy
import platform
from pathlib import Path
import shlex
import json
import hashlib
from uuid import uuid4
from typing import Any, Mapping

from freqinout.core.multi_radio_store import _normalize_system_key
from freqinout.core.configuration_edit_session import (
    ConfigEditSession, ConfigurationConflict, load_unfinished_configuration,
    save_configuration_edit,
)
from freqinout.core.software_identity_bundle import (
    build_guided_identity_records, identity_record_from_mapping, identity_record_to_mapping,
)
from freqinout.core.varac_launch_recipe import legacy_varac_structured_launch
from freqinout.core.js8_storage import normalize_rig_name, rig_name_collision_key

LINKS = {'js8call': 'js8_instance_id', 'fast_light': 'fast_light_config_id', 'varac': 'varac_node_id'}
TABLES = {'js8call': 'js8_instances', 'fast_light': 'fast_light_configs', 'varac': 'varac_nodes'}
FIELDS = {
    'js8call': {'application_path':'install_path','configuration_path':'profile_path',
                'storage_path':'application_data_root','rig_name':'rig_name','host':'host','port':'port',
                'instance_name':'name','offset_hz':'offset_hz','directed_path':'directed_path','all_path':'all_path',
                'inbox_path':'inbox_path','forms_path':'forms_path'},
    'fast_light': {'application_path':'flrig_path','flrig_application_path':'flrig_path',
                   'fldigi_application_path':'fldigi_path','secondary_application_path':'fldigi_path',
                   'host':'flrig_host','port':'flrig_port','secondary_port':'fldigi_port',
                   'fldigi_host':'fldigi_host',
                   'storage_path':'fldigi_log_path','secondary_storage_path':'fldigi_checkin_dir','instance_name':'name'},
    'varac': {'application_path':'install_path','configuration_path':'ini_path','storage_path':'db_path',
              'launch_command':'launch_cmd','vara_runtime_path':'vara_runtime_path','vara_ini_path':'vara_ini_path',
              'secondary_storage_path':'incoming_path','instance_name':'name'},
}
VARAC_PROFILE_PATHS = (('outbox_path', 'varac_outbox_dir'), ('bbs_path', 'varac_bbs_dir'),
                       ('bbs_archive_path', 'varac_bbs_archive_dir'))
# Operator fields and the claims consumed by Fast Light adoption/inventory.
# Keep explicit clears distinct from omitted fields in a component-scoped edit.
FAST_LIGHT_PATH_CLAIMS = (
    ('flrig_configuration_path', 'flrig_configuration', True),
    ('fldigi_configuration_path', 'fldigi_configuration', True),
    ('flmsg_configuration_path', 'flmsg_root', True),
    ('flamp_configuration_path', 'flamp_root', False),
    ('storage_path', 'fldigi_logs', True),
    ('secondary_storage_path', 'fldigi_checkins', True),
    ('flmsg_message_path', 'flmsg_messages', True),
    ('flmsg_templates_path', 'flmsg_templates', True),
    ('flmsg_auto_path', 'flmsg_auto', True),
    ('flamp_receive_path', 'flamp_receive', False),
    ('flamp_outgoing_path', 'flamp_outgoing', False),
)

def application_manifest(store, family, row):
    """Find an application's saved manifest by owner, including guided keys."""
    matches = [manifest for manifest in store.list_software_instance_manifests()
               if manifest.get('family_key') == family
               and manifest.get('application_system_key') == row['system_key']]
    if len(matches) > 1:
        raise ConfigurationConflict('Multiple saved identities refer to this application; review Manage instances.')
    return matches[0] if matches else None


def application_editor_values(family: str, row: Mapping[str, Any], manifest=None, *, profile=None) -> dict:
    """Cache-only projection. Actual typed columns override old editor evidence."""
    evidence = (manifest or {}).get('evidence', {}) or {}
    values = deepcopy(dict(evidence.get('operator_values', {}) or {}))
    if manifest:
        from freqinout.core.configuration_attention import _saved_launch_control
        choice = _saved_launch_control({**values, 'launch_recipe': evidence.get('launch_recipe'),
                                        'management_mode': manifest.get('management_mode')})
        if choice:
            values.setdefault('launch_control', choice)
        if family == 'fast_light':
            recipe = evidence.get('launch_recipe') or {}
            parts = recipe.get('components', ()) if isinstance(recipe, Mapping) else ()
            for part in parts:
                if not isinstance(part, Mapping):
                    continue
                key = part.get('component_key')
                if key in {'flrig', 'fldigi', 'flmsg', 'flamp'}:
                    choice = _saved_launch_control({'launch_recipe': {'components': [part]}})
                    if choice:
                        values.setdefault(f'{key}_launch_control', choice)
    authoritative = evidence.get('operator_values_version') == 1
    for key, column in FIELDS[family].items():
        value = row.get(column)
        if value not in (None, '') or authoritative or not values.get(key):
            values[key] = deepcopy(value if value is not None else '')
    for key in ('port', 'secondary_port'):
        if key in values:
            values[key] = str(values[key]) if values[key] is not None else ''
    if family == 'js8call':
        values['settings_identity_mode'] = 'isolated' if values.get('rig_name') else 'default'
        if evidence.get('settings_identity_mode') == 'custom_launcher':
            values['settings_identity_mode'] = 'custom_launcher'
    if family == 'varac' and profile is not None:
        # These paths belong to the radio/cluster projection, not varac_nodes
        # or historical manifest editor evidence. Load what Save reads back.
        for field, column in VARAC_PROFILE_PATHS:
            values[field] = str(profile.get(column) or '')
    if family == 'fast_light':
        claims = {claim.get('kind'): claim.get('value') for claim in
                  (manifest or {}).get('resource_claims', ()) or ()}
        for field, kind, _exclusive in FAST_LIGHT_PATH_CLAIMS:
            # FLDigi logs/check-ins already come from typed application columns.
            if field not in FIELDS[family] and kind in claims:
                values[field] = str(claims[kind] or '')
        if profile is not None:
            for field, column in (('flmsg_message_path', 'flmsg_message_path'),
                                  ('flamp_receive_path', 'flamp_message_path')):
                values[field] = str(profile.get(column) or '')
    return values


JS8_REVIEW_PATHS = ('configuration_path', 'storage_path', 'directed_path', 'all_path', 'inbox_path')


def js8_profile_path_review_token(values):
    """Bind explicit review to this native identity and these exact read paths."""
    selected = {key: values.get(key, '') for key in ('settings_identity_mode', 'rig_name', 'launch_command', 'custom_argv', *JS8_REVIEW_PATHS)}
    if selected['settings_identity_mode'] != 'isolated':
        selected['rig_name'] = ''
    if selected['settings_identity_mode'] != 'custom_launcher':
        selected['launch_command'] = ''
        selected['custom_argv'] = ''
    return hashlib.sha256(json.dumps(selected, sort_keys=True, default=str).encode()).hexdigest()


def js8_profile_paths_need_review(base, values):
    identity = lambda data: (data.get('settings_identity_mode', 'default'),
                             str(data.get('rig_name') or '').strip() if data.get('settings_identity_mode') == 'isolated' else '',
                             str(data.get('launch_command') or '') if data.get('settings_identity_mode') == 'custom_launcher' else '',
                             str(data.get('custom_argv') or '') if data.get('settings_identity_mode') == 'custom_launcher' else '')
    return bool(base and identity(base) != identity(values)
                and any(base.get(key) and values.get(key) == base.get(key) for key in JS8_REVIEW_PATHS))


def application_issues(family: str, values: Mapping[str, Any], *, corrections=None) -> tuple[str, ...]:
    issues = []
    def add(message, field=None):
        issues.append(message)
        if corrections is not None:
            corrections.append({'field': field, 'message': message})
    if family == 'js8call' or (family == 'fast_light' and values.get('flrig_enabled')):
        if not str(values.get('host') or '').strip():
            add('Enter a JS8Call API host.' if family == 'js8call' else 'Enter an FLRig control host.', 'host')
        try:
            if not 1 <= int(values.get('port') or '') <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            add('Enter a JS8Call TCP API port from 1 to 65535.' if family == 'js8call' else 'Enter an FLRig control port from 1 to 65535.', 'port')
    if family == 'js8call' and values.get('settings_identity_mode') == 'isolated' and not str(values.get('rig_name') or '').strip():
        add('Enter the native JS8Call profile name, or select Default profile.', 'rig_name')
    if family != 'fast_light' and values.get('launch_at_startup') and values.get('launch_control', 'cp') != 'cp':
        add('Choose CommsPlan launches before enabling application startup.', 'launch_control')
    if family != 'fast_light' and values.get('launch_control') == 'cp' and not (values.get('application_path') or values.get('launch_command')):
        add('Choose the application or launcher for CommsPlan to launch.', 'application_path')
    if family == 'varac' and not str(values.get('configuration_path') or '').strip():
        add('Choose the existing VarAC INI file.', 'configuration_path')
    if family == 'fast_light':
        if not any(values.get(f'{name}_enabled') for name in ('flrig','fldigi','flmsg','flamp')):
            add('Choose at least one Fast Light component.')
        if values.get('fldigi_enabled') and values.get('secondary_port') in ('', None):
            add('Enter an FLDigi API port from 1 to 65535.', 'secondary_port')
        for name in ('flrig','fldigi','flmsg','flamp'):
            if values.get(f'{name}_enabled') and values.get(f'{name}_launch_at_startup') and not values.get(f'{name}_application_path'):
                add(f'Choose the {name.upper()} executable before enabling startup.', f'{name}_application_path')
            if values.get(f'{name}_enabled') and values.get('settings_identity_mode') == 'separatefolder' and not values.get(f'{name}_configuration_path'):
                add(f'Choose the {name.upper()} settings folder.', f'{name}_configuration_path')
    for field in ('secondary_port', 'offset_hz'):
        if values.get(field) not in ('', None):
            try:
                number = int(values[field])
                if (field == 'secondary_port' and not 1 <= number <= 65535) or (field == 'offset_hz' and number <= 0):
                    raise ValueError
            except (TypeError, ValueError):
                add(f'Enter a valid whole number for {field.replace("_", " ")}.', field)
    if family == 'js8call' and values.get('settings_identity_mode') == 'custom_launcher' and not (values.get('launch_command') or values.get('application_path')):
        add('Choose or enter the existing launcher command.', 'launch_command')
    if family == 'fast_light' and values.get('launch_command'):
        add('Choose each Fast Light executable separately; a family launcher cannot identify its components.')
    try:
        operator_launch_recipe(family, values)
    except (ValueError, TypeError) as exc:
        add(str(exc))
    return tuple(issues)


def operator_launch_recipe(family: str, values: Mapping[str, Any], *, platform_name=None) -> dict:
    """One exact reviewed argv, shared by canonical launch and display projection."""
    startup = bool(values.get('launch_at_startup'))
    def component(key, exe, args=(), *, cwd='', env=None, selector=''):
        component_startup = bool(values.get(f'{key}_launch_at_startup', startup)) if family == 'fast_light' else startup
        control = values.get(f'{key}_launch_control', 'cp' if component_startup else 'operator') if family == 'fast_light' else values.get('launch_control', 'cp' if startup else 'operator')
        return dict(component_key=key, executable=str(exe or ''), arguments=list(args),
                    working_directory=cwd or str(values.get('working_directory') or ''),
                    environment=env or {}, profile_selector=selector, operator_starts=control != 'cp',
                    launch_at_startup=component_startup, monitor_health=True,
                    execution_scope='remote' if control == 'remote' else 'receive_only' if values.get('radio_role') == 'observer' else 'standard',
                    readiness={'kind':'process','operator_recipe_v1':True,'launch_control':control,'local_expected':control != 'remote',
                               'settings_identity_mode':values.get('settings_identity_mode','default'),
                               'rig_name':selector if family == 'js8call' else '',
                               'launch_arguments':list(args),'profile_selector':selector,
                               **({'process_executable':str(values.get('process_application_path') or next((str(arg) for arg in (exe, *args) if str(arg).replace('\\','/').casefold().endswith('/varac.exe') or str(arg).casefold() == 'varac.exe'), '') or (str(values.get('application_path') or '') if not Path(str(values.get('application_path') or '')).suffix else '')),
                                   'process_ini':str(values.get('configuration_path') or ''),
                                   'wine_prefix':str((env or {}).get('WINEPREFIX') or values.get('wine_prefix') or '')} if family == 'varac' else {})})
    custom = str(values.get('launch_command') or '').strip()
    if custom and (family != 'js8call' or values.get('settings_identity_mode') == 'custom_launcher'):
        chosen_executable = str(values.get('application_path') or '')
        if chosen_executable and (custom == chosen_executable or custom.startswith(chosen_executable + ' ')):
            argv = [chosen_executable, *shlex.split(custom[len(chosen_executable):].strip(), posix=(platform_name or platform.system()) != 'Windows')]
        else:
            argv = shlex.split(custom, posix=(platform_name or platform.system()) != 'Windows')
        if not argv:
            raise ValueError('The custom launcher command is empty.')
        argv += shlex.split(str(values.get('custom_argv') or ''), posix=(platform_name or platform.system()) != 'Windows')
        parts = [component(family, argv[0], argv[1:])]
    elif family == 'js8call':
        name = normalize_rig_name(values.get('rig_name') or '') if values.get('settings_identity_mode') == 'isolated' else ''
        args = ['--rig-name', name] if name else []
        args += shlex.split(str(values.get('custom_argv') or ''))
        if values.get('settings_identity_mode') == 'default' and any(a in {'-r','--rig-name'} or a.startswith(('--rig-name=', '-r=')) for a in args):
            raise ValueError('Default profile cannot include a named-profile argument.')
        if name and sum(1 for arg in args if arg in {'-r','--rig-name'} or arg.startswith(('--rig-name=', '-r='))) != 1:
            raise ValueError('The named profile is chosen above; remove duplicate profile arguments.')
        parts = [component('js8call', values.get('application_path'), args, selector=name)]
    elif family == 'fast_light':
        parts = []
        for key in ('flrig','fldigi','flmsg','flamp'):
            if not values.get(f'{key}_enabled'):
                continue
            root = str(values.get(f'{key}_configuration_path') or '')
            args = ['--config-dir', root] if values.get('settings_identity_mode') == 'separatefolder' else []
            parts.append(component(key, values.get(f'{key}_application_path') or (values.get('application_path') if key == 'flrig' else ''), args, selector=root if args else ''))
    else:
        launch = legacy_varac_structured_launch(
            {'install_path':values.get('application_path'), 'ini_path':values.get('configuration_path')},
            platform_name=platform_name or platform.system(), wine_executable=str(values.get('wine_runner') or 'wine'))
        if launch:
            env = dict(launch['environment'])
            if values.get('wine_prefix') and str(values['wine_prefix']).rstrip('/') != str(env.get('WINEPREFIX','')).rstrip('/'):
                raise ValueError('The selected Wine prefix does not match the executable/INI paths. Choose paths inside that prefix or an existing launcher.')
            parts = [component('varac',launch['executable'],launch['launch_arguments'],cwd=launch['working_directory'],env=env,selector=str(values.get('configuration_path') or ''))]
        elif startup or values.get("launch_control") == "cp":
            raise ValueError('Choose an existing launcher, or a VarAC INI inside its Wine prefix.')
        else:
            parts = [component('varac',values.get('application_path'))]
    return {'status':'ready_with_warnings', 'components':parts,
            'summary':'Operator-selected launch; live connection has not been verified.'}


def validate_js8_native_profile_claims(store, conn, radio_id, values):
    """Shared worker-only guard for application edits and radio re-enablement."""
    if values.get('launch_control') == 'remote' or str(values.get('host') or '').strip().casefold() not in {'localhost', '127.0.0.1', '::1', '[::1]'}:
        return
    recipe = operator_launch_recipe('js8call', values)
    if values.get('settings_identity_mode') == 'custom_launcher':
        from freqinout.core.station_launch_planner import StationLaunchPlanner
        native_name = StationLaunchPlanner._rig_name_from_command(shlex.join([recipe['components'][0]['executable'], *recipe['components'][0]['arguments']]))
    else:
        native_name = values.get('rig_name') if values.get('settings_identity_mode') == 'isolated' else ''
    for other in conn.execute('SELECT j.*, d.name AS radio_name FROM js8_instances j JOIN device_profiles d ON d.js8_instance_id=j.id WHERE d.id<>? AND d.use_js8call=1', (radio_id,)).fetchall():
        other = dict(other)
        if str(other.get('host') or '127.0.0.1').strip().casefold() not in {'localhost', '127.0.0.1', '::1', '[::1]'}:
            continue
        other_manifest = application_manifest(store, 'js8call', other)
        other_values = application_editor_values('js8call', other, other_manifest)
        other_mode = (other_manifest or {}).get('management_mode')
        if other_mode == 'remote':
            continue
        other_name = other_values.get('rig_name', '')
        if other_values.get('settings_identity_mode') == 'custom_launcher':
            from freqinout.core.station_launch_planner import StationLaunchPlanner
            other_component = operator_launch_recipe('js8call', other_values)['components'][0]
            other_name = StationLaunchPlanner._rig_name_from_command(shlex.join([other_component['executable'], *other_component['arguments']]))
        if rig_name_collision_key(native_name) == rig_name_collision_key(other_name):
            raise ValueError(f"JS8Call native profile '{native_name or 'Default'}' is already assigned to {other['radio_name']}. Choose a different native profile or stop using that assignment.")


def _current_launch_values(store, radio_id, family, fallback=None):
    fallback = fallback or {}
    launch = store.get_radio_launch_bundle(radio_id)
    names = {'js8call':{'JS8Call'},'fast_light':{'FLRig','FLDigi','FLMsg','FLAmp'},'varac':{'VarAC','VARA'}}[family]
    items = [item for item in launch['items'] if item.get('app_name',item.get('name')) in names]
    values = {'launch_at_startup':any(bool(item.get('launch_at_startup',item.get('startup'))) for item in items)}
    records = store.list_radio_software_identity_records(radio_id)
    record = next((record for record in records if record.family_key == family),None)
    control = fallback.get('launch_control','operator') if record is None else 'remote' if record and record.management_mode == 'remote' else 'cp' if record and any(not item.launch.get('operator_starts', True) for item in record.components) else 'operator'
    values['launch_control'] = control
    values['mode'] = {'cp':'managed','remote':'remote','operator':'manual'}[control]
    if family == 'fast_light':
        for key,label in (('flrig','FLRig'),('fldigi','FLDigi'),('flmsg','FLMsg'),('flamp','FLAmp')):
            part = next((part for part in (record.components if record else ()) if part.component_id == key),None)
            values[f'{key}_launch_control'] = fallback.get(f'{key}_launch_control','operator') if record is None else 'cp' if part and not part.launch.get('operator_starts',True) else 'operator'
            values[f'{key}_launch_at_startup'] = any(bool(item.get('launch_at_startup',item.get('startup'))) for item in items if item.get('app_name',item.get('name')) == label)
    return launch,values


def load_operator_application_context(store, payload):
    family, radio_id = str(payload['family_key']), int(payload['radio_id'])
    with store.configuration_read_transaction():
        profile = store.get_device_profile(radio_id)
        if not profile:
            raise ConfigurationConflict('This radio no longer exists.')
        instance_id = int(profile.get(LINKS[family]) or 0) or None
        values = {}
        if instance_id:
            with store.connect() as conn:
                row = dict(conn.execute(f'SELECT * FROM {TABLES[family]} WHERE id=?',(instance_id,)).fetchone())
            manifest = application_manifest(store, family, row)
            values = application_editor_values(family,row,manifest, profile=profile)
        elif family == 'varac':
            values.update({field: str(profile.get(column) or '') for field, column in VARAC_PROFILE_PATHS})
        launch, launch_values = _current_launch_values(store,radio_id,family,values)
        values.update(launch_values)
        if family == 'fast_light':
            for name in ('flrig','fldigi','flmsg','flamp'):
                values[f'{name}_enabled'] = bool(profile.get(f'use_{name}'))
                if name in {'flmsg','flamp'}:
                    values[f'{name}_application_path'] = profile.get(f'{name}_path','')
        metadata = dict(expected_current_instance_id=instance_id,replacement_instance_id=instance_id,
                        expected_identity_generation=store.radio_software_identity_generation(radio_id),
                        operator_launch_snapshot=launch_configuration_token(launch))
    return {'current_values':values,'context':metadata}


def _launch_configuration_snapshot(bundle):
    """Compare persisted choices, excluding write bookkeeping.

    Launch Control can persist an unchanged bundle while an editor is open.
    Retain all policy, ordering and identity fields as concurrency evidence.
    """
    result = deepcopy(dict(bundle))
    result.pop('updated_utc', None)
    for item in result.get('items', ()):
        item.pop('updated_utc', None)
        # The query already sorts the items. Re-saving may renumber sparse
        # order values without changing that actual launch order.
        item.pop('display_order', None)
        for raw, parsed, fallback in (('dependencies_json', 'dependencies', []),
                                      ('readiness_json', 'readiness', {})):
            if raw in item:
                if parsed not in item:
                    try:
                        item[parsed] = json.loads(item[raw] or '')
                    except (ValueError, TypeError):
                        item[parsed] = deepcopy(fallback)
                item.pop(raw)
        if isinstance(item.get('readiness'), dict):
            item['readiness'].setdefault('execution_scope', 'standard')
    return result


def launch_configuration_token(bundle):
    """Bounded semantic concurrency evidence, also accepting retained old snapshots."""
    if isinstance(bundle, str) and bundle.startswith('launch-v1:'):
        return bundle
    encoded = json.dumps(_launch_configuration_snapshot(bundle), sort_keys=True, separators=(',', ':'), default=str)
    return 'launch-v1:' + hashlib.sha256(encoded.encode()).hexdigest()


def save_operator_varac_guard(store, radio_id, expected, proposed):
    """Save only CP-owned guard policy; canonical/native identity is untouched."""
    from freqinout.core.configuration_attention import VARAC_GUARD_FIELDS, varac_guard_configuration_issues
    if proposed.get('_source_profile_id') and int(proposed['_source_profile_id']) != int(radio_id):
        raise ConfigurationConflict('Inbound Guard draft belongs to a different radio. Select the radio again.')
    issues = varac_guard_configuration_issues(proposed)
    if issues:
        raise ValueError(' '.join(issues))
    values = [int(str(proposed[key]).strip().lower() in {'1', 'true', 'yes', 'on'})
              for key in VARAC_GUARD_FIELDS[:2]]
    policy = proposed.get(VARAC_GUARD_FIELDS[2]) or ''
    values.append(json.dumps(policy, sort_keys=True) if isinstance(policy, dict) else str(policy))
    with store.configuration_save_transaction() as transaction:
        with store.connect() as conn:
            row = conn.execute('SELECT * FROM device_profiles WHERE id=?', (int(radio_id),)).fetchone()
            if row is None:
                raise ConfigurationConflict('This radio no longer exists.')
            if any(row[key] != expected.get(key) for key in VARAC_GUARD_FIELDS):
                raise ConfigurationConflict('Inbound Guard changed after this editor opened; reopen it to review.')
            conn.execute('UPDATE device_profiles SET ' + ', '.join(f'{key}=?' for key in VARAC_GUARD_FIELDS)
                         + ' WHERE id=?', [*values, int(radio_id)])
            result = store.get_device_profile(int(radio_id))
        transaction.complete()
    return result


def save_operator_application(store, payload: Mapping[str, Any]):
    """Atomic field-scoped edit. No launch, scheduling, native apply or discovery."""
    family = str(payload['family_key'])
    radio_id = int(payload['radio_id'])
    if family not in LINKS:
        raise ValueError('Choose a supported application.')
    base = deepcopy(dict(payload['operator_base_values']))
    proposed = deepcopy(dict(payload['operator_values']))
    # Save is an explicit review boundary, including a first unchanged review
    # of a legacy assignment. Publish its chosen launch policy once rather than
    # returning Saved without retaining any operator-owned configuration.
    proposed['operator_review_version'] = 1
    # Reconcile prior empty endpoint projections only at an explicit Save.
    # This does not move profiles or change native application settings.
    proposed['operator_projection_version'] = 1
    if family == 'fast_light':
        # Component-scoped forms retain blank defaults for hidden endpoints.
        # They do not clear another component's required typed port. Omit a
        # fresh blank so the store can supply its schema-compatible default;
        # retain an existing baseline without replaying it as an edit.
        for field, component in (('port', 'flrig'), ('secondary_port', 'fldigi')):
            if not proposed.get(f'{component}_enabled') and proposed.get(field) in ('', None):
                if base.get(field) not in ('', None):
                    proposed[field] = base[field]
                else:
                    proposed.pop(field, None)
                    base.pop(field, None)
        # The short form edits component paths. Legacy aliases project the
        # same typed columns and must agree before computing changed fields;
        # otherwise a fresh hidden blank alias rejects a valid typed readback.
        for values in (proposed, base):
            for component, alias in (('flrig', 'application_path'),
                                     ('fldigi', 'secondary_application_path')):
                field = f'{component}_application_path'
                if field in values:
                    # A blank default must still accept an older alias-only
                    # seed. A reviewed clear of an existing component wins.
                    cleared = field in base and values[field] != base[field]
                    path = values[field] if values[field] or cleared else values.get(alias, values[field])
                    values[field] = values[alias] = path
                elif alias in values:
                    values[field] = values[alias]
        # The short form exposes per-component policy, not the derived family
        # flag. Normalize its baseline too so an unchanged review is not a
        # synthetic concurrent edit of that hidden aggregate.
        for values in (base, proposed):
            values['launch_at_startup'] = any(bool(values.get(f'{key}_launch_at_startup')) for key in ('flrig','fldigi','flmsg','flamp') if values.get(f'{key}_enabled'))
    if not str(proposed.get('instance_name') or '').strip():
        profile_label = store.get_device_profile(radio_id) or {}
        proposed['instance_name'] = f"{profile_label.get('name','Radio')} {family.replace('_',' ')}"
    if family == 'js8call':
        # The form returns text while SQLite returns an integer. Normalize both
        # sides before computing the edit set: an unchanged offset is not an
        # operator edit and must not overwrite a newer runtime observation.
        for numeric in (base, proposed):
            if numeric.get('offset_hz') not in ('', None):
                try:
                    numeric['offset_hz'] = int(numeric['offset_hz'])
                except (TypeError, ValueError):
                    pass
        if proposed.get('offset_hz') in ('',None):
            if base.get('offset_hz') not in ('',None):
                proposed['offset_hz'] = base['offset_hz']
            else:
                proposed.pop('offset_hz',None)
                base.pop('offset_hz',None)
        else:
            try:
                proposed['offset_hz'] = int(proposed['offset_hz'])
            except (TypeError,ValueError):
                pass
    if family == 'fast_light':
        cp_launch = any(proposed.get(f'{key}_launch_control', 'cp' if proposed.get(f'{key}_launch_at_startup') else 'operator') == 'cp' for key in ('flrig','fldigi','flmsg','flamp') if proposed.get(f'{key}_enabled'))
        proposed['mode'] = 'managed' if cp_launch else 'manual'
    else:
        proposed.setdefault('launch_control', 'cp' if proposed.get('launch_at_startup') or proposed.get('mode') == 'managed' else 'operator')
        proposed['mode'] = {'cp':'managed','remote':'remote','operator':'manual'}.get(proposed['launch_control'],'manual')
    if family == 'js8call' and proposed.get('settings_identity_mode') == 'default':
        proposed['rig_name'] = ''
    expected_id = payload.get('replacement_instance_id') or payload.get('expected_current_instance_id') or None
    expected_id = int(expected_id) if expected_id else None
    if expected_id is None:
        base = ({field: base[field] for field, _column in VARAC_PROFILE_PATHS if field in base}
                if family == 'varac' else {})
    scope = f'application:{radio_id}:{family}'
    envelope = load_unfinished_configuration(store, scope)
    # The host must resolve retained unfinished work explicitly. Never silently
    # replace it on a fresh edit or restore it over a manual selection.
    session = ConfigEditSession(base, draft_revision=envelope['revision'])
    if envelope.get('payload') and envelope['payload'].get('session_id') not in {payload.get('operator_session_id'), payload.get('operator_recovered_from')}:
        raise ConfigurationConflict('Saved unfinished work exists. Recover or discard it before another save.')
    session.values = proposed
    if payload.get('operator_session_id'):
        session.session_id = str(payload['operator_session_id'])
    session.recovered_from = payload.get('operator_recovered_from')
    key = str(payload.get('initial_application_system_key') or payload.get('operator_system_key') or payload.get('system_key') or '')
    if not key:
        key = f'operator_{uuid4().hex}'
    key = _normalize_system_key(key,'operator')

    published = False

    def read(conn):
        profile = store.get_device_profile(radio_id)
        if not profile:
            raise ConfigurationConflict('This radio no longer exists.')
        actual = int(profile.get(LINKS[family]) or 0) or None
        if actual != expected_id:
            raise ConfigurationConflict('The radio application assignment changed; reopen it to review.')
        generation = payload.get('expected_identity_generation')
        if not published and generation is not None and int(generation) != store.radio_software_identity_generation(radio_id):
            raise ConfigurationConflict('The application identity changed after this editor opened; reopen it to review.')
        launch_snapshot = payload.get('operator_launch_snapshot')
        if (not published and launch_snapshot is not None
                and launch_configuration_token(launch_snapshot)
                != launch_configuration_token(store.get_radio_launch_bundle(radio_id))):
            raise ConfigurationConflict('Launch Control changed after this editor opened; reopen it to review.')
        if actual is None:
            current = deepcopy(base)
            if family == 'varac':
                current.update({field: str(profile.get(column) or '')
                                for field, column in VARAC_PROFILE_PATHS if field in base})
            return current
        row = dict(conn.execute(f'SELECT * FROM {TABLES[family]} WHERE id=?',(actual,)).fetchone())
        manifest = application_manifest(store, family, row)
        actual_values = application_editor_values(family,row,manifest, profile=profile)
        current = {**base, **actual_values, **_current_launch_values(store,radio_id,family,actual_values)[1]}
        if family == 'fast_light':
            current.update({f'{name}_enabled':bool(profile.get(f'use_{name}')) for name in ('flrig','fldigi','flmsg','flamp')})
        # Keep equivalent raw formatting (e.g. a leading-zero port) without
        # concealing a genuine typed change made by another editor.
        for field in ('port','secondary_port'):
            try:
                if field in base and int(base[field]) == int(current[field]):
                    current[field] = base[field]
            except (ValueError, TypeError, KeyError):
                pass
        return current

    def publish(conn, values):
        nonlocal expected_id, base, published
        profile = store.get_device_profile(radio_id)
        observer = profile.get('device_class') == 'observer'
        values['radio_role'] = 'observer' if observer else 'transceiver'
        if family == 'js8call' and values.get('settings_identity_mode') == 'default':
            values['rig_name'] = ''
        recipe = operator_launch_recipe(family, values)
        had_assignment = expected_id is not None
        row = {}
        source_id = int(payload.get('operator_source_instance_id') or 0) or None
        if source_id is not None and source_id != expected_id:
            source_row = conn.execute(f'SELECT * FROM {TABLES[family]} WHERE id=?',(source_id,)).fetchone()
            if source_row is None:
                raise ConfigurationConflict('The selected saved application no longer exists.')
            row = dict(source_row)
            source_snapshot = payload.get('operator_source_snapshot') or {}
            if not source_snapshot or any(row.get(column) != value for column,value in source_snapshot.items() if column in row):
                raise ConfigurationConflict('The selected saved application changed after it was chosen; Find and review it again.')
        if expected_id and not row:
            row = dict(conn.execute(f'SELECT * FROM {TABLES[family]} WHERE id=?',(expected_id,)).fetchone())
        if not row:
            collision = conn.execute(f'SELECT 1 FROM {TABLES[family]} WHERE system_key=?',(key,)).fetchone()
            if collision:
                raise ConfigurationConflict('This application identity belongs to another saved instance. Choose an independent identity.')
        if family == 'js8call':
            # Leave unrelated edits to pre-existing legacy collisions available.
            identity_changed = (expected_id is None or payload.get('operator_source_instance_id')
                                or any(values.get(k) != base.get(k) for k in ('settings_identity_mode', 'rig_name', 'launch_control', 'host', 'launch_command', 'custom_argv')))
            if identity_changed:
                validate_js8_native_profile_claims(store, conn, radio_id, values)
        app = {**{column:value for column,value in row.items() if value is not None}, 'system_key':row.get('system_key') or key, 'name':values.get('instance_name') or f"{profile['name']} · {family}", 'enabled':row.get('enabled',1)}
        for field, column in FIELDS[family].items():
            if field in values and values[field] is not None:
                app[column] = int(values[field]) if field in {'port','secondary_port','offset_hz'} and values[field] not in ('',None) else values[field]
        if family == 'js8call':
            app['rig_name_source'] = 'managed' if values.get('settings_identity_mode') == 'isolated' else 'legacy_default'
        if family == 'fast_light':
            app['flrig_path'] = str(values.get('flrig_application_path') or values.get('application_path') or '') if values.get('flrig_enabled') else ''
            app['fldigi_path'] = str(values.get('fldigi_application_path') or '') if values.get('fldigi_enabled') else ''
        if family == 'varac':
            app.update({field:values[field] for field in ('outbox_path','bbs_path','bbs_archive_path') if field in values})
        prior_manifest = application_manifest(store, family, row) if row else None
        evidence = {**dict((prior_manifest or {}).get('evidence') or {}),
                    'operator_values':deepcopy(values), 'operator_values_version':1, 'launch_recipe':recipe,
                    'settings_identity_mode':values.get('settings_identity_mode','default'),
                    'receive_only_ingest':observer, 'transmit_authority':False if observer else True}
        manifest = {**dict(prior_manifest or {}), **values,
                    'instance_key':(prior_manifest or {}).get('instance_key') or f"{family}:{app['system_key']}",
                    'application_system_key':app['system_key'], 'management_mode':'fio_managed' if values.get('mode') == 'managed' else 'remote' if values.get('mode') == 'remote' else 'operator',
                    'verification_state':'configured','verification_summary':'Saved in CommsPlan; connection not checked.',
                    'evidence':evidence}
        if family == 'fast_light':
            # Adoption projects these claims into radio-owned paths. Replace
            # reviewed folders as well as executables; otherwise canonical
            # identity paths change while adoption replays old/empty folders.
            reviewed_claims = [
                (field, kind, exclusive) for field, kind, exclusive in FAST_LIGHT_PATH_CLAIMS
                if field in values
            ] + [(f'{name}_application_path', f'{name}_application', False)
                 for name in ('flmsg', 'flamp')]
            replaced_kinds = {kind for _field, kind, _exclusive in reviewed_claims}
            manifest['resource_claims'] = [
                claim for claim in manifest.get('resource_claims', ()) or ()
                if claim.get('kind') not in replaced_kinds
            ] + [dict(kind=kind, value=values[field], exclusive=exclusive)
                 for field, kind, exclusive in reviewed_claims if values.get(field)]
        cluster_context = {}
        if family == 'varac' and expected_id and source_id in (None, expected_id):
            # Editing this member does not select a new VarAC arrangement.
            # Preserve its existing explicit cluster contract for claim checks.
            member = conn.execute('''SELECT cluster_id, instance_number
                FROM varac_cluster_members WHERE device_profile_id=? AND enabled=1''',
                (radio_id,)).fetchall()
            if len(member) == 1:
                cluster_context = dict(varac_cluster_db_id=member[0]['cluster_id'],
                                       varac_cluster_instance_number=member[0]['instance_number'])
                cluster = conn.execute('SELECT shared_bbs_path, shared_bbs_archive_path FROM varac_clusters WHERE id=?',
                                       (member[0]['cluster_id'],)).fetchone()
                if cluster is not None:
                    for field, column in (('bbs_path', 'shared_bbs_path'), ('bbs_archive_path', 'shared_bbs_archive_path')):
                        if field in values and str(values[field] or '') != str(cluster[column] or ''):
                            raise ValueError('These BBS folders belong to the VarAC cluster. Open Cluster settings to change the shared BBS and archive folders.')
        retained = list(store.list_radio_software_identity_records(radio_id))
        generation = store.radio_software_identity_generation(radio_id)
        result = store.adopt_software_instance(family_key=family,radio_profile_id=radio_id,application_values=app,
            manifest_values=manifest,replace_existing=expected_id is not None,expected_current_instance_id=expected_id,
            launch_at_startup=bool(values.get('launch_at_startup')), **cluster_context)
        identity_payload = {**values, **manifest, 'mode':values.get('mode','manual'), 'source_mode':'manual', 'launch_recipe':recipe,
                            'execution_scope':'remote' if values.get('launch_control') == 'remote' else 'receive_only' if observer else 'standard'}
        if cluster_context:
            identity_payload['resource_claims'] = store.get_software_instance_manifest(
                manifest['instance_key']).get('resource_claims', ())
        replacement = build_guided_identity_records(result['radio'],{family:identity_payload},(family,))[0]
        # Manifest and canonical endpoint facts come from one reviewed record.
        saved_manifest = store.get_software_instance_manifest(manifest['instance_key'])
        store.save_software_instance_manifest({**saved_manifest,
            'ports': [dict(name=endpoint.get('endpoint_key') or 'primary',
                           protocol=endpoint.get('transport') or 'tcp',
                           host=endpoint.get('host'), port=endpoint.get('port'))
                      for endpoint in replacement.endpoints if endpoint.get('port')]})
        # Availability is independent of configuration ownership. Retain the
        # identity even when this radio is not currently using the software.
        records = [record for record in retained if record.family_key != family] + [replacement]
        if family == 'js8call':
            for index, record in enumerate(records):
                if record.family_key == 'commstat':
                    mapped = dict(identity_record_to_mapping(record))
                    mapped['bindings'] = [{**dict(binding),'endpoint':dict(replacement.endpoints[0])} if binding.get('kind') == 'radio-js8-endpoint' and replacement.endpoints else dict(binding) for binding in mapped.get('bindings',())]
                    records[index] = identity_record_from_mapping(mapped)
        store.save_radio_software_identity_records(radio_id,tuple(records),expected_generation=generation)
        expected_id = int(result['radio'][LINKS[family]])
        preserved = store.get_device_profile(radio_id)
        if family == 'fast_light':
            for name in ('flrig','fldigi','flmsg','flamp'):
                preserved[f'use_{name}'] = bool(values.get(f'{name}_enabled'))
        else:
            flag = 'use_js8call' if family == 'js8call' else 'use_varac'
            preserved[flag] = profile.get(flag,0) if had_assignment else 1
        use_fields = [f'use_{name}' for name in ('flrig','fldigi','flmsg','flamp')] if family == 'fast_light' else [flag]
        conn.execute('UPDATE device_profiles SET ' + ', '.join(f'{field}=?' for field in use_fields) + ' WHERE id=?',
                     [int(bool(preserved[field])) for field in use_fields] + [radio_id])
        launch_names = {'js8call':[('JS8Call',bool(preserved.get('use_js8call')))],
                        'varac':[('VarAC',bool(preserved.get('use_varac')))],
                        'fast_light':[(label,bool(values.get(f'{key}_enabled'))) for key,label in (('flrig','FLRig'),('fldigi','FLDigi'),('flmsg','FLMsg'),('flamp','FLAmp'))]}[family]
        for label, enabled in launch_names:
            # An explicit family save supersedes prior launch entries for that
            # same family on this radio. Preserve opaque legacy command/path
            # evidence, but never leave it starting beside the reviewed recipe.
            # This is scoped to known app slots, not custom tools or other radios.
            prefix = manifest['instance_key'] + ':'
            conn.execute('''UPDATE radio_launch_bundle_items
                SET enabled=CASE WHEN substr(instance_key,1,?)=? THEN ? ELSE 0 END,
                    launch_at_startup=CASE WHEN substr(instance_key,1,?)=? THEN launch_at_startup ELSE 0 END
                WHERE radio_profile_id=? AND app_name=?''',
                (len(prefix),prefix,int(enabled),len(prefix),prefix,radio_id,label))
        published = True
        # Verify the actual typed columns, independently of editor evidence.
        saved = dict(conn.execute(f'SELECT * FROM {TABLES[family]} WHERE id=?',(expected_id,)).fetchone())
        mismatches = [column for column,value in app.items() if column in saved and column not in {'updated_utc','created_utc','enabled'} and saved.get(column) != value]
        if family == 'varac':
            current_profile = store.get_device_profile(radio_id)
            for field,column in (('outbox_path','varac_outbox_dir'),('bbs_path','varac_bbs_dir'),('bbs_archive_path','varac_bbs_archive_dir')):
                if field in values and str(current_profile.get(column) or '') != str(values[field] or ''):
                    mismatches.append(field)
        if family == 'fast_light':
            current_profile = store.get_device_profile(radio_id)
            for field, column in (('flmsg_message_path', 'flmsg_message_path'),
                                  ('flamp_receive_path', 'flamp_message_path')):
                if field in values and str(current_profile.get(column) or '') != str(values[field] or ''):
                    mismatches.append(field)
            saved_claims = {claim.get('kind'): str(claim.get('value') or '') for claim in
                            store.get_software_instance_manifest(manifest['instance_key']).get('resource_claims', ())}
            for field, kind, _exclusive in FAST_LIGHT_PATH_CLAIMS:
                if field in values and saved_claims.get(kind, '') != str(values[field] or ''):
                    mismatches.append(kind)
        if mismatches:
            raise ValueError('Application settings readback did not match: ' + ', '.join(mismatches))
        # An unselected application may intentionally retain editable settings.
        # Validate its data/launch projection without enabling it as a side effect.
        projection_issues = tuple(issue for issue in
            store.validate_radio_software_identity_projections(radio_id).get(family, ())
            if issue != 'radio selection no longer includes this identity')
        if projection_issues:
            raise ValueError('Saved configuration readback did not match: ' + '; '.join(projection_issues))

    request = session.snapshot()
    request['operator_context'] = {field:deepcopy(payload[field]) for field in ('expected_identity_generation','operator_launch_snapshot','expected_current_instance_id','replacement_instance_id','operator_source_instance_id','operator_source_snapshot') if field in payload}
    def validate(values):
        issues = application_issues(family, values)
        if (family == 'js8call' and js8_profile_paths_need_review(base, values)
                and values.get('profile_paths_acknowledged') != js8_profile_path_review_token(values)):
            issues += ('The native JS8Call profile changed while previous message/readback paths remain. Review the paths and acknowledge that CommsPlan should use them.',)
        return issues
    return save_configuration_edit(store,scope,request,read_current=read,
                                   validate=validate,publish=publish)
