"""Canonical, immutable projection of guided software identity.

This module deliberately has no storage or UI dependency.  Add Radio and
Software Administration can therefore present the same records without either
surface reconstructing launch identity from display-only fields.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence, Tuple

from freqinout.core.guided_radio_software_model import (
    AtomicInstanceBundle,
    CompletionPolicy,
    EndpointRecord,
    ExecutionScope,
    InstanceSourceMode,
    LaunchComponentRecord,
    ManagementMode,
    ResourceClaimRecord,
    SoftwareFamily,
    atomic_bundle_from_mapping,
    endpoint_to_mapping,
    lock_existing_bundle,
    resource_claim_to_mapping,
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _text(value: object) -> str:
    return str(value or "").strip()


def _family_key(value: object) -> str:
    if isinstance(value, SoftwareFamily):
        return value.value
    return _text(value).casefold().replace("-", "_").replace(" ", "_")


def _key(value: object, fallback: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in _text(value).casefold()
    ).strip("-._")
    return normalized or fallback


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(_thaw(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SoftwareIdentityComponent:
    component_id: str
    argv: Tuple[str, ...] = field(default_factory=tuple)
    cwd: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    dependencies: Tuple[str, ...] = field(default_factory=tuple)
    launch: Mapping[str, Any] = field(default_factory=dict)
    readiness: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "component_id", _text(self.component_id))
        if not self.component_id:
            raise ValueError("software identity component_id is required")
        object.__setattr__(self, "argv", tuple(_text(item) for item in self.argv))
        object.__setattr__(self, "cwd", _text(self.cwd))
        object.__setattr__(self, "env", _freeze(self.env))
        object.__setattr__(self, "dependencies", tuple(_text(item) for item in self.dependencies))
        object.__setattr__(self, "launch", _freeze(self.launch))
        object.__setattr__(self, "readiness", _freeze(self.readiness))


@dataclass(frozen=True)
class SoftwareIdentityBinding:
    binding_id: str
    kind: str
    radio_key: str = ""
    endpoint: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _text(self.binding_id))
        object.__setattr__(self, "kind", _text(self.kind))
        object.__setattr__(self, "radio_key", _text(self.radio_key))
        object.__setattr__(self, "endpoint", _freeze(self.endpoint))
        if not self.binding_id:
            raise ValueError("software identity binding_id is required")
        if not self.kind:
            raise ValueError("software identity binding kind is required")


@dataclass(frozen=True)
class SoftwareIdentityRecord:
    bundle_id: str
    identity_key: str
    family_key: str
    owner: str
    scope: str
    source_mode: str
    management_mode: str
    completion_policy: str
    provenance: str
    verification: Mapping[str, Any] = field(default_factory=dict)
    paths: Mapping[str, Any] = field(default_factory=dict)
    resources: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    endpoints: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    components: Tuple[SoftwareIdentityComponent, ...] = field(default_factory=tuple)
    bindings: Tuple[SoftwareIdentityBinding, ...] = field(default_factory=tuple)
    launch: Mapping[str, Any] = field(default_factory=dict)
    readiness: Mapping[str, Any] = field(default_factory=dict)
    fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "bundle_id", _text(self.bundle_id))
        object.__setattr__(self, "identity_key", _text(self.identity_key))
        object.__setattr__(self, "family_key", _family_key(self.family_key))
        object.__setattr__(self, "owner", _text(self.owner))
        object.__setattr__(self, "scope", _text(self.scope))
        object.__setattr__(self, "source_mode", _text(self.source_mode))
        object.__setattr__(self, "management_mode", _text(self.management_mode))
        object.__setattr__(self, "completion_policy", _text(self.completion_policy))
        object.__setattr__(self, "provenance", _text(self.provenance))
        object.__setattr__(self, "verification", _freeze(self.verification))
        object.__setattr__(self, "paths", _freeze(self.paths))
        object.__setattr__(self, "resources", tuple(_freeze(item) for item in self.resources))
        object.__setattr__(self, "endpoints", tuple(_freeze(item) for item in self.endpoints))
        object.__setattr__(self, "components", tuple(
            item if isinstance(item, SoftwareIdentityComponent) else _component_from_mapping(item)
            for item in self.components
        ))
        object.__setattr__(self, "bindings", tuple(
            item if isinstance(item, SoftwareIdentityBinding) else _binding_from_mapping(item)
            for item in self.bindings
        ))
        object.__setattr__(self, "launch", _freeze(self.launch))
        object.__setattr__(self, "readiness", _freeze(self.readiness))
        # Mapping input may contain an old fingerprint after a user correction.
        # Recompute rather than rejecting it so parity validation can explain the
        # precise changed field instead of producing a generic decode failure.
        object.__setattr__(self, "fingerprint", _fingerprint(self._fingerprint_payload()))
        _validate_identity_record(self)

    def _fingerprint_payload(self) -> Mapping[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "identity_key": self.identity_key,
            "family_key": self.family_key,
            "owner": self.owner,
            "scope": self.scope,
            "source_mode": self.source_mode,
            "management_mode": self.management_mode,
            "completion_policy": self.completion_policy,
            "provenance": self.provenance,
            "verification": self.verification,
            "paths": self.paths,
            "resources": self.resources,
            "endpoints": self.endpoints,
            "components": [_component_to_mapping(item) for item in self.components],
            "bindings": [_binding_to_mapping(item) for item in self.bindings],
            "launch": self.launch,
            "readiness": self.readiness,
        }


def _component_to_mapping(value: SoftwareIdentityComponent) -> Mapping[str, Any]:
    return {
        "component_id": value.component_id,
        "argv": list(value.argv),
        "cwd": value.cwd,
        "env": _thaw(value.env),
        "dependencies": list(value.dependencies),
        "launch": _thaw(value.launch),
        "readiness": _thaw(value.readiness),
    }


def _component_from_mapping(value: Mapping[str, Any]) -> SoftwareIdentityComponent:
    return SoftwareIdentityComponent(
        component_id=_text(value.get("component_id")),
        argv=tuple(value.get("argv", ()) or ()),
        cwd=_text(value.get("cwd")),
        env=value.get("env", {}) or {},
        dependencies=tuple(value.get("dependencies", ()) or ()),
        launch=value.get("launch", {}) or {},
        readiness=value.get("readiness", {}) or {},
    )


def _binding_to_mapping(value: SoftwareIdentityBinding) -> Mapping[str, Any]:
    result: dict[str, Any] = {"binding_id": value.binding_id, "kind": value.kind}
    if value.radio_key:
        result["radio_key"] = value.radio_key
    if value.endpoint:
        result["endpoint"] = _thaw(value.endpoint)
    return result


def _binding_from_mapping(value: Mapping[str, Any]) -> SoftwareIdentityBinding:
    return SoftwareIdentityBinding(
        binding_id=_text(value.get("binding_id")),
        kind=_text(value.get("kind")),
        radio_key=_text(value.get("radio_key")),
        endpoint=value.get("endpoint", {}) or {},
    )


def _validate_identity_record(record: SoftwareIdentityRecord) -> None:
    """Fail closed before an identity record can enter a UI or database.

    This is intentionally stricter than display-time parity checks: a malformed
    record must never become a persisted "canonical" source that later screens
    have to guess how to repair.  Optional Fast Light components remain optional
    -- their *presence* is the selected-set evidence -- while their dependencies
    and identifiers remain explicit.
    """

    required_fields = {
        "bundle_id": record.bundle_id,
        "identity_key": record.identity_key,
        "family_key": record.family_key,
        "owner": record.owner,
        "scope": record.scope,
        "source_mode": record.source_mode,
        "management_mode": record.management_mode,
        "completion_policy": record.completion_policy,
        "provenance": record.provenance,
    }
    missing = [field_name for field_name, value in required_fields.items() if not value]
    if missing:
        raise ValueError("software identity record requires " + ", ".join(missing))
    try:
        family = SoftwareFamily(record.family_key)
    except ValueError as exc:
        raise ValueError(f"unknown software identity family: {record.family_key}") from exc
    if record.scope not in {item.value for item in ExecutionScope}:
        raise ValueError(f"invalid software identity scope: {record.scope}")
    if record.source_mode not in {item.value for item in InstanceSourceMode}:
        raise ValueError(f"invalid software identity source mode: {record.source_mode}")
    if record.management_mode not in {item.value for item in ManagementMode}:
        raise ValueError(f"invalid software identity management mode: {record.management_mode}")
    if record.completion_policy not in {item.value for item in CompletionPolicy}:
        raise ValueError(f"invalid software identity completion policy: {record.completion_policy}")

    component_ids = tuple(component.component_id for component in record.components)
    duplicate_components = sorted(
        component_id for component_id in set(component_ids) if component_ids.count(component_id) > 1
    )
    if duplicate_components:
        raise ValueError(
            "duplicate software identity component id(s): " + ", ".join(duplicate_components)
        )
    binding_ids = tuple(binding.binding_id for binding in record.bindings)
    duplicate_bindings = sorted(
        binding_id for binding_id in set(binding_ids) if binding_ids.count(binding_id) > 1
    )
    if duplicate_bindings:
        raise ValueError(
            "duplicate software identity binding id(s): " + ", ".join(duplicate_bindings)
        )

    station_families = {SoftwareFamily.FIO_SPOTTER, SoftwareFamily.COMMSTAT}
    if family in station_families:
        if record.owner != "station":
            raise ValueError(f"{family.value} identity must be station-owned")
    else:
        if record.owner == "station":
            raise ValueError(f"only station services may use station owner: {family.value}")
        if record.scope in {
            ExecutionScope.STATION_SHARED_UTILITY.value,
            ExecutionScope.BUILT_IN.value,
        }:
            raise ValueError(
                f"radio-owned {family.value} identity has invalid scope: {record.scope}"
            )

    if family == SoftwareFamily.FIO_SPOTTER:
        if (
            record.scope != ExecutionScope.BUILT_IN.value
            or record.source_mode != InstanceSourceMode.BUILT_IN.value
            or record.management_mode != ManagementMode.BUILT_IN.value
        ):
            raise ValueError("FIO Spotter identity must be built-in and station-owned")
        if component_ids != ("fio-spotter",):
            raise ValueError("FIO Spotter identity requires exactly one fio-spotter component")
        if len(record.bindings) != 1 or record.bindings[0].kind != "built-in-radio":
            raise ValueError("FIO Spotter identity requires one built-in-radio binding")
        if not record.bindings[0].radio_key:
            raise ValueError("FIO Spotter built-in-radio binding requires a radio key")

    if family == SoftwareFamily.COMMSTAT:
        if (
            record.scope != ExecutionScope.STATION_SHARED_UTILITY.value
            or record.source_mode != InstanceSourceMode.SHARED_STATION_TOOL.value
            or record.bundle_id != "commstat:station"
        ):
            raise ValueError(
                "CommStat identity must use the station-shared commstat:station service"
            )
        station_bindings = [item for item in record.bindings if item.kind == "station-process"]
        radio_bindings = [item for item in record.bindings if item.kind == "radio-js8-endpoint"]
        if len(station_bindings) != 1 or station_bindings[0].radio_key:
            raise ValueError("CommStat identity requires one station-process binding")
        if not radio_bindings or any(not item.radio_key for item in radio_bindings):
            raise ValueError("CommStat identity requires a radio-js8-endpoint binding per radio")
        if len(record.bindings) != len(station_bindings) + len(radio_bindings):
            raise ValueError("CommStat identity has unsupported binding kinds")

    if family == SoftwareFamily.FAST_LIGHT:
        fast_light_components = {"flrig", "fldigi", "flmsg", "flamp"}
        # A component may carry a radio or station qualifier (for example
        # ``fldigi:radio-a``) while retaining its application identity.
        component_kinds = {component_id.split(":", 1)[0] for component_id in component_ids}
        unknown = sorted(component_kinds - fast_light_components)
        if unknown:
            raise ValueError(
                "Fast Light identity has unknown component id(s): " + ", ".join(unknown)
            )
        # Each Fast Light application may be configured independently. Any
        # explicitly chosen launch dependencies use the ordinary graph checks;
        # selecting FLMsg or FLAmp does not require another installed component.


def identity_record_to_mapping(value: SoftwareIdentityRecord) -> Mapping[str, Any]:
    """Return JSON-ready canonical identity data, including the computed hash."""

    result = _thaw(value._fingerprint_payload())
    result["fingerprint"] = value.fingerprint
    return result


def identity_record_from_mapping(value: Mapping[str, Any]) -> SoftwareIdentityRecord:
    """Reconstruct a record and recompute its fingerprint from complete data."""

    return SoftwareIdentityRecord(
        bundle_id=_text(value.get("bundle_id")),
        identity_key=_text(value.get("identity_key")),
        family_key=_family_key(value.get("family_key")),
        owner=_text(value.get("owner")),
        scope=_text(value.get("scope")),
        source_mode=_text(value.get("source_mode")),
        management_mode=_text(value.get("management_mode")),
        completion_policy=_text(value.get("completion_policy")),
        provenance=_text(value.get("provenance")),
        verification=value.get("verification", {}) or {},
        paths=value.get("paths", {}) or {},
        resources=tuple(value.get("resources", ()) or ()),
        endpoints=tuple(value.get("endpoints", ()) or ()),
        components=tuple(_component_from_mapping(item) for item in value.get("components", ()) or ()),
        bindings=tuple(_binding_from_mapping(item) for item in value.get("bindings", ()) or ()),
        launch=value.get("launch", {}) or {},
        readiness=value.get("readiness", {}) or {},
        fingerprint=_text(value.get("fingerprint")),
    )


def _bundle_from_value(value: object) -> AtomicInstanceBundle:
    if isinstance(value, AtomicInstanceBundle):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("guided identity draft must be an AtomicInstanceBundle or mapping")
    candidate = value.get("bundle", value.get("atomic_bundle", value))
    if isinstance(candidate, AtomicInstanceBundle):
        return candidate
    if not isinstance(candidate, Mapping):
        raise TypeError("guided identity draft bundle must be a mapping")
    try:
        return atomic_bundle_from_mapping(candidate)
    except (KeyError, TypeError, ValueError):
        family_key = _family_key(candidate.get("family_key") or candidate.get("family"))
        return _bundle_from_guided_mapping(family_key, candidate, radio_key="")


def _execution_scope(value: object, *, observer: bool = False) -> ExecutionScope:
    normalized = _text(value).casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"receive_only", "observer"} or observer:
        return ExecutionScope.RECEIVE_ONLY
    if normalized in {"station_shared", "station_shared_utility", "shared"}:
        return ExecutionScope.STATION_SHARED_UTILITY
    if normalized == "remote":
        return ExecutionScope.REMOTE
    if normalized in {"built_in", "builtin"}:
        return ExecutionScope.BUILT_IN
    return ExecutionScope.STANDARD


def _source_mode(value: object) -> InstanceSourceMode:
    normalized = _text(value).casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"discover", "existing", "use_existing_instance"}:
        return InstanceSourceMode.EXISTING
    if normalized in {"manual", "remote", "manual_or_remote"}:
        return InstanceSourceMode.MANUAL_OR_REMOTE
    if normalized in {"shared", "shared_station_tool"}:
        return InstanceSourceMode.SHARED_STATION_TOOL
    if normalized in {"built_in", "builtin", "built_into_fio"}:
        return InstanceSourceMode.BUILT_IN
    return InstanceSourceMode.CREATE_DISTINCT


def _management_mode(value: object, source: InstanceSourceMode) -> ManagementMode:
    normalized = _text(value).casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"fio", "managed", "fio_managed"}:
        return ManagementMode.FIO_MANAGED
    if normalized == "remote":
        return ManagementMode.REMOTE
    if normalized in {"built_in", "builtin"}:
        return ManagementMode.BUILT_IN
    if source == InstanceSourceMode.BUILT_IN:
        return ManagementMode.BUILT_IN
    if source == InstanceSourceMode.MANUAL_OR_REMOTE and normalized == "remote":
        return ManagementMode.REMOTE
    return ManagementMode.OPERATOR


def _endpoint_records(value: object, *, host: str = "127.0.0.1") -> Tuple[EndpointRecord, ...]:
    rows = value if isinstance(value, (tuple, list)) else ()
    records: list[EndpointRecord] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            continue
        try:
            port = int(raw.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= port <= 65535:
            continue
        label = _text(raw.get("endpoint_key") or raw.get("name")) or f"endpoint-{index + 1}"
        records.append(
            EndpointRecord(
                endpoint_key=_key(label, f"endpoint-{index + 1}"),
                transport=_text(raw.get("transport") or raw.get("protocol") or "tcp").casefold(),
                host=_text(raw.get("host") or host) or "127.0.0.1",
                port=port,
                target=_text(raw.get("target")),
                exclusive=bool(raw.get("exclusive", True)),
            )
        )
    return tuple(records)


def _resource_records(value: object) -> Tuple[ResourceClaimRecord, ...]:
    rows = value if isinstance(value, (tuple, list)) else ()
    records: list[ResourceClaimRecord] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            continue
        item_value = _text(raw.get("value"))
        if not item_value:
            continue
        label = _text(raw.get("resource_key") or raw.get("kind")) or f"resource-{index + 1}"
        records.append(
            ResourceClaimRecord(
                resource_key=_key(label, f"resource-{index + 1}"),
                resource_type=_text(raw.get("resource_type") or raw.get("kind") or "resource"),
                value=item_value,
                exclusive=bool(raw.get("exclusive", True)),
            )
        )
    return tuple(records)


def _launch_components(value: object, *, default_scope: ExecutionScope) -> Tuple[LaunchComponentRecord, ...]:
    recipe = value if isinstance(value, Mapping) else {}
    rows = recipe.get("components", ()) if isinstance(recipe, Mapping) else ()
    components: list[LaunchComponentRecord] = []
    for raw in rows if isinstance(rows, (tuple, list)) else ():
        if not isinstance(raw, Mapping):
            continue
        component_key = _key(raw.get("component_key"), "component")
        executable = _text(raw.get("executable") or raw.get("launch_executable"))
        operator_starts = bool(raw.get("operator_starts", False) or not executable)
        environment = raw.get("environment", {})
        readiness = raw.get("readiness", raw.get("readiness_policy", {}))
        components.append(
            LaunchComponentRecord(
                component_key=component_key,
                executable=executable,
                arguments=tuple(_text(item) for item in raw.get("arguments", ()) or ()),
                working_directory=_text(raw.get("working_directory")),
                environment=dict(environment) if isinstance(environment, Mapping) else {},
                profile_selector=_text(raw.get("profile_selector")),
                dependencies=tuple(_key(item, "dependency") for item in raw.get("dependencies", ()) or ()),
                readiness_policy=dict(readiness) if isinstance(readiness, Mapping) else {},
                execution_scope=_execution_scope(raw.get("execution_scope"))
                if _text(raw.get("execution_scope"))
                else default_scope,
                operator_starts=operator_starts,
                launch_at_startup=bool(raw.get("launch_at_startup", False)) and not operator_starts,
                monitor_health=bool(raw.get("monitor_health", True)),
            )
        )
    return tuple(components)


def _bundle_from_guided_mapping(
    family_key: str,
    value: Mapping[str, Any],
    *,
    radio_key: str,
) -> AtomicInstanceBundle:
    """Adapt the existing guided assistant payload once at the boundary."""

    family = SoftwareFamily(family_key)
    source = _source_mode(value.get("source_mode") or value.get("mode"))
    role = _text(value.get("radio_role") or value.get("device_class")).casefold()
    scope = _execution_scope(value.get("execution_scope"), observer=role == "observer")
    instance_seed = (
        value.get("instance_key")
        or value.get("application_system_key")
        or value.get("imported_system_key")
        or value.get("system_key")
        or value.get("instance_name")
        or family_key
    )
    instance_key = _text(instance_seed)
    if ":" not in instance_key:
        instance_key = f"{family_key}:{_key(instance_key, family_key)}"
    host = _text(value.get("host")) or "127.0.0.1"
    endpoints = _endpoint_records(value.get("ports"), host=host)
    if not endpoints:
        fallback_ports = (
            ("primary", value.get("port")),
            ("secondary", value.get("secondary_port")),
        )
        endpoints = _endpoint_records(
            [
                {"name": name, "host": host, "port": port, "protocol": "tcp"}
                for name, port in fallback_ports
                if port not in (None, "", 0, "0")
            ],
            host=host,
        )
    component_message_paths: list[str] = []
    raw_recipe = value.get("launch_recipe")
    if isinstance(raw_recipe, Mapping):
        for raw_component in raw_recipe.get("components", ()) or ():
            if not isinstance(raw_component, Mapping):
                continue
            if _text(raw_component.get("component_key")).casefold() not in {"flmsg", "flamp"}:
                continue
            component_message_paths.extend(
                _text(path)
                for path in raw_component.get("data_roots", ()) or ()
                if _text(path)
            )
    message_paths = tuple(
        dict.fromkeys(
            path
            for path in (
                _text(value.get("secondary_storage_path")),
                _text(value.get("outbox_path")),
                _text(value.get("bbs_path")),
                _text(value.get("bbs_archive_path")),
                _text(value.get("flmsg_message_path")),
                _text(value.get("flmsg_templates_path")),
                _text(value.get("flmsg_auto_path")),
                _text(value.get("flamp_receive_path")),
                _text(value.get("flamp_outgoing_path")),
                *component_message_paths,
            )
            if path
        )
    )
    resources = list(_resource_records(value.get("resource_claims")))
    working_directory = _text(value.get("working_directory"))
    if working_directory and not any(item.value == working_directory for item in resources):
        resources.append(
            ResourceClaimRecord("working-directory", "working_directory", working_directory)
        )
    components = _launch_components(value.get("launch_recipe"), default_scope=scope)
    raw = AtomicInstanceBundle(
        family=family,
        instance_key=instance_key,
        source_mode=(
            InstanceSourceMode.CREATE_DISTINCT
            if source == InstanceSourceMode.EXISTING
            else source
        ),
        completion_policy=CompletionPolicy.REQUIRED,
        owner_radio_key=_key(radio_key or value.get("owner_radio_key"), "radio"),
        management_mode=_management_mode(value.get("management_mode"), source),
        variant=_text(value.get("variant")),
        version=_text(value.get("version")),
        configuration_path=_text(value.get("configuration_path")),
        data_path=_text(value.get("storage_path")),
        message_paths=message_paths,
        execution_scope=scope,
        endpoints=endpoints,
        resources=tuple(resources),
        launch_components=components,
        provenance=_text(value.get("provenance") or value.get("mode") or "guided"),
        desired_fingerprint=_text(value.get("desired_fingerprint")),
        observed_fingerprint=_text(value.get("observed_fingerprint")),
        verification_evidence={
            "native_configuration_status": _text(value.get("native_configuration_status")),
            "launch_recipe_fingerprint": _text(value.get("launch_recipe_fingerprint")),
        },
        recovery_state=_text(value.get("recovery_state")),
    )
    return lock_existing_bundle(raw) if source == InstanceSourceMode.EXISTING else raw


def _selected_family_keys(selected_families: Sequence[object]) -> Tuple[str, ...]:
    result: list[str] = []
    for item in selected_families:
        if isinstance(item, Mapping):
            if not bool(item.get("selected", True)):
                continue
            key = _family_key(item.get("family", item.get("family_key")))
        else:
            key = _family_key(item)
        if key == "receiver":
            key = SoftwareFamily.SDRPP.value
        if key and key not in result:
            result.append(key)
    return tuple(result)


def _component_record(value: Any) -> SoftwareIdentityComponent:
    return SoftwareIdentityComponent(
        component_id=value.component_key,
        argv=(value.executable, *value.arguments),
        cwd=value.working_directory,
        env=value.environment,
        dependencies=value.dependencies,
        launch={
            "operator_starts": value.operator_starts,
            "at_startup": value.launch_at_startup,
            "monitor_health": value.monitor_health,
        },
        readiness=value.readiness_policy,
    )


def _station_commstat_bindings(
    radio_key: str,
    bundle: AtomicInstanceBundle,
    drafts: Mapping[str, object],
) -> Tuple[SoftwareIdentityBinding, ...]:
    js8_endpoint: Mapping[str, Any] = {}
    js8_value = drafts.get("js8call")
    if js8_value is not None:
        js8 = _bundle_from_value(js8_value)
        if js8.endpoints:
            js8_endpoint = endpoint_to_mapping(js8.endpoints[0])
    return (
        SoftwareIdentityBinding("commstat:station", "station-process"),
        SoftwareIdentityBinding(
            f"commstat:{radio_key}:js8-endpoint",
            "radio-js8-endpoint",
            radio_key,
            js8_endpoint,
        ),
    )


def _record_from_bundle(
    radio_key: str,
    family_key: str,
    bundle: AtomicInstanceBundle,
    drafts: Mapping[str, object],
) -> SoftwareIdentityRecord:
    components = tuple(_component_record(item) for item in bundle.launch_components)
    bindings: Tuple[SoftwareIdentityBinding, ...] = ()
    if family_key == SoftwareFamily.FIO_SPOTTER.value:
        # This component is deliberately explicit even though it is built into
        # FIO and has no child executable.  It prevents later screens from
        # inferring Spotter merely because JS8Call happened to be selected.
        components = (
            SoftwareIdentityComponent(
                component_id="fio-spotter",
                launch={"built_in": True, "at_startup": True},
                readiness={"kind": "fio_internal"},
            ),
        )
        bindings = (SoftwareIdentityBinding(f"fio-spotter:{radio_key}", "built-in-radio", radio_key),)
    elif family_key == SoftwareFamily.COMMSTAT.value:
        bindings = _station_commstat_bindings(radio_key, bundle, drafts)
    is_spotter = family_key == SoftwareFamily.FIO_SPOTTER.value
    is_commstat = family_key == SoftwareFamily.COMMSTAT.value
    bundle_id = "commstat:station" if is_commstat else bundle.instance_key
    owner = "station" if (is_spotter or is_commstat) else (bundle.owner_radio_key or radio_key)
    scope = (
        ExecutionScope.BUILT_IN.value if is_spotter
        else ExecutionScope.STATION_SHARED_UTILITY.value if is_commstat
        else bundle.execution_scope.value
    )
    source_mode = (
        InstanceSourceMode.BUILT_IN.value if is_spotter
        else InstanceSourceMode.SHARED_STATION_TOOL.value if is_commstat
        else bundle.source_mode.value
    )
    management_mode = (
        ManagementMode.BUILT_IN.value if is_spotter
        else ManagementMode.OPERATOR.value if is_commstat
        else bundle.management_mode.value
    )
    launch = {
        "argv": {item.component_id: list(item.argv) for item in components},
        "cwd": {item.component_id: item.cwd for item in components},
        "env": {item.component_id: _thaw(item.env) for item in components},
        "dependencies": {item.component_id: list(item.dependencies) for item in components},
    }
    if family_key == SoftwareFamily.COMMSTAT.value:
        # AtomicInstanceBundle only permits dependencies among its own launch
        # components.  CommStat's JS8 relationship crosses a bundle boundary,
        # so retain it here as canonical identity/binding metadata instead of
        # smuggling ``js8call`` into the station-service launch graph.
        js8_binding = next(
            (item for item in bindings if item.kind == "radio-js8-endpoint"),
            None,
        )
        launch["cross_family_dependencies"] = {
            "commstat": [{
                "family_key": SoftwareFamily.JS8CALL.value,
                "binding_id": js8_binding.binding_id if js8_binding is not None else "",
                "radio_key": js8_binding.radio_key if js8_binding is not None else radio_key,
            }]
        }
    elif family_key == SoftwareFamily.EXTERNAL_JS8SPOTTER.value:
        # Same boundary rule as CommStat: this companion uses the radio's
        # JS8 API, but JS8Call is not a child process in its atomic bundle.
        launch["cross_family_dependencies"] = {
            "external-js8spotter": [{
                "family_key": SoftwareFamily.JS8CALL.value,
                "radio_key": radio_key,
                "kind": "js8call-api",
            }]
        }
    readiness = {item.component_id: _thaw(item.readiness) for item in components}
    return SoftwareIdentityRecord(
        bundle_id=bundle_id,
        identity_key=f"{radio_key}:{family_key}:{bundle_id}",
        family_key=family_key,
        owner=owner,
        scope=scope,
        source_mode=source_mode,
        management_mode=management_mode,
        completion_policy=bundle.completion_policy.value,
        provenance=bundle.provenance,
        verification={
            "desired_fingerprint": bundle.desired_fingerprint,
            "observed_fingerprint": bundle.observed_fingerprint,
            "evidence": dict(bundle.verification_evidence),
            "recovery_state": bundle.recovery_state,
        },
        paths={
            "configuration_path": bundle.configuration_path,
            "data_path": bundle.data_path,
            "message_paths": list(bundle.message_paths),
        },
        resources=tuple(resource_claim_to_mapping(item) for item in bundle.resources),
        endpoints=tuple(endpoint_to_mapping(item) for item in bundle.endpoints),
        components=components,
        bindings=bindings,
        launch=launch,
        readiness=readiness,
    )


def build_guided_identity_records(
    radio_profile: Mapping[str, Any],
    guided_drafts: Mapping[str, object],
    selected_families: Sequence[object],
) -> Tuple[SoftwareIdentityRecord, ...]:
    """Project the selected guided bundles into stable, complete identity records."""

    radio_key = _text(radio_profile.get("system_key") or radio_profile.get("radio_key") or radio_profile.get("id"))
    if not radio_key:
        raise ValueError("radio_profile needs system_key, radio_key, or id")
    selected = _selected_family_keys(selected_families)
    records: list[SoftwareIdentityRecord] = []
    for family_key in selected:
        value = guided_drafts.get(family_key)
        if value is None and family_key == SoftwareFamily.FIO_SPOTTER.value:
            bundle = AtomicInstanceBundle(
                family=SoftwareFamily.FIO_SPOTTER,
                instance_key=f"fio-spotter:{_key(radio_key, 'radio')}",
                source_mode=InstanceSourceMode.BUILT_IN,
                completion_policy=CompletionPolicy.REQUIRED,
                owner_radio_key=_key(radio_key, "radio"),
                management_mode=ManagementMode.BUILT_IN,
                execution_scope=ExecutionScope.BUILT_IN,
                provenance="guided",
            )
        elif value is None and family_key == SoftwareFamily.COMMSTAT.value:
            js8_value = guided_drafts.get(SoftwareFamily.JS8CALL.value)
            launch_path = _text(radio_profile.get("commstat_launch_path"))
            bundle = AtomicInstanceBundle(
                family=SoftwareFamily.COMMSTAT,
                instance_key="commstat:station",
                source_mode=InstanceSourceMode.SHARED_STATION_TOOL,
                completion_policy=CompletionPolicy.REQUIRED,
                owner_radio_key="",
                management_mode=ManagementMode.OPERATOR,
                execution_scope=ExecutionScope.STATION_SHARED_UTILITY,
                launch_components=(
                    LaunchComponentRecord(
                        component_key="commstat",
                        executable=launch_path,
                        # JS8Call is a different atomic bundle.  Its required
                        # endpoint relationship is retained in the resulting
                        # SoftwareIdentityRecord, not as a local launch edge.
                        dependencies=(),
                        execution_scope=ExecutionScope.STATION_SHARED_UTILITY,
                        operator_starts=not bool(launch_path),
                        launch_at_startup=False,
                        monitor_health=True,
                        readiness_policy={"kind": "station_process"},
                    ),
                ),
                provenance="guided",
                verification_evidence={
                    "js8_binding_available": "true" if js8_value is not None else "false"
                },
            )
        elif value is None and family_key == SoftwareFamily.EXTERNAL_JS8SPOTTER.value:
            launch_path = _text(radio_profile.get("spotter_launch_path"))
            js8_draft = guided_drafts.get(SoftwareFamily.JS8CALL.value)
            js8_draft = js8_draft if isinstance(js8_draft, Mapping) else {}
            launch_at_startup = bool(
                js8_draft.get("external_spotter_launch_at_startup", False)
            )
            bundle = AtomicInstanceBundle(
                family=SoftwareFamily.EXTERNAL_JS8SPOTTER,
                instance_key=f"external-js8spotter:{_key(radio_key, 'radio')}",
                source_mode=InstanceSourceMode.MANUAL_OR_REMOTE,
                completion_policy=CompletionPolicy.OPTIONAL,
                owner_radio_key=_key(radio_key, "radio"),
                management_mode=ManagementMode.OPERATOR,
                execution_scope=ExecutionScope.STANDARD,
                launch_components=(
                    LaunchComponentRecord(
                        component_key="external-js8spotter",
                        executable=launch_path,
                        dependencies=(),
                        execution_scope=ExecutionScope.STANDARD,
                        operator_starts=not bool(launch_path),
                        launch_at_startup=bool(launch_path and launch_at_startup),
                        monitor_health=bool(launch_path),
                        readiness_policy={"kind": "operator_confirmed"},
                    ),
                ),
                provenance="guided",
            )
        elif value is None and family_key == SoftwareFamily.SDRPP.value:
            launch_item = radio_profile.get("receiver_launch_item")
            launch_item = launch_item if isinstance(launch_item, Mapping) else {}
            launch_readiness = launch_item.get("readiness", {})
            launch_readiness = launch_readiness if isinstance(launch_readiness, Mapping) else {}
            launch_path = _text(
                launch_item.get("path_override")
                or launch_readiness.get("executable")
                or radio_profile.get("receiver_launch_path")
                or radio_profile.get("sdr_launch_path")
                or radio_profile.get("launch_path")
            )
            host = _text(radio_profile.get("sdr_host")) or "127.0.0.1"
            try:
                port = int(radio_profile.get("sdr_port") or 0)
            except (TypeError, ValueError):
                port = 0
            bundle = AtomicInstanceBundle(
                family=SoftwareFamily.SDRPP,
                instance_key=f"receiver:{_key(radio_key, 'radio')}",
                source_mode=InstanceSourceMode.MANUAL_OR_REMOTE,
                completion_policy=CompletionPolicy.REQUIRED,
                owner_radio_key=_key(radio_key, "radio"),
                management_mode=ManagementMode.OPERATOR,
                variant=_text(radio_profile.get("sdr_application")),
                execution_scope=ExecutionScope.RECEIVE_ONLY,
                endpoints=(
                    EndpointRecord(
                        endpoint_key="receiver-control",
                        transport="tcp",
                        host=host,
                        port=port,
                        target=_text(radio_profile.get("sdr_target")),
                        exclusive=True,
                    ),
                ) if 1 <= port <= 65535 else (),
                launch_components=(
                    LaunchComponentRecord(
                        component_key="receiver",
                        executable=launch_path,
                        arguments=tuple(
                            _text(item)
                            for item in launch_readiness.get("launch_arguments", ()) or ()
                        ),
                        working_directory=_text(launch_readiness.get("working_directory")),
                        environment=(
                            dict(launch_readiness.get("environment") or {})
                            if isinstance(launch_readiness.get("environment"), Mapping)
                            else {}
                        ),
                        dependencies=tuple(
                            _key(item, "dependency")
                            for item in launch_item.get("dependencies", ()) or ()
                        ),
                        execution_scope=ExecutionScope.RECEIVE_ONLY,
                        operator_starts=not bool(launch_path),
                        launch_at_startup=bool(
                            launch_path
                            and (
                                launch_item.get("launch_at_startup")
                                or radio_profile.get("receiver_launch_enabled")
                            )
                        ),
                        monitor_health=bool(launch_path or port),
                        readiness_policy={
                            **{str(key): str(item) for key, item in launch_readiness.items()},
                            "kind": _text(
                                launch_readiness.get("kind")
                                or radio_profile.get("sdr_adapter")
                            ) or "manual",
                            "host": _text(launch_readiness.get("host") or host),
                            "port": _text(launch_readiness.get("port") or (str(port) if port else "")),
                        },
                    ),
                ),
                provenance="guided",
                verification_evidence={
                    "state": _text(radio_profile.get("sdr_verification_state")) or "manual"
                },
            )
        elif value is None:
            raise ValueError(f"selected family {family_key} has no guided identity bundle")
        elif isinstance(value, AtomicInstanceBundle):
            bundle = value
        elif isinstance(value, Mapping):
            candidate = value.get("bundle", value.get("atomic_bundle"))
            if isinstance(candidate, AtomicInstanceBundle):
                bundle = candidate
            elif isinstance(candidate, Mapping):
                bundle = atomic_bundle_from_mapping(candidate)
            else:
                bundle = _bundle_from_guided_mapping(family_key, value, radio_key=radio_key)
        else:
            raise TypeError("guided identity draft must be an AtomicInstanceBundle or mapping")
        record = _record_from_bundle(radio_key, family_key, bundle, guided_drafts)
        if isinstance(value, Mapping):
            # The atomic model normalizes its internal key for validation, but
            # the guided draft's instance key is also the durable manifest
            # foreign identity.  Preserve that exact reviewed value so the
            # canonical record links to the same manifest after save/reload.
            manifest_key = _text(
                value.get("instance_key")
                or value.get("application_system_key")
                or value.get("imported_system_key")
            )
            if manifest_key and manifest_key != record.bundle_id:
                mapped = dict(identity_record_to_mapping(record))
                mapped["bundle_id"] = manifest_key
                mapped["identity_key"] = f"{radio_key}:{family_key}:{manifest_key}"
                record = identity_record_from_mapping(mapped)
        records.append(record)
    return tuple(records)


def _records_by_identity(records: Sequence[object]) -> tuple[dict[str, SoftwareIdentityRecord], Tuple[str, ...]]:
    indexed: dict[str, SoftwareIdentityRecord] = {}
    duplicates: list[str] = []
    for raw in records:
        record = raw if isinstance(raw, SoftwareIdentityRecord) else identity_record_from_mapping(raw)
        if record.identity_key in indexed:
            duplicates.append(record.identity_key)
        else:
            indexed[record.identity_key] = record
    return indexed, tuple(duplicates)


def validate_identity_parity(
    expected: Sequence[object], actual: Sequence[object],
) -> Tuple[str, ...]:
    """Return actionable differences; an empty tuple means exact identity parity."""

    expected_by_key, expected_duplicates = _records_by_identity(expected)
    actual_by_key, actual_duplicates = _records_by_identity(actual)
    issues: list[str] = []
    for key in expected_duplicates:
        issues.append(f"expected duplicate identity {key}")
    for key in actual_duplicates:
        issues.append(f"actual duplicate identity {key}")
    for key in expected_by_key:
        if key not in actual_by_key:
            issues.append(f"missing identity {key}")
    for key in actual_by_key:
        if key not in expected_by_key:
            issues.append(f"extra identity {key}")
    for key, wanted in expected_by_key.items():
        observed = actual_by_key.get(key)
        if observed is None:
            continue
        prefix = wanted.family_key or key
        wanted_components = [item.component_id for item in wanted.components]
        actual_components = [item.component_id for item in observed.components]
        for component_id in wanted_components:
            if component_id not in actual_components:
                issues.append(f"{prefix}: missing component {component_id}")
        for component_id in actual_components:
            if actual_components.count(component_id) > 1:
                message = f"{prefix}: duplicate component {component_id}"
                if message not in issues:
                    issues.append(message)
        for field_name in (
            "bundle_id", "family_key", "owner", "scope", "source_mode", "management_mode",
            "completion_policy", "provenance", "verification", "paths", "resources", "endpoints",
            "bindings", "launch", "readiness",
        ):
            if getattr(wanted, field_name) != getattr(observed, field_name):
                issues.append(f"{prefix}: {field_name} differs")
        wanted_by_component = {item.component_id: item for item in wanted.components}
        actual_by_component = {item.component_id: item for item in observed.components}
        for component_id in set(wanted_by_component).intersection(actual_by_component):
            if wanted_by_component[component_id] != actual_by_component[component_id]:
                issues.append(f"{prefix}: component {component_id} differs")
        if wanted.fingerprint != observed.fingerprint:
            issues.append(f"{prefix}: fingerprint differs")
    return tuple(issues)


__all__ = (
    "SoftwareIdentityBinding",
    "SoftwareIdentityComponent",
    "SoftwareIdentityRecord",
    "build_guided_identity_records",
    "identity_record_from_mapping",
    "identity_record_to_mapping",
    "validate_identity_parity",
)
