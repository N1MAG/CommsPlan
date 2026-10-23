"""Changed-field persistence for the compact radio editors; no native app writes."""
from copy import deepcopy
import json

from freqinout.core.configuration_edit_session import ConfigurationConflict
from freqinout.core.operator_radio_configuration import load_operator_radio_context, save_operator_radio

PORTS = {'flrig_port': 12345, 'rig_port': 4532, 'js8_port': 2442}
CONTROL_FIELDS = frozenset(('control_backend', 'flrig_host', 'flrig_port', 'rig_host',
                            'rig_port', 'js8_host', 'js8_port', 'use_flrig', 'use_js8call'))
USE_FIELDS = frozenset(f'use_{name}' for name in ('flrig', 'fldigi', 'flmsg', 'flamp',
                       'js8call', 'js8spotter', 'external_js8spotter', 'commstat', 'varac'))
METADATA_FIELDS = frozenset(('name', 'radio_manufacturer', 'radio_model', 'radio_catalog_id',
    'device_class', 'deployment_mode', 'enabled', 'ptt_group', 'antenna_group',
    'antenna_supported_bands', 'antenna_band_guard_mode', 'band_overlap_guard_group',
    'band_overlap_guard_mode', 'advanced_frequency_guard_group', 'advanced_frequency_guard_mode',
    'advanced_frequency_guard_window_hz', 'frontend_group', 'amplifier_group', 'notes',
    'scheduler_enabled', 'schedule_hold_minutes_default', 'freq_enforcement_mode',
    'freq_prompt_interval', 'fldigi_enforcement_mode', 'fldigi_prompt_interval',
    'js8_enforcement_mode', 'js8_prompt_interval'))


def strict_port(value, field, fallback):
    text = str(value if value is not None else '').strip()
    if not text:
        text = str(fallback)
    if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= 65535:
        raise ValueError(f'Enter {field.replace("_", " ")} as a whole number from 1 to 65535.')
    return int(text)


def _field(profile, key):
    if key in {'flrig_host', 'rig_host', 'js8_host'}:
        return profile.get(key) or '127.0.0.1'
    if key in PORTS:
        return profile.get(key) or PORTS[key]
    if key == 'antenna_supported_bands':
        return profile.get(key, json.loads(profile.get('antenna_supported_bands_json') or '[]'))
    return profile.get(key)


def _equivalent(left, right):
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        return list(left or ()) == list(right or ())
    if isinstance(left, bool):
        left = int(left)
    if isinstance(right, bool):
        right = int(right)
    return str(left if left is not None else '') == str(right if right is not None else '')


def save_inline_radio_profile(store, expected, values, *, scope):
    """Merge one reviewed section under a write lock, retaining current app state.

    The caller may enclose this save in its compatibility-projection transaction.
    Application changes go through the same canonical owner as Edit Apps.
    """
    allowed = {'metadata': METADATA_FIELDS, 'control': CONTROL_FIELDS, 'use': USE_FIELDS}[scope]
    requested = dict(values)
    radio_id = int(requested.pop('id'))
    if radio_id != int(expected['id']) or set(requested) - allowed:
        raise ValueError('This radio edit contains fields owned by another configuration section.')
    if scope == 'control':
        for key, default in PORTS.items():
            if key in requested:
                requested[key] = strict_port(requested[key], key, expected.get(key) or default)
    changed = {key: value for key, value in requested.items()
               if not _equivalent(value, _field(expected, key))}
    with store.configuration_save_transaction() as transaction:
        current = store.get_device_profile(radio_id)
        if current is None:
            raise ConfigurationConflict('This radio no longer exists. Reopen the radio list.')
        watch = set(changed)
        # A role or runtime-authority transition needs a new review, even when
        # another field is being edited. Metadata never replays cached app links.
        watch.update(('device_class', 'runtime_active', 'runtime_primary'))
        if scope != 'metadata':
            watch.update(('control_backend', 'js8_instance_id', 'fast_light_config_id', 'varac_node_id'))
        for key in watch:
            if not _equivalent(_field(current, key), _field(expected, key)):
                raise ConfigurationConflict(f'{key.replace("_", " ")} changed after this editor opened. Reopen the radio to review it.')
        if not changed:
            saved = current
        elif scope == 'metadata':
            saved = store.save_device_profile({'id': radio_id, **changed})
        else:
            context = load_operator_radio_context(store, radio_id)
            proposed = {**current, **changed}
            drafts = {}
            fast_flags = tuple(f'use_{name}' for name in ('flrig', 'fldigi', 'flmsg', 'flamp'))
            fast_selected = any(proposed.get(flag) for flag in fast_flags)
            fast_edit = bool(set(changed) & {'flrig_host', 'flrig_port', *fast_flags})
            if fast_selected and (fast_edit or (changed.get('control_backend') == 'flrig' and not current.get('fast_light_config_id'))):
                base = context['application_seeds']['fast_light']
                app = deepcopy(base)
                app.update({f'{name}_enabled': bool(proposed.get(f'use_{name}'))
                            for name in ('flrig', 'fldigi', 'flmsg', 'flamp')})
                app.update(host=str(proposed.get('flrig_host') or '127.0.0.1'),
                           port=str(proposed.get('flrig_port') or PORTS['flrig_port']))
                if not current.get('fast_light_config_id'):
                    app.setdefault('secondary_port', str(proposed.get('fldigi_port') or 7362))
                    app.setdefault('settings_identity_mode', 'default')
                # The outer radio service publishes reviewed Use flags before
                # the inner app draft runs. Its baseline must reflect our own
                # already-fenced flag writes, without weakening the outer CAS.
                app_base = {**base, **{f'{name}_enabled': bool(proposed.get(f'use_{name}'))
                            for name in ('flrig', 'fldigi', 'flmsg', 'flamp') if f'use_{name}' in changed}}
                drafts['fast_light'] = {**base, 'operator_base_values': app_base, 'operator_values': app}
            if not fast_selected and current.get('fast_light_config_id') and set(changed) & {'flrig_host', 'flrig_port'}:
                raise ValueError('Use Edit Apps to change the endpoint of an inactive Fast Light assignment.')
            js8_edit = bool(set(changed) & {'js8_host', 'js8_port'})
            if proposed.get('use_js8call') and (js8_edit or changed.get('use_js8call') or (changed.get('control_backend') == 'js8call' and not current.get('js8_instance_id'))):
                base = context['application_seeds']['js8call']
                app = {**deepcopy(base), 'host': str(proposed.get('js8_host') or '127.0.0.1'),
                       'port': str(proposed.get('js8_port') or PORTS['js8_port'])}
                app.setdefault('settings_identity_mode', 'default')
                drafts['js8call'] = {**base, 'operator_base_values': base, 'operator_values': app}
            if changed.get('use_varac') and not current.get('varac_node_id'):
                raise ValueError('Configure the VarAC instance in Edit Apps before enabling VarAC.')
            # Unassigned/inactive endpoint preferences do not authorize an app.
            # Retain them as radio fields without inventing application rows.
            endpoint_only = {key: value for key, value in changed.items()
                             if key in {'js8_host', 'js8_port'} and 'js8call' not in drafts}
            if scope == 'control' and not drafts and not (set(changed) & USE_FIELDS):
                # A backend/rigctld-only edit does not review or migrate any
                # existing application identity, including older installations.
                saved = store.save_device_profile({'id': radio_id, **changed, 'scheduler_enabled': 0})
            else:
                if endpoint_only and current.get('js8_instance_id'):
                    raise ValueError('Use Edit Apps to change the endpoint of an inactive JS8Call assignment.')
                if endpoint_only:
                    store.save_device_profile({'id': radio_id, **endpoint_only})
                result = save_operator_radio(store, {'base': context['profile'],
                    'values': {key: value for key, value in changed.items() if key not in endpoint_only},
                    'application_drafts': drafts})
                if result['result'].state != 'saved':
                    raise ValueError(' '.join(str(issue) for issue in result['result'].issues))
                saved = store.get_device_profile(radio_id)
        transaction.complete()
    return saved
