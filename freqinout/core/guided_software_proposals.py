"""Pure discovery evidence and atomic proposal planning for guided software.

GRS-1 deliberately keeps this module below discovery, persistence, launch, and
UI adapters.  A scanner converts observations into DiscoveryEvidence and
DiscoveredSoftwareCandidate; this module never reads a file, opens a socket,
starts a process, or changes a configuration.  A coordinator may therefore
publish immutable snapshots without exposing mutable scanner state to the GUI.

The planners work only with complete AtomicInstanceBundle values from
guided_radio_software_model.  They never return a port from one proposal and
paths from another.  A JS8 proposal allocates its API port, profile/data/message
roots, rig name, and launch identity in one operation.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from freqinout.core.guided_radio_software_model import (
    AllowedState,
    AtomicInstanceBundle,
    CompletionPolicy,
    EndpointRecord,
    ExecutionScope,
    GuidedRadioSoftwareValidationError,
    InstanceSourceMode,
    LaunchComponentRecord,
    ManagementMode,
    RadioRole,
    ResourceClaimRecord,
    SoftwareFamily,
    atomic_bundle_from_mapping,
    capability_for,
    lock_existing_bundle,
    radio_role_from_persisted,
    software_family_for_capability,
)


class GuidedSoftwareProposalError(GuidedRadioSoftwareValidationError):
    """Raised when discovery evidence cannot form a safe complete proposal."""


class CandidateState(str, Enum):
    """Scanner confidence state; only COMPLETE candidates may be imported."""

    COMPLETE = "complete"
    AMBIGUOUS = "ambiguous"
    INCOMPLETE = "incomplete"
    CONFLICTING = "conflicting"


_ID_RE = re.compile(r"[^a-z0-9_.-]+")
_MAX_SHORT_TEXT = 256
_MAX_TEXT = 4096
_MAX_ATTRIBUTES = 64
_MAX_EVIDENCE = 64
_MAX_CANDIDATES = 128
_MAX_BUNDLES = 256
_MAX_ALLOCATION_ATTEMPTS = 4096


def _text(value: object, field_name: str, *, required: bool = False, maximum: int = _MAX_TEXT) -> str:
    result = str(value or "").strip()
    if len(result) > maximum:
        raise GuidedSoftwareProposalError(f"{field_name} exceeds {maximum} characters")
    if required and not result:
        raise GuidedSoftwareProposalError(f"{field_name} is required")
    return result


def _key(value: object, field_name: str, *, required: bool = True) -> str:
    result = _ID_RE.sub("-", _text(value, field_name, required=required, maximum=_MAX_SHORT_TEXT).casefold()).strip("-._")
    if required and not result:
        raise GuidedSoftwareProposalError(f"{field_name} is required")
    return result


def _bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _bounded_tuple(values: Iterable[Any], field_name: str, maximum: int) -> Tuple[Any, ...]:
    result = tuple(values or ())
    if len(result) > maximum:
        raise GuidedSoftwareProposalError(f"{field_name} has more than {maximum} values")
    return result


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise GuidedSoftwareProposalError(f"{field_name} must be a mapping")
    return value


def _enum(enum_type: Any, value: object, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    normalized = _text(value, field_name, required=True, maximum=_MAX_SHORT_TEXT).casefold()
    try:
        return enum_type(normalized)
    except ValueError as exc:
        raise GuidedSoftwareProposalError(f"Unknown {field_name}: {value}") from exc


def _component_keys(bundle: AtomicInstanceBundle) -> Tuple[str, ...]:
    return tuple(component.component_key for component in bundle.launch_components)


def _resource_keys(bundle: AtomicInstanceBundle) -> Tuple[str, ...]:
    return tuple(resource.resource_key for resource in bundle.resources)


def _has_external_tx_disabled_claim(bundle: AtomicInstanceBundle) -> bool:
    return any(
        resource.resource_key == "external-tx-disabled"
        and resource.value.casefold() in {"1", "true", "yes", "on", "disabled"}
        for resource in bundle.resources
    )


def _append_path(root: str, *parts: str) -> str:
    """Join a proposed path without resolving or touching the local filesystem."""

    root = _text(root, "managed root", required=True)
    joiner = ntpath if "\\" in root and "/" not in root else posixpath
    return joiner.join(root, *parts)


@dataclass(frozen=True)
class DiscoveryEvidence:
    """One bounded scanner fact; it grants no ownership by itself."""

    evidence_key: str
    source: str
    summary: str
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_key", _key(self.evidence_key, "evidence_key"))
        object.__setattr__(self, "source", _key(self.source, "evidence source"))
        object.__setattr__(self, "summary", _text(self.summary, "evidence summary", required=True, maximum=_MAX_TEXT))
        raw = _mapping(self.attributes, "evidence attributes")
        if len(raw) > _MAX_ATTRIBUTES:
            raise GuidedSoftwareProposalError("evidence attributes has too many values")
        attributes: dict[str, str] = {}
        for raw_key, raw_value in raw.items():
            key = _key(raw_key, "evidence attribute key")
            if key in attributes:
                raise GuidedSoftwareProposalError("duplicate evidence attribute key")
            attributes[key] = _text(raw_value, "evidence attribute value", maximum=_MAX_TEXT)
        object.__setattr__(self, "attributes", MappingProxyType(attributes))


@dataclass(frozen=True)
class DiscoveredSoftwareCandidate:
    """A candidate with one immutable importable identity bundle.

    Construction locks the supplied bundle as use_existing_instance.  This
    prevents later code from combining its old port with a new profile or
    message path.
    """

    candidate_key: str
    bundle: AtomicInstanceBundle
    state: CandidateState = CandidateState.COMPLETE
    evidence: Tuple[DiscoveryEvidence, ...] = field(default_factory=tuple)
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_key", _key(self.candidate_key, "candidate_key"))
        bundle = self.bundle if isinstance(self.bundle, AtomicInstanceBundle) else atomic_bundle_from_mapping(self.bundle)
        if bundle.family == SoftwareFamily.FIO_SPOTTER:
            raise GuidedSoftwareProposalError("built-in CommsPlan Spotter is not a discovered software candidate")
        object.__setattr__(self, "bundle", lock_existing_bundle(bundle))
        object.__setattr__(self, "state", _enum(CandidateState, self.state, "candidate state"))
        evidence = tuple(
            item if isinstance(item, DiscoveryEvidence) else DiscoveryEvidence(**dict(_mapping(item, "candidate evidence")))
            for item in _bounded_tuple(self.evidence, "candidate evidence", _MAX_EVIDENCE)
        )
        if len({item.evidence_key for item in evidence}) != len(evidence):
            raise GuidedSoftwareProposalError("duplicate candidate evidence key")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "note", _text(self.note, "candidate note", maximum=_MAX_TEXT))

    @property
    def importable(self) -> bool:
        return self.state == CandidateState.COMPLETE


@dataclass(frozen=True)
class DiscoverySnapshot:
    """One coordinator generation of immutable scanner output."""

    snapshot_key: str
    generation: int
    candidates: Tuple[DiscoveredSoftwareCandidate, ...] = field(default_factory=tuple)
    evidence: Tuple[DiscoveryEvidence, ...] = field(default_factory=tuple)
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)
    elapsed_ms: int = 0
    cancelled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_key", _key(self.snapshot_key, "snapshot_key"))
        if isinstance(self.generation, bool):
            raise GuidedSoftwareProposalError("generation must be a non-negative integer")
        try:
            generation = int(self.generation)
        except (TypeError, ValueError) as exc:
            raise GuidedSoftwareProposalError("generation must be a non-negative integer") from exc
        if generation < 0:
            raise GuidedSoftwareProposalError("generation must be a non-negative integer")
        object.__setattr__(self, "generation", generation)
        candidates = tuple(
            item if isinstance(item, DiscoveredSoftwareCandidate) else DiscoveredSoftwareCandidate(**dict(_mapping(item, "candidate")))
            for item in _bounded_tuple(self.candidates, "candidates", _MAX_CANDIDATES)
        )
        if len({item.candidate_key for item in candidates}) != len(candidates):
            raise GuidedSoftwareProposalError("duplicate candidate key")
        object.__setattr__(self, "candidates", candidates)
        evidence = tuple(
            item if isinstance(item, DiscoveryEvidence) else DiscoveryEvidence(**dict(_mapping(item, "snapshot evidence")))
            for item in _bounded_tuple(self.evidence, "snapshot evidence", _MAX_EVIDENCE)
        )
        if len({item.evidence_key for item in evidence}) != len(evidence):
            raise GuidedSoftwareProposalError("duplicate snapshot evidence key")
        object.__setattr__(self, "evidence", evidence)
        diagnostics = tuple(_text(item, "discovery diagnostic", required=True, maximum=_MAX_TEXT) for item in _bounded_tuple(self.diagnostics, "diagnostics", _MAX_EVIDENCE))
        object.__setattr__(self, "diagnostics", diagnostics)
        if isinstance(self.elapsed_ms, bool):
            raise GuidedSoftwareProposalError("elapsed_ms must be a non-negative integer")
        try:
            elapsed_ms = int(self.elapsed_ms)
        except (TypeError, ValueError) as exc:
            raise GuidedSoftwareProposalError("elapsed_ms must be a non-negative integer") from exc
        if elapsed_ms < 0:
            raise GuidedSoftwareProposalError("elapsed_ms must be a non-negative integer")
        object.__setattr__(self, "elapsed_ms", elapsed_ms)
        object.__setattr__(self, "cancelled", _bool(self.cancelled))

    def candidates_for(self, family: object, *, include_nonimportable: bool = True) -> Tuple[DiscoveredSoftwareCandidate, ...]:
        parsed_family = software_family_for_capability(family)
        return tuple(
            item for item in self.candidates
            if item.bundle.family == parsed_family and (include_nonimportable or item.importable)
        )


@dataclass(frozen=True)
class ProposalConflict:
    """A bounded display-safe collision detected before persistence."""

    kind: str
    value: str
    existing_instance_key: str
    proposed_instance_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _key(self.kind, "conflict kind"))
        object.__setattr__(self, "value", _text(self.value, "conflict value", required=True, maximum=_MAX_TEXT))
        object.__setattr__(self, "existing_instance_key", _key(self.existing_instance_key, "existing instance key"))
        object.__setattr__(self, "proposed_instance_key", _key(self.proposed_instance_key, "proposed instance key"))


@dataclass(frozen=True)
class ProposalInventory:
    """Known saved/current bundles used for deterministic no-I/O allocation."""

    bundles: Tuple[AtomicInstanceBundle, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        bundles = tuple(
            item if isinstance(item, AtomicInstanceBundle) else atomic_bundle_from_mapping(item)
            for item in _bounded_tuple(self.bundles, "proposal inventory bundles", _MAX_BUNDLES)
        )
        if len({item.computed_identity_fingerprint for item in bundles}) != len(bundles):
            raise GuidedSoftwareProposalError("proposal inventory contains duplicate complete bundle identities")
        object.__setattr__(self, "bundles", bundles)

    @property
    def instance_keys(self) -> Tuple[str, ...]:
        return tuple(item.instance_key for item in self.bundles)

    def conflicts_for(
        self,
        proposal: AtomicInstanceBundle,
        *,
        ignore_identity_fingerprint: str = "",
    ) -> Tuple[ProposalConflict, ...]:
        ignored = _text(ignore_identity_fingerprint, "ignored identity fingerprint", maximum=_MAX_SHORT_TEXT).casefold()
        conflicts: list[ProposalConflict] = []
        seen: set[Tuple[str, str, str]] = set()
        for existing in self.bundles:
            if ignored and existing.computed_identity_fingerprint == ignored:
                continue
            if existing.instance_key == proposal.instance_key:
                conflict = ProposalConflict("instance-key", proposal.instance_key, existing.instance_key, proposal.instance_key)
                marker = (conflict.kind, conflict.value, conflict.existing_instance_key)
                if marker not in seen:
                    conflicts.append(conflict)
                    seen.add(marker)
            for endpoint in proposal.endpoints:
                if not endpoint.exclusive:
                    continue
                for occupied in existing.endpoints:
                    if occupied.exclusive and endpoint.collision_identity == occupied.collision_identity:
                        value = "%s://%s:%s/%s" % (endpoint.transport, endpoint.host, endpoint.port or "", endpoint.target)
                        conflict = ProposalConflict("endpoint", value, existing.instance_key, proposal.instance_key)
                        marker = (conflict.kind, conflict.value, conflict.existing_instance_key)
                        if marker not in seen:
                            conflicts.append(conflict)
                            seen.add(marker)
            for resource in proposal.resources:
                if not resource.exclusive:
                    continue
                for occupied in existing.resources:
                    if occupied.exclusive and resource.collision_identity == occupied.collision_identity:
                        value = "%s:%s" % (resource.resource_type, resource.value)
                        conflict = ProposalConflict("resource", value, existing.instance_key, proposal.instance_key)
                        marker = (conflict.kind, conflict.value, conflict.existing_instance_key)
                        if marker not in seen:
                            conflicts.append(conflict)
                            seen.add(marker)
        return tuple(conflicts)

    def first_available_tcp_port(
        self,
        *,
        host: str,
        start_port: int,
        end_port: int = 65535,
        reserved_ports: Sequence[int] = (),
    ) -> int:
        """Return the first known-free port; this intentionally does not probe it."""

        normalized_host = _text(host, "port host", required=True, maximum=_MAX_SHORT_TEXT).casefold()
        if normalized_host in {"localhost", "::1"}:
            normalized_host = "127.0.0.1"
        if isinstance(start_port, bool) or isinstance(end_port, bool):
            raise GuidedSoftwareProposalError("port bounds must be integers")
        try:
            low, high = int(start_port), int(end_port)
        except (TypeError, ValueError) as exc:
            raise GuidedSoftwareProposalError("port bounds must be integers") from exc
        if not 1 <= low <= high <= 65535:
            raise GuidedSoftwareProposalError("port bounds must be within 1..65535")
        if high - low + 1 > _MAX_ALLOCATION_ATTEMPTS:
            high = low + _MAX_ALLOCATION_ATTEMPTS - 1
        occupied = set()
        for bundle in self.bundles:
            for endpoint in bundle.endpoints:
                endpoint_host = endpoint.host
                if endpoint_host in {"localhost", "::1"}:
                    endpoint_host = "127.0.0.1"
                if endpoint.transport == "tcp" and endpoint_host == normalized_host and endpoint.port is not None:
                    occupied.add(endpoint.port)
        for value in reserved_ports:
            if isinstance(value, bool):
                raise GuidedSoftwareProposalError("reserved port must be an integer")
            try:
                port = int(value)
            except (TypeError, ValueError) as exc:
                raise GuidedSoftwareProposalError("reserved port must be an integer") from exc
            if not 1 <= port <= 65535:
                raise GuidedSoftwareProposalError("reserved port must be within 1..65535")
            occupied.add(port)
        for port in range(low, high + 1):
            if port not in occupied:
                return port
        raise GuidedSoftwareProposalError("no known-free TCP port is available in the bounded allocation range")


@dataclass(frozen=True)
class CompleteBundleRequest:
    """A complete already-composed new bundle awaiting pure policy checks."""

    request_key: str
    radio_key: str
    radio_role: RadioRole
    bundle: AtomicInstanceBundle

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_key", _key(self.request_key, "proposal request key"))
        object.__setattr__(self, "radio_key", _key(self.radio_key, "proposal radio key"))
        object.__setattr__(self, "radio_role", radio_role_from_persisted(self.radio_role))
        bundle = self.bundle if isinstance(self.bundle, AtomicInstanceBundle) else atomic_bundle_from_mapping(self.bundle)
        object.__setattr__(self, "bundle", bundle)


@dataclass(frozen=True)
class ExistingCandidateRequest:
    request_key: str
    radio_key: str
    radio_role: RadioRole
    candidate: DiscoveredSoftwareCandidate

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_key", _key(self.request_key, "proposal request key"))
        object.__setattr__(self, "radio_key", _key(self.radio_key, "proposal radio key"))
        object.__setattr__(self, "radio_role", radio_role_from_persisted(self.radio_role))
        candidate = self.candidate if isinstance(self.candidate, DiscoveredSoftwareCandidate) else DiscoveredSoftwareCandidate(**dict(_mapping(self.candidate, "existing candidate")))
        object.__setattr__(self, "candidate", candidate)


@dataclass(frozen=True)
class Js8DistinctProposalRequest:
    """Inputs used to build a complete new JS8 identity in one pure operation."""

    request_key: str
    radio_key: str
    radio_role: RadioRole
    variant: str
    version: str
    managed_root: str
    executable: str
    working_directory: str = ""
    launch_arguments: Tuple[str, ...] = field(default_factory=tuple)
    api_host: str = "127.0.0.1"
    base_api_port: int = 2442
    max_api_port: int = 65535
    completion_policy: CompletionPolicy = CompletionPolicy.REQUIRED
    launch_at_startup: bool = True
    monitor_health: bool = True
    readiness_policy: Mapping[str, str] = field(default_factory=lambda: {"kind": "tcp-api"})
    operator_starts: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_key", _key(self.request_key, "proposal request key"))
        object.__setattr__(self, "radio_key", _key(self.radio_key, "proposal radio key"))
        object.__setattr__(self, "radio_role", radio_role_from_persisted(self.radio_role))
        object.__setattr__(self, "variant", _text(self.variant, "JS8 variant", required=True, maximum=_MAX_SHORT_TEXT))
        object.__setattr__(self, "version", _text(self.version, "JS8 version", required=True, maximum=_MAX_SHORT_TEXT))
        object.__setattr__(self, "managed_root", _text(self.managed_root, "managed root", required=True))
        object.__setattr__(self, "executable", _text(self.executable, "JS8 executable", required=True))
        object.__setattr__(self, "working_directory", _text(self.working_directory, "JS8 working directory"))
        arguments = tuple(_text(item, "JS8 launch argument", required=True) for item in _bounded_tuple(self.launch_arguments, "JS8 launch arguments", _MAX_ATTRIBUTES))
        object.__setattr__(self, "launch_arguments", arguments)
        object.__setattr__(self, "api_host", _text(self.api_host, "JS8 API host", required=True, maximum=_MAX_SHORT_TEXT).casefold())
        for name in ("base_api_port", "max_api_port"):
            raw = getattr(self, name)
            if isinstance(raw, bool):
                raise GuidedSoftwareProposalError("JS8 API port bounds must be integers")
            try:
                port = int(raw)
            except (TypeError, ValueError) as exc:
                raise GuidedSoftwareProposalError("JS8 API port bounds must be integers") from exc
            if not 1 <= port <= 65535:
                raise GuidedSoftwareProposalError("JS8 API port bounds must be within 1..65535")
            object.__setattr__(self, name, port)
        if self.max_api_port < self.base_api_port:
            raise GuidedSoftwareProposalError("JS8 maximum API port must not precede the base API port")
        object.__setattr__(self, "completion_policy", _enum(CompletionPolicy, self.completion_policy, "completion policy"))
        object.__setattr__(self, "launch_at_startup", _bool(self.launch_at_startup))
        object.__setattr__(self, "monitor_health", _bool(self.monitor_health))
        object.__setattr__(self, "operator_starts", _bool(self.operator_starts))
        if self.operator_starts and self.launch_at_startup:
            raise GuidedSoftwareProposalError("operator-start JS8 proposal cannot launch at startup")
        readiness = _mapping(self.readiness_policy, "JS8 readiness policy")
        if len(readiness) > _MAX_ATTRIBUTES:
            raise GuidedSoftwareProposalError("JS8 readiness policy has too many values")
        normalized_readiness: dict[str, str] = {}
        for raw_key, raw_value in readiness.items():
            key = _key(raw_key, "JS8 readiness key")
            if key in normalized_readiness:
                raise GuidedSoftwareProposalError("duplicate JS8 readiness key")
            normalized_readiness[key] = _text(raw_value, "JS8 readiness value", maximum=_MAX_TEXT)
        object.__setattr__(self, "readiness_policy", MappingProxyType(normalized_readiness))


@dataclass(frozen=True)
class SoftwareProposal:
    """The planner output: one full immutable bundle plus allocation facts."""

    request_key: str
    bundle: AtomicInstanceBundle
    candidate_key: str = ""
    allocated_port: Optional[int] = None
    allocation_metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_key", _key(self.request_key, "proposal request key"))
        bundle = self.bundle if isinstance(self.bundle, AtomicInstanceBundle) else atomic_bundle_from_mapping(self.bundle)
        object.__setattr__(self, "bundle", bundle)
        object.__setattr__(self, "candidate_key", _key(self.candidate_key, "candidate key", required=False))
        if self.allocated_port is None:
            object.__setattr__(self, "allocated_port", None)
        else:
            if isinstance(self.allocated_port, bool):
                raise GuidedSoftwareProposalError("allocated port must be an integer")
            try:
                port = int(self.allocated_port)
            except (TypeError, ValueError) as exc:
                raise GuidedSoftwareProposalError("allocated port must be an integer") from exc
            if not 1 <= port <= 65535:
                raise GuidedSoftwareProposalError("allocated port must be within 1..65535")
            object.__setattr__(self, "allocated_port", port)
        raw = _mapping(self.allocation_metadata, "allocation metadata")
        if len(raw) > _MAX_ATTRIBUTES:
            raise GuidedSoftwareProposalError("allocation metadata has too many values")
        metadata: dict[str, str] = {}
        for raw_key, raw_value in raw.items():
            key = _key(raw_key, "allocation metadata key")
            if key in metadata:
                raise GuidedSoftwareProposalError("duplicate allocation metadata key")
            metadata[key] = _text(raw_value, "allocation metadata value", maximum=_MAX_TEXT)
        object.__setattr__(self, "allocation_metadata", MappingProxyType(metadata))

    @property
    def imports_existing_candidate(self) -> bool:
        return bool(self.candidate_key)


def _capability_permits(role: RadioRole, family: SoftwareFamily) -> None:
    capability = capability_for(role, family)
    if capability.allowed_state not in (AllowedState.ALLOWED, AllowedState.OPTIONAL):
        raise GuidedSoftwareProposalError(f"{family.value} is not available for {role.value}")


def _validate_role_bundle(role: RadioRole, bundle: AtomicInstanceBundle) -> None:
    """Apply planner safety checks without relying on a UI draft."""

    capability = capability_for(role, bundle.family)
    if capability.allowed_state not in (AllowedState.ALLOWED, AllowedState.OPTIONAL):
        raise GuidedSoftwareProposalError(f"{bundle.family.value} is not available for {role.value}")
    if role == RadioRole.OBSERVER:
        if bundle.execution_scope not in (
            ExecutionScope.RECEIVE_ONLY,
            ExecutionScope.STATION_SHARED_UTILITY,
            ExecutionScope.BUILT_IN,
        ):
            raise GuidedSoftwareProposalError("observer proposal must be receive-only, shared utility, or built-in")
        for component in bundle.launch_components:
            if component.execution_scope not in (
                ExecutionScope.RECEIVE_ONLY,
                ExecutionScope.STATION_SHARED_UTILITY,
            ):
                raise GuidedSoftwareProposalError("observer launch component has a non-receive-only execution scope")
        if bundle.family == SoftwareFamily.FAST_LIGHT and "flrig" in _component_keys(bundle):
            raise GuidedSoftwareProposalError("observer Fast Light proposal cannot include FLRig/CAT control")
        if capability.external_tx_disable_required and not _has_external_tx_disabled_claim(bundle):
            raise GuidedSoftwareProposalError(
                "observer proposal requires a reviewed external TX-disable resource claim"
            )
    if bundle.family in (SoftwareFamily.VARAC, SoftwareFamily.VARAC_CLUSTER):
        if role != RadioRole.TRANSCEIVER:
            raise GuidedSoftwareProposalError("VarAC proposals require a transceiver")
        if any(component.execution_scope == ExecutionScope.RECEIVE_ONLY for component in bundle.launch_components):
            raise GuidedSoftwareProposalError("VarAC proposal cannot contain a receive-only component")


def _assert_inventory_available(
    inventory: ProposalInventory,
    bundle: AtomicInstanceBundle,
    *,
    ignore_identity_fingerprint: str = "",
) -> None:
    conflicts = inventory.conflicts_for(bundle, ignore_identity_fingerprint=ignore_identity_fingerprint)
    if conflicts:
        first = conflicts[0]
        raise GuidedSoftwareProposalError(
            "%s conflict for %s already owned by %s" % (
                first.kind,
                first.value,
                first.existing_instance_key,
            )
        )


def propose_complete_bundle(request: CompleteBundleRequest, inventory: ProposalInventory) -> SoftwareProposal:
    """Validate one caller-composed non-import bundle as a unit.

    Distinct managed, manual/remote, and intentionally shared station bundles
    use the same collision and safety checks. Existing candidates use
    :func:`propose_existing_candidate` so their source fingerprint remains
    locked and cannot be partially rewritten.
    """

    request = request if isinstance(request, CompleteBundleRequest) else CompleteBundleRequest(**dict(_mapping(request, "complete bundle request")))
    inventory = inventory if isinstance(inventory, ProposalInventory) else ProposalInventory(bundles=inventory)
    bundle = request.bundle
    allowed_sources = {
        InstanceSourceMode.CREATE_DISTINCT,
        InstanceSourceMode.MANUAL_OR_REMOTE,
        InstanceSourceMode.SHARED_STATION_TOOL,
    }
    if bundle.source_mode not in allowed_sources:
        raise GuidedSoftwareProposalError("proposal bundle must be distinct, manual/remote, or station-shared")
    if bundle.source_mode == InstanceSourceMode.CREATE_DISTINCT and bundle.management_mode != ManagementMode.FIO_MANAGED:
        raise GuidedSoftwareProposalError("distinct managed proposal must use CommsPlan-managed lifecycle")
    if bundle.source_mode == InstanceSourceMode.MANUAL_OR_REMOTE and bundle.management_mode not in (
        ManagementMode.OPERATOR,
        ManagementMode.REMOTE,
    ):
        raise GuidedSoftwareProposalError("manual or remote proposal must remain operator- or remote-managed")
    if bundle.source_mode == InstanceSourceMode.SHARED_STATION_TOOL and bundle.management_mode not in (
        ManagementMode.OPERATOR,
        ManagementMode.FIO_MANAGED,
    ):
        raise GuidedSoftwareProposalError("shared station proposal must be operator- or CommsPlan-managed")
    if bundle.source_mode == InstanceSourceMode.SHARED_STATION_TOOL and bundle.owner_radio_key:
        raise GuidedSoftwareProposalError("station-shared proposal must not claim one owning radio")
    if bundle.source_mode != InstanceSourceMode.SHARED_STATION_TOOL and bundle.owner_radio_key != request.radio_key:
        raise GuidedSoftwareProposalError("new proposal bundle must belong to the requested radio")
    _validate_role_bundle(request.radio_role, bundle)
    _assert_inventory_available(inventory, bundle)
    return SoftwareProposal(request_key=request.request_key, bundle=bundle)


def propose_existing_candidate(request: ExistingCandidateRequest, inventory: ProposalInventory) -> SoftwareProposal:
    """Select exactly one candidate bundle without renumbering or rewriting it."""

    request = request if isinstance(request, ExistingCandidateRequest) else ExistingCandidateRequest(**dict(_mapping(request, "existing candidate request")))
    inventory = inventory if isinstance(inventory, ProposalInventory) else ProposalInventory(bundles=inventory)
    candidate = request.candidate
    if not candidate.importable:
        raise GuidedSoftwareProposalError("only a complete discovery candidate can be imported")
    bundle = candidate.bundle
    if bundle.owner_radio_key and bundle.owner_radio_key != request.radio_key:
        raise GuidedSoftwareProposalError("existing candidate belongs to a different radio and cannot be partially reassigned")
    _validate_role_bundle(request.radio_role, bundle)
    _assert_inventory_available(
        inventory,
        bundle,
        ignore_identity_fingerprint=bundle.computed_identity_fingerprint,
    )
    return SoftwareProposal(
        request_key=request.request_key,
        bundle=bundle,
        candidate_key=candidate.candidate_key,
        allocation_metadata={"selection": "existing-complete-bundle"},
    )


def _allocate_instance_key(inventory: ProposalInventory, family: SoftwareFamily, radio_key: str, variant: str) -> str:
    base = _key("%s-%s-%s" % (family.value, radio_key, variant or "instance"), "new instance key")
    occupied = set(inventory.instance_keys)
    for ordinal in range(1, _MAX_ALLOCATION_ATTEMPTS + 1):
        value = base if ordinal == 1 else "%s-%d" % (base, ordinal)
        if value not in occupied:
            return value
    raise GuidedSoftwareProposalError("no bounded distinct instance key is available")


def propose_distinct_js8(request: Js8DistinctProposalRequest, inventory: ProposalInventory) -> SoftwareProposal:
    """Build a full new JS8 family bundle with one collision-free identity."""

    request = request if isinstance(request, Js8DistinctProposalRequest) else Js8DistinctProposalRequest(**dict(_mapping(request, "JS8 proposal request")))
    inventory = inventory if isinstance(inventory, ProposalInventory) else ProposalInventory(bundles=inventory)
    _capability_permits(request.radio_role, SoftwareFamily.JS8CALL)
    instance_key = _allocate_instance_key(inventory, SoftwareFamily.JS8CALL, request.radio_key, request.variant)
    api_port = inventory.first_available_tcp_port(
        host=request.api_host,
        start_port=request.base_api_port,
        end_port=request.max_api_port,
    )
    instance_root = _append_path(request.managed_root, instance_key)
    profile_root = _append_path(instance_root, "profile")
    data_root = _append_path(instance_root, "data")
    directed_path = _append_path(data_root, "DIRECTED.TXT")
    all_path = _append_path(data_root, "ALL.TXT")
    inbox_path = _append_path(data_root, "inbox.db3")
    forms_root = _append_path(instance_root, "forms")
    scope = ExecutionScope.RECEIVE_ONLY if request.radio_role == RadioRole.OBSERVER else ExecutionScope.STANDARD
    resources = (
        ResourceClaimRecord("native-profile", "path", profile_root),
        ResourceClaimRecord("application-data", "path", data_root),
        ResourceClaimRecord("directed-txt", "path", directed_path),
        ResourceClaimRecord("all-txt", "path", all_path),
        ResourceClaimRecord("message-inbox", "path", inbox_path),
        ResourceClaimRecord("forms-root", "path", forms_root),
        ResourceClaimRecord("rig-name", "rig-name", instance_key),
        ResourceClaimRecord("launch-identity", "launch-identity", "%s|%s" % (request.executable, profile_root)),
    )
    if request.radio_role == RadioRole.OBSERVER:
        resources += (
            ResourceClaimRecord("external-tx-disabled", "capability", "true", exclusive=False),
        )
    component = LaunchComponentRecord(
        component_key="js8call",
        executable=request.executable,
        arguments=request.launch_arguments,
        working_directory=request.working_directory,
        profile_selector=profile_root,
        execution_scope=scope,
        operator_starts=request.operator_starts,
        launch_at_startup=request.launch_at_startup,
        monitor_health=request.monitor_health,
        readiness_policy=request.readiness_policy,
    )
    bundle = AtomicInstanceBundle(
        family=SoftwareFamily.JS8CALL,
        instance_key=instance_key,
        source_mode=InstanceSourceMode.CREATE_DISTINCT,
        completion_policy=request.completion_policy,
        owner_radio_key=request.radio_key,
        management_mode=ManagementMode.FIO_MANAGED,
        variant=request.variant,
        version=request.version,
        configuration_path=profile_root,
        data_path=data_root,
        message_paths=(directed_path, all_path, inbox_path, forms_root),
        execution_scope=scope,
        endpoints=(EndpointRecord("js8-api", "tcp", request.api_host, api_port),),
        resources=resources,
        launch_components=(component,),
        provenance="guided-distinct-js8-proposal",
    )
    plan = propose_complete_bundle(
        CompleteBundleRequest(request.request_key, request.radio_key, request.radio_role, bundle),
        inventory,
    )
    return SoftwareProposal(
        request_key=plan.request_key,
        bundle=plan.bundle,
        allocated_port=api_port,
        allocation_metadata={
            "allocation": "distinct-js8-family-bundle",
            "profile-root": profile_root,
            "data-root": data_root,
            "directed-path": directed_path,
        },
    )


def propose_receiver_bundle(request: CompleteBundleRequest, inventory: ProposalInventory) -> SoftwareProposal:
    """Validate a complete SDR++ receiver proposal as receive-only."""

    request = request if isinstance(request, CompleteBundleRequest) else CompleteBundleRequest(**dict(_mapping(request, "receiver proposal request")))
    if request.bundle.family != SoftwareFamily.SDRPP:
        raise GuidedSoftwareProposalError("receiver proposal requires the SDR++ family")
    if request.bundle.execution_scope != ExecutionScope.RECEIVE_ONLY:
        raise GuidedSoftwareProposalError("receiver proposal must use receive-only execution scope")
    if "sdrpp" not in _component_keys(request.bundle):
        raise GuidedSoftwareProposalError("receiver proposal requires an SDR++ launch component")
    return propose_complete_bundle(request, inventory)


def propose_fast_light_bundle(request: CompleteBundleRequest, inventory: ProposalInventory) -> SoftwareProposal:
    """Validate an atomic Fast Light component set without inventing commands."""

    request = request if isinstance(request, CompleteBundleRequest) else CompleteBundleRequest(**dict(_mapping(request, "Fast Light proposal request")))
    bundle = request.bundle
    if bundle.family != SoftwareFamily.FAST_LIGHT:
        raise GuidedSoftwareProposalError("Fast Light proposal requires the fast_light family")
    component_keys = set(_component_keys(bundle))
    allowed = {"flrig", "fldigi", "flmsg", "flamp"}
    if not component_keys or not component_keys.issubset(allowed):
        raise GuidedSoftwareProposalError("Fast Light proposal requires only reviewed Fast Light components")
    if bundle.source_mode == InstanceSourceMode.SHARED_STATION_TOOL:
        if not component_keys.issubset({"flmsg", "flamp"}):
            raise GuidedSoftwareProposalError("station-shared Fast Light proposal may contain only FLMsg/FLAmp")
        return propose_complete_bundle(request, inventory)
    if "fldigi" not in component_keys:
        raise GuidedSoftwareProposalError("radio-scoped Fast Light proposal requires FLDigi")
    if request.radio_role == RadioRole.OBSERVER and "flrig" in component_keys:
        raise GuidedSoftwareProposalError("observer Fast Light proposal cannot include FLRig")
    if request.radio_role == RadioRole.TRANSCEIVER and not {"flrig", "fldigi"}.issubset(component_keys):
        raise GuidedSoftwareProposalError("transceiver Fast Light proposal requires explicit FLRig and FLDigi components")
    return propose_complete_bundle(request, inventory)


def propose_varac_bundle(request: CompleteBundleRequest, inventory: ProposalInventory) -> SoftwareProposal:
    """Validate an atomic standalone or cluster VarAC node proposal."""

    request = request if isinstance(request, CompleteBundleRequest) else CompleteBundleRequest(**dict(_mapping(request, "VarAC proposal request")))
    bundle = request.bundle
    if bundle.family not in (SoftwareFamily.VARAC, SoftwareFamily.VARAC_CLUSTER):
        raise GuidedSoftwareProposalError("VarAC proposal requires varac or varac_cluster family")
    if request.radio_role != RadioRole.TRANSCEIVER:
        raise GuidedSoftwareProposalError("VarAC proposal requires a transceiver")
    if "varac" not in _component_keys(bundle):
        raise GuidedSoftwareProposalError("VarAC proposal requires a VarAC launch component")
    if bundle.family == SoftwareFamily.VARAC_CLUSTER:
        resource_keys = set(_resource_keys(bundle))
        if not {"cluster-id", "cluster-member-number"}.issubset(resource_keys):
            raise GuidedSoftwareProposalError("VarAC cluster proposal requires cluster ID and member-number resource claims")
    return propose_complete_bundle(request, inventory)


__all__ = (
    "CandidateState",
    "CompleteBundleRequest",
    "DiscoveryEvidence",
    "DiscoverySnapshot",
    "DiscoveredSoftwareCandidate",
    "ExistingCandidateRequest",
    "GuidedSoftwareProposalError",
    "Js8DistinctProposalRequest",
    "ProposalConflict",
    "ProposalInventory",
    "SoftwareProposal",
    "propose_complete_bundle",
    "propose_distinct_js8",
    "propose_existing_candidate",
    "propose_fast_light_bundle",
    "propose_receiver_bundle",
    "propose_varac_bundle",
)
