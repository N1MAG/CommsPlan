"""Pure radio-hardware identity and matching primitives.

This module deliberately has no operating-system, database, Qt, or radio-control
side effects.  Platform inventory providers may feed it evidence, while the
settings store owns persistence and transactions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


HARDWARE_BINDING_SCHEMA_VERSION = 1
HARDWARE_ROLES = frozenset({"cat", "ptt", "audio_rx", "audio_tx"})
HARDWARE_MATCH_STATES = frozenset(
    {"exact", "port_bound", "unstable_confirmed", "missing", "ambiguous", "changed", "conflict", "unsupported"}
)
HARDWARE_STABILITY_CLASSES = frozenset({"stable", "port_bound", "unstable_confirmed"})

_SECRET_FIELD_FRAGMENTS = ("password", "passcode", "pairing_code", "credential", "secret", "token")
_IDENTITY_FIELDS = (
    "endpoint_uid",
    "device_instance_id",
    "container_id",
    "hardware_serial",
    "by_id",
    "physical_path",
    "unstable_locator",
)
_QUALIFIER_FIELDS = ("vendor_id", "product_id", "interface_number", "function", "direction")
_DISPLAY_FIELDS = frozenset(
    {
        "display_name",
        "description",
        "current_locator",
        "driver",
        "manufacturer",
        "model",
        "product_name",
    }
)


class HardwareBindingError(ValueError):
    """Raised when a proposed hardware binding is incomplete or unsafe."""


@dataclass(frozen=True)
class HardwareMatchResult:
    state: str
    matches: tuple[dict[str, Any], ...] = ()
    reason: str = ""


def _clean_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value).strip()


def _clean_json(value: Any, *, field_path: str = "") -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            if not key:
                continue
            lowered = key.lower()
            if any(fragment in lowered for fragment in _SECRET_FIELD_FRAGMENTS):
                raise HardwareBindingError(f"Hardware identity must not contain secret field: {field_path}{key}")
            clean_value = _clean_json(raw_value, field_path=f"{field_path}{key}.")
            if clean_value not in (None, "", [], {}):
                cleaned[key] = clean_value
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_clean_json(item, field_path=field_path) for item in value]
    return _clean_scalar(value)


def normalize_hardware_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role not in HARDWARE_ROLES:
        raise HardwareBindingError(f"Unsupported hardware role: {role or 'blank'}")
    return role


def normalize_hardware_selector(
    value: Mapping[str, Any],
    *,
    platform: str,
    stability_class: str | None = None,
) -> dict[str, Any]:
    """Return a stable, serializable selector without observational display data."""

    if not isinstance(value, Mapping):
        raise HardwareBindingError("Hardware selector must be an object.")
    selector = dict(_clean_json(value))
    selector["schema_version"] = int(selector.get("schema_version", HARDWARE_BINDING_SCHEMA_VERSION) or 0)
    if selector["schema_version"] != HARDWARE_BINDING_SCHEMA_VERSION:
        raise HardwareBindingError("Unsupported hardware selector schema version.")

    normalized_platform = str(platform or "").strip().lower()
    if not normalized_platform:
        raise HardwareBindingError("Hardware selector platform is required.")
    selector["platform"] = normalized_platform

    identity_field = next((field for field in _IDENTITY_FIELDS if str(selector.get(field, "") or "").strip()), "")
    if not identity_field:
        raise HardwareBindingError(
            "Hardware identity requires a stable endpoint, device instance, serial, by-id path, physical path, or confirmed locator."
        )

    stability = str(stability_class or selector.get("stability_class", "") or "").strip().lower()
    if not stability:
        if identity_field == "physical_path":
            stability = "port_bound"
        elif identity_field == "unstable_locator":
            stability = "unstable_confirmed"
        else:
            stability = "stable"
    if stability not in HARDWARE_STABILITY_CLASSES:
        raise HardwareBindingError(f"Unsupported hardware stability class: {stability}")
    if stability == "unstable_confirmed" and not bool(selector.get("operator_confirmed", False)):
        raise HardwareBindingError("An unstable locator requires explicit operator confirmation.")
    selector["stability_class"] = stability

    if normalized_platform == "windows":
        for field in ("endpoint_uid", "device_instance_id", "container_id"):
            if field in selector:
                selector[field] = str(selector[field]).casefold()
    return selector


def hardware_selector_fingerprint(selector: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(selector), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hardware_exclusive_claim_key(
    *,
    platform: str,
    provider: str,
    transport: str,
    selector: Mapping[str, Any],
) -> str:
    """Build a role-independent claim for one physical interface/endpoint."""

    identity: dict[str, Any] = {}
    anchor = next((field for field in _IDENTITY_FIELDS if selector.get(field) not in (None, "")), "")
    fields = ((anchor,) if anchor else ()) + _QUALIFIER_FIELDS
    for field in fields:
        value = selector.get(field)
        if value not in (None, ""):
            identity[field] = value
    if not identity:
        raise HardwareBindingError("Cannot claim hardware without stable identity evidence.")
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:32]
    return ":".join(
        (
            str(platform or "").strip().lower(),
            str(provider or "").strip().lower(),
            str(transport or "").strip().lower(),
            digest,
        )
    )


def normalize_hardware_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    role = normalize_hardware_role(value.get("role"))
    platform = str(value.get("platform", "") or "").strip().lower()
    provider = str(value.get("provider", "") or "").strip().lower()
    transport = str(value.get("transport", "") or "").strip().lower()
    if not platform or not provider or not transport:
        raise HardwareBindingError("Hardware platform, provider, and transport are required.")
    selector = normalize_hardware_selector(
        value.get("selector", {}),
        platform=platform,
        stability_class=str(value.get("stability_class", "") or "") or None,
    )
    locator = _clean_json(value.get("observed_locator", {}))
    if not isinstance(locator, dict):
        raise HardwareBindingError("Observed hardware locator must be an object.")
    canonical_claim_key = hardware_exclusive_claim_key(
        platform=platform,
        provider=provider,
        transport=transport,
        selector=selector,
    )
    supplied_claim_key = str(value.get("claim_key", "") or "").strip().lower()
    if supplied_claim_key and supplied_claim_key != canonical_claim_key:
        raise HardwareBindingError("Hardware ownership claim does not match its normalized identity.")
    return {
        "role": role,
        "platform": platform,
        "provider": provider,
        "transport": transport,
        "selector": selector,
        "selector_fingerprint": hardware_selector_fingerprint(selector),
        "claim_key": canonical_claim_key,
        "label": str(value.get("label", "") or "").strip(),
        "observed_locator": locator,
        "stability_class": str(selector["stability_class"]),
        "required": bool(value.get("required", True)),
        "transmit_capable": bool(value.get("transmit_capable", role in {"ptt", "audio_tx"})),
    }


def _identity_projection(selector: Mapping[str, Any]) -> dict[str, Any]:
    anchor = next((field for field in _IDENTITY_FIELDS if selector.get(field) not in (None, "")), "")
    if not anchor:
        return {}
    fields = (anchor,) + _QUALIFIER_FIELDS
    return {field: selector[field] for field in fields if selector.get(field) not in (None, "")}


def _candidate_selector(candidate: Mapping[str, Any], platform: str) -> dict[str, Any] | None:
    raw = candidate.get("selector", candidate)
    if not isinstance(raw, Mapping):
        return None
    try:
        return normalize_hardware_selector(raw, platform=platform)
    except HardwareBindingError:
        return None


def match_hardware_selector(
    saved_selector: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
) -> HardwareMatchResult:
    """Match one saved selector without fuzzy-name or ordinal fallback."""

    platform = str(saved_selector.get("platform", "") or "").strip().lower()
    saved = normalize_hardware_selector(saved_selector, platform=platform)
    wanted = _identity_projection(saved)
    exact: list[dict[str, Any]] = []
    changed = False
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        normalized = _candidate_selector(candidate, platform)
        if normalized is None:
            continue
        # Candidate enrichment is additive. A binding saved with by-id-only
        # identity must keep matching when a later provider also discovers a
        # hardware serial or other stronger evidence.
        if wanted and all(normalized.get(key) == value for key, value in wanted.items()):
            exact.append(candidate)
            continue
        stable_qualifiers = (
            "vendor_id",
            "product_id",
            "interface_number",
            "direction",
        )
        comparable = [key for key in stable_qualifiers if saved.get(key) not in (None, "")]
        if comparable and all(normalized.get(key) == saved.get(key) for key in comparable):
            changed = True

    if len(exact) > 1:
        return HardwareMatchResult(
            "ambiguous",
            tuple(exact),
            "More than one connected device satisfies the saved hardware identity.",
        )
    if len(exact) == 1:
        stability = str(saved.get("stability_class", "stable"))
        state = "exact" if stability == "stable" else stability
        return HardwareMatchResult(state, (exact[0],), "The saved hardware identity matched exactly.")
    if changed:
        return HardwareMatchResult(
            "changed",
            (),
            "A similar device is present, but it does not satisfy the saved hardware identity.",
        )
    return HardwareMatchResult("missing", (), "The saved hardware identity is not currently present.")


def hardware_selector_public_summary(selector: Mapping[str, Any]) -> dict[str, Any]:
    """Return support-safe identity detail without hardware serial values."""

    summary = {
        key: value
        for key, value in selector.items()
        if key not in _DISPLAY_FIELDS and key not in {"hardware_serial", "unstable_locator"}
    }
    summary["hardware_serial_present"] = bool(str(selector.get("hardware_serial", "") or "").strip())
    return summary
