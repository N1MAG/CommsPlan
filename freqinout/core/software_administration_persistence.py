"""Field ownership and atomic saves for compact Software Administration forms."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


FAMILY_STATE_KEYS: dict[str, tuple[str, ...]] = {
    "js8call": (
        "path_js8call", "js8_profile_path", "js8_host", "js8_port",
        "js8_offset_hz", "js8_directed_path",
    ),
    "fast_light": (
        "path_flrig", "flrig_port", "path_fldigi", "fldigi_host",
        "fldigi_port", "fldigi_log_path", "fldigi_checkin_dir",
        "path_flmsg", "path_flamp", "message_paths.flmsg",
        "message_paths.flamp",
    ),
    "varac": (
        "varac_path", "varac_ini_path", "varac_launch_cmd",
        "message_paths.varac", "varac_incoming_path", "varac_outbox_dir",
        "varac_bbs_dir", "varac_bbs_archive_dir", "varac_bbs_enabled",
        "varac_bbs_limit_access_enabled", "varac_bbs_allowed_callsigns",
        "varac_bbs_allowed_group_sources", "varac_bbs_announce_enabled",
        "varac_bbs_auto_archive_enabled", "varac_bbs_auto_archive_days",
        "varac_guard_allow_bbs_allowed_callsigns",
        "varac_guard_allow_operator_trusted", "varac_bbs_vault_enabled",
        "varac_guard_access_policy_v1",
        "varac_bbs_vault_managed_root", "varac_bbs_vault_default_location_id",
        "varac_bbs_vault_global_code_policy", "varac_bbs_vault_trigger_mode",
        "varac_bbs_vault_return_mode", "varac_bbs_vault_failed_attempt_limit",
        "varac_bbs_vault_cooldown_seconds", "varac_bbs_vault_idle_timeout_seconds",
        "varac_bbs_vault_flamp_enabled", "varac_bbs_vault_flamp_relay_dir",
        "varac_bbs_vault_flamp_listing_max_age_days",
        "varac_bbs_vault_locations_v1", "varac_bbs_sweeper_rules_v1",
        "varac_bbs_vault_runtime_state_v1", "varac_bbs_vault_last_summary",
    ),
    "commstat": ("path_commstat",),
    "external_spotter": ("path_js8spotter",),
    "fio_spotter": ("js8_forms_path",),
}


def family_state_keys(family_key: str) -> tuple[str, ...]:
    return FAMILY_STATE_KEYS.get(str(family_key or "").strip().lower(), ())


def _read(source: Mapping[str, Any], dotted_key: str) -> tuple[bool, Any]:
    current: Any = source
    for part in dotted_key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _write(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    current = target
    for part in parts[:-1]:
        existing = current.get(part)
        copied = deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
        current[part] = copied
        current = copied
    current[parts[-1]] = deepcopy(value)


def merge_family_state(
    persisted_state: Mapping[str, Any],
    draft_state: Mapping[str, Any],
    family_key: str,
) -> dict[str, Any]:
    """Merge only one family's owned values into a persisted base state."""

    merged = deepcopy(dict(persisted_state or {}))
    for key in family_state_keys(family_key):
        present, value = _read(draft_state, key)
        if present:
            _write(merged, key, value)
    source_id = draft_state.get("_source_profile_id")
    if source_id not in (None, "", 0, "0"):
        merged["_source_profile_id"] = source_id
    return merged


def family_state_changed(
    persisted_state: Mapping[str, Any],
    draft_state: Mapping[str, Any],
    family_key: str,
) -> bool:
    for key in family_state_keys(family_key):
        base_present, base_value = _read(persisted_state, key)
        draft_present, draft_value = _read(draft_state, key)
        if draft_present and (not base_present or draft_value != base_value):
            return True
    return False


__all__ = [
    "FAMILY_STATE_KEYS",
    "family_state_changed",
    "family_state_keys",
    "merge_family_state",
]


# Compact legacy forms own only these columns. Native identity/launch choices
# continue through the operator application adapter, never this projection.
_PROFILE_COLUMNS = {
    'path_js8call': 'js8_install_path', 'js8_profile_path': 'js8_profile_path',
    'js8_host': 'js8_host', 'js8_port': 'js8_port', 'js8_offset_hz': 'js8_offset_hz',
    'js8_directed_path': 'js8_directed_path', 'js8_forms_path': 'js8_forms_path',
    'path_js8spotter': 'spotter_launch_path', 'path_commstat': 'commstat_launch_path',
    'path_flrig': 'flrig_path', 'path_fldigi': 'fldigi_path', 'path_flmsg': 'flmsg_path',
    'path_flamp': 'flamp_path', 'message_paths.flmsg': 'flmsg_message_path',
    'message_paths.flamp': 'flamp_message_path', 'message_paths.varac': 'varac_incoming_path',
    'varac_path': 'varac_install_path', 'varac_launch_cmd': 'launch_cmd',
}
_APP_COLUMNS = {
    'path_js8call': ('js8call', 'install_path'), 'js8_profile_path': ('js8call', 'profile_path'),
    'js8_host': ('js8call', 'host'), 'js8_port': ('js8call', 'port'),
    'js8_offset_hz': ('js8call', 'offset_hz'), 'js8_directed_path': ('js8call', 'directed_path'),
    'js8_forms_path': ('js8call', 'forms_path'), 'path_commstat': ('js8call', 'commstat_launch_path'),
    'path_js8spotter': ('js8call', 'spotter_launch_path'),
    'path_flrig': ('fast_light', 'flrig_path'), 'path_fldigi': ('fast_light', 'fldigi_path'),
    'flrig_port': ('fast_light', 'flrig_port'), 'fldigi_host': ('fast_light', 'fldigi_host'),
    'fldigi_port': ('fast_light', 'fldigi_port'), 'fldigi_log_path': ('fast_light', 'fldigi_log_path'),
    'fldigi_checkin_dir': ('fast_light', 'fldigi_checkin_dir'),
    'varac_path': ('varac', 'install_path'), 'varac_ini_path': ('varac', 'ini_path'),
    'varac_launch_cmd': ('varac', 'launch_cmd'), 'message_paths.varac': ('varac', 'incoming_path'),
    'varac_incoming_path': ('varac', 'incoming_path'),
}


def save_software_fields(store, expected_profile, baseline, proposed, families):
    """Atomic changed-field save for compact legacy forms; no implicit Use changes.

    A family's saved paths are evidence, not permission to enable that family.
    Read the current rows under the transaction and retain all unedited columns.
    """
    from freqinout.core.configuration_edit_session import ConfigurationConflict
    from freqinout.core.configuration_attention import VARAC_GUARD_FIELDS, varac_guard_configuration_issues
    from freqinout.core.multi_radio_store import _validate_software_application_claims_conn, _utc_now_iso
    families = tuple(families)
    if not families or any(not family_state_keys(family) for family in families):
        raise ValueError('Choose a supported software family before saving.')
    changed = {}
    for family in families:
        for key in family_state_keys(family):
            present, value = _read(proposed, key)
            base_present, base_value = _read(baseline, key)
            if present and (not base_present or value != base_value):
                changed[key] = value
    radio_id = int(expected_profile['id'])
    if proposed.get('_source_profile_id') and int(proposed['_source_profile_id']) != radio_id:
        raise ConfigurationConflict('This software draft belongs to a different radio. Select the radio again.')
    numeric = {'js8_port', 'js8_offset_hz', 'flrig_port', 'fldigi_port', 'varac_bbs_auto_archive_days'}
    for key in numeric & changed.keys():
        changed[key] = int(changed[key])
        if key.endswith('port') and not 1 <= changed[key] <= 65535:
            raise ValueError('Enter a port from 1 to 65535.')
    if any(key in changed for key in VARAC_GUARD_FIELDS):
        policy = {key: _read(proposed, key)[1] for key in VARAC_GUARD_FIELDS}
        issues = varac_guard_configuration_issues(policy)
        if issues:
            raise ValueError(' '.join(issues))
    def equivalent(a, b):
        return str(a if a is not None else '') == str(b if b is not None else '')
    with store.configuration_save_transaction() as transaction:
        with store.connect() as conn:
            current = store.get_device_profile(radio_id)
            if current is None:
                raise ConfigurationConflict('This radio no longer exists.')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(device_profiles)')}
            updates, app_updates = {}, {}
            links = {'js8call': 'js8_instance_id', 'fast_light': 'fast_light_config_id', 'varac': 'varac_node_id'}
            tables = {'js8call': 'js8_instances', 'fast_light': 'fast_light_configs', 'varac': 'varac_nodes'}
            for key, value in changed.items():
                column = _PROFILE_COLUMNS.get(key, key)
                if column not in columns:
                    raise ValueError(f'{key} requires the application configuration editor; this form cannot save it.')
                if not equivalent(current.get(column), expected_profile.get(column)):
                    raise ConfigurationConflict(f'{key} changed after this editor opened; review both values.')
                if key in _APP_COLUMNS:
                    family, app_column = _APP_COLUMNS[key]
                    instance_id = int(current.get(links[family]) or 0)
                    if not instance_id:
                        raise ValueError('Assign this application through Manage instances before editing its settings.')
                    if instance_id != int(expected_profile.get(links[family]) or 0):
                        raise ConfigurationConflict('The radio application assignment changed; reopen it to review.')
                    row = dict(conn.execute(f'SELECT * FROM {tables[family]} WHERE id=?', (instance_id,)).fetchone())
                    if not equivalent(row.get(app_column), expected_profile.get(column)):
                        raise ConfigurationConflict(f'{key} application and radio records disagree; review Edit configuration.')
                    app_updates.setdefault(family, (row, {}))[1][app_column] = value
                if isinstance(value, (dict, list)):
                    import json
                    value = json.dumps(value, sort_keys=True, separators=(',', ':'))
                updates[column] = value
            if 'varac' in families and {'varac_bbs_dir', 'varac_bbs_archive_dir'} & updates.keys():
                if conn.execute('SELECT 1 FROM varac_cluster_members WHERE device_profile_id=? AND enabled=1', (radio_id,)).fetchone():
                    raise ValueError('Open Cluster settings to change shared BBS and archive folders.')
            for family, (row, fields) in app_updates.items():
                app = {**row, **fields}
                if ((family == 'js8call' and {'host', 'port'} & fields.keys())
                        or (family == 'fast_light' and {'flrig_host', 'flrig_port', 'fldigi_host', 'fldigi_port'} & fields.keys())):
                    _validate_software_application_claims_conn(conn, family, app)
                elif family == 'varac':
                    _validate_software_application_claims_conn(conn, family, {'id': app['id'], **{k: v for k, v in fields.items() if k in {'ini_path', 'incoming_path'}}})
                {'js8call': store.save_js8_instance, 'fast_light': store.save_fast_light_config,
                 'varac': store.save_varac_node}[family](app)
            if updates:
                updates['updated_utc'] = _utc_now_iso()
                conn.execute('UPDATE device_profiles SET ' + ', '.join(f'{key}=?' for key in updates) + ' WHERE id=?',
                             [*updates.values(), radio_id])
            result = store.get_device_profile(radio_id)
        transaction.complete()
    return result


def only_varac_guard_changes(baseline, proposed):
    from freqinout.core.configuration_attention import VARAC_GUARD_FIELDS
    changed = {key for key in family_state_keys('varac')
               if _read(proposed, key)[0] and _read(proposed, key) != _read(baseline, key)}
    return bool(changed) and changed.issubset(VARAC_GUARD_FIELDS)
