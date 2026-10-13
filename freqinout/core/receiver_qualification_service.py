"""Worker-owned receiver qualification on the shared endpoint lane registry.

The coordinator owns no Qt objects and no database connection.  Callers pass an
immutable profile snapshot and marshal the completion back to their UI thread.
Qualification uses the same target-qualified lane registry as scheduled receiver
commands, so an explicit setup test cannot race an active schedule tune.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import threading
import time
import uuid
from typing import Callable, Mapping, Optional

from freqinout.core.receiver_control import (
    ReceiverControlClient,
    receiver_identity_from_profile,
)
from freqinout.core.receiver_qualification import (
    probe_receiver_control,
    run_reversible_receiver_tune_test,
)
from freqinout.core.scheduler_coordination import EndpointKey, EndpointResult
from freqinout.core.scheduler_endpoint_lane import EndpointLaneRegistry, LaneSubmission


ReceiverClientFactory = Callable[[Mapping[str, object]], Optional[ReceiverControlClient]]
QualificationCompletion = Callable[[EndpointResult], None]


# This is deliberately transient.  A qualification can be useful while the
# guided-radio dialog still holds an unsaved draft, but only a saved profile
# may retain its resulting evidence.  The token lets the UI distinguish that
# draft from another open dialog (and from an earlier click on the same
# dialog) without inventing a database identity.
QUALIFICATION_REQUEST_ID_FIELD = "qualification_request_id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _qualification_request_id(value: object = None) -> str:
    """Return a bounded opaque correlation token for one test request.

    UI callers normally provide a UUID-like value.  Generating one here as a
    fallback keeps the coordinator safe for non-UI callers and existing saved
    profile flows.  Rejecting malformed caller values prevents an unbounded
    display/input string from becoming lane metadata or an endpoint result.
    """

    token = str(value or "").strip()
    if token:
        if _REQUEST_ID_RE.fullmatch(token) is None:
            raise ValueError("Receiver test request ID is invalid.")
        return token
    return uuid.uuid4().hex


class ReceiverQualificationCoordinator:
    """Submit bounded reversible receiver checks to existing endpoint lanes."""

    def __init__(
        self,
        lane_registry: EndpointLaneRegistry,
        client_factory: ReceiverClientFactory,
        *,
        timeout_s: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lanes = lane_registry
        self._factory = client_factory
        self._timeout_s = max(0.25, min(float(timeout_s), 10.0))
        self._monotonic = monotonic
        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        self._active_clients: dict[int, ReceiverControlClient] = {}

    def request(
        self,
        profile: Mapping[str, object],
        completion: QualificationCompletion,
        *,
        qualification_request_id: object = None,
    ) -> LaneSubmission:
        """Queue one explicit test without opening an endpoint on the caller."""

        snapshot = dict(profile)
        request_id = _qualification_request_id(
            qualification_request_id
            if qualification_request_id is not None
            else snapshot.get(QUALIFICATION_REQUEST_ID_FIELD)
        )
        snapshot[QUALIFICATION_REQUEST_ID_FIELD] = request_id
        raw_profile_id = snapshot.get("id") or snapshot.get("device_profile_id")
        if raw_profile_id in (None, ""):
            profile_id = 0
        else:
            try:
                profile_id = int(raw_profile_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("receiver profile ID is invalid") from exc
        if profile_id < 0:
            raise ValueError("receiver profile ID is invalid")
        # The existing adapter contract correctly requires a positive profile
        # identity.  Supply one only to the in-memory adapter snapshot for a
        # draft; the operator snapshot and all evidence below retain ID 0.
        # A deterministic, bounded hash avoids both database allocation and a
        # fake stable profile identity.
        factory_snapshot = snapshot
        if profile_id == 0:
            transient_id = int.from_bytes(
                hashlib.sha256(request_id.encode("utf-8")).digest()[:8], "big"
            )
            factory_snapshot = dict(snapshot)
            factory_snapshot["id"] = max(1, transient_id)
        identity = receiver_identity_from_profile(factory_snapshot)
        adapter = str(snapshot.get("sdr_adapter") or "manual").strip().lower().replace("-", "_")
        if adapter in {"", "manual", "none"}:
            raise ValueError("Select a receive-only CommsPlan control adapter before testing.")
        host = str(snapshot.get("sdr_host") or "").strip()
        port = snapshot.get("sdr_port")
        target = str(snapshot.get("sdr_target") or "").strip().lower()
        if not host or port in (None, "") or not target:
            raise ValueError("Receiver testing requires an application host, port, and target.")
        endpoint_key = EndpointKey.network(adapter, host, port, target=target)
        if self._cancelled.is_set():
            return LaneSubmission(endpoint_key, 0, "stopped", False)
        # A draft is intentionally allowed to run this reversible test.  Its
        # evidence remains transient until the enclosing guided setup saves
        # the profile and explicitly writes it through normal validation.
        occurrence_id = f"receiver-qualification:{request_id}"

        def _is_cancelled() -> bool:
            return self._cancelled.is_set()

        def _operation() -> Mapping[str, object]:
            deadline = self._monotonic() + self._timeout_s
            client = self._factory(factory_snapshot)
            if client is None:
                return {
                    "ok": False,
                    "reason_code": "receiver_adapter_unavailable",
                    "detail": "The selected receiver adapter is unavailable; manual tuning remains available.",
                }
            with self._lock:
                if self._cancelled.is_set():
                    client.close()
                    return {
                        "ok": False,
                        "reason_code": "receiver_cancelled",
                        "detail": "Receiver test cancelled; manual tuning remains available.",
                    }
                self._active_clients[id(client)] = client
            try:
                probe = probe_receiver_control(
                    client,
                    identity,
                    deadline=deadline,
                    cancel=_is_cancelled,
                    monotonic=self._monotonic,
                )
                current_hz = probe.state.frequency_hz if probe.state is not None else None
                if current_hz is None:
                    return {
                        "ok": False,
                        "reason_code": "receiver_frequency_unavailable",
                        "detail": "CommsPlan connected but could not read the selected VFO frequency; manual tuning remains available.",
                    }
                # The explicit test changes the selected VFO by a small visible
                # amount, verifies it, then restores and verifies the original.
                # This proves write/readback instead of treating a no-op write
                # or an open port as control evidence.
                step_hz = max(1_000, int(probe.capabilities.readback_tolerance_hz) * 2 + 1)
                test_hz = int(current_hz) + step_hz
                qualified = run_reversible_receiver_tune_test(
                    client,
                    identity,
                    test_hz,
                    deadline=deadline,
                    cancel=_is_cancelled,
                    monotonic=self._monotonic,
                )
                now_utc = _dt.datetime.now(_dt.timezone.utc).isoformat()
                evidence = {
                    "schema_version": 1,
                    "tested_at_utc": now_utc,
                    "adapter": adapter,
                    "application": str(snapshot.get("sdr_application") or "").strip(),
                    "host": host,
                    "port": int(port),
                    "target": str(snapshot.get("sdr_target") or "").strip(),
                    "original_frequency_hz": int(current_hz),
                    "test_frequency_hz": test_hz,
                    "tune_readback_verified": bool(
                        qualified.verified is not None and qualified.verified.verified
                    ),
                    "restore_readback_verified": bool(
                        qualified.restore_verified is not None
                        and qualified.restore_verified.verified
                    ),
                    "capabilities": {
                        "read_state": bool(probe.capabilities.can_read_state),
                        "set_frequency": bool(probe.capabilities.can_set_receive_frequency),
                        "set_mode": bool(probe.capabilities.can_set_receive_mode),
                        "verify_state": bool(probe.capabilities.can_verify_state),
                        "readback_tolerance_hz": int(probe.capabilities.readback_tolerance_hz),
                    },
                }
                return {
                    "ok": bool(qualified.success),
                    "reason_code": "" if qualified.success else "receiver_qualification_failed",
                    "detail": qualified.detail,
                    "actual_state": {
                        "profile_id": profile_id,
                        QUALIFICATION_REQUEST_ID_FIELD: request_id,
                        "verification_state": "verified" if qualified.success else "failed",
                        "operator_state": qualified.operator_state,
                        "verification": evidence,
                    },
                }
            finally:
                with self._lock:
                    self._active_clients.pop(id(client), None)
                try:
                    client.close()
                except Exception:
                    pass

        def _complete(result: EndpointResult) -> None:
            actual = result.actual_state()
            actual.setdefault("profile_id", profile_id)
            # Lane-generated timeout/superseded results do not retain the
            # operation's actual state.  Reattach the captured request token
            # so every completion is safely correlatable on the UI thread.
            actual.setdefault(QUALIFICATION_REQUEST_ID_FIELD, request_id)
            completion(
                EndpointResult.create(
                    endpoint_key=result.endpoint_key,
                    generation=result.generation,
                    status=result.status,
                    actual_state=actual,
                    reason_code=result.reason_code,
                    detail=result.detail,
                )
            )

        # This is an explicit operator retry and may half-open only this
        # target's circuit; it cannot disturb another receiver lane.
        self._lanes.retry_now(endpoint_key)
        return self._lanes.submit(
            endpoint_key,
            occurrence_id=occurrence_id,
            operation=_operation,
            completion=_complete,
            timeout_s=self._timeout_s,
        )

    def stop(self) -> None:
        """Fence new work and interrupt any active adapter sockets."""

        self._cancelled.set()
        with self._lock:
            clients = tuple(self._active_clients.values())
        for client in clients:
            try:
                client.close()
            except Exception:
                continue

    def is_stopped(self) -> bool:
        with self._lock:
            return self._cancelled.is_set() and not self._active_clients


__all__ = ["QUALIFICATION_REQUEST_ID_FIELD", "ReceiverQualificationCoordinator"]
