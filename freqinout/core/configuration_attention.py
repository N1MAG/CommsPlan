"""Pure configuration navigation evidence. Runtime/process observations are excluded."""
from __future__ import annotations
import json
from typing import Any, Mapping

LABELS = {'complete': 'Complete', 'review': 'Review', 'unknown': 'Not checked',
          'optional': 'Optional', 'unsaved': 'Unsaved', 'error': 'Error'}
SYMBOLS = {'complete': '✓', 'review': '⚠', 'unknown': '○', 'optional': '—',
           'unsaved': '●', 'error': '×'}


def attention(*, checked=False, issues=(), optional=False, dirty=False, reason='') -> dict[str, str]:
    problems = tuple(str(issue).strip() for issue in issues if str(issue).strip())
    state = ('unsaved' if dirty else 'review' if problems else 'optional' if optional
             else 'complete' if checked else 'unknown')
    detail = ('Changes are not saved. Save this configuration before relying on it.' if dirty else
              '; '.join(problems) if problems else reason or
              {'complete': 'Required saved settings pass local configuration checks. Files and runtime are not verified.',
               'optional': 'Not selected for this radio. Configuration is optional.',
               'unknown': 'Configuration has not been checked for this section.'}[state])
    return {'state': state, 'label': LABELS[state], 'reason': detail}


def attention_text(title: str, entry: Mapping[str, Any]) -> str:
    state = str(entry.get('state', ''))
    return f'{SYMBOLS[state]}  {title}' if state in SYMBOLS else title


def _enabled(value):
    return value is not None and str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _value(values, key):
    if key.startswith('message_paths.'):
        return str((values.get('message_paths') or {}).get(key.split('.')[1]) or '').strip()
    return str(values.get(key) or '').strip()


def identity_issue_task(family: str, issue: str) -> str:
    """Scope persisted identity diagnostics to the question they can affect."""
    text = str(issue).lower()
    if family == 'fast_light':
        for component, task in (('flrig', 'flrig_control'), ('fldigi', 'fldigi_modem_logs'),
                                ('flmsg', 'flmsg'), ('flamp', 'flamp_signing')):
            if f'component {component}' in text:
                return task
    if 'endpoint' in text or 'binding' in text:
        return {'js8call': 'api_radio', 'commstat': 'transport_mapping',
                'fio_spotter': 'dependencies', 'varac': 'application_radio'}.get(family, 'overview')
    if 'launch' in text:
        return 'launch'
    if 'executable' in text or 'application path' in text or 'install path' in text:
        return {'js8call': 'application_profile', 'fast_light': 'application_profile',
                'varac': 'application_radio'}.get(family, 'overview')
    if 'component' in text:
        return 'launch'
    if 'message path' in text or 'data path' in text:
        return {'js8call': 'message_storage', 'fast_light': 'message_folders',
                'varac': 'inbox_outbox'}.get(family, 'overview')
    if 'configuration path' in text:
        return {'js8call': 'application_profile', 'varac': 'application_radio'}.get(family, 'overview')
    if 'canonical identity' in text or 'manifest' in text or 'fingerprint' in text:
        return 'overview'
    return 'overview'


def identity_issue_explanation(issue: str) -> str:
    raw = str(issue).strip()
    text = raw.lower()
    if text == 'canonical identity record is missing':
        return ('Saved launch details have not been recorded for this older setup. '
                'Review and save its configuration; existing application settings are retained.')
    if 'endpoint' in text or 'binding' in text:
        return ('Saved connection records disagree. Review the host and port, then Save to '
                'reconcile CommsPlan’s records. Native application settings are retained.')
    if 'launch' in text:
        component = next((label for key, label in (
            ('js8call', 'JS8Call'), ('flrig', 'FLRig'), ('fldigi', 'FLDigi'),
            ('flmsg', 'FLMsg'), ('flamp', 'FLAmp'), ('varac', 'VarAC'),
        ) if key in text), 'Application')
        return (f'{component} launch choice needs review. Review Launch and Save the '
                'intended launch recipe.')
    if 'executable' in text or 'application path' in text or 'install path' in text:
        return ('Saved application path needs review. Review Application & Profile and '
                'Save the intended executable path.')
    if 'component' in text:
        component = next((label for key, label in (
            ('js8call', 'JS8Call'), ('flrig', 'FLRig'), ('fldigi', 'FLDigi'),
            ('flmsg', 'FLMsg'), ('flamp', 'FLAmp'), ('varac', 'VarAC'),
        ) if key in text), 'Application')
        return (f'{component} component choice needs review. Review Launch and Save the '
                'intended launch recipe.')
    if 'manifest' in text or 'fingerprint' in text or 'metadata' in text:
        return ('Saved application identity needs review. Review Overview and Save the '
                'canonical identity details.')
    return raw


def _saved_launch_control(values: Mapping[str, Any]) -> str:
    """Return an explicitly persisted launch choice, if one exists.

    Older rows did not store ``launch_control`` directly.  Their canonical
    launch recipe or management mode is still authoritative when present.  A
    missing recipe/mode is deliberately left blank; assuming the historical
    default would turn an unreviewed launch into a false green badge.
    """
    explicit = str(values.get('launch_control') or '').strip().lower()
    if explicit in {'cp', 'operator', 'remote'}:
        return explicit
    recipe = values.get('launch_recipe')
    if isinstance(recipe, Mapping):
        components = recipe.get('components')
        if isinstance(components, (list, tuple)) and components:
            controls = []
            starts = []
            for component in components:
                if not isinstance(component, Mapping):
                    continue
                readiness = component.get('readiness')
                if isinstance(readiness, Mapping):
                    control = str(readiness.get('launch_control') or '').strip().lower()
                    if control in {'cp', 'operator', 'remote'}:
                        controls.append(control)
                if 'operator_starts' in component:
                    starts.append(bool(component.get('operator_starts')))
            if controls:
                if all(control == 'remote' for control in controls):
                    return 'remote'
                if any(control == 'cp' for control in controls):
                    return 'cp'
                if all(control == 'operator' for control in controls):
                    return 'operator'
            if starts:
                return 'operator' if all(starts) else 'cp'
    mode = str(values.get('management_mode') or '').strip().lower()
    if mode in {'remote'}:
        return 'remote'
    if mode in {'fio_managed', 'managed', 'cp'}:
        return 'cp'
    if mode in {'operator', 'operator_managed'}:
        return 'operator'
    return ''


def _application_launch_issues(family: str, values: Mapping[str, Any], *, launch_control: str) -> list[str]:
    """Check only saved application/launch facts; never probes the host."""
    issues: list[str] = []
    if family != 'fast_light' and _enabled(values.get('launch_at_startup')) and launch_control != 'cp':
        issues.append('Choose CommsPlan launches before enabling application startup.')
    if family != 'fast_light' and launch_control == 'cp' and not (
        str(values.get('application_path') or '').strip()
        or str(values.get('launch_command') or '').strip()
    ):
        issues.append('Choose the application or launcher for CommsPlan to launch.')
    if family == 'fast_light' and launch_control == 'cp':
        # Fast Light can mix operator and CommsPlan control per component. A
        # recipe executable or the matching saved component path satisfies the
        # requirement for each component that CP owns.
        paths = {
            'flrig': str(values.get('flrig_application_path') or values.get('application_path') or '').strip(),
            'fldigi': str(values.get('fldigi_application_path') or '').strip(),
            'flmsg': str(values.get('flmsg_application_path') or '').strip(),
            'flamp': str(values.get('flamp_application_path') or '').strip(),
        }
        recipe = values.get('launch_recipe')
        missing = []
        if isinstance(recipe, Mapping) and isinstance(recipe.get('components'), (list, tuple)):
            for component in recipe.get('components'):
                if not isinstance(component, Mapping):
                    continue
                readiness = component.get('readiness') if isinstance(component.get('readiness'), Mapping) else {}
                control = str(readiness.get('launch_control') or '').strip().lower()
                owned = control == 'cp' or (not control and component.get('operator_starts') is False)
                if owned:
                    key = str(component.get('component_key') or '').strip().lower()
                    if not str(component.get('executable') or '').strip() and not paths.get(key, ''):
                        missing.append(key.upper() or 'Fast Light')
        else:
            for key in ('flrig', 'fldigi', 'flmsg', 'flamp'):
                component_control = str(
                    values.get(f'{key}_launch_control') or launch_control
                ).strip().lower()
                if values.get(f'{key}_enabled') and component_control == 'cp' and not paths[key]:
                    missing.append(key.upper())
        if missing:
            issues.append('Choose the executable for CommsPlan-controlled Fast Light component(s): ' + ', '.join(missing) + '.')
    if family == 'js8call':
        mode = str(values.get('settings_identity_mode') or '').strip().lower()
        if mode == 'isolated' and not str(values.get('rig_name') or '').strip():
            issues.append('Enter the native JS8Call profile name, or select Default profile.')
        if mode == 'custom_launcher' and not (
            str(values.get('application_path') or '').strip()
            or str(values.get('launch_command') or '').strip()
        ):
            issues.append('Choose or enter the existing launcher command.')
    try:
        # This validates named/default profile argument collisions and the
        # saved launch vector without checking executable existence.
        from freqinout.core.operator_application_configuration import operator_launch_recipe
        launch_values = dict(values)
        if family == 'js8call' and not str(launch_values.get('settings_identity_mode') or '').strip():
            launch_values['settings_identity_mode'] = 'default'
        operator_launch_recipe(family, launch_values)
    except (ValueError, TypeError) as exc:
        issues.append(str(exc))
    return issues


# The required endpoints and message paths are also checked by station_readiness.
# Executables are deliberately not required: operator/remote launch is supported.
TASK_CHECKS = {
    'fast_light': {
        'flrig_control': (('use_flrig',), (('flrig_port', 'FLRig XML-RPC port', 'port'),)),
        'fldigi_modem_logs': (('use_fldigi',), (('fldigi_host', 'FLDigi XML-RPC host', 'text'), ('fldigi_port', 'FLDigi XML-RPC port', 'port'))),
        'flmsg': (('use_flmsg',), (('message_paths.flmsg', 'FLMsg ICS messages folder', 'text'),)),
        'flamp_signing': (('use_flamp',), (('message_paths.flamp', 'FLAmp receive folder', 'text'),)),
        'message_folders': (('use_flmsg', 'use_flamp'), ()),
        'launch': (('use_flrig', 'use_fldigi', 'use_flmsg', 'use_flamp'), ()),
    },
    'js8call': {
        'application_profile': (('use_js8call',), ()),
        'api_radio': (('use_js8call',), (('js8_host', 'JS8Call TCP host', 'text'), ('js8_port', 'JS8Call TCP port', 'port'))),
        'message_storage': (('use_js8call',), (('js8_directed_path', 'JS8Call DIRECTED.TXT path', 'text'),)),
        'ingest_forms': (('use_js8call',), (('js8_directed_path', 'JS8Call DIRECTED.TXT path', 'text'),)),
        'launch': (('use_js8call',), ()),
    },
    'varac': {
        'application_radio': (('use_varac',), (('varac_ini_path', 'VarAC configuration file', 'text'),)),
        'runtime_paths': (('use_varac',), (('varac_ini_path', 'VarAC configuration file', 'text'),)),
        'inbox_outbox': (('use_varac',), (('message_paths.varac', 'VarAC incoming folder', 'text'),)),
        'inbound_guard': (('use_varac',), ()),
        'launch': (('use_varac',), ()),
    },
    'commstat': {
        'transport_mapping': (('use_commstat',), (('js8_host', 'JS8 transport host', 'text'),
                                                ('js8_port', 'JS8 transport port', 'port'))),
    },
    'fio_spotter': {
        'dependencies': (('use_js8spotter',), (('js8_host', 'JS8 transport host', 'text'),
                                             ('js8_port', 'JS8 transport port', 'port'))),
    },
}

# Navigation destinations own no per-radio form to validate. Do not invent a
# pending check (or a success badge) for opening a shared workspace/coordinator.
REFERENCE_TASKS = {
    'varac': frozenset({'cluster'}),
    'fio_spotter': frozenset({'forms', 'operational_workspace'}),
    'external_spotter': frozenset({'forms', 'profile'}),
}

VARAC_GUARD_FIELDS = ('varac_guard_allow_bbs_allowed_callsigns',
                      'varac_guard_allow_operator_trusted', 'varac_guard_access_policy_v1')

def varac_guard_configuration_issues(saved):
    problems = []
    for key, label in zip(VARAC_GUARD_FIELDS[:2], ('Allow BBS access list', 'Allow trusted operators')):
        if saved.get(key) is None or str(saved[key]).strip().lower() not in {'0', '1', 'true', 'false', 'yes', 'no', 'on', 'off'}:
            problems.append(f'{label} has no valid saved choice. Review and save Inbound Guard.')
    value = saved.get(VARAC_GUARD_FIELDS[2])
    if value:
        try:
            policy = json.loads(value) if isinstance(value, str) else value
            if (not isinstance(policy, dict) or policy.get('version') != 1
                    or any(not isinstance(policy.get(field, []), list) for field in
                           ('manual_callsigns', 'group_sources', 'resolved_group_callsigns'))):
                raise ValueError
            if any(not isinstance(call, str) for field in ('manual_callsigns', 'resolved_group_callsigns')
                   for call in policy.get(field, [])):
                raise ValueError
            for source in policy.get('group_sources', []):
                if (not isinstance(source, dict) or not isinstance(source.get('family'), str)
                        or not isinstance(source.get('groups'), list)
                        or any(not isinstance(group, str) for group in source['groups'])):
                    raise ValueError
        except (ValueError, TypeError):
            problems.append('HF Operator access scope has an invalid saved policy. Review and save Inbound Guard.')
    return tuple(problems)


def software_attention(family: str, profile: Mapping, saved: Mapping,
                       task_fields: Mapping[str, tuple[str, ...]], *, draft=None,
                       loaded=False, identity_issues=(), repair=False,
                       application_values: Mapping[str, Any] | None = None) -> dict:
    """Required-field checks over raw saved values, never editor defaults or runtime.

    This is an explanatory local check, not native verification/save authority.
    Coverage is explicit; unvalidated sections cannot acquire a success badge.
    """
    result = {}
    rules = TASK_CHECKS.get(family, {})
    scoped_issues = {}
    for issue in identity_issues:
        owner = identity_issue_task(family, issue)
        if owner not in task_fields:
            owner = next((key for key in ('overview', 'application_profile', 'application_radio',
                                          'launch', 'transport_mapping', 'dependencies')
                          if key in task_fields), next(iter(task_fields), 'overview'))
        scoped_issues.setdefault(owner, []).append(identity_issue_explanation(issue))
    if scoped_issues.get('overview') and loaded and 'overview' in task_fields:
        result['overview'] = attention(issues=scoped_issues['overview'])
    launch_policy_missing_reason = (
        'Older setup has no recorded launch choices. Review and save configuration '
        'to record how each application starts; existing paths are retained.'
    )
    missing_fast_light_launch_policy = False
    if family == 'fast_light' and loaded and application_values is not None:
        selected_fast_light = any(
            _enabled(profile.get(flag))
            for flag in ('use_flrig', 'use_fldigi', 'use_flmsg', 'use_flamp')
        )
        missing_fast_light_launch_policy = (
            selected_fast_light and not _saved_launch_control(application_values)
        )
    if missing_fast_light_launch_policy and 'overview' in task_fields and 'overview' not in result:
        result['overview'] = attention(reason=launch_policy_missing_reason)
    for task, fields in task_fields.items():
        if task in {'overview', 'advanced', 'health'} or task in REFERENCE_TASKS.get(family, ()):
            continue
        dirty = draft is not None and any(_value(draft, key) != _value(saved, key) for key in fields)
        flags, requirements = rules.get(task, ((), ()))
        selected = tuple(flag for flag in flags if _enabled(profile.get(flag)))
        known_flags = bool(flags) and all(profile.get(flag) is not None for flag in flags)
        backend_required = ((family, task, str(profile.get('control_backend') or '').lower()) in {
            ('fast_light', 'flrig_control', 'flrig'), ('fast_light', 'fldigi_modem_logs', 'fldigi'),
            ('js8call', 'api_radio', 'js8call')})
        flag_conflict = backend_required and known_flags and not selected
        if backend_required:
            selected, known_flags = flags, True
        optional = known_flags and not selected
        checked = loaded and known_flags and bool(selected) and task in rules
        problems = (["This application is selected for radio control but disabled in Applications. Review the radio application selection."] if flag_conflict else [])
        if loaded and selected and task in rules:
            if task == 'message_folders':
                requirements = tuple((f'message_paths.{key}', label, 'text')
                                     for key, label, flag in (('flmsg', 'FLMsg ICS messages folder', 'use_flmsg'), ('flamp', 'FLAmp receive folder', 'use_flamp'))
                                     if _enabled(profile.get(flag)))
            for key, label, kind in requirements:
                value = _value(saved, key)
                if not value:
                    problems.append(f'{label} is missing. Enter it in this section.')
                elif kind == 'port':
                    try:
                        valid = 1 <= int(value) <= 65535
                    except ValueError:
                        valid = False
                    if not valid:
                        problems.append(f'{label} must be from 1 to 65535. Correct it in this section.')
            if family == 'varac' and task == 'inbound_guard':
                problems.extend(varac_guard_configuration_issues(saved))
        if loaded and selected and family == 'fast_light' and task == 'fldigi_modem_logs' and _value(saved, 'arq_port'):
            try:
                valid = 1 <= int(_value(saved, 'arq_port')) <= 65535
            except ValueError:
                valid = False
            if not valid:
                problems.append('FLDigi / FLAmp ARQ port must be from 1 to 65535. Correct it in this section.')
        if not optional and loaded:
            problems.extend(scoped_issues.get(task, ()))
        if loaded and repair and family == 'fast_light' and task in {'flmsg', 'flamp_signing'} and not optional:
            problems.append('Saved FLMsg/FLAmp wiring needs review. Use Repair components in FLAmp & Signing.')
        entry = attention(checked=checked, issues=problems, optional=optional, dirty=dirty)
        if (task in {'application_profile', 'launch'} or family == 'varac' and task == 'runtime_paths') and family in {'js8call', 'fast_light', 'varac'}:
            # A linked application row and its matched manifest are required
            # evidence.  The editor's defaults and a save click are not proof.
            if optional:
                entry = attention(optional=True, dirty=dirty)
            elif application_values is None:
                if problems:
                    entry = attention(issues=problems, dirty=dirty)
                else:
                    entry = attention(dirty=dirty, reason='Saved application and launch choices have not been checked.')
            elif loaded and selected:
                launch_control = _saved_launch_control(application_values)
                if not launch_control:
                    entry = attention(issues=problems, dirty=dirty,
                                      reason='Saved launch control has not been selected.')
                else:
                    launch_issues = list(problems)
                    launch_issues.extend(_application_launch_issues(
                        family, application_values, launch_control=launch_control
                    ))
                    entry = attention(checked=not launch_issues, issues=launch_issues, dirty=dirty)
        result[task] = entry
    return result
