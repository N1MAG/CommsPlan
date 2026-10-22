"""Cache-only read model for software-centered Settings administration.

This module deliberately performs no database, filesystem, process, socket, or
radio work.  Callers provide already-loaded rows and cached readiness evidence;
the returned frozen snapshot is safe for the UI to navigate synchronously.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class SoftwareInstanceSummary:
    instance_id: int
    instance_name: str
    enabled: bool


@dataclass(frozen=True)
class RadioSoftwareAssignment:
    radio_id: int
    radio_name: str
    radio_enabled: bool
    software_enabled: bool
    instance_id: Optional[int]
    instance_name: str
    readiness: str
    status_text: str
    shared_radio_names: tuple[str, ...] = ()
    manifest_instance_key: str = ""
    management_mode: str = ""
    provenance: str = ""
    verification_state: str = ""
    endpoint_summary: str = ""
    configuration_summary: str = ""
    data_summary: str = ""
    canonical_bundle_id: str = ""
    canonical_identity_key: str = ""
    canonical_fingerprint: str = ""
    canonical_component_ids: tuple[str, ...] = ()
    canonical_binding_ids: tuple[str, ...] = ()
    canonical_parity_state: str = ""
    canonical_parity_detail: str = ""
    launch_summary: str = ""

    @property
    def is_shared(self) -> bool:
        return len(self.shared_radio_names) > 1


@dataclass(frozen=True)
class SoftwareFamilySummary:
    key: str
    title: str
    description: str
    assignments: tuple[RadioSoftwareAssignment, ...]
    unassigned_instances: tuple[SoftwareInstanceSummary, ...] = ()
    operational_route: str = ""

    @property
    def assigned_radio_count(self) -> int:
        return len(self.assignments)

    @property
    def attention_count(self) -> int:
        return sum(
            1
            for assignment in self.assignments
            if assignment.status_text in {
                "Needs setup", "Needs attention", "Needs review", "Unavailable",
                "Stale", "Unverified", "Unknown",
            }
            or "runtime unverified" in assignment.status_text.casefold()
        )


@dataclass(frozen=True)
class SoftwareAdministrationSnapshot:
    families: tuple[SoftwareFamilySummary, ...]

    def family(self, key: str) -> Optional[SoftwareFamilySummary]:
        wanted = str(key or "").strip().lower()
        return next((family for family in self.families if family.key == wanted), None)


_FAMILY_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("js8call", "JS8Call", "Digital messaging, API, profiles, and message storage."),
    ("fast_light", "Fast Light", "FLRig, FLDigi, FLMsg, and FLAmp tools."),
    ("varac", "VarAC", "Radio-specific VarAC runtime, paths, guard, and cluster setup."),
    ("commstat", "CommStat", "External CommStat application and JS8 transport mapping."),
    ("external_spotter", "External Spotter", "Optional external Spotter application and forms."),
    ("fio_spotter", "CommsPlan Spotter", "Built-in rules, Expect queries, watches, forms, and activity."),
    ("receiver", "Receiver Software", "Receive-only application and control adapter."),
)


def _mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {key: row[key] for key in keys()}
    return vars(row) if hasattr(row, "__dict__") else {}


def _integer(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _enabled(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _instance_index(rows: Iterable[Any]) -> dict[int, Mapping[str, Any]]:
    indexed: dict[int, Mapping[str, Any]] = {}
    for raw in rows:
        row = _mapping(raw)
        instance_id = _integer(row.get("id"))
        if instance_id is not None:
            indexed[instance_id] = row
    return indexed


def _readiness_evidence(
    readiness_by_radio: Optional[Mapping[Any, Any]],
    radio_id: int,
    family_key: str,
) -> Any:
    if not readiness_by_radio:
        return None
    direct = readiness_by_radio.get((radio_id, family_key))
    if direct is None:
        per_radio = readiness_by_radio.get(radio_id)
        if isinstance(per_radio, Mapping):
            direct = per_radio.get(family_key)
    return direct


def _readiness_text(
    readiness_by_radio: Optional[Mapping[Any, Any]],
    radio_id: int,
    family_key: str,
) -> str:
    """Return the raw operator evidence label for the public read model."""

    direct = _readiness_evidence(readiness_by_radio, radio_id, family_key)
    if isinstance(direct, Mapping):
        direct = direct.get("label") or direct.get("status") or direct.get("state")
    return _text(direct)


def software_runtime_status(evidence: Any, raw: str = "") -> str:
    """Normalize cached evidence without treating unknown as runtime success.

    Status service rows carry both endpoint/process state and freshness metadata.
    The administration read model keeps the raw label in ``readiness`` while
    projecting one concise, conservative status for the operator-facing card.
    """
    row = evidence if isinstance(evidence, Mapping) else {}
    state = _text(row.get("state") or row.get("status") or row.get("label") or raw).casefold()
    value = _text(row.get("value")).casefold()
    configuration_state = _text(
        row.get("configuration_state") or row.get("config_state") or row.get("configuration")
    ).casefold()
    runtime_state = _text(row.get("runtime_state") or row.get("process_state")).casefold()
    conflict = _enabled(
        row.get("identity_conflict")
        or row.get("conflict")
        or row.get("configuration_conflict")
    ) or state in {"conflict", "readback_mismatch", "mismatch", "needs_review", "review"}
    if conflict:
        return "Needs review"

    # Verification is intentionally distinct from an unknown cache.  These
    # aliases are published by different endpoint/readback paths but have the
    # same operator meaning in the Software Administration selector.
    verification_state = state.replace("-", "_").replace(" ", "_")
    verification_value = value.replace("-", "_").replace(" ", "_")
    if verification_state in {
        "unverified", "verification_unavailable", "js8_verification_unavailable",
    } or verification_value in {
        "unverified", "verification_unavailable", "js8_verification_unavailable",
    }:
        return "Unverified"

    unavailable = state in {"error", "failed", "unavailable", "offline"} or value in {
        "error", "failed", "unavailable", "offline"
    }
    if unavailable:
        return "Unavailable"

    stale = _enabled(row.get("stale") or row.get("expired") or row.get("invalidated"))
    checked_at = row.get("checked_at") or row.get("observed_at") or row.get("updated_at")
    if stale:
        # Initial cache rows have no observation and are neutral until the
        # worker publishes its first result.  A previously observed row is
        # explicitly stale and must remain visible as such.
        if checked_at not in (None, "", 0, 0.0) or state not in {"", "idle", "unknown"}:
            return "Stale"
        return ""

    # Configuration evidence and runtime evidence are separate.  If a caller
    # supplies both, preserve that distinction in the visible state rather
    # than presenting configuration as a connected/running process.
    if configuration_state in {"configured", "valid", "ready", "reviewed"}:
        if runtime_state in {"", "unknown", "unverified", "not_checked"}:
            return "Unverified"

    if state in {"", "idle", "unknown", "unverified", "verification_unavailable", "not_checked"}:
        return "Unknown" if state and state not in {"idle", "not_checked"} else ""
    if state in {"connected", "ok", "reachable"} or value in {"connected", "ok", "reachable"}:
        return "Connected"
    if state in {"running", "active"} or value in {"running", "active"}:
        return "Running"
    if state in {"ready", "healthy"} or value in {"ready", "healthy"}:
        return "Ready"
    if state in {"configured", "valid", "reviewed"}:
        return "Configured" if state != "reviewed" else "Reviewed"
    return _text(raw)


def _family_link(profile: Mapping[str, Any], family_key: str) -> tuple[bool, Optional[int]]:
    if family_key == "receiver":
        is_observer = _text(profile.get("device_class") or profile.get("radio_role")).casefold() in {
            "observer", "receive_only", "receive-only", "sdr"
        }
        configured = bool(_text(profile.get("sdr_application")) or _text(profile.get("sdr_adapter")))
        return is_observer and configured, None
    if family_key == "js8call":
        return _enabled(profile.get("use_js8call")), _integer(profile.get("js8_instance_id"))
    if family_key == "fast_light":
        used = any(
            _enabled(profile.get(flag))
            for flag in ("use_flrig", "use_fldigi", "use_flmsg", "use_flamp")
        )
        return used, _integer(profile.get("fast_light_config_id"))
    if family_key == "varac":
        return _enabled(profile.get("use_varac")), _integer(profile.get("varac_node_id"))
    if family_key == "commstat":
        return _enabled(profile.get("use_commstat")), _integer(profile.get("js8_instance_id"))
    if family_key == "fio_spotter":
        # FIO Spotter is an explicit built-in capability selection. Its JS8
        # transport binding is provenance, not evidence that it was selected.
        instance_id = _integer(profile.get("js8_instance_id"))
        return _enabled(profile.get("use_js8spotter")), instance_id
    return False, _integer(profile.get("js8_instance_id"))


def _launch_summary(components: Sequence[Any]) -> str:
    """Project saved native selectors, never the CommsPlan display label."""
    parts: list[str] = []
    for component in components:
        if not isinstance(component, Mapping):
            continue
        launch = component.get("launch") or {}
        readiness = component.get("readiness") or {}
        if not isinstance(launch, Mapping) or not isinstance(readiness, Mapping):
            continue
        argv = component.get("argv") or ()
        selector = _text(launch.get("profile_selector") or readiness.get("profile_selector"))
        mode = _text(readiness.get("settings_identity_mode"))
        if not selector and isinstance(argv, (list, tuple)):
            for index, arg in enumerate(argv):
                if arg in {"--rig-name", "-r", "--config-dir"} and index + 1 < len(argv):
                    selector = _text(argv[index + 1])
                elif str(arg).startswith("--rig-name="):
                    selector = str(arg).partition("=")[2]
        identity = selector or ("Existing launcher" if mode == "custom_launcher" else
                                "Default settings" if mode == "default" else "Settings identity unverified")
        parts.append(f'{_text(component.get("component_id"))}: {identity}')
    return "; ".join(parts)


def _status_text(
    *,
    radio_enabled: bool,
    software_enabled: bool,
    instance_required: bool,
    instance_id: Optional[int],
    instance_found: bool,
    readiness: str,
    readiness_evidence: Any = None,
) -> str:
    if not radio_enabled:
        return "Radio inactive"
    if not software_enabled:
        return "Not enabled"
    if instance_required and (instance_id is None or not instance_found):
        return "Needs setup"
    normalized = software_runtime_status(readiness_evidence, readiness)
    if normalized in {"error", "failed", "unavailable", "offline"}:
        return "Unavailable"
    if normalized in {
        "warning", "warn", "needs attention", "attention",
    }:
        return "Needs attention"
    if normalized:
        return normalized
    return "Not yet verified"


def build_software_administration_snapshot(
    device_profiles: Sequence[Any],
    *,
    js8_instances: Sequence[Any] = (),
    fast_light_configs: Sequence[Any] = (),
    varac_nodes: Sequence[Any] = (),
    instance_manifests: Sequence[Any] = (),
    readiness_by_radio: Optional[Mapping[Any, Any]] = None,
    identity_records: Sequence[Any] = (),
    identity_projection_issues: Optional[Mapping[tuple[int, str], Sequence[str]]] = None,
) -> SoftwareAdministrationSnapshot:
    """Build a deterministic reverse index from already-loaded configuration rows."""

    profiles = tuple(_mapping(row) for row in device_profiles)
    profiles = tuple(
        sorted(
            profiles,
            key=lambda row: (
                int(row.get("display_order") or 0),
                _text(row.get("name")).casefold(),
                int(row.get("id") or 0),
            ),
        )
    )
    indexes = {
        "receiver": {},
        "js8call": _instance_index(js8_instances),
        "fast_light": _instance_index(fast_light_configs),
        "varac": _instance_index(varac_nodes),
    }
    indexes["commstat"] = indexes["js8call"]
    indexes["external_spotter"] = indexes["js8call"]
    indexes["fio_spotter"] = indexes["js8call"]
    manifest_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw in instance_manifests:
        row = _mapping(raw)
        key = (
            _text(row.get("family_key")).casefold(),
            _text(row.get("application_system_key")),
        )
        if key[0] and key[1]:
            manifest_index[key] = row

    # Canonical records carry the exact durable identity. Normalize through
    # the public record/mapping codec, then index only by explicit owner or
    # explicit station-service radio binding. Never infer an identity from a
    # display label, adjacent legacy row, or native path.
    from freqinout.core.software_identity_bundle import (
        SoftwareIdentityRecord,
        identity_record_from_mapping,
        identity_record_to_mapping,
    )

    canonical_by_family_radio: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    canonical_radio_keys: set[str] = set()
    for raw in identity_records:
        if isinstance(raw, SoftwareIdentityRecord):
            record = raw
        elif isinstance(raw, Mapping):
            record = identity_record_from_mapping(raw)
        else:
            continue
        mapped = identity_record_to_mapping(record)
        family_key = _text(mapped.get("family_key")).casefold()
        if family_key == "external_js8spotter":
            family_key = "external_spotter"
        elif family_key == "sdrpp":
            family_key = "receiver"
        if family_key not in {key for key, _title, _description in _FAMILY_DEFINITIONS}:
            continue
        owner = _text(mapped.get("owner"))
        if family_key in {"fio_spotter", "commstat"}:
            radio_keys = {
                _text(binding.get("radio_key"))
                for binding in (mapped.get("bindings") or ())
                if isinstance(binding, Mapping) and _text(binding.get("radio_key"))
            }
        else:
            radio_keys = {owner} if owner else set()
        for radio_key in radio_keys:
            canonical_radio_keys.add(radio_key)
            canonical_by_family_radio.setdefault((family_key, radio_key), []).append(mapped)

    family_assignments: dict[str, list[RadioSoftwareAssignment]] = {
        key: [] for key, _title, _description in _FAMILY_DEFINITIONS
    }
    linked_ids: dict[str, set[int]] = {key: set() for key in indexes}

    for profile in profiles:
        radio_id = _integer(profile.get("id"))
        if radio_id is None:
            continue
        radio_name = _text(profile.get("name")) or f"Radio {radio_id}"
        radio_enabled = _enabled(profile.get("enabled"), True)
        radio_key = _text(profile.get("system_key") or profile.get("radio_key") or profile.get("id"))

        for family_key in family_assignments:
            software_enabled, instance_id = _family_link(profile, family_key)
            canonical_matches = canonical_by_family_radio.get((family_key, radio_key), [])
            instance_row = indexes[family_key].get(instance_id or -1)
            manifest_family = (
                "js8call"
                if family_key in {"commstat", "external_spotter", "fio_spotter"}
                else family_key
            )
            manifest = manifest_index.get(
                (manifest_family, _text((instance_row or {}).get("system_key")))
            )

            if family_key == "external_spotter":
                selected = profile.get("use_external_js8spotter")
                software_enabled = (_enabled(selected) if selected is not None else
                                    bool(instance_row and _text(instance_row.get("spotter_launch_path"))))
            if canonical_matches and family_key == "receiver":
                software_enabled = True

            # Retain disabled linked application instances so the workspace can
            # explain the assignment instead of making it disappear.
            linked = instance_id is not None and family_key in {"js8call", "fast_light", "varac"}
            if family_key == "external_spotter" and instance_id is not None:
                linked = bool(instance_row and _text(instance_row.get("spotter_launch_path")))
            if not software_enabled and not linked and not canonical_matches:
                continue

            if instance_id is not None:
                linked_ids[family_key].add(instance_id)
            instance_name = _text((instance_row or {}).get("name"))
            if not instance_name and instance_id is not None:
                instance_name = f"Missing instance {instance_id}"
            readiness_evidence = _readiness_evidence(
                readiness_by_radio, radio_id, family_key
            )
            readiness = _readiness_text(readiness_by_radio, radio_id, family_key)
            manifest_state = _text((manifest or {}).get("verification_state")).casefold()
            if not readiness and manifest_state:
                readiness = {
                    "verified": "Verified",
                    "reachable": "Reachable",
                    "configured": "Configured",
                    "detected": "Detected",
                    "needs_attention": "Needs attention",
                }.get(manifest_state, "")
            ports = (manifest or {}).get("ports")
            endpoint_parts: list[str] = []
            if isinstance(ports, Sequence) and not isinstance(ports, (str, bytes)):
                for item in ports:
                    if not isinstance(item, Mapping):
                        continue
                    name = _text(item.get("name")) or "Service"
                    host = _text(item.get("host")) or _text((manifest or {}).get("host"))
                    port = _integer(item.get("port"))
                    if host and port:
                        endpoint_parts.append(f"{name}: {host}:{port}")
            canonical = canonical_matches[0] if len(canonical_matches) == 1 else {}
            if len(canonical_matches) > 1:
                canonical_parity_state = "needs_attention"
            elif canonical:
                canonical_parity_state = "verified"
            elif radio_key in canonical_radio_keys and software_enabled:
                canonical_parity_state = "missing"
            else:
                canonical_parity_state = ""
            canonical_family_key = {
                "receiver": "sdrpp",
                "external_spotter": "external_js8spotter",
            }.get(family_key, family_key)
            projection_issues = tuple(
                (identity_projection_issues or {}).get(
                    (radio_id, canonical_family_key), ()
                )
                or ()
            )
            if projection_issues:
                canonical_parity_state = "needs_attention"
            canonical_components = canonical.get("components", ())
            canonical_bindings = canonical.get("bindings", ())
            family_assignments[family_key].append(
                RadioSoftwareAssignment(
                    radio_id=radio_id,
                    radio_name=radio_name,
                    radio_enabled=radio_enabled,
                    software_enabled=software_enabled,
                    instance_id=instance_id,
                    instance_name=instance_name,
                    readiness=readiness,
                    status_text=(
                        "Needs attention"
                        if software_enabled and canonical_parity_state in {"missing", "needs_attention"}
                        else _status_text(
                            radio_enabled=radio_enabled,
                            software_enabled=software_enabled,
                            instance_required=family_key in {"js8call", "fast_light", "varac"},
                            instance_id=instance_id,
                            instance_found=instance_row is not None,
                            readiness=readiness,
                            readiness_evidence=readiness_evidence,
                        )
                    ),
                    manifest_instance_key=_text((manifest or {}).get("instance_key")),
                    management_mode=_text((manifest or {}).get("management_mode")),
                    provenance=_text((manifest or {}).get("provenance")),
                    verification_state=manifest_state,
                    endpoint_summary=" · ".join(endpoint_parts),
                    configuration_summary=(
                        _text((manifest or {}).get("configuration_path"))
                        or _text((manifest or {}).get("configuration_root"))
                    ),
                    data_summary=_text((manifest or {}).get("data_root")),
                    canonical_bundle_id=_text(canonical.get("bundle_id")),
                    canonical_identity_key=_text(canonical.get("identity_key")),
                    canonical_fingerprint=_text(canonical.get("fingerprint")),
                    canonical_component_ids=tuple(
                        _text(item.get("component_id"))
                        for item in canonical_components
                        if isinstance(item, Mapping) and _text(item.get("component_id"))
                    ),
                    canonical_binding_ids=tuple(
                        _text(item.get("binding_id"))
                        for item in canonical_bindings
                        if isinstance(item, Mapping) and _text(item.get("binding_id"))
                    ),
                    canonical_parity_state=canonical_parity_state,
                    canonical_parity_detail="; ".join(str(item) for item in projection_issues),
                    launch_summary=_launch_summary(canonical_components),
                )
            )

    # Add shared-instance disclosure after all reverse relationships are known.
    for family_key, assignments in tuple(family_assignments.items()):
        radios_by_instance: dict[int, tuple[str, ...]] = {}
        for assignment in assignments:
            if assignment.instance_id is None:
                continue
            names = tuple(
                item.radio_name
                for item in assignments
                if item.instance_id == assignment.instance_id
            )
            radios_by_instance[assignment.instance_id] = names
        family_assignments[family_key] = [
            RadioSoftwareAssignment(
                **{
                    **assignment.__dict__,
                    "shared_radio_names": radios_by_instance.get(assignment.instance_id or -1, ()),
                }
            )
            for assignment in assignments
        ]

    families: list[SoftwareFamilySummary] = []
    for key, title, description in _FAMILY_DEFINITIONS:
        source_key = key if key in indexes else "js8call"
        unassigned: list[SoftwareInstanceSummary] = []
        for instance_id, row in indexes[source_key].items():
            if instance_id in linked_ids[key]:
                continue
            if key == "commstat" and not _text(row.get("commstat_launch_path")):
                continue
            if key == "external_spotter" and not _text(row.get("spotter_launch_path")):
                continue
            if key == "fio_spotter":
                continue
            unassigned.append(
                SoftwareInstanceSummary(
                    instance_id=instance_id,
                    instance_name=_text(row.get("name")) or f"Instance {instance_id}",
                    enabled=_enabled(row.get("enabled"), True),
                )
            )
        unassigned.sort(key=lambda item: (item.instance_name.casefold(), item.instance_id))
        families.append(
            SoftwareFamilySummary(
                key=key,
                title=title,
                description=description,
                assignments=tuple(family_assignments[key]),
                unassigned_instances=tuple(unassigned),
                operational_route="CommsPlan Spotter" if key == "fio_spotter" else "",
            )
        )

    return SoftwareAdministrationSnapshot(families=tuple(families))


__all__ = [
    "RadioSoftwareAssignment",
    "SoftwareAdministrationSnapshot",
    "SoftwareFamilySummary",
    "SoftwareInstanceSummary",
    "build_software_administration_snapshot",
]
