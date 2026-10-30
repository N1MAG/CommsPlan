"""Pure lifecycle model for durable external-software instances.

The manifest complements the application-specific settings tables.  It stores
how an instance was found or created and the evidence needed to review it; it
does not replace the radio-to-instance links in ``device_profiles``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


SUPPORTED_INSTANCE_FAMILIES = frozenset({"js8call", "fast_light", "varac"})
SUPPORTED_MANAGEMENT_MODES = frozenset({"operator", "fio_managed", "remote"})
SUPPORTED_VERIFICATION_STATES = frozenset(
    {"configured", "detected", "reachable", "verified", "needs_attention"}
)
LOCAL_HOSTS = frozenset({"", "127.0.0.1", "localhost", "::1"})
MAX_JSON_CHARS = 64_000
PATH_RESOURCE_KINDS = frozenset(
    {
        "config_root",
        "settings_profile",
        "message_storage",
        "flrig_configuration",
        "fldigi_configuration",
        "fldigi_logs",
        "fldigi_checkins",
        "varac_ini",
        "varac_database",
        "varac_incoming",
        "varac_outbox",
    }
)


# Message/traffic data may intentionally be shared between Fast Light radios.
# Apply this policy when comparing older manifests too, without rewriting them.
FAST_LIGHT_SHARED_DATA_KINDS = frozenset({
    "fldigi_logs", "fldigi_checkins", "flmsg_messages", "flmsg_templates",
    "flmsg_auto", "flamp_receive", "flamp_outgoing",
})


def _exclusive_resource(manifest, claim) -> bool:
    return claim.exclusive and not (
        manifest.family_key == "fast_light" and claim.kind in FAST_LIGHT_SHARED_DATA_KINDS
    )


@dataclass(frozen=True)
class EndpointClaim:
    name: str
    protocol: str
    host: str
    port: int

    @property
    def collision_key(self) -> tuple[str, str, int]:
        host = self.host.strip().casefold()
        if host in LOCAL_HOSTS:
            host = "local"
        return self.protocol.strip().lower(), host, int(self.port)


@dataclass(frozen=True)
class ResourceClaim:
    kind: str
    value: str
    exclusive: bool = True

    @property
    def collision_key(self) -> tuple[str, str]:
        kind = self.kind.strip().casefold()
        value = self.value.strip()
        if kind in PATH_RESOURCE_KINDS:
            expanded = os.path.expandvars(os.path.expanduser(value))
            try:
                value = str(Path(expanded).resolve(strict=False))
            except (OSError, RuntimeError):
                value = os.path.abspath(expanded)
            return "path", os.path.normcase(value).casefold()
        return kind, value.casefold()


@dataclass(frozen=True)
class SoftwareInstanceManifest:
    instance_key: str
    family_key: str
    application_system_key: str
    management_mode: str
    provenance: str
    executable_path: str
    configuration_path: str
    configuration_root: str
    data_root: str
    launch_command: str
    host: str
    ports: tuple[EndpointClaim, ...]
    resource_claims: tuple[ResourceClaim, ...]
    desired_fingerprint: str
    observed_fingerprint: str
    verification_state: str
    verification_summary: str
    evidence: Mapping[str, Any]
    last_discovered_utc: str
    last_verified_utc: str


@dataclass(frozen=True)
class ManifestIssue:
    severity: str
    code: str
    message: str
    other_instance_key: str = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bounded_json_value(value: Any, *, default: Any) -> Any:
    if value in (None, ""):
        return default
    parsed = value
    if isinstance(value, str):
        if len(value) > MAX_JSON_CHARS:
            raise ValueError("Software instance metadata is too large.")
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Software instance metadata is not valid JSON.") from exc
    try:
        encoded = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Software instance metadata contains unsupported values.") from exc
    if len(encoded) > MAX_JSON_CHARS:
        raise ValueError("Software instance metadata is too large.")
    return parsed


def normalize_endpoint_claims(value: Any) -> tuple[EndpointClaim, ...]:
    rows = _bounded_json_value(value, default=[])
    if not isinstance(rows, list):
        raise ValueError("Software instance ports must be a list.")
    claims: list[EndpointClaim] = []
    seen: set[tuple[str, str, int]] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("Each software instance port must be an object.")
        name = _text(raw.get("name")) or "Service"
        protocol = _text(raw.get("protocol")).lower() or "tcp"
        if protocol not in {"tcp", "udp"}:
            raise ValueError(f"Unsupported endpoint protocol: {protocol}")
        host = _text(raw.get("host")) or "127.0.0.1"
        try:
            port = int(raw.get("port"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} requires a numeric port.") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"{name} port must be between 1 and 65535.")
        claim = EndpointClaim(name=name, protocol=protocol, host=host, port=port)
        if claim.collision_key in seen:
            raise ValueError(f"Duplicate endpoint in one instance: {protocol.upper()} {host}:{port}")
        seen.add(claim.collision_key)
        claims.append(claim)
    return tuple(claims)


def normalize_resource_claims(value: Any) -> tuple[ResourceClaim, ...]:
    rows = _bounded_json_value(value, default=[])
    if not isinstance(rows, list):
        raise ValueError("Software instance resources must be a list.")
    claims: list[ResourceClaim] = []
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("Each software instance resource must be an object.")
        kind = _text(raw.get("kind")).lower()
        item_value = _text(raw.get("value"))
        if not kind or not item_value:
            raise ValueError("Software instance resources require kind and value.")
        claim = ResourceClaim(kind=kind, value=item_value, exclusive=bool(raw.get("exclusive", True)))
        if claim.collision_key in seen:
            continue
        seen.add(claim.collision_key)
        claims.append(claim)
    return tuple(claims)


def manifest_from_mapping(values: Mapping[str, Any]) -> SoftwareInstanceManifest:
    family = _text(values.get("family_key")).lower()
    if family not in SUPPORTED_INSTANCE_FAMILIES:
        raise ValueError(f"Unsupported software instance family: {family or 'blank'}")
    mode = _text(values.get("management_mode")).lower() or "operator"
    if mode not in SUPPORTED_MANAGEMENT_MODES:
        raise ValueError(f"Unsupported software management mode: {mode}")
    state = _text(values.get("verification_state")).lower() or "configured"
    if state not in SUPPORTED_VERIFICATION_STATES:
        raise ValueError(f"Unsupported software verification state: {state}")
    instance_key = _text(values.get("instance_key"))
    if not instance_key:
        raise ValueError("Software instance key is required.")
    app_key = _text(values.get("application_system_key"))
    evidence = _bounded_json_value(values.get("evidence", values.get("evidence_json")), default={})
    if not isinstance(evidence, Mapping):
        raise ValueError("Software instance evidence must be an object.")
    return SoftwareInstanceManifest(
        instance_key=instance_key,
        family_key=family,
        application_system_key=app_key,
        management_mode=mode,
        provenance=_text(values.get("provenance")) or "manual",
        executable_path=_text(values.get("executable_path")),
        configuration_path=_text(values.get("configuration_path")),
        configuration_root=_text(values.get("configuration_root")),
        data_root=_text(values.get("data_root")),
        launch_command=_text(values.get("launch_command")),
        host=_text(values.get("host")) or "127.0.0.1",
        ports=normalize_endpoint_claims(values.get("ports", values.get("ports_json"))),
        resource_claims=normalize_resource_claims(
            values.get("resource_claims", values.get("resource_claims_json"))
        ),
        desired_fingerprint=_text(values.get("desired_fingerprint")),
        observed_fingerprint=_text(values.get("observed_fingerprint")),
        verification_state=state,
        verification_summary=_text(values.get("verification_summary")),
        evidence=dict(evidence),
        last_discovered_utc=_text(values.get("last_discovered_utc")),
        last_verified_utc=_text(values.get("last_verified_utc")),
    )


def manifest_to_record(manifest: SoftwareInstanceManifest) -> dict[str, Any]:
    return {
        "instance_key": manifest.instance_key,
        "family_key": manifest.family_key,
        "application_system_key": manifest.application_system_key,
        "management_mode": manifest.management_mode,
        "provenance": manifest.provenance,
        "executable_path": manifest.executable_path,
        "configuration_path": manifest.configuration_path,
        "configuration_root": manifest.configuration_root,
        "data_root": manifest.data_root,
        "launch_command": manifest.launch_command,
        "host": manifest.host,
        "ports_json": json.dumps(
            [
                {"name": item.name, "protocol": item.protocol, "host": item.host, "port": item.port}
                for item in manifest.ports
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "resource_claims_json": json.dumps(
            [
                {"kind": item.kind, "value": item.value, "exclusive": item.exclusive}
                for item in manifest.resource_claims
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "desired_fingerprint": manifest.desired_fingerprint,
        "observed_fingerprint": manifest.observed_fingerprint,
        "verification_state": manifest.verification_state,
        "verification_summary": manifest.verification_summary,
        "evidence_json": json.dumps(dict(manifest.evidence), sort_keys=True, separators=(",", ":")),
        "last_discovered_utc": manifest.last_discovered_utc,
        "last_verified_utc": manifest.last_verified_utc,
    }


def find_manifest_conflicts(
    proposed: SoftwareInstanceManifest,
    existing: Iterable[SoftwareInstanceManifest],
) -> tuple[ManifestIssue, ...]:
    issues: list[ManifestIssue] = []
    for other in existing:
        if other.instance_key == proposed.instance_key:
            continue
        if (
            proposed.application_system_key
            and proposed.family_key == other.family_key
            and proposed.application_system_key == other.application_system_key
        ):
            issues.append(
                ManifestIssue(
                    "error",
                    "duplicate_application_identity",
                    "This application instance identity is already registered.",
                    other.instance_key,
                )
            )
        other_ports = {claim.collision_key: claim for claim in other.ports}
        for claim in proposed.ports:
            matched = other_ports.get(claim.collision_key)
            if matched is not None:
                issues.append(
                    ManifestIssue(
                        "error",
                        "endpoint_collision",
                        f"{claim.name} conflicts with {matched.name} at {claim.host}:{claim.port}/{claim.protocol}.",
                        other.instance_key,
                    )
                )
        other_resources = {claim.collision_key: claim for claim in other.resource_claims if _exclusive_resource(other, claim)}
        for claim in proposed.resource_claims:
            matched = other_resources.get(claim.collision_key)
            if _exclusive_resource(proposed, claim) and matched is not None:
                issues.append(
                    ManifestIssue(
                        "error",
                        "resource_collision",
                        f"{claim.kind.replace('_', ' ').title()} is already owned by another instance: {claim.value}",
                        other.instance_key,
                    )
                )
    return tuple(issues)


def next_available_port(
    preferred: int,
    *,
    protocol: str,
    host: str,
    manifests: Iterable[SoftwareInstanceManifest],
    reserved: Iterable[int] = (),
    maximum_attempts: int = 200,
) -> int:
    used = {int(port) for port in reserved}
    normalized_host = "local" if host.strip().casefold() in LOCAL_HOSTS else host.strip().casefold()
    normalized_protocol = protocol.strip().lower()
    for manifest in manifests:
        for claim in manifest.ports:
            proto, claim_host, port = claim.collision_key
            if proto == normalized_protocol and claim_host == normalized_host:
                used.add(port)
    start = max(1, min(65535, int(preferred)))
    for port in range(start, min(65536, start + max(1, maximum_attempts))):
        if port not in used:
            return port
    raise ValueError(f"No available {normalized_protocol.upper()} port found near {start}.")


__all__ = [
    "EndpointClaim",
    "ManifestIssue",
    "ResourceClaim",
    "SoftwareInstanceManifest",
    "SUPPORTED_INSTANCE_FAMILIES",
    "SUPPORTED_MANAGEMENT_MODES",
    "SUPPORTED_VERIFICATION_STATES",
    "find_manifest_conflicts",
    "manifest_from_mapping",
    "manifest_to_record",
    "next_available_port",
    "normalize_endpoint_claims",
    "normalize_resource_claims",
]
