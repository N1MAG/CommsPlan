"""Pure field partitioning for scoped Software administration saves."""

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
