"""Pure authority helpers for guided software-instance drafts.

This module deliberately performs no discovery, filesystem access, socket
work, or persistence.  UI hosts provide already-loaded application rows and
retained drafts.  The helpers turn those values into one immutable inventory
fingerprint, stable draft identities, and source-lock evidence that can be
checked again immediately before an atomic save.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping


_KEY_RE = re.compile(r"[^a-z0-9_.-]+")
_IDENTITY_FIELDS = {
    "js8call": (
        "system_key", "host", "port", "udp_port", "rig_name",
        "application_path", "configuration_path", "storage_path",
        "launch_command", "settings_identity_mode",
    ),
    "fast_light": (
        "system_key", "host", "port", "secondary_port", "arq_port",
        "application_path", "secondary_application_path",
        "configuration_path", "secondary_configuration_path",
        "storage_path", "secondary_storage_path", "launch_command",
        "settings_identity_mode",
    ),
    "varac": (
        "system_key", "application_path", "configuration_path",
        "storage_path", "secondary_storage_path", "outbox_path",
        "working_directory", "cluster_path", "cluster_id",
        "cluster_instance_number", "launch_command",
    ),
}

_CLASS_USABLE = "usable_existing"
_CLASS_RECOVERY = "recovery_only"
_CLASS_DIAGNOSTIC = "diagnostic_only"
_CLASS_RETAINED = "retained_draft"
_LINKED_ID_KEYS = (
    "radio_id",
    "device_profile_id",
    "owner_radio_id",
    "assigned_radio_id",
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _key(value: object) -> str:
    return _KEY_RE.sub("-", _text(value).casefold()).strip("-._")


def _family(value: object) -> str:
    family = _key(value)
    if family not in _IDENTITY_FIELDS:
        raise ValueError(f"Unsupported guided software family: {value}")
    return family


def _positive_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _linked_to_radio(row: Mapping[str, Any], linked_ids: frozenset[int] | None) -> bool:
    """Return durable linkage evidence, never inferred from enabled/path fields."""

    row_id = _positive_int(row.get("id"))
    if linked_ids is not None:
        return bool(row_id and row_id in linked_ids)
    return any(_positive_int(row.get(key)) > 0 for key in _LINKED_ID_KEYS)


def _has_source_evidence(row: Mapping[str, Any]) -> bool:
    """Return explicit persisted provenance for an unlinked bundle.

    A generated fingerprint is not evidence by itself.  The caller must pass
    the already-loaded manifest/provenance marker (or an observed native
    fingerprint); this keeps an empty-manifest inventory fail-closed.
    """

    return bool(
        row.get("manifest_present")
        or row.get("manifest")
        or _text(row.get("manifest_id"))
        or _text(row.get("observed_fingerprint"))
    )


def _complete_identity(family: str, row: Mapping[str, Any], normalized: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Classify completeness without filesystem, process, or endpoint I/O."""

    reasons: list[str] = []
    if not (_text(row.get("system_key")) or _text(row.get("instance_key")) or _text(row.get("draft_instance_key"))):
        reasons.append("missing stable instance identity")
    if family == "js8call":
        required = [
            ("application_path", "missing application path"),
            ("configuration_path", "missing profile/configuration root"),
            ("storage_path", "missing application-data root"),
        ]
        if _settings_identity_mode(family, normalized) != "default":
            required.append(("rig_name", "missing rig/profile selector"))
        for key, reason in required:
            if not _text(normalized.get(key)):
                reasons.append(reason)
        if _positive_int(normalized.get("port")) <= 0:
            reasons.append("missing TCP API port")
    elif family == "fast_light":
        required = (
            ("application_path", "missing FLRig application path"),
            ("secondary_application_path", "missing FLDigi application path"),
        )
        for key, reason in required:
            if not _text(normalized.get(key)):
                reasons.append(reason)
        if _positive_int(normalized.get("port")) <= 0:
            reasons.append("missing FLRig endpoint")
        if _positive_int(normalized.get("secondary_port")) <= 0:
            reasons.append("missing FLDigi endpoint")
    elif family == "varac":
        required = (
            ("application_path", "missing VarAC application/launcher path"),
            ("configuration_path", "missing VarAC INI path"),
            ("storage_path", "missing VarAC database path"),
            ("secondary_storage_path", "missing VarAC incoming path"),
        )
        for key, reason in required:
            if not _text(normalized.get(key)):
                reasons.append(reason)
    return (not reasons, tuple(reasons))


def _normalise_resource_value(kind: str, value: object) -> str:
    text = _text(value)
    if kind in {"tcp", "udp"}:
        return text.casefold()
    if kind == "path":
        # Do not resolve or touch the filesystem.  This is only a stable
        # collision key for the already-loaded configuration snapshot.
        return re.sub(r"/+", "/", text).rstrip("/").casefold()
    return text.casefold()


def _resource_claims(family: str, normalized: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Build deduplicatable resource claims from one normalized row."""

    host = _text(normalized.get("host")) or "127.0.0.1"
    claims: set[tuple[str, str]] = set()

    def add_endpoint(kind: str, port: object) -> None:
        parsed = _positive_int(port)
        if parsed:
            claims.add((kind, _normalise_resource_value(kind, f"{host}:{parsed}")))

    def add_path(value: object) -> None:
        if _text(value):
            claims.add(("path", _normalise_resource_value("path", value)))

    if _settings_identity_mode(family, normalized) == "default":
        claims.add(("native_settings", f"{family}:default"))

    if family == "js8call":
        add_endpoint("tcp", normalized.get("port"))
        add_endpoint("udp", normalized.get("udp_port"))
        add_path(normalized.get("configuration_path"))
        add_path(normalized.get("storage_path"))
    elif family == "fast_light":
        add_endpoint("tcp", normalized.get("port"))
        add_endpoint("tcp", normalized.get("secondary_port"))
        add_endpoint("tcp", normalized.get("arq_port"))
        add_path(normalized.get("configuration_path"))
        add_path(normalized.get("secondary_configuration_path"))
        add_path(normalized.get("storage_path"))
        add_path(normalized.get("secondary_storage_path"))
    elif family == "varac":
        add_path(normalized.get("configuration_path"))
        add_path(normalized.get("storage_path"))
        add_path(normalized.get("secondary_storage_path"))
        add_path(normalized.get("outbox_path"))
    return tuple(sorted(claims))


def _settings_identity_mode(family: str, normalized: Mapping[str, Any]) -> str:
    explicit = _text(normalized.get("settings_identity_mode")).casefold()
    if explicit in {"default", "isolated"}:
        return explicit
    system_key = _text(normalized.get("system_key")).casefold()
    if family == "js8call" and system_key == "default_js8_instance":
        return "default"
    if family == "fast_light" and system_key == "default_fast_light":
        return "default"
    return "isolated"


def stable_draft_instance_key(owner_draft_key: object, family_key: object) -> str:
    """Return an opaque identity unaffected by editable names or navigation."""

    owner = _text(owner_draft_key)
    family = _family(family_key)
    if not owner:
        raise ValueError("owner_draft_key is required for an unsaved instance")
    digest = hashlib.sha256(f"{owner}\0{family}".encode("utf-8")).hexdigest()[:20]
    return f"draft-{family}-{digest}"


def stable_application_system_key(owner_draft_key: object, family_key: object) -> str:
    """Allocate the durable application key paired with one guided draft.

    The key is stable across assistant navigation and Save, but remains
    separate from the transaction-only draft key and from native application
    names or filesystem paths.
    """

    family = _family(family_key)
    digest = stable_draft_instance_key(owner_draft_key, family).rsplit("-", 1)[-1]
    return f"{family}-instance-{digest}"


def _normalized_identity(family_key: object, row: Mapping[str, Any]) -> dict[str, Any]:
    family = _family(family_key)
    aliases = {
        "host": ("host", "flrig_host", "fldigi_host"),
        "application_path": ("application_path", "install_path", "path", "flrig_path"),
        "secondary_application_path": ("secondary_application_path", "fldigi_path"),
        "configuration_path": ("configuration_path", "profile_path", "ini_path"),
        "secondary_configuration_path": (
            "secondary_configuration_path",
            "fldigi_config_path",
        ),
        "storage_path": ("storage_path", "application_data_root", "db_path", "fldigi_log_path"),
        "secondary_storage_path": ("secondary_storage_path", "incoming_path", "fldigi_checkin_dir"),
        "outbox_path": ("outbox_path", "outbox_dir"),
        "port": ("port", "js8_tcp_port", "flrig_port"),
        "secondary_port": ("secondary_port", "fldigi_port"),
        "arq_port": ("arq_port", "fldigi_arq_port"),
        "udp_port": ("udp_port", "js8_udp_port"),
        "rig_name": ("rig_name", "js8_rig_name"),
        "launch_command": ("launch_command", "launch_cmd"),
        "settings_identity_mode": (
            "settings_identity_mode",
            "native_settings_identity",
            "profile_identity_mode",
        ),
    }
    result: dict[str, Any] = {"family_key": family}
    for name in _IDENTITY_FIELDS[family]:
        candidates = aliases.get(name, (name,))
        value: Any = ""
        for candidate in candidates:
            if row.get(candidate) not in (None, ""):
                value = row.get(candidate)
                break
        if name in {"port", "udp_port", "secondary_port", "arq_port", "cluster_instance_number"}:
            try:
                value = int(value or 0)
            except (TypeError, ValueError):
                value = 0
        elif isinstance(value, str):
            value = value.strip()
        result[name] = value
    return result


def source_identity_fingerprint(family_key: object, row: Mapping[str, Any]) -> str:
    """Fingerprint the complete imported identity that must remain locked."""

    encoded = json.dumps(
        _normalized_identity(family_key, row),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_row(
    family: str,
    row: Mapping[str, Any],
    *,
    linked_ids: frozenset[int] | None = None,
) -> dict[str, Any]:
    normalized = _normalized_identity(family, row)
    complete, completeness_reasons = _complete_identity(family, row, normalized)
    linked = _linked_to_radio(row, linked_ids)
    retained = bool(_text(row.get("owner_draft_key")) or _text(row.get("draft_instance_key")))
    source_evidenced = _has_source_evidence(row)
    if retained:
        classification = _CLASS_RETAINED
    elif not complete:
        classification = _CLASS_DIAGNOSTIC
    elif linked:
        classification = _CLASS_USABLE
    elif source_evidenced:
        classification = _CLASS_RECOVERY
    else:
        classification = _CLASS_DIAGNOSTIC
        completeness_reasons = (*completeness_reasons, "missing durable ownership/source evidence")
    claims = _resource_claims(family, normalized)
    normalized.update(
        {
            "id": int(row.get("id") or 0),
            "name": _text(row.get("name") or row.get("instance_name")),
            "instance_key": _text(row.get("instance_key") or row.get("draft_instance_key")),
            "owner_draft_key": _text(row.get("owner_draft_key")),
            "source_fingerprint": source_identity_fingerprint(family, row),
            "candidate_classification": classification,
            # ``candidate_usable`` is the short public predicate consumed by
            # the assistant; retain the more explicit ``usable_existing``
            # name for callers that need to distinguish imported candidates.
            "candidate_usable": classification == _CLASS_USABLE,
            "usable_existing": classification == _CLASS_USABLE,
            "recovery_only": classification == _CLASS_RECOVERY,
            "diagnostic_only": classification == _CLASS_DIAGNOSTIC,
            "retained_draft": classification == _CLASS_RETAINED,
            "linked_to_radio": linked,
            "completeness_reasons": completeness_reasons,
            "candidate_reasons": completeness_reasons,
            "provenance_unverified": not source_evidenced,
            "resource_claims": claims,
        }
    )
    return normalized


@dataclass(frozen=True)
class GuidedInstanceInventorySnapshot:
    """One immutable saved-plus-retained inventory generation."""

    generation: int
    rows_by_family: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    linked_ids_by_family: Mapping[str, Iterable[int]] = field(default_factory=dict)
    fingerprint: str = field(init=False)
    _usable_rows_by_family: Mapping[str, tuple[Mapping[str, Any], ...]] = field(init=False, repr=False)
    _diagnostic_rows_by_family: Mapping[str, tuple[Mapping[str, Any], ...]] = field(init=False, repr=False)
    _recovery_rows_by_family: Mapping[str, tuple[Mapping[str, Any], ...]] = field(init=False, repr=False)
    _resource_claims_by_family: Mapping[str, tuple[tuple[str, str], ...]] = field(init=False, repr=False)
    _duplicate_claims_by_family: Mapping[str, tuple[tuple[str, str, int], ...]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        generation = int(self.generation)
        if generation < 0:
            raise ValueError("inventory generation must be non-negative")
        raw_linked_ids = dict(self.linked_ids_by_family or {})
        linked_ids_normalized: dict[str, frozenset[int]] = {}
        for raw_family, ids in raw_linked_ids.items():
            family = _family(raw_family)
            linked_ids_normalized[family] = frozenset(
                _positive_int(value) for value in (ids or ()) if _positive_int(value)
            )
        normalized: dict[str, tuple[Mapping[str, Any], ...]] = {}
        usable: dict[str, tuple[Mapping[str, Any], ...]] = {}
        diagnostic: dict[str, tuple[Mapping[str, Any], ...]] = {}
        recovery: dict[str, tuple[Mapping[str, Any], ...]] = {}
        resource_claims: dict[str, tuple[tuple[str, str], ...]] = {}
        duplicate_claims: dict[str, tuple[tuple[str, str, int], ...]] = {}
        for raw_family, raw_rows in dict(self.rows_by_family).items():
            family = _family(raw_family)
            rows = tuple(
                MappingProxyType(
                    _snapshot_row(
                        family,
                        row,
                        linked_ids=linked_ids_normalized.get(family),
                    )
                )
                for row in raw_rows
                if isinstance(row, Mapping)
            )
            sorted_rows = tuple(
                sorted(
                    rows,
                    key=lambda item: (
                        _text(item.get("instance_key")),
                        _text(item.get("system_key")),
                        int(item.get("id") or 0),
                        _text(item.get("source_fingerprint")),
                    ),
                )
            )
            normalized[family] = sorted_rows
            usable[family] = tuple(row for row in sorted_rows if bool(row.get("usable_existing")))
            diagnostic[family] = tuple(row for row in sorted_rows if bool(row.get("diagnostic_only")))
            recovery[family] = tuple(row for row in sorted_rows if bool(row.get("recovery_only")))
            counts: dict[tuple[str, str], int] = {}
            for row in sorted_rows:
                for claim in tuple(row.get("resource_claims") or ()):
                    if isinstance(claim, (tuple, list)) and len(claim) == 2:
                        key = (_text(claim[0]).casefold(), _text(claim[1]).casefold())
                        counts[key] = counts.get(key, 0) + 1
            resource_claims[family] = tuple(sorted(counts))
            duplicate_claims[family] = tuple(
                (kind, value, count)
                for (kind, value), count in sorted(counts.items())
                if count > 1
            )
        serializable = {
            family: [dict(row) for row in rows]
            for family, rows in sorted(normalized.items())
        }
        digest = hashlib.sha256(
            json.dumps(serializable, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "rows_by_family", MappingProxyType(normalized))
        object.__setattr__(self, "linked_ids_by_family", MappingProxyType(linked_ids_normalized))
        object.__setattr__(self, "_usable_rows_by_family", MappingProxyType(usable))
        object.__setattr__(self, "_diagnostic_rows_by_family", MappingProxyType(diagnostic))
        object.__setattr__(self, "_recovery_rows_by_family", MappingProxyType(recovery))
        object.__setattr__(self, "_resource_claims_by_family", MappingProxyType(resource_claims))
        object.__setattr__(self, "_duplicate_claims_by_family", MappingProxyType(duplicate_claims))
        object.__setattr__(self, "fingerprint", digest)

    def rows_for(self, family_key: object) -> tuple[Mapping[str, Any], ...]:
        return self.rows_by_family.get(_family(family_key), ())

    def usable_rows_for(self, family_key: object) -> tuple[Mapping[str, Any], ...]:
        """Return only complete, durably linked existing candidates."""

        return self._usable_rows_by_family.get(_family(family_key), ())

    def diagnostic_rows_for(self, family_key: object) -> tuple[Mapping[str, Any], ...]:
        """Return incomplete/unlinked rows for diagnostics and recovery UI only."""

        return self._diagnostic_rows_by_family.get(_family(family_key), ())

    def recovery_rows_for(self, family_key: object) -> tuple[Mapping[str, Any], ...]:
        """Return complete but unassigned bundles for an explicit recovery picker."""

        return self._recovery_rows_by_family.get(_family(family_key), ())

    def resource_claims_for(self, family_key: object | None = None) -> tuple[tuple[str, str], ...]:
        """Return deduplicated conservative claims from the immutable snapshot."""

        if family_key is not None:
            return self._resource_claims_by_family.get(_family(family_key), ())
        return tuple(
            sorted(
                {
                    claim
                    for claims in self._resource_claims_by_family.values()
                    for claim in claims
                }
            )
        )

    def duplicate_resource_claims_for(
        self,
        family_key: object | None = None,
    ) -> tuple[tuple[str, str, int], ...]:
        """Return duplicate claims without expanding them into repeated probes."""

        if family_key is not None:
            return self._duplicate_claims_by_family.get(_family(family_key), ())
        combined: dict[tuple[str, str], int] = {}
        for duplicates in self._duplicate_claims_by_family.values():
            for kind, value, count in duplicates:
                combined[(kind, value)] = combined.get((kind, value), 0) + count
        return tuple((kind, value, count) for (kind, value), count in sorted(combined.items()))

    def source_is_current(self, family_key: object, source_id: object, fingerprint: object) -> bool:
        try:
            wanted_id = int(source_id or 0)
        except (TypeError, ValueError):
            return False
        wanted = _text(fingerprint)
        return bool(
            wanted
            and any(
                int(row.get("id") or 0) == wanted_id
                and _text(row.get("source_fingerprint")) == wanted
                for row in self.rows_for(family_key)
            )
        )


def build_guided_instance_inventory(
    saved_by_family: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    retained_drafts: Mapping[str, Mapping[str, Any]] | None = None,
    linked_ids_by_family: Mapping[str, Iterable[int]] | None = None,
    generation: int = 0,
) -> GuidedInstanceInventorySnapshot:
    """Combine saved rows and unsaved reviewed drafts into one collision view."""

    combined: dict[str, list[Mapping[str, Any]]] = {}
    for family, rows in saved_by_family.items():
        normalized_family = _family(family)
        combined[normalized_family] = [row for row in rows if isinstance(row, Mapping)]
    for family, draft in dict(retained_drafts or {}).items():
        if not isinstance(draft, Mapping):
            continue
        normalized_family = _family(family)
        combined.setdefault(normalized_family, []).append(dict(draft))
    return GuidedInstanceInventorySnapshot(
        generation=generation,
        rows_by_family=combined,
        linked_ids_by_family=dict(linked_ids_by_family or {}),
    )


def first_available_port(
    snapshot: GuidedInstanceInventorySnapshot,
    family_key: object,
    *,
    start_port: int,
    field_name: str = "port",
) -> int:
    claim_kind = "udp" if str(field_name or "").strip().lower() == "udp_port" else "tcp"
    occupied = {
        int(value.rsplit(":", 1)[-1])
        for kind, value in snapshot.resource_claims_for()
        if kind == claim_kind and value.rsplit(":", 1)[-1].isdigit()
    }
    for port in range(int(start_port), 65536):
        if port not in occupied:
            return port
    raise ValueError(f"No available port remains for {_family(family_key)}")


def distinct_draft_seed(
    family_key: object,
    *,
    owner_draft_key: object,
    snapshot: GuidedInstanceInventorySnapshot,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a safe new-instance seed that never reuses source identity."""

    family = _family(family_key)
    evidence = dict(source or {})
    draft_instance_key = stable_draft_instance_key(owner_draft_key, family)
    seed: dict[str, Any] = {
        "family_key": family,
        "mode": "managed",
        "ownership": "fio-managed",
        "owner_draft_key": _text(owner_draft_key),
        "draft_instance_key": draft_instance_key,
        # Allocated once with the draft but durable across review and save.
        # Unlike ``draft_instance_key`` this is the application/manifest/
        # canonical foreign identity and is never used in a native path.
        "application_system_key": stable_application_system_key(owner_draft_key, family),
        "inventory_fingerprint": snapshot.fingerprint,
        "source_fingerprint": "",
        "source_locked": False,
        "imported_id": None,
        "imported_system_key": "",
        # Executables and verified version evidence may be shared safely.
        "application_path": _text(evidence.get("application_path") or evidence.get("install_path") or evidence.get("path") or evidence.get("flrig_path")),
        "secondary_application_path": _text(evidence.get("secondary_application_path") or evidence.get("fldigi_path")),
        "flmsg_application_path": _text(evidence.get("flmsg_application_path") or evidence.get("flmsg_path")),
        "flamp_application_path": _text(evidence.get("flamp_application_path") or evidence.get("flamp_path")),
        "variant": _text(evidence.get("variant") or evidence.get("variant_family")),
        "version": _text(evidence.get("version") or evidence.get("variant_version")),
        "host": _text(evidence.get("host")) or "127.0.0.1",
        "configuration_path": "",
        "secondary_configuration_path": "",
        "storage_path": "",
        "secondary_storage_path": "",
        "outbox_path": "",
        "working_directory": "",
        "launch_command": "",
        "rig_name": "",
        "cluster_path": "standalone",
        "cluster_id": "",
        "cluster_instance_number": 0,
    }
    if family == "js8call":
        seed["port"] = first_available_port(snapshot, family, start_port=2442)
        seed["udp_port"] = first_available_port(snapshot, family, start_port=2237, field_name="udp_port")
    elif family == "fast_light":
        seed["port"] = first_available_port(snapshot, family, start_port=12345)
        seed["secondary_port"] = first_available_port(snapshot, family, start_port=7362, field_name="secondary_port")
        seed["arq_port"] = first_available_port(snapshot, family, start_port=7322, field_name="arq_port")
    else:
        seed["port"] = 0
    return seed


__all__ = [
    "GuidedInstanceInventorySnapshot",
    "build_guided_instance_inventory",
    "distinct_draft_seed",
    "first_available_port",
    "source_identity_fingerprint",
    "stable_application_system_key",
    "stable_draft_instance_key",
]
