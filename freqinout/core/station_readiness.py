from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha1
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from freqinout.core.varac_bbs_vault import load_vault_locations


STATUS_DISPLAY_ITEMS: Sequence[Tuple[str, str]] = (
    ("FLRig", "FLRig"),
    ("RigCtlD", "RigCtlD"),
    ("FLDigi", "FLDigi"),
    ("FLMsg", "FLMsg"),
    ("FLAmp", "FLAmp"),
    ("JS8Call_API", "JS8"),
    ("VarAC", "VarAC"),
    ("JS8Spotter", "CommsPlan Spotter"),
    ("CommStat", "CommStat"),
)


@dataclass(frozen=True)
class ReadinessStateSpec:
    key: str
    label: str
    sentence_fragment: str
    description: str
    card_level: str


READINESS_STATE_SPECS: Dict[str, ReadinessStateSpec] = {
    "ready": ReadinessStateSpec(
        key="ready",
        label="Ready",
        sentence_fragment="is ready",
        description="Configured for the workflows CommsPlan is currently tracking.",
        card_level="success",
    ),
    "needs_setup": ReadinessStateSpec(
        key="needs_setup",
        label="Needs Setup",
        sentence_fragment="needs setup",
        description="Required setup is missing before CommsPlan can rely on this area.",
        card_level="danger",
    ),
    "degraded": ReadinessStateSpec(
        key="degraded",
        label="Degraded",
        sentence_fragment="is degraded",
        description="Core setup is present, but one or more recommended items should be reviewed for a fuller workflow.",
        card_level="warning",
    ),
    "not_enabled": ReadinessStateSpec(
        key="not_enabled",
        label="Not Enabled",
        sentence_fragment="is not enabled",
        description="This area is not enabled for the current station or radio workflow.",
        card_level="info",
    ),
    "external_manual": ReadinessStateSpec(
        key="external_manual",
        label="External / Manual",
        sentence_fragment="is managed externally",
        description="This area is intentionally managed outside CommsPlan or excluded from CommsPlan-managed launch behavior.",
        card_level="info",
    ),
}


@dataclass(frozen=True)
class ReadinessIssue:
    severity: str
    section_key: str
    scope: str
    message: str
    resolution_hint: str = ""
    radio_id: Optional[int] = None
    integration_key: str = ""
    deep_link_target: str = ""
    state_key: str = ""


@dataclass(frozen=True)
class RadioReadinessSummary:
    radio_id: Optional[int]
    name: str
    overall_state: str
    required_count: int
    recommended_count: int
    informational_count: int
    messages: Tuple[str, ...]
    state_counts: Tuple[Tuple[str, int], ...] = ()

    def state_count(self, state_key: str) -> int:
        target = str(state_key or "").strip().lower()
        for key, count in self.state_counts:
            if str(key or "").strip().lower() == target:
                return int(count or 0)
        return 0


@dataclass(frozen=True)
class ReadinessReport:
    overall_state: str
    issues: Tuple[ReadinessIssue, ...]
    radio_summaries: Tuple[RadioReadinessSummary, ...]
    required_count: int
    recommended_count: int
    informational_count: int
    digest: str
    state_counts: Tuple[Tuple[str, int], ...] = ()

    def first_actionable_issue(self) -> Optional[ReadinessIssue]:
        for severity in ("required", "recommended", "informational"):
            for issue in self.issues:
                if issue.severity == severity:
                    return issue
        return None

    def summary_for_radio(self, radio_id: Optional[int]) -> Optional[RadioReadinessSummary]:
        target = int(radio_id or 0)
        for summary in self.radio_summaries:
            if int(summary.radio_id or 0) == target:
                return summary
        return None


def should_show_startup_review(
    report: ReadinessReport,
    *,
    dismissed_digest: str = "",
    suppressed_version: str = "",
    current_version: str = "",
) -> bool:
    actionable_count = int(report.required_count) + int(report.recommended_count)
    if actionable_count <= 0:
        return False
    if current_version and str(suppressed_version or "").strip() == str(current_version or "").strip():
        return False
    if report.digest and str(dismissed_digest or "").strip() == str(report.digest or "").strip():
        return False
    return True


def readiness_state_spec(state_key: str) -> ReadinessStateSpec:
    key = str(state_key or "").strip().lower() or "ready"
    return READINESS_STATE_SPECS.get(key, READINESS_STATE_SPECS["ready"])


def readiness_state_label(state_key: str) -> str:
    return readiness_state_spec(state_key).label


def readiness_state_card_level(state_key: str) -> str:
    return readiness_state_spec(state_key).card_level


def readiness_state_description(state_key: str) -> str:
    return readiness_state_spec(state_key).description


def format_readiness_issue(issue: ReadinessIssue, *, include_resolution: bool = True) -> str:
    detail = str(issue.message or "").strip()
    hint = str(issue.resolution_hint or "").strip()
    if include_resolution and hint:
        detail += f" ({hint})"
    return detail


def _issue_state_key(severity: str, fallback: str = "") -> str:
    key = str(fallback or "").strip().lower()
    if key:
        return key
    sev = str(severity or "").strip().lower()
    if sev == "required":
        return "needs_setup"
    if sev == "recommended":
        return "degraded"
    return "ready"


def _state_counts_from_issues(issues: Iterable[ReadinessIssue]) -> Tuple[Tuple[str, int], ...]:
    counts: Dict[str, int] = {}
    for issue in issues:
        state_key = _issue_state_key(issue.severity, issue.state_key)
        if not state_key or state_key == "ready":
            continue
        counts[state_key] = int(counts.get(state_key, 0) or 0) + 1
    ordered_keys = ("needs_setup", "degraded", "not_enabled", "external_manual")
    return tuple((key, int(counts[key])) for key in ordered_keys if int(counts.get(key, 0) or 0) > 0)


def _summary_overall_state(
    *,
    required_count: int,
    recommended_count: int,
    state_counts: Sequence[Tuple[str, int]],
) -> str:
    if required_count > 0:
        return "needs_setup"
    if recommended_count > 0:
        return "degraded"
    state_map = {str(key or "").strip().lower(): int(count or 0) for key, count in state_counts}
    if int(state_map.get("not_enabled", 0) or 0) > 0:
        return "not_enabled"
    if int(state_map.get("external_manual", 0) or 0) > 0:
        return "external_manual"
    return "ready"


def readiness_summary_badge_text(summary: RadioReadinessSummary) -> str:
    return readiness_state_label(summary.overall_state)


def readiness_summary_status_text(summary: RadioReadinessSummary, *, subject: str = "This area") -> str:
    spec = readiness_state_spec(summary.overall_state)
    prefix = f"{subject} {spec.sentence_fragment}."
    if summary.overall_state == "needs_setup":
        return f"{prefix} {int(summary.required_count)} required item(s) still need attention."
    if summary.overall_state == "degraded":
        return f"{prefix} {int(summary.recommended_count)} recommended item(s) should be reviewed."
    if summary.overall_state in {"not_enabled", "external_manual"}:
        return f"{prefix} {spec.description}"
    return prefix


def readiness_report_overall_text(report: ReadinessReport) -> str:
    spec = readiness_state_spec(report.overall_state)
    if report.overall_state == "needs_setup":
        degraded_count = sum(
            int(count or 0) for key, count in report.state_counts if str(key or "").strip().lower() == "degraded"
        )
        if degraded_count > 0:
            return (
                f"{spec.label}. {int(report.required_count)} required item(s) still need attention and "
                f"{int(degraded_count)} area(s) are degraded."
            )
        return f"{spec.label}. {int(report.required_count)} required item(s) still need attention."
    if report.overall_state == "degraded":
        return f"{spec.label}. {int(report.recommended_count)} recommended item(s) should be reviewed."
    return f"{spec.label}. {spec.description}"


def readiness_report_detail_text(report: ReadinessReport, *, title: str = "") -> str:
    lines: List[str] = []
    heading = str(title or "").strip()
    if heading:
        lines.append(heading)
    lines.append(f"Overall: {readiness_report_overall_text(report)}")
    for issue in report.issues:
        state_text = readiness_state_label(issue.state_key or issue.severity)
        lines.append(f"- [{state_text}] {format_readiness_issue(issue)}")
    return "\n".join(lines)


def _text(source: Mapping[str, Any], key: str, default: str = "") -> str:
    try:
        return str(source.get(key, default) or "").strip()
    except Exception:
        return str(default or "").strip()


def _text_any(source: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = _text(source, key)
        if value:
            return value
    return str(default or "").strip()


def _truthy(source: Mapping[str, Any], key: str, default: bool = False) -> bool:
    try:
        return bool(source.get(key, default))
    except Exception:
        return bool(default)


def _truthy_any(source: Mapping[str, Any], *keys: str, default: bool = False) -> bool:
    for key in keys:
        try:
            if key in source:
                return bool(source.get(key, default))
        except Exception:
            continue
    return bool(default)


def _int_text(value: Any) -> str:
    return str(value or "").strip()


def _enabled_profiles(device_profiles: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in device_profiles:
        if not isinstance(row, Mapping):
            continue
        data = dict(row)
        if int(data.get("enabled", 1) or 0) != 1:
            continue
        rows.append(data)
    return rows


def _profile_display_name(profile: Mapping[str, Any]) -> str:
    name = _text(profile, "name")
    if name:
        return name
    ident = int(profile.get("id", 0) or 0)
    return f"Radio {ident}" if ident > 0 else "Radio"


def _profile_scope(profile: Mapping[str, Any]) -> str:
    ident = int(profile.get("id", 0) or 0)
    return f"radio:{ident}" if ident > 0 else "radio"


def _profile_is_primary_or_active(profile: Mapping[str, Any]) -> bool:
    return int(profile.get("runtime_primary", 0) or 0) == 1 or int(profile.get("runtime_active", 0) or 0) == 1


def _profile_backend(profile: Mapping[str, Any]) -> str:
    return _text(profile, "control_backend", "manual").lower() or "manual"


def _profile_explicit_software_flag(profile: Mapping[str, Any], key: str) -> Optional[bool]:
    normalized = str(key or "").strip().lower()
    field_map = {
        "flrig": "use_flrig",
        "fldigi": "use_fldigi",
        "flmsg": "use_flmsg",
        "flamp": "use_flamp",
        "js8call": "use_js8call",
        "js8spotter": "use_js8spotter",
        "commstat": "use_commstat",
        "varac": "use_varac",
    }
    field_name = field_map.get(normalized)
    if not field_name:
        return None
    explicit = profile.get(field_name)
    if explicit in (None, ""):
        return None
    return bool(int(explicit or 0))


def _profiles_all_explicitly_disabled(profiles: Iterable[Mapping[str, Any]], key: str) -> bool:
    any_explicit = False
    for profile in profiles:
        explicit = _profile_explicit_software_flag(profile, key)
        if explicit is None:
            return False
        any_explicit = True
        if explicit:
            return False
    return any_explicit


def _profile_software_enabled(profile: Mapping[str, Any], key: str) -> bool:
    normalized = str(key or "").strip().lower()
    backend = _profile_backend(profile)
    if normalized == "flrig":
        explicit = _profile_explicit_software_flag(profile, "flrig")
        if explicit is not None:
            return explicit
        return backend == "flrig" or bool(_text(profile, "flrig_path"))
    if normalized == "fldigi":
        explicit = _profile_explicit_software_flag(profile, "fldigi")
        if explicit is not None:
            return explicit
        return bool(_text(profile, "fldigi_host") or _int_text(profile.get("fldigi_port")))
    if normalized == "js8call":
        explicit = _profile_explicit_software_flag(profile, "js8call")
        if explicit is not None:
            return explicit
        return backend == "js8call" or bool(_text(profile, "js8_host") or _int_text(profile.get("js8_port")) or _text(profile, "js8_install_path"))
    if normalized == "js8spotter":
        explicit = _profile_explicit_software_flag(profile, "js8spotter")
        if explicit is not None:
            return explicit
        return bool(_text(profile, "spotter_launch_path"))
    if normalized == "commstat":
        explicit = _profile_explicit_software_flag(profile, "commstat")
        if explicit is not None:
            return explicit
        return bool(_text(profile, "commstat_launch_path"))
    if normalized == "varac":
        explicit = _profile_explicit_software_flag(profile, "varac")
        if explicit is not None:
            return explicit
        return any(
            [
                _text(profile, "varac_install_path"),
                _text(profile, "varac_db_path"),
                _text(profile, "varac_ini_path"),
                _text(profile, "launch_cmd"),
            ]
        )
    return False


def _profile_text_any(profiles: Iterable[Mapping[str, Any]], *keys: str) -> str:
    for profile in profiles:
        if not isinstance(profile, Mapping):
            continue
        text = _text_any(profile, *keys)
        if text:
            return text
    return ""


def _issue_deep_link(section_key: str, radio_id: Optional[int]) -> str:
    target = str(section_key or "freqinout").strip().lower() or "freqinout"
    if radio_id:
        return f"{target}:radio:{int(radio_id)}"
    return target


def _assignment_validation_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("validation_status_json")
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
        except Exception:
            return {}
        if isinstance(parsed, Mapping):
            return parsed
    return {}


def _assignment_validation_messages(validation: Mapping[str, Any], state: str) -> List[str]:
    keys = ["blocked"] if state == "blocked" else ["warnings"]
    messages: List[str] = []
    for key in keys:
        value = validation.get(key)
        if isinstance(value, str) and value.strip():
            messages.append(value.strip())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                text = str(item or "").strip()
                if text:
                    messages.append(text)
    return messages


def _append_assignment_rf_guard_issues(
    issues: List[ReadinessIssue],
    assigned_schedule_status: Iterable[Mapping[str, Any]],
) -> None:
    for row in assigned_schedule_status:
        if not isinstance(row, Mapping):
            continue
        validation = _assignment_validation_payload(row)
        state = str(validation.get("state") or row.get("guard_status") or "").strip().lower()
        if state not in {"warning", "blocked"}:
            continue
        try:
            radio_id = int(row.get("device_profile_id", 0) or 0) or None
        except Exception:
            radio_id = None
        radio_name = str(row.get("device_name") or (f"Radio {radio_id}" if radio_id else "Radio")).strip()
        plan_name = str(row.get("frequency_plan_name") or "assigned Frequency Plan").strip()
        validation_messages = _assignment_validation_messages(validation, state)
        first_message = validation_messages[0] if validation_messages else "RF Guard found a schedule assignment mismatch."
        extra_count = max(0, len(validation_messages) - 1)
        suffix = f" (+{extra_count} more)" if extra_count else ""
        severity = "required" if state == "blocked" else "recommended"
        state_key = "needs_setup" if state == "blocked" else "degraded"
        issues.append(
            ReadinessIssue(
                severity=severity,
                section_key="schedule_assignments",
                scope=f"radio:{radio_id}" if radio_id else "global",
                radio_id=radio_id,
                integration_key="rf_guard",
                message=f"{radio_name}: RF Guard review needed for {plan_name}: {first_message}{suffix}",
                resolution_hint=(
                    "Choose a compatible Frequency Plan, update supported antenna bands, "
                    "or confirm the RF path before transmitting."
                ),
                deep_link_target=_issue_deep_link("schedule_assignments", radio_id),
                state_key=state_key,
            )
        )


def visible_status_programs(
    settings: Mapping[str, Any],
    *,
    device_profiles: Optional[Iterable[Mapping[str, Any]]] = None,
) -> List[Tuple[str, str]]:
    profiles = _enabled_profiles(device_profiles or [])
    active_profiles = [profile for profile in profiles if _profile_is_primary_or_active(profile)]
    status_profiles = active_profiles or profiles
    backend_counts = {
        "flrig": any(_profile_backend(profile) == "flrig" for profile in status_profiles),
        "rigctld": any(_profile_backend(profile) == "rigctld" for profile in status_profiles),
        "js8call": any(_profile_backend(profile) == "js8call" for profile in status_profiles),
    }

    profile_js8_visible = any(_profile_software_enabled(profile, "js8call") for profile in status_profiles)
    profile_fldigi_visible = any(_profile_software_enabled(profile, "fldigi") for profile in status_profiles)
    profile_flmsg_visible = any(_profile_software_enabled(profile, "flmsg") for profile in status_profiles)
    profile_flamp_visible = any(_profile_software_enabled(profile, "flamp") for profile in status_profiles)
    profile_js8spotter_visible = any(_profile_software_enabled(profile, "js8spotter") for profile in status_profiles)
    profile_commstat_visible = any(_profile_software_enabled(profile, "commstat") for profile in status_profiles)
    profile_varac_visible = any(_profile_software_enabled(profile, "varac") for profile in status_profiles)
    js8_global_allowed = not _profiles_all_explicitly_disabled(status_profiles, "js8call")
    flrig_global_allowed = not _profiles_all_explicitly_disabled(status_profiles, "flrig")
    fldigi_global_allowed = not _profiles_all_explicitly_disabled(status_profiles, "fldigi")
    flmsg_global_allowed = not _profiles_all_explicitly_disabled(status_profiles, "flmsg")
    flamp_global_allowed = not _profiles_all_explicitly_disabled(status_profiles, "flamp")
    js8spotter_global_allowed = not _profiles_all_explicitly_disabled(status_profiles, "js8spotter")
    commstat_global_allowed = not _profiles_all_explicitly_disabled(status_profiles, "commstat")
    varac_global_allowed = not _profiles_all_explicitly_disabled(status_profiles, "varac")

    js8_visible = any(
        [
            js8_global_allowed and _text(settings, "path_js8call"),
            js8_global_allowed and _text(settings, "js8_host"),
            js8_global_allowed and _text(settings, "js8_port"),
            js8_global_allowed and _text(settings, "js8_directed_path"),
            js8_global_allowed and _text(settings, "js8_forms_path"),
            profile_js8_visible,
            backend_counts["js8call"],
        ]
    )
    flrig_visible = any(
        [
            flrig_global_allowed and _text(settings, "path_flrig"),
            flrig_global_allowed and _text(settings, "flrig_host"),
            flrig_global_allowed and _text(settings, "flrig_port"),
            any(_profile_software_enabled(profile, "flrig") for profile in status_profiles),
            backend_counts["flrig"],
        ]
    )
    rigctld_visible = any(
        [
            _text(settings, "control_via").upper() == "RIGCTLD",
            _text(settings, "control_via").upper() == "RIGCTLD" and _text(settings, "rig_host"),
            _text(settings, "control_via").upper() == "RIGCTLD" and _text(settings, "rig_port"),
            backend_counts["rigctld"],
        ]
    )
    message_paths = settings.get("message_paths", {})
    if not isinstance(message_paths, Mapping):
        message_paths = {}
    fldigi_visible = any(
        [
            fldigi_global_allowed and _text(settings, "path_fldigi"),
            fldigi_global_allowed and _text(settings, "fldigi_host"),
            fldigi_global_allowed and _text(settings, "fldigi_port"),
            fldigi_global_allowed and _text(settings, "fldigi_log_path"),
            fldigi_global_allowed and _text(settings, "fldigi_checkin_dir"),
            profile_fldigi_visible,
        ]
    )
    flmsg_visible = (flmsg_global_allowed and bool(_text(settings, "path_flmsg"))) or profile_flmsg_visible
    flamp_visible = (flamp_global_allowed and bool(_text(settings, "path_flamp"))) or profile_flamp_visible
    flmsg_visible = flmsg_visible or (flmsg_global_allowed and bool(_text(message_paths, "flmsg")))
    flamp_visible = flamp_visible or (flamp_global_allowed and bool(_text(message_paths, "flamp")))
    varac_visible = any(
        [
            varac_global_allowed and _text(settings, "varac_path"),
            varac_global_allowed and _text(settings, "varac_launch_cmd"),
            varac_global_allowed and _text(message_paths, "varac"),
            varac_global_allowed and _text(settings, "varac_outbox_dir"),
            varac_global_allowed and _text(settings, "varac_bbs_dir"),
            varac_global_allowed and _text(settings, "varac_bbs_archive_dir"),
            profile_varac_visible,
        ]
    )
    js8spotter_visible = any(
        [
            js8spotter_global_allowed and _text(settings, "path_js8spotter"),
            js8spotter_global_allowed and _text(settings, "js8_forms_path"),
            profile_js8spotter_visible,
        ]
    )
    commstat_visible = (commstat_global_allowed and bool(_text(settings, "path_commstat"))) or profile_commstat_visible

    visible = {
        "FLRig": flrig_visible,
        "RigCtlD": rigctld_visible,
        "FLDigi": fldigi_visible,
        "FLMsg": flmsg_visible,
        "FLAmp": flamp_visible,
        "JS8Call_API": js8_visible,
        "VarAC": varac_visible,
        "JS8Spotter": js8spotter_visible,
        "CommStat": commstat_visible,
    }
    return [item for item in STATUS_DISPLAY_ITEMS if visible.get(item[0], False)]


def build_station_readiness_report(
    settings: Mapping[str, Any],
    *,
    device_profiles: Optional[Iterable[Mapping[str, Any]]] = None,
    operating_groups: Optional[Iterable[Any]] = None,
    assigned_schedule_status: Optional[Iterable[Mapping[str, Any]]] = None,
) -> ReadinessReport:
    from freqinout.core.station_bbs_state import project_station_bbs_settings
    settings = project_station_bbs_settings(settings, settings.get('station_bbs_catalog'))
    issues: List[ReadinessIssue] = []
    profiles = _enabled_profiles(device_profiles or [])
    runtime_profiles = [profile for profile in profiles if _profile_is_primary_or_active(profile)] or profiles
    groups = list(operating_groups or [])

    if not _text_any(settings, "operator_callsign", "callsign"):
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="freqinout",
                scope="global",
                message="Callsign missing",
                resolution_hint="Set the station callsign in CommsPlan Settings.",
                deep_link_target=_issue_deep_link("freqinout", None),
                state_key="needs_setup",
            )
        )
    if not _text_any(settings, "operator_grid6", "operator_grid", "grid"):
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="freqinout",
                scope="global",
                message="Grid missing",
                resolution_hint="Set the station grid in CommsPlan Settings.",
                deep_link_target=_issue_deep_link("freqinout", None),
                state_key="needs_setup",
            )
        )

    if not groups:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="operating_groups",
                scope="global",
                message="No HF operating groups configured",
                resolution_hint="Add at least one operating group before relying on schedule and QSY guidance.",
                deep_link_target=_issue_deep_link("operating_groups", None),
                state_key="needs_setup",
            )
        )

    if not profiles:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="radio_profiles",
                scope="global",
                message="No radio profiles configured",
                resolution_hint="Add a radio profile so multi-rig knows what hardware and backend you want to run.",
                deep_link_target=_issue_deep_link("radio_profiles", None),
                state_key="needs_setup",
            )
        )
    else:
        if not any(int(profile.get("runtime_primary", 0) or 0) == 1 for profile in profiles):
            issues.append(
                ReadinessIssue(
                    severity="required",
                    section_key="radio_profiles",
                    scope="global",
                    message="No default radio selected",
                    resolution_hint="Choose one radio profile as the default radio.",
                    deep_link_target=_issue_deep_link("radio_profiles", None),
                    state_key="needs_setup",
                )
            )
        if not any(int(profile.get("runtime_active", 0) or 0) == 1 for profile in profiles):
            issues.append(
                ReadinessIssue(
                    severity="recommended",
                    section_key="radio_profiles",
                    scope="global",
                    message="No active radio profiles",
                    resolution_hint="Activate at least one radio profile if you expect schedule and runtime activity.",
                    deep_link_target=_issue_deep_link("radio_profiles", None),
                    state_key="degraded",
                )
            )

    for profile in profiles:
        scope = _profile_scope(profile)
        radio_id = int(profile.get("id", 0) or 0) or None
        name = _profile_display_name(profile)
        backend = _profile_backend(profile)
        active_or_primary = _profile_is_primary_or_active(profile)
        launch_control_enabled = bool(int(profile.get("launch_enabled", 0) or 0))

        if not active_or_primary:
            issues.append(
                ReadinessIssue(
                    severity="informational",
                    section_key="radio_profiles",
                    scope=scope,
                    radio_id=radio_id,
                    message=f"{name}: configured but inactive",
                    resolution_hint="Activate this radio when you want it to participate in runtime and schedule workflows.",
                    deep_link_target=_issue_deep_link("radio_profiles", radio_id),
                    state_key="not_enabled",
                )
            )
        if active_or_primary and not _text(profile, "radio_model"):
            issues.append(
                ReadinessIssue(
                    severity="recommended",
                    section_key="radio_profiles",
                    scope=scope,
                    radio_id=radio_id,
                    message=f"{name}: radio model not selected",
                    resolution_hint="Select the actual radio model so CommsPlan can offer smarter setup guidance for this radio.",
                    deep_link_target=_issue_deep_link("radio_profiles", radio_id),
                    state_key="degraded",
                )
            )
        if not active_or_primary:
            continue
        _ = launch_control_enabled

        js8_enabled = _profile_software_enabled(profile, "js8call")
        flrig_enabled = _profile_software_enabled(profile, "flrig")
        fldigi_enabled = _profile_software_enabled(profile, "fldigi")
        spotter_enabled = _profile_software_enabled(profile, "js8spotter")
        varac_enabled = _profile_software_enabled(profile, "varac")

        if backend == "js8call" or js8_enabled:
            if not _text(profile, "js8_host"):
                issues.append(
                    ReadinessIssue(
                        severity="required",
                        section_key="radio_profiles",
                        scope=scope,
                        radio_id=radio_id,
                        integration_key="js8call",
                        message=f"{name}: JS8Call host missing",
                        resolution_hint="Set the JS8Call TCP host for this radio.",
                        deep_link_target=_issue_deep_link("radio_profiles", radio_id),
                        state_key="needs_setup",
                    )
                )
            if not _int_text(profile.get("js8_port")):
                issues.append(
                    ReadinessIssue(
                        severity="required",
                        section_key="radio_profiles",
                        scope=scope,
                        radio_id=radio_id,
                        integration_key="js8call",
                        message=f"{name}: JS8Call port missing",
                        resolution_hint="Set the JS8Call TCP port for this radio.",
                        deep_link_target=_issue_deep_link("radio_profiles", radio_id),
                        state_key="needs_setup",
                    )
                )
        if backend == "flrig" or flrig_enabled:
            if not _int_text(profile.get("flrig_port")):
                issues.append(
                    ReadinessIssue(
                        severity="required",
                        section_key="radio_profiles",
                        scope=scope,
                        radio_id=radio_id,
                        integration_key="flrig",
                        message=f"{name}: FLRig XML RPC port missing",
                        resolution_hint="Set the FLRig port for this radio.",
                        deep_link_target=_issue_deep_link("radio_profiles", radio_id),
                        state_key="needs_setup",
                    )
                )
            fldigi_host = _text(profile, "fldigi_host")
            fldigi_port = _int_text(profile.get("fldigi_port"))
            if fldigi_enabled and bool(fldigi_host) != bool(fldigi_port):
                issues.append(
                    ReadinessIssue(
                        severity="recommended",
                        section_key="radio_profiles",
                        scope=scope,
                        radio_id=radio_id,
                        integration_key="fldigi",
                        message=f"{name}: FLDigi endpoint setup is incomplete",
                        resolution_hint="Set both FLDigi host and port if this FLRig radio should also use FLDigi workflows.",
                        deep_link_target=_issue_deep_link("radio_profiles", radio_id),
                        state_key="degraded",
                    )
                )
        elif backend == "rigctld":
            if not _text(profile, "rig_host"):
                issues.append(
                    ReadinessIssue(
                        severity="required",
                        section_key="radio_profiles",
                        scope=scope,
                        radio_id=radio_id,
                        integration_key="rigctld",
                        message=f"{name}: RigCtlD host missing",
                        resolution_hint="Set the RigCtlD host for this radio.",
                        deep_link_target=_issue_deep_link("radio_profiles", radio_id),
                        state_key="needs_setup",
                    )
                )
            if not _int_text(profile.get("rig_port")):
                issues.append(
                    ReadinessIssue(
                        severity="required",
                        section_key="radio_profiles",
                        scope=scope,
                        radio_id=radio_id,
                        integration_key="rigctld",
                        message=f"{name}: RigCtlD port missing",
                        resolution_hint="Set the RigCtlD port for this radio.",
                        deep_link_target=_issue_deep_link("radio_profiles", radio_id),
                        state_key="needs_setup",
                    )
                )
        if varac_enabled:
            missing_parts: List[str] = []
            if not _text(profile, "varac_install_path"):
                missing_parts.append("install")
            if not _text(profile, "varac_db_path"):
                missing_parts.append("db")
            if not _text(profile, "varac_ini_path"):
                missing_parts.append("ini")
            if missing_parts:
                issues.append(
                    ReadinessIssue(
                        severity="recommended",
                        section_key="radio_profiles",
                        scope=scope,
                        radio_id=radio_id,
                        integration_key="varac",
                        message=f"{name}: VarAC radio setup is incomplete",
                        resolution_hint="Set the VarAC install, DB, and INI paths for this radio if VarAC should run with it.",
                        deep_link_target=_issue_deep_link("radio_profiles", radio_id),
                        state_key="degraded",
                    )
                )

    js8call_path = _text(settings, "path_js8call")
    js8_host = _text(settings, "js8_host")
    js8_port = _text(settings, "js8_port")
    js8_directed = _text(settings, "js8_directed_path")
    js8_forms = _text(settings, "js8_forms_path")
    js8spotter_path = _text(settings, "path_js8spotter")
    profile_js8call_enabled = any(_profile_software_enabled(profile, "js8call") for profile in runtime_profiles)
    profile_js8spotter_enabled = any(_profile_software_enabled(profile, "js8spotter") for profile in runtime_profiles)
    profile_commstat_enabled = any(_profile_software_enabled(profile, "commstat") for profile in runtime_profiles)
    js8call_enabled = profile_js8call_enabled or (
        not _profiles_all_explicitly_disabled(runtime_profiles, "js8call")
        and bool(js8call_path or js8_host or js8_port or js8_directed or js8_forms)
    )
    js8spotter_enabled = profile_js8spotter_enabled or (
        not _profiles_all_explicitly_disabled(runtime_profiles, "js8spotter") and bool(js8spotter_path)
    )
    commstat_enabled = profile_commstat_enabled or (
        not _profiles_all_explicitly_disabled(runtime_profiles, "commstat") and bool(_text(settings, "path_commstat"))
    )
    legacy_js8_check = not profile_js8call_enabled
    if legacy_js8_check and js8call_enabled and not js8_host:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="js8call",
                scope="global",
                integration_key="js8call",
                message="JS8Call TCP host missing",
                resolution_hint="Set the JS8Call TCP host in Settings.",
                deep_link_target=_issue_deep_link("js8call", None),
                state_key="needs_setup",
            )
        )
    if legacy_js8_check and js8call_enabled and not js8_port:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="js8call",
                scope="global",
                integration_key="js8call",
                message="JS8Call TCP port missing",
                resolution_hint="Set the JS8Call TCP port in Settings.",
                deep_link_target=_issue_deep_link("js8call", None),
                state_key="needs_setup",
            )
        )
    if legacy_js8_check and js8call_enabled and not js8_directed:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="js8call",
                scope="global",
                integration_key="js8call",
                message="JS8Call DIRECTED.TXT path missing",
                resolution_hint="Set the DIRECTED.TXT path in Settings.",
                deep_link_target=_issue_deep_link("js8call", None),
                state_key="needs_setup",
            )
        )
    # CP ships a usable forms catalog. A custom or legacy catalog is an
    # optional source choice, not a prerequisite for enabling the service.
    # Catalog/file health is checked in the Forms Library, never inferred from
    # the absence of a user override here.

    path_flrig = _text(settings, "path_flrig")
    path_fldigi = _text(settings, "path_fldigi")
    path_flmsg = _text(settings, "path_flmsg")
    path_flamp = _text(settings, "path_flamp")
    fldigi_host = _text(settings, "fldigi_host")
    fldigi_port = _text(settings, "fldigi_port")
    flrig_port = _text(settings, "flrig_port")
    fldigi_checkin_dir = _text(settings, "fldigi_checkin_dir")
    message_paths = settings.get("message_paths", {})
    if not isinstance(message_paths, Mapping):
        message_paths = {}
    flrig_enabled = any(_profile_software_enabled(profile, "flrig") for profile in runtime_profiles) or (
        not _profiles_all_explicitly_disabled(runtime_profiles, "flrig") and bool(path_flrig or flrig_port)
    )
    fldigi_enabled = any(_profile_software_enabled(profile, "fldigi") for profile in runtime_profiles) or (
        not _profiles_all_explicitly_disabled(runtime_profiles, "fldigi")
        and bool(path_fldigi or fldigi_host or fldigi_port or fldigi_checkin_dir)
    )
    flmsg_enabled = any(_profile_software_enabled(profile, "flmsg") for profile in runtime_profiles) or (
        not _profiles_all_explicitly_disabled(runtime_profiles, "flmsg")
        and bool(path_flmsg or _text(message_paths, "flmsg"))
    )
    flamp_enabled = any(_profile_software_enabled(profile, "flamp") for profile in runtime_profiles) or (
        not _profiles_all_explicitly_disabled(runtime_profiles, "flamp")
        and bool(path_flamp or _text(message_paths, "flamp"))
    )
    if flrig_enabled and not flrig_port:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="fast_light",
                scope="global",
                integration_key="flrig",
                message="FLRig XML RPC port missing",
                resolution_hint="Set the FLRig XML RPC port in Settings.",
                deep_link_target=_issue_deep_link("fast_light", None),
                state_key="needs_setup",
            )
        )
    if fldigi_enabled and not fldigi_host:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="fast_light",
                scope="global",
                integration_key="fldigi",
                message="FLDigi XML RPC host missing",
                resolution_hint="Set the FLDigi XML RPC host in Settings.",
                deep_link_target=_issue_deep_link("fast_light", None),
                state_key="needs_setup",
            )
        )
    if fldigi_enabled and not fldigi_port:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="fast_light",
                scope="global",
                integration_key="fldigi",
                message="FLDigi XML RPC port missing",
                resolution_hint="Set the FLDigi XML RPC port in Settings.",
                deep_link_target=_issue_deep_link("fast_light", None),
                state_key="needs_setup",
            )
        )
    if flmsg_enabled and not _text(message_paths, "flmsg"):
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="fast_light",
                scope="global",
                integration_key="flmsg",
                message="FLMsg ICS Messages path missing",
                resolution_hint="Set the FLMsg message path in Settings.",
                deep_link_target=_issue_deep_link("fast_light", None),
                state_key="needs_setup",
            )
        )
    if flamp_enabled and not _text(message_paths, "flamp"):
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="fast_light",
                scope="global",
                integration_key="flamp",
                message="FLAmp FLAMP rx path missing",
                resolution_hint="Set the FLAmp receive path in Settings.",
                deep_link_target=_issue_deep_link("fast_light", None),
                state_key="needs_setup",
            )
        )
    if fldigi_enabled and fldigi_checkin_dir and not path_fldigi:
        issues.append(
            ReadinessIssue(
                severity="recommended",
                section_key="fast_light",
                scope="global",
                integration_key="fldigi",
                message="FLDigi check in support is configured without an FLDigi executable path",
                resolution_hint="Set the FLDigi launch path if this station should run FLDigi from CommsPlan.",
                deep_link_target=_issue_deep_link("fast_light", None),
                state_key="degraded",
            )
        )

    varac_install = _text(settings, "varac_path")
    varac_launch = _text(settings, "varac_launch_cmd")
    varac_incoming = _text(message_paths, "varac")
    varac_outbox = _text(settings, "varac_outbox_dir")
    varac_bbs_dir = _text(settings, "varac_bbs_dir")
    varac_bbs_archive = _text(settings, "varac_bbs_archive_dir")
    varac_auto_archive = _truthy_any(settings, "varac_bbs_auto_archive_enabled", "varac_bbs_auto_archive")
    varac_bbs_vault_enabled = _truthy(settings, "varac_bbs_vault_enabled")
    varac_bbs_vault_root = _text(settings, "varac_bbs_vault_managed_root")
    varac_bbs_vault_default_location_id = _text(settings, "varac_bbs_vault_default_location_id", "default")
    varac_bbs_vault_summary = _text(settings, "varac_bbs_vault_last_summary")
    varac_bbs_vault_locations = load_vault_locations(settings.get("varac_bbs_vault_locations_v1", []))
    varac_enabled = any(_profile_software_enabled(profile, "varac") for profile in runtime_profiles) or (
        not _profiles_all_explicitly_disabled(runtime_profiles, "varac")
        and bool(varac_install or varac_launch or varac_incoming or varac_outbox or varac_bbs_dir or varac_bbs_archive)
    )
    varac_engaged = any(
        [varac_install, varac_launch, varac_incoming, varac_outbox, varac_bbs_dir, varac_bbs_archive, varac_auto_archive]
    )
    if varac_enabled and varac_engaged and not (varac_install or varac_launch):
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="varac",
                scope="global",
                integration_key="varac",
                message="VarAC install folder or launch override missing",
                resolution_hint="Set the VarAC install folder or launch override in Settings.",
                deep_link_target=_issue_deep_link("varac", None),
                state_key="needs_setup",
            )
        )
    if varac_enabled and (varac_install or varac_launch) and not varac_incoming:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="varac",
                scope="global",
                integration_key="varac",
                message="VarAC inbox directory missing",
                resolution_hint="Set the VarAC incoming files directory in Settings.",
                deep_link_target=_issue_deep_link("varac", None),
                state_key="needs_setup",
            )
        )
    if varac_enabled and (varac_install or varac_launch) and not varac_outbox:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="varac",
                scope="global",
                integration_key="varac",
                message="VarAC outbox directory missing",
                resolution_hint="Set the VarAC outbox directory in Settings.",
                deep_link_target=_issue_deep_link("varac", None),
                state_key="needs_setup",
            )
        )
    if varac_enabled and bool(varac_bbs_dir) != bool(varac_bbs_archive):
        issues.append(
            ReadinessIssue(
                severity="recommended",
                section_key="varac",
                scope="global",
                integration_key="varac_bbs",
                message="VarAC BBS directory and archive setup is incomplete",
                resolution_hint="Set both BBS directory and archive directory if you want BBS archive support.",
                deep_link_target=_issue_deep_link("varac", None),
                state_key="degraded",
            )
        )
    if varac_enabled and varac_auto_archive and not (varac_bbs_dir and varac_bbs_archive):
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="varac",
                scope="global",
                integration_key="varac_bbs",
                message="VarAC auto archive requires both BBS directories",
                resolution_hint="Set both BBS directory and BBS archive directory before enabling auto archive.",
                deep_link_target=_issue_deep_link("varac", None),
                state_key="needs_setup",
            )
        )
    if settings.get('station_bbs_catalog') is not None and not varac_bbs_dir:
        # With several active radios the global compatibility mirror is
        # intentionally absent; each radio retains its own live service folder.
        varac_bbs_dir = next((str(profile.get('varac_bbs_dir') or '').strip()
                              for profile in runtime_profiles if _profile_software_enabled(profile, 'varac')
                              and profile.get('varac_bbs_enabled') and profile.get('varac_bbs_dir')), '')
    if varac_enabled and varac_bbs_vault_enabled and not varac_bbs_dir:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="varac",
                scope="global",
                integration_key="varac_bbs_vault",
                message="Managed BBS Library has no live BBS directory",
                resolution_hint="Set the VarAC BBS directory before enabling Managed BBS Library.",
                deep_link_target=_issue_deep_link("varac", None),
                state_key="needs_setup",
            )
        )
    if varac_enabled and varac_bbs_vault_enabled and not varac_bbs_vault_root:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="varac",
                scope="global",
                integration_key="varac_bbs_vault",
                message="Managed BBS Library root missing",
                resolution_hint="Set the Managed Root before enabling Managed BBS Library.",
                deep_link_target=_issue_deep_link("varac", None),
                state_key="needs_setup",
            )
        )
    if varac_enabled and varac_bbs_vault_enabled and not varac_bbs_vault_locations:
        issues.append(
            ReadinessIssue(
                severity="required",
                section_key="varac",
                scope="global",
                integration_key="varac_bbs_vault",
                message="Managed BBS Library has no locations",
                resolution_hint="Initialize the Managed BBS Library or add at least one location before using it.",
                deep_link_target=_issue_deep_link("varac", None),
                state_key="needs_setup",
            )
        )
    if varac_enabled and varac_bbs_vault_enabled and varac_bbs_vault_locations:
        default_location = next((loc for loc in varac_bbs_vault_locations if loc.id == varac_bbs_vault_default_location_id), None)
        if default_location is None:
            issues.append(
                ReadinessIssue(
                    severity="required",
                    section_key="varac",
                    scope="global",
                    integration_key="varac_bbs_vault",
                    message="Managed BBS Library default location is missing",
                    resolution_hint="Choose a valid Default location for Managed BBS Library.",
                    deep_link_target=_issue_deep_link("varac", None),
                    state_key="needs_setup",
                )
            )
        elif not str(default_location.source_dir or "").strip():
            issues.append(
                ReadinessIssue(
                    severity="required",
                    section_key="varac",
                    scope="global",
                    integration_key="varac_bbs_vault",
                    message="Managed BBS Library default location has no source folder",
                    resolution_hint="Set a source folder for the Default location.",
                    deep_link_target=_issue_deep_link("varac", None),
                    state_key="needs_setup",
                )
            )
    if varac_enabled and varac_bbs_vault_enabled and "degraded" in varac_bbs_vault_summary.lower():
        issues.append(
            ReadinessIssue(
                severity="recommended",
                section_key="varac",
                scope="global",
                integration_key="varac_bbs_vault",
                message="Managed BBS Library reported a degraded state",
                resolution_hint="Review the last Managed BBS Library status in Settings or ControlFreq and correct the reported folder issue.",
                deep_link_target=_issue_deep_link("varac", None),
                state_key="degraded",
            )
        )

    if assigned_schedule_status:
        _append_assignment_rf_guard_issues(issues, assigned_schedule_status)

    radio_summaries: List[RadioReadinessSummary] = []
    for profile in profiles:
        radio_id = int(profile.get("id", 0) or 0) or None
        name = _profile_display_name(profile)
        scoped_issues = [issue for issue in issues if int(issue.radio_id or 0) == int(radio_id or 0)]
        required_radio = sum(1 for issue in scoped_issues if issue.severity == "required")
        recommended_radio = sum(1 for issue in scoped_issues if issue.severity == "recommended")
        informational_radio = sum(1 for issue in scoped_issues if issue.severity == "informational")
        state_counts = _state_counts_from_issues(scoped_issues)
        radio_summaries.append(
            RadioReadinessSummary(
                radio_id=radio_id,
                name=name,
                overall_state=_summary_overall_state(
                    required_count=required_radio,
                    recommended_count=recommended_radio,
                    state_counts=state_counts,
                ),
                required_count=required_radio,
                recommended_count=recommended_radio,
                informational_count=informational_radio,
                messages=tuple(issue.message for issue in scoped_issues),
                state_counts=state_counts,
            )
        )

    required_count = sum(1 for issue in issues if issue.severity == "required")
    recommended_count = sum(1 for issue in issues if issue.severity == "recommended")
    informational_count = sum(1 for issue in issues if issue.severity == "informational")
    state_counts = _state_counts_from_issues(issues)
    overall_state = _summary_overall_state(
        required_count=required_count,
        recommended_count=recommended_count,
        state_counts=state_counts,
    )
    digest_payload = "|".join(
        f"{issue.severity}:{issue.section_key}:{issue.scope}:{issue.message}:{issue.deep_link_target}" for issue in issues
    )
    digest = sha1(digest_payload.encode("utf-8")).hexdigest() if digest_payload else ""
    return ReadinessReport(
        overall_state=overall_state,
        issues=tuple(issues),
        radio_summaries=tuple(radio_summaries),
        required_count=required_count,
        recommended_count=recommended_count,
        informational_count=informational_count,
        digest=digest,
        state_counts=state_counts,
    )
