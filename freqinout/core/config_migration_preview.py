from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple

from freqinout.core.config_paths import get_config_dir
from freqinout.core.multi_radio_store import (
    DEFAULT_DEVICE_NAME,
    DEFAULT_OPERATING_NAME,
    SUPPORTED_SOFTWARE_ROLES,
    _seed_device_defaults,
    _seed_operating_defaults,
)


@dataclass(frozen=True)
class SingleRigUpgradePreview:
    radio_profile: Mapping[str, Any]
    operating_profile: Mapping[str, Any]
    enabled_software_roles: Tuple[str, ...]
    backup_paths: Tuple[str, ...]
    referenced_paths_not_backed_up: Tuple[str, ...]
    summary: str
    warnings: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SingleRigUpgradeApplyPlan:
    preview: SingleRigUpgradePreview
    backup_reason: str
    backup_paths: Tuple[str, ...]
    can_apply: bool
    blockers: Tuple[str, ...] = field(default_factory=tuple)


def build_single_rig_upgrade_preview(
    settings_values: Mapping[str, Any],
    *,
    radio_name: str = "",
    operating_plan_name: str = "",
    config_dir: Optional[Path] = None,
    extra_backup_paths: Iterable[Path] = (),
) -> SingleRigUpgradePreview:
    radio_profile = dict(
        _seed_device_defaults(
            settings_values,
            js8_instance_id=0,
            fast_light_config_id=0,
            varac_node_id=0,
        )
    )
    requested_radio_name = str(radio_name or "").strip()
    if requested_radio_name:
        radio_profile["name"] = requested_radio_name
        radio_profile["needs_operator_name"] = 0
    elif not str(radio_profile.get("name", "") or "").strip():
        radio_profile["name"] = DEFAULT_DEVICE_NAME

    operating_profile = dict(_seed_operating_defaults(settings_values))
    requested_plan_name = str(operating_plan_name or "").strip()
    if requested_plan_name:
        operating_profile["name"] = requested_plan_name
    elif not str(operating_profile.get("name", "") or "").strip():
        operating_profile["name"] = DEFAULT_OPERATING_NAME

    roles = _enabled_roles_from_radio_profile(radio_profile)
    referenced_data_paths = collect_referenced_data_paths_not_backed_up(settings_values)
    combined_extra_backup_paths = tuple(extra_backup_paths) + tuple(Path(path) for path in referenced_data_paths)
    backups = collect_single_rig_upgrade_backup_paths(
        settings_values,
        config_dir=config_dir,
        extra_backup_paths=combined_extra_backup_paths,
    )
    backed_up = {str(Path(path)) for path in backups}
    referenced_paths_not_backed_up = tuple(
        path for path in referenced_data_paths if str(Path(path)) not in backed_up
    )
    warnings = _preview_warnings(radio_profile, roles, referenced_paths_not_backed_up)
    return SingleRigUpgradePreview(
        radio_profile=radio_profile,
        operating_profile=operating_profile,
        enabled_software_roles=roles,
        backup_paths=backups,
        referenced_paths_not_backed_up=referenced_paths_not_backed_up,
        summary=_preview_summary(radio_profile, roles),
        warnings=warnings,
    )


def build_single_rig_upgrade_apply_plan(
    settings_values: Mapping[str, Any],
    *,
    radio_name: str = "",
    operating_plan_name: str = "",
    config_dir: Optional[Path] = None,
    extra_backup_paths: Iterable[Path] = (),
    backup_reason: str = "pre-multirig",
) -> SingleRigUpgradeApplyPlan:
    preview = build_single_rig_upgrade_preview(
        settings_values,
        radio_name=radio_name,
        operating_plan_name=operating_plan_name,
        config_dir=config_dir,
        extra_backup_paths=extra_backup_paths,
    )
    blockers = []
    if not preview.backup_paths:
        blockers.append("No CommsPlan configuration path is available to back up before migration.")
    elif not Path(preview.backup_paths[0]).exists():
        blockers.append("CommsPlan configuration path does not exist, so it cannot be backed up before migration.")
    if not str(preview.radio_profile.get("name", "") or "").strip():
        blockers.append("A first radio name is required before migration.")
    return SingleRigUpgradeApplyPlan(
        preview=preview,
        backup_reason=str(backup_reason or "pre-multirig").strip() or "pre-multirig",
        backup_paths=preview.backup_paths,
        can_apply=not blockers,
        blockers=tuple(blockers),
    )


def collect_single_rig_upgrade_backup_paths(
    settings_values: Mapping[str, Any],
    *,
    config_dir: Optional[Path] = None,
    extra_backup_paths: Iterable[Path] = (),
) -> Tuple[str, ...]:
    candidates = [Path(config_dir) if config_dir is not None else get_config_dir()]
    for key in (
        "js8_profile_path",
        "js8_directed_path",
        "js8_forms_path",
        "varac_ini_path",
        "varac_db_path",
    ):
        value = str(settings_values.get(key, "") or "").strip()
        if value:
            candidates.append(Path(value))
    for path in extra_backup_paths:
        candidates.append(Path(path))
    return tuple(str(path) for path in _unique_paths(candidates))


def collect_referenced_data_paths_not_backed_up(settings_values: Mapping[str, Any]) -> Tuple[str, ...]:
    candidates = []
    for key in (
        "fldigi_log_path",
        "fldigi_checkin_dir",
        "varac_outbox_dir",
        "varac_bbs_dir",
        "varac_bbs_archive_dir",
    ):
        value = str(settings_values.get(key, "") or "").strip()
        if value:
            candidates.append(Path(value))
    message_paths = settings_values.get("message_paths", {}) or {}
    if isinstance(message_paths, Mapping):
        for value in message_paths.values():
            txt = str(value or "").strip()
            if txt:
                candidates.append(Path(txt))
    return tuple(str(path) for path in _unique_paths(candidates))


def _enabled_roles_from_radio_profile(radio_profile: Mapping[str, Any]) -> Tuple[str, ...]:
    role_flags = {
        "fast_light": bool(radio_profile.get("use_flrig") or radio_profile.get("use_fldigi")),
        "js8call": bool(radio_profile.get("use_js8call")),
        "js8spotter": bool(radio_profile.get("use_js8spotter")),
        "commstat": bool(radio_profile.get("use_commstat")),
        "flmsg": bool(radio_profile.get("use_flmsg")),
        "flamp": bool(radio_profile.get("use_flamp")),
        "varac": bool(radio_profile.get("use_varac")),
    }
    return tuple(role for role in sorted(SUPPORTED_SOFTWARE_ROLES) if role_flags.get(role))


def _preview_warnings(
    radio_profile: Mapping[str, Any],
    roles: Tuple[str, ...],
    referenced_paths_not_backed_up: Tuple[str, ...],
) -> Tuple[str, ...]:
    warnings = []
    control_backend = str(radio_profile.get("control_backend", "") or "manual").strip().lower()
    if control_backend == "manual":
        warnings.append("No radio control app is configured yet; CommsPlan will create the radio as Manual control.")
    if "varac" not in roles:
        warnings.append("VarAC will remain disabled unless the operator enables it.")
    if referenced_paths_not_backed_up:
        warnings.append("Message, log, and BBS data folders are referenced but not backed up by the upgrade preview.")
    return tuple(warnings)


def _preview_summary(radio_profile: Mapping[str, Any], roles: Tuple[str, ...]) -> str:
    name = str(radio_profile.get("name", "") or DEFAULT_DEVICE_NAME).strip() or DEFAULT_DEVICE_NAME
    control_backend = str(radio_profile.get("control_backend", "") or "manual").strip().lower()
    control_label = _control_label(control_backend)
    role_labels = ", ".join(_role_label(role) for role in roles)
    if role_labels and role_labels != control_label:
        return f"CommsPlan will create first radio '{name}' using {control_label} with {role_labels}."
    return f"CommsPlan will create first radio '{name}' using {control_label}."


def _control_label(control_backend: str) -> str:
    return {
        "flrig": "FLRig",
        "js8call": "JS8Call",
        "rigctld": "RigCtlD",
        "manual": "Manual control",
    }.get(control_backend, "Manual control")


def _role_label(role: str) -> str:
    return {
        "fast_light": "FLRig/FLDigi",
        "js8call": "JS8Call",
        "js8spotter": "JS8Spotter",
        "commstat": "CommStat",
        "flmsg": "FLMsg",
        "flamp": "FLAmp",
        "varac": "VarAC",
    }.get(role, role)


def _unique_paths(paths: Iterable[Path]) -> Tuple[Path, ...]:
    out = []
    seen = set()
    for raw in paths:
        txt = str(raw or "").strip()
        if not txt:
            continue
        path = Path(txt).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return tuple(out)
