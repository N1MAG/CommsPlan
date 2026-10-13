"""Bounded, Qt-free receiver probe and reversible tune qualification.

This module coordinates an already-constructed receive-only application
adapter.  It does not discover devices, open vendor drivers, create workers,
persist configuration, or touch Qt.  Callers must execute it in the selected
endpoint lane (or another bounded worker) and provide one absolute monotonic
deadline plus cooperative cancellation callback.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from freqinout.core.receiver_control import (
    CancelCheck,
    ReceiverCapabilities,
    ReceiverCommand,
    ReceiverControlClient,
    ReceiverIdentity,
    ReceiverState,
)


MAX_RECEIVER_TARGETS = 32


class ReceiverQualificationError(RuntimeError):
    """A truthful bounded qualification failure, safe to show to an operator."""


@dataclass(frozen=True)
class ReceiverProbeSnapshot:
    identity: ReceiverIdentity
    capabilities: ReceiverCapabilities
    targets: Tuple[ReceiverIdentity, ...]
    state: Optional[ReceiverState]
    operator_state: str
    detail: str = ""


@dataclass(frozen=True)
class ReceiverTuneQualification:
    success: bool
    identity: ReceiverIdentity
    capabilities: ReceiverCapabilities
    requested_frequency_hz: int
    before: Optional[ReceiverState]
    applied: Optional[ReceiverState]
    verified: Optional[ReceiverState]
    restored: Optional[ReceiverState]
    restore_verified: Optional[ReceiverState]
    operator_state: str
    detail: str = ""


def _cancelled(cancel: CancelCheck) -> bool:
    try:
        return bool(cancel())
    except Exception:
        return True


def _require_budget(*, deadline: float, cancel: CancelCheck, monotonic: Callable[[], float]) -> None:
    if _cancelled(cancel):
        raise ReceiverQualificationError("Receiver test cancelled; manual tuning remains available.")
    if monotonic() >= float(deadline):
        raise ReceiverQualificationError("Receiver test timed out; manual tuning remains available.")


def _target_matches(configured: ReceiverIdentity, candidate: ReceiverIdentity) -> bool:
    return (
        configured.adapter_id == candidate.adapter_id
        and configured.receiver_id == candidate.receiver_id
        and configured.target_id == candidate.target_id
    )


def probe_receiver_control(
    client: ReceiverControlClient,
    configured_target: ReceiverIdentity,
    *,
    deadline: float,
    cancel: CancelCheck,
    max_targets: int = MAX_RECEIVER_TARGETS,
    monotonic: Callable[[], float] = time.monotonic,
) -> ReceiverProbeSnapshot:
    """Probe capabilities and one configured target without guessing identity."""

    limit = int(max_targets)
    if limit <= 0 or limit > MAX_RECEIVER_TARGETS:
        raise ValueError(f"max_targets must be between 1 and {MAX_RECEIVER_TARGETS}")
    _require_budget(deadline=deadline, cancel=cancel, monotonic=monotonic)
    identity, capabilities = client.probe(deadline=deadline, cancel=cancel)
    if capabilities.manual_only:
        return ReceiverProbeSnapshot(
            identity=identity,
            capabilities=capabilities,
            targets=(identity,),
            state=None,
            operator_state="Manual tuning",
            detail=capabilities.detail or "Tune this receiver manually.",
        )

    targets: Tuple[ReceiverIdentity, ...] = (identity,)
    if capabilities.can_list_targets:
        _require_budget(deadline=deadline, cancel=cancel, monotonic=monotonic)
        reported = tuple(client.list_targets(deadline=deadline, cancel=cancel))
        if len(reported) > limit:
            raise ReceiverQualificationError(
                f"Receiver reported more than {limit} targets; narrow the application selection and retry."
            )
        targets = reported
    selected = next((target for target in targets if _target_matches(configured_target, target)), None)
    if selected is None:
        raise ReceiverQualificationError(
            "The configured receiver target was not reported; select the intended receiver/VFO and retry."
        )

    state: Optional[ReceiverState] = None
    if capabilities.can_read_state:
        _require_budget(deadline=deadline, cancel=cancel, monotonic=monotonic)
        state = client.read_state(selected, deadline=deadline, cancel=cancel)
        if state.cancelled:
            raise ReceiverQualificationError("Receiver test cancelled; manual tuning remains available.")
    available = bool(state.available) if state is not None else True
    return ReceiverProbeSnapshot(
        identity=selected,
        capabilities=capabilities,
        targets=targets,
        state=state,
        operator_state="Connected; verify tuning" if available else "Receiver unavailable",
        detail=(state.detail if state is not None else capabilities.detail),
    )


def run_reversible_receiver_tune_test(
    client: ReceiverControlClient,
    configured_target: ReceiverIdentity,
    frequency_hz: int,
    *,
    deadline: float,
    cancel: CancelCheck,
    tolerance_hz: Optional[int] = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ReceiverTuneQualification:
    """Tune, verify, restore the prior frequency, and verify the restoration.

    A successful write is never sufficient.  Success requires both target
    readback and restoration readback.  Any failure leaves the profile outside
    the verified/automatic state and preserves the manual workflow.
    """

    requested = int(frequency_hz)
    if requested <= 0:
        raise ValueError("frequency_hz must be a positive integer")
    probe = probe_receiver_control(
        client,
        configured_target,
        deadline=deadline,
        cancel=cancel,
        monotonic=monotonic,
    )
    capabilities = probe.capabilities
    if not (
        capabilities.can_read_state
        and capabilities.can_set_receive_frequency
        and capabilities.can_verify_state
    ):
        return ReceiverTuneQualification(
            success=False,
            identity=probe.identity,
            capabilities=capabilities,
            requested_frequency_hz=requested,
            before=probe.state,
            applied=None,
            verified=None,
            restored=None,
            restore_verified=None,
            operator_state="Manual tuning",
            detail="This receiver does not report the frequency write and readback capabilities required for CommsPlan tuning.",
        )
    before = probe.state
    if before is None or before.frequency_hz is None:
        return ReceiverTuneQualification(
            success=False,
            identity=probe.identity,
            capabilities=capabilities,
            requested_frequency_hz=requested,
            before=before,
            applied=None,
            verified=None,
            restored=None,
            restore_verified=None,
            operator_state="Connected; verify tuning",
            detail="CommsPlan could not read the current frequency, so a reversible tuning test was not attempted.",
        )

    tolerance = capabilities.readback_tolerance_hz if tolerance_hz is None else int(tolerance_hz)
    if tolerance < 0:
        raise ValueError("tolerance_hz cannot be negative")
    applied: Optional[ReceiverState] = None
    verified: Optional[ReceiverState] = None
    restored: Optional[ReceiverState] = None
    restore_verified: Optional[ReceiverState] = None
    failure_detail = ""
    tune_attempted = False
    try:
        _require_budget(deadline=deadline, cancel=cancel, monotonic=monotonic)
        tune_attempted = True
        applied = client.set_receive_frequency(
            probe.identity,
            requested,
            deadline=deadline,
            cancel=cancel,
        )
        _require_budget(deadline=deadline, cancel=cancel, monotonic=monotonic)
        verified = client.verify_state(
            probe.identity,
            ReceiverCommand(target_id=probe.identity.target_id, frequency_hz=requested),
            tolerance_hz=tolerance,
            deadline=deadline,
            cancel=cancel,
        )
        if not verified.verified or verified.frequency_hz is None or abs(verified.frequency_hz - requested) > tolerance:
            failure_detail = "Receiver frequency readback did not match the test frequency."
    except Exception as exc:
        failure_detail = str(exc).strip() or "Receiver tuning test failed."
    finally:
        # Once a tune call was attempted, make one bounded best-effort restore
        # unless cancellation/deadline already prevents further endpoint I/O.
        if tune_attempted and not _cancelled(cancel) and monotonic() < float(deadline):
            try:
                restored = client.set_receive_frequency(
                    probe.identity,
                    int(before.frequency_hz),
                    deadline=deadline,
                    cancel=cancel,
                )
                _require_budget(deadline=deadline, cancel=cancel, monotonic=monotonic)
                restore_verified = client.verify_state(
                    probe.identity,
                    ReceiverCommand(target_id=probe.identity.target_id, frequency_hz=int(before.frequency_hz)),
                    tolerance_hz=tolerance,
                    deadline=deadline,
                    cancel=cancel,
                )
            except Exception as exc:
                if not failure_detail:
                    failure_detail = str(exc).strip() or "Receiver restoration failed."

    restored_ok = bool(
        restore_verified is not None
        and restore_verified.verified
        and restore_verified.frequency_hz is not None
        and abs(restore_verified.frequency_hz - int(before.frequency_hz)) <= tolerance
    )
    tune_ok = bool(
        verified is not None
        and verified.verified
        and verified.frequency_hz is not None
        and abs(verified.frequency_hz - requested) <= tolerance
    )
    success = tune_ok and restored_ok
    if not restored_ok:
        restore_warning = "CommsPlan could not verify restoration of the original receiver frequency. Check the receiver before continuing."
        failure_detail = f"{failure_detail} {restore_warning}".strip()
    return ReceiverTuneQualification(
        success=success,
        identity=probe.identity,
        capabilities=capabilities,
        requested_frequency_hz=requested,
        before=before,
        applied=applied,
        verified=verified,
        restored=restored,
        restore_verified=restore_verified,
        operator_state="FIO tuning ready" if success else "Connected; verify tuning",
        detail=(
            "Frequency tuning and restoration were verified."
            if success
            else failure_detail or "Receiver tuning verification did not pass; manual tuning remains available."
        ),
    )


__all__ = [
    "MAX_RECEIVER_TARGETS",
    "ReceiverProbeSnapshot",
    "ReceiverQualificationError",
    "ReceiverTuneQualification",
    "probe_receiver_control",
    "run_reversible_receiver_tune_test",
]
