"""Pure presentation state for Guided Add Radio receiver safety UI.

This module deliberately performs no endpoint, scheduler, database, or file I/O.
The Settings dialog supplies immutable values it already owns so Connections,
Receiver Guard, Receive Schedule, and Review can use one vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple


@dataclass(frozen=True)
class ReceiverStatePresentation:
    key: str
    title: str
    detail: str
    tone: str
    automatic_retune_allowed: bool


@dataclass(frozen=True)
class GuidedRecoveryPresentation:
    status: str
    detail: str
    retry_route: str
    tone: str


def receiver_state_presentation(
    *,
    application: str,
    adapter: str,
    host: str,
    port: str,
    target: str,
    verification_state: str,
    evidence_present: bool,
    evidence_matches: bool,
    automatic_tuning_enabled: bool,
    resource_blocks: Iterable[str] = (),
) -> ReceiverStatePresentation:
    """Return the product-contract receiver state from already-loaded values."""

    app = str(application or "").strip()
    adapter_key = str(adapter or "manual").strip().lower()
    complete_identity = bool(
        app
        and app.casefold() not in {"other / manual", "manual"}
        and adapter_key not in {"", "manual", "none"}
        and str(host or "").strip()
        and str(port or "").strip()
        and str(target or "").strip()
    )
    blocks = tuple(str(item).strip() for item in resource_blocks if str(item).strip())
    verified = bool(
        complete_identity
        and str(verification_state or "").strip().lower() == "verified"
        and evidence_present
        and evidence_matches
    )

    if verified and automatic_tuning_enabled and blocks:
        return ReceiverStatePresentation(
            key="resource_blocked",
            title="Blocked by receiver resource",
            detail="Automatic receive retuning is held because " + "; ".join(blocks) + ".",
            tone="warning",
            automatic_retune_allowed=False,
        )
    if verified and automatic_tuning_enabled:
        return ReceiverStatePresentation(
            key="verified",
            title="Verified — automatic retune allowed",
            detail=(
                f"Matching control evidence authorizes receive-only retuning through {app}. "
                "Receiver Guard still applies before every automatic tune."
            ),
            tone="success",
            automatic_retune_allowed=True,
        )
    if (
        complete_identity
        and str(verification_state or "").strip().lower() == "verified"
        and not (evidence_present and evidence_matches)
    ):
        return ReceiverStatePresentation(
            key="verification_changed",
            title="Verification expired or changed",
            detail=(
                "Saved verification no longer matches the current application, adapter, endpoint, or target. "
                "The receive schedule remains reminders only until control is tested again."
            ),
            tone="warning",
            automatic_retune_allowed=False,
        )
    if not complete_identity and adapter_key not in {"", "manual", "none"}:
        return ReceiverStatePresentation(
            key="not_configured",
            title="Not configured",
            detail=(
                "Complete the receiver application, adapter, host, port, and target. "
                "No automatic receive command is available."
            ),
            tone="info",
            automatic_retune_allowed=False,
        )
    if not app or app.casefold() in {"other / manual", "manual"}:
        return ReceiverStatePresentation(
            key="not_configured",
            title="Not configured",
            detail=(
                "Choose a receiver application and control adapter, or keep this receiver available for manual tuning."
            ),
            tone="info",
            automatic_retune_allowed=False,
        )
    return ReceiverStatePresentation(
        key="manual",
        title="Manual / reminders only",
        detail=(
            "The receiver remains usable for manual tuning and schedule reminders. "
            "CommsPlan will not retune it automatically without matching verification and explicit tuning opt-in."
        ),
        tone="info",
        automatic_retune_allowed=False,
    )


def validation_messages(validation: object, *, observer: bool) -> Tuple[str, ...]:
    """Return every scheduler/guard mismatch without changing its meaning."""

    if not isinstance(validation, dict):
        return ()
    prefix = "Receiver Guard" if observer else "RF Guard"
    blocked = tuple(str(item).strip() for item in validation.get("blocked", ()) if str(item).strip())
    warnings = tuple(str(item).strip() for item in validation.get("warnings", ()) if str(item).strip())
    return tuple(f"{prefix} blocked: {item}" for item in blocked) + tuple(
        f"{prefix} warning: {item}" for item in warnings
    )


def guided_recovery_presentation(
    *,
    operator_start_apps: Iterable[str] = (),
    verification_pending_apps: Iterable[str] = (),
    needs_attention_app: str = "",
    launch_bundle_retry_app: str = "",
) -> GuidedRecoveryPresentation:
    """Choose one exact post-save state and an object-specific recovery route."""

    operator_apps = tuple(str(item).strip() for item in operator_start_apps if str(item).strip())
    verification_apps = tuple(
        str(item).strip() for item in verification_pending_apps if str(item).strip()
    )
    bundle_app = str(launch_bundle_retry_app or "").strip()
    attention_app = str(needs_attention_app or "").strip()
    if bundle_app:
        return GuidedRecoveryPresentation(
            status="Saved — launch bundle retry required",
            detail=f"{bundle_app} was saved, but its reviewed launch bundle still needs to be applied.",
            retry_route=f"Settings → Radios → {bundle_app} → Launch Control",
            tone="warning",
        )
    if attention_app:
        return GuidedRecoveryPresentation(
            status="Saved — one app needs attention",
            detail=f"{attention_app} is incomplete; the remaining radio and software configuration stays saved.",
            retry_route=f"Settings → Software → {attention_app}",
            tone="warning",
        )
    if verification_apps:
        names = ", ".join(verification_apps)
        return GuidedRecoveryPresentation(
            status="Saved — verification pending",
            detail=f"Live verification is still required for {names}.",
            retry_route=f"Settings → Software → {verification_apps[0]} → Verify",
            tone="info",
        )
    if operator_apps:
        names = ", ".join(operator_apps)
        return GuidedRecoveryPresentation(
            status="Saved — operator start required",
            detail=f"CommsPlan will not start {names}; start the application manually when it is needed.",
            retry_route=f"Settings → Software → {operator_apps[0]} → Launch",
            tone="info",
        )
    return GuidedRecoveryPresentation(
        status="Saved — ready",
        detail="The reviewed radio and software configuration is saved.",
        retry_route="",
        tone="success",
    )


__all__ = [
    "GuidedRecoveryPresentation",
    "ReceiverStatePresentation",
    "guided_recovery_presentation",
    "receiver_state_presentation",
    "validation_messages",
]
