"""Generation-fenced, side-effect-free radio hardware reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from freqinout.core.hardware_inventory import HardwareInventorySnapshot
from freqinout.core.radio_hardware_binding import HardwareBindingError, match_hardware_selector


@dataclass(frozen=True)
class HardwareRoleSnapshot:
    role: str
    binding_key: str
    state: str
    label: str
    observed_locator: Mapping[str, Any]
    reason: str
    application_verified: bool = False
    platform: str = ""
    provider: str = ""
    transport: str = ""


@dataclass(frozen=True)
class RadioHardwareSnapshot:
    radio_profile_id: int
    bundle_generation: int
    inventory_generation: int
    aggregate_state: str
    roles: tuple[HardwareRoleSnapshot, ...]
    transmit_allowed: bool = False
    reason: str = ""


def reconcile_radio_hardware(
    bundle: Mapping[str, Any] | None,
    inventory: HardwareInventorySnapshot,
    *,
    expected_bundle_generation: int | None = None,
) -> RadioHardwareSnapshot:
    """Join one saved bundle to one inventory snapshot without mutating either."""

    if not bundle or str(bundle.get("lifecycle_state", "") or "") == "retired":
        return RadioHardwareSnapshot(
            radio_profile_id=int((bundle or {}).get("radio_profile_id", 0) or 0),
            bundle_generation=int((bundle or {}).get("generation", 0) or 0),
            inventory_generation=int(inventory.generation),
            aggregate_state="unbound",
            roles=(),
            reason="No reviewed CommsPlan-owned hardware assignment exists.",
        )
    generation = int(bundle.get("generation", 0) or 0)
    if expected_bundle_generation is not None and generation != int(expected_bundle_generation):
        raise HardwareBindingError(
            f"Hardware reconciliation is stale (expected generation {int(expected_bundle_generation)}, found {generation})."
        )

    role_snapshots: list[HardwareRoleSnapshot] = []
    for binding in bundle.get("bindings", []):
        if not isinstance(binding, Mapping) or not bool(binding.get("active", True)):
            continue
        role = str(binding.get("role", "") or "")
        platform = str(binding.get("platform", "") or "")
        provider = str(binding.get("provider", "") or "")
        transport = str(binding.get("transport", "") or "")
        compatible = []
        for candidate in inventory.candidates:
            capabilities = tuple(candidate.get("capabilities", (candidate.get("role"),)))
            if role not in capabilities:
                continue
            if str(candidate.get("platform", "") or "") != platform:
                continue
            if str(candidate.get("provider", "") or "") != provider:
                continue
            if str(candidate.get("transport", "") or "") != transport:
                continue
            compatible.append(candidate)
        result = match_hardware_selector(dict(binding.get("selector", {}) or {}), compatible)
        observed_locator: Mapping[str, Any] = {}
        label = str(binding.get("label", "") or role.replace("_", " ").title())
        if len(result.matches) == 1:
            match = result.matches[0]
            observed = match.get("observed_locator", {})
            observed_locator = dict(observed) if isinstance(observed, Mapping) else {}
            label = str(match.get("label", "") or label)
        role_snapshots.append(
            HardwareRoleSnapshot(
                role=role,
                binding_key=str(binding.get("binding_key", "") or ""),
                state=result.state,
                label=label,
                observed_locator=observed_locator,
                reason=result.reason,
                application_verified=False,
                platform=platform,
                provider=provider,
                transport=transport,
            )
        )

    states = {snapshot.state for snapshot in role_snapshots}
    priority = ("conflict", "ambiguous", "changed", "unsupported", "missing")
    failed_state = next((state for state in priority if state in states), "")
    if failed_state:
        aggregate = failed_state
        reason = next(snapshot.reason for snapshot in role_snapshots if snapshot.state == failed_state)
    elif role_snapshots:
        aggregate = "matched_unverified"
        reason = "Hardware identities matched; application use has not yet been verified."
    else:
        aggregate = "unbound"
        reason = "The saved bundle has no active hardware assignments."
    return RadioHardwareSnapshot(
        radio_profile_id=int(bundle.get("radio_profile_id", 0) or 0),
        bundle_generation=generation,
        inventory_generation=int(inventory.generation),
        aggregate_state=aggregate,
        roles=tuple(role_snapshots),
        transmit_allowed=False,
        reason=reason,
    )
