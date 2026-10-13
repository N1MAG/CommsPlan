"""Pure multi-instance discovery review and launch-preflight helpers.

Discovery is evidence, never an instruction to alter a third-party application's
configuration.  These helpers intentionally accept already-discovered values and
return immutable, user-reviewable proposals.  A caller must still perform an
explicit, backed-up save/apply operation before adopting any proposal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


_FAMILY_LABELS = {
    "js8call": "JS8Call",
    "fast_light": "Fast Light",
    "varac": "VarAC",
}
_FAMILY_INSTANCE_KEYS = {
    "js8call": "js8_instance_id",
    "fast_light": "fast_light_config_id",
    "varac": "varac_node_id",
}
_FAMILY_ENABLED = {
    "js8call": ("use_js8call",),
    "fast_light": ("use_flrig", "use_fldigi", "use_flmsg", "use_flamp"),
    "varac": ("use_varac",),
}
_FAMILY_PATH_KEYS = {
    "js8call": ("js8_install_path", "js8_profile_path", "js8_directed_path"),
    "fast_light": ("flrig_path", "fldigi_path", "flmsg_path", "flamp_path"),
    "varac": (
        "varac_install_path",
        "varac_ini_path",
        "varac_db_path",
        "varac_incoming_path",
        "varac_outbox_dir",
    ),
}


@dataclass(frozen=True)
class InstanceAdoptionTarget:
    """One radio/family relationship that an operator can review and adopt."""

    family_key: str
    radio_id: int
    radio_name: str
    existing_instance_id: int | None
    state: str
    configured_paths: tuple[str, ...]
    discovered_paths: tuple[str, ...]
    guidance: tuple[str, ...]


@dataclass(frozen=True)
class MultiInstanceAdoptionPlan:
    """Read-only plan; ``requires_explicit_apply`` is deliberately always true."""

    family_key: str
    family_title: str
    targets: tuple[InstanceAdoptionTarget, ...]
    review_notes: tuple[str, ...]
    requires_explicit_apply: bool = True


@dataclass(frozen=True)
class MultiInstanceIssue:
    severity: str
    family_key: str
    code: str
    radio_ids: tuple[int, ...]
    radio_names: tuple[str, ...]
    detail: str
    remediation: str
    paths: tuple[str, ...] = field(default_factory=tuple)


def build_multi_instance_adoption_plan(
    family_key: str,
    device_profiles: Sequence[Mapping[str, Any]],
    *,
    discovered: Iterable[Any] = (),
) -> MultiInstanceAdoptionPlan:
    """Build a non-mutating adoption review from cached profiles and discovery.

    ``discovered`` may contain path strings, mappings with a ``path`` field, or
    discovery result objects exposing ``path``.  No filesystem access occurs in
    this function.  The intentionally compact result is suitable for a review
    screen, a guided setup step, or an audit log preview.
    """

    family = _normalize_family(family_key)
    discovered_paths = _normalized_paths(discovered)
    instance_key = _FAMILY_INSTANCE_KEYS[family]
    targets: list[InstanceAdoptionTarget] = []
    for profile in _ordered_profiles(device_profiles):
        if not _family_is_enabled(profile, family):
            continue
        radio_id = _positive_int(profile.get("id"))
        if radio_id is None:
            continue
        radio_name = _text(profile.get("name")) or f"Radio {radio_id}"
        configured_paths = _normalized_paths(profile.get(key) for key in _FAMILY_PATH_KEYS[family])
        existing_id = _positive_int(profile.get(instance_key))
        if existing_id is not None:
            state = "assigned"
        elif configured_paths:
            state = "ready_to_adopt"
        elif discovered_paths:
            state = "discovery_available"
        else:
            state = "needs_setup"
        targets.append(
            InstanceAdoptionTarget(
                family_key=family,
                radio_id=radio_id,
                radio_name=radio_name,
                existing_instance_id=existing_id,
                state=state,
                configured_paths=configured_paths,
                discovered_paths=discovered_paths,
                guidance=_guidance_for(family, state),
            )
        )
    review_notes = (
        "Discovery is read-only evidence and does not modify external application files. Review each candidate before it becomes a radio assignment.",
        "Saving an adoption must preserve the existing external configuration until an explicit apply action has made a backup.",
    )
    return MultiInstanceAdoptionPlan(
        family_key=family,
        family_title=_FAMILY_LABELS[family],
        targets=tuple(targets),
        review_notes=review_notes,
    )


def validate_multi_instance_launch_records(records: Sequence[Mapping[str, Any]]) -> tuple[MultiInstanceIssue, ...]:
    """Return deterministic launch blockers for independently planned instances.

    Same-instance records are permitted: launch planning may legitimately dedupe
    or intentionally share an existing assignment.  This validator only flags
    collisions between different planned identities.
    """

    rows = tuple(record for record in records if isinstance(record, Mapping))
    issues: list[MultiInstanceIssue] = []
    issues.extend(_endpoint_issues(rows, app_name="JS8Call", family="js8call"))
    issues.extend(_endpoint_issues(rows, app_name="FLRig", family="fast_light"))
    issues.extend(_endpoint_issues(rows, app_name="FLDigi", family="fast_light"))
    issues.extend(_varac_resource_issues(rows))
    return tuple(sorted(issues, key=lambda item: (item.family_key, item.code, item.radio_ids, item.paths)))


def blocking_issue_message(issues: Sequence[MultiInstanceIssue]) -> str:
    """Format blocking launch issues without hiding the corrective action."""

    blockers = [issue for issue in issues if issue.severity == "error"]
    return "\n".join(f"{issue.detail} {issue.remediation}" for issue in blockers)


def _endpoint_issues(
    records: Sequence[Mapping[str, Any]], *, app_name: str, family: str
) -> list[MultiInstanceIssue]:
    grouped: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for record in records:
        if _text(record.get("name")).casefold() != app_name.casefold():
            continue
        readiness = record.get("readiness_policy")
        readiness = readiness if isinstance(readiness, Mapping) else {}
        host = _text(readiness.get("host")).casefold()
        port = _positive_int(readiness.get("port"))
        if not host or port is None:
            continue
        grouped.setdefault((host, port), []).append(record)
    issues: list[MultiInstanceIssue] = []
    for (host, port), rows in grouped.items():
        if len({_identity(row) for row in rows}) < 2:
            continue
        radio_ids, radio_names = _radios(rows)
        issues.append(
            MultiInstanceIssue(
                severity="error",
                family_key=family,
                code=f"duplicate_{app_name.casefold()}_endpoint",
                radio_ids=radio_ids,
                radio_names=radio_names,
                detail=f"{app_name} launch entries for {', '.join(radio_names)} use the same endpoint {host}:{port}.",
                remediation=f"Assign each {app_name} instance a unique host/port before launch.",
                paths=(f"{host}:{port}",),
            )
        )
    return issues


def _varac_resource_issues(records: Sequence[Mapping[str, Any]]) -> list[MultiInstanceIssue]:
    varac = [record for record in records if _text(record.get("name")).casefold() == "varac"]
    by_resource: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in varac:
        configuration_paths = record.get("configuration_paths")
        configuration_paths = configuration_paths if isinstance(configuration_paths, Mapping) else {}
        for key in ("ini_path", "db_path", "incoming_path", "outbox_dir"):
            value = _canonical_path(configuration_paths.get(key))
            if value:
                by_resource.setdefault((key, value), []).append(record)
    issues: list[MultiInstanceIssue] = []
    for (resource_name, resource), rows in by_resource.items():
        if len({_identity(row) for row in rows}) < 2:
            continue
        radio_ids, radio_names = _radios(rows)
        label = resource_name.replace("_", " ")
        issues.append(
            MultiInstanceIssue(
                severity="error",
                family_key="varac",
                code=f"duplicate_varac_{resource_name}",
                radio_ids=radio_ids,
                radio_names=radio_names,
                detail=f"VarAC launch entries for {', '.join(radio_names)} share the same {label}: {resource}.",
                remediation="Give each independently launched VarAC node its own INI, database, incoming, and outbox locations; review and apply native changes only through a backed-up explicit action.",
                paths=(resource,),
            )
        )
    return issues


def _guidance_for(family: str, state: str) -> tuple[str, ...]:
    general = "Review before saving; discovery does not modify external application files."
    if family == "js8call":
        return (
            general,
            "Give every independently launched JS8Call instance a unique API port and CommsPlan-managed --rig-name.",
            "Confirm the distinct application-data root for the rig name; SaveDir is not message storage.",
        )
    if family == "fast_light":
        return (
            general,
            "Give independently launched FLRig and FLDigi services unique local TCP ports.",
            "A shared application install may be reused; endpoint identities must still be distinct.",
        )
    return (
        general,
        "Clustered VarAC nodes need separate INI, database, incoming, and outbox resources.",
        "Do not adopt a discovered native path into more than one independent node without an explicit backed-up apply.",
    )


def _normalize_family(value: str) -> str:
    family = _text(value).casefold().replace("-", "_").replace(" ", "_")
    if family not in _FAMILY_LABELS:
        raise ValueError(f"Unsupported multi-instance family: {value!r}")
    return family


def _ordered_profiles(profiles: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        sorted(
            (profile for profile in profiles if isinstance(profile, Mapping)),
            key=lambda profile: (
                int(profile.get("display_order", 0) or 0),
                _text(profile.get("name")).casefold(),
                int(profile.get("id", 0) or 0),
            ),
        )
    )


def _family_is_enabled(profile: Mapping[str, Any], family: str) -> bool:
    return any(_truthy(profile.get(key)) for key in _FAMILY_ENABLED[family])


def _normalized_paths(values: Iterable[Any]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if isinstance(raw, Mapping):
            raw = raw.get("path", "")
        elif not isinstance(raw, (str, bytes, Path)):
            raw = getattr(raw, "path", raw)
        value = _text(raw)
        if not value:
            continue
        key = _canonical_path(value).casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return tuple(output)


def _canonical_path(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError):
        return str(path.absolute())


def _identity(record: Mapping[str, Any]) -> str:
    return _text(record.get("instance_identity")) or "|".join(
        (str(value) for value in record.get("radio_ids", ()) if _text(value))
    )


def _radios(records: Sequence[Mapping[str, Any]]) -> tuple[tuple[int, ...], tuple[str, ...]]:
    ids: list[int] = []
    names: list[str] = []
    for record in records:
        record_ids = tuple(record.get("radio_ids", ()) or ())
        record_names = tuple(record.get("radio_names", ()) or ())
        for index, raw_id in enumerate(record_ids):
            radio_id = _positive_int(raw_id)
            if radio_id is None or radio_id in ids:
                continue
            ids.append(radio_id)
            name = _text(record_names[index] if index < len(record_names) else "") or f"Radio {radio_id}"
            names.append(name)
    ordered = sorted(zip(ids, names), key=lambda item: item[0])
    return tuple(item[0] for item in ordered), tuple(item[1] for item in ordered)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "InstanceAdoptionTarget",
    "MultiInstanceAdoptionPlan",
    "MultiInstanceIssue",
    "blocking_issue_message",
    "build_multi_instance_adoption_plan",
    "validate_multi_instance_launch_records",
]
