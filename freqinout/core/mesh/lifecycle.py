from __future__ import annotations

from dataclasses import dataclass
import threading
import time


class MeshOperationCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class MeshOperationSnapshot:
    operation_id: str
    source_id: str
    request_class: str
    state: str
    progress_current: int = 0
    progress_total: int = 0
    started_monotonic_ms: int = 0
    completed_monotonic_ms: int = 0
    elapsed_ms: int = 0
    supersedes: str = ""
    detail: str = ""


@dataclass(frozen=True)
class MeshRetryPolicy:
    initial_delay_ms: int = 15_000
    maximum_delay_ms: int = 300_000
    multiplier: float = 2.0
    maximum_failures: int = 3

    def delay_ms(self, failure_count: int) -> int:
        failures = max(1, int(failure_count))
        delay = float(max(250, self.initial_delay_ms)) * (max(1.0, self.multiplier) ** (failures - 1))
        return min(max(250, self.maximum_delay_ms), int(delay))


@dataclass
class MeshRetryState:
    failure_count: int = 0
    next_retry_ms: int = 0
    operator_action_required: bool = False

    def due(self, now_ms: int) -> bool:
        if self.operator_action_required:
            return False
        return self.next_retry_ms <= 0 or int(now_ms) >= self.next_retry_ms

    def record_failure(self, now_ms: int, policy: MeshRetryPolicy) -> int:
        self.operator_action_required = False
        self.failure_count += 1
        if self.failure_count >= max(1, int(policy.maximum_failures)):
            self.next_retry_ms = 0
            self.operator_action_required = True
            return 0
        delay = policy.delay_ms(self.failure_count)
        self.next_retry_ms = int(now_ms) + delay
        return delay

    def record_operator_action_required(self) -> None:
        self.failure_count += 1
        self.next_retry_ms = 0
        self.operator_action_required = True

    def record_success(self) -> None:
        self.failure_count = 0
        self.next_retry_ms = 0
        self.operator_action_required = False

    def retry_now(self) -> None:
        """Start a fresh bounded series after an explicit operator action."""

        self.failure_count = 0
        self.next_retry_ms = 0
        self.operator_action_required = False

    def remaining_ms(self, now_ms: int) -> int:
        return max(0, self.next_retry_ms - int(now_ms))

    def copy(self) -> "MeshRetryState":
        """Return a detached state for handoff between worker instances."""

        return MeshRetryState(
            failure_count=self.failure_count,
            next_retry_ms=self.next_retry_ms,
            operator_action_required=self.operator_action_required,
        )

    def defer(self, now_ms: int, delay_ms: int) -> int:
        """Defer without counting another failure.

        Used when another worker still owns the same connection attempt. A
        replacement worker must not turn an in-flight cancellation/teardown
        into a second simultaneous connect attempt.
        """

        self.next_retry_ms = int(now_ms) + max(250, int(delay_ms))
        return max(250, int(delay_ms))


_RETRY_HANDOFF_TTL_SEC = 30.0
_retry_handoff_lock = threading.Lock()
_retry_handoff: dict[object, tuple[float, MeshRetryState]] = {}


def save_mesh_retry_handoff(key: object, state: MeshRetryState) -> None:
    """Save retry state for an immediately replaced runtime worker.

    This is deliberately process-local: it preserves a live app's backoff
    across QThread replacement without creating a new settings or database
    persistence contract.
    """

    with _retry_handoff_lock:
        _retry_handoff[key] = (time.monotonic(), state.copy())


def take_mesh_retry_handoff(key: object) -> MeshRetryState | None:
    """Consume a recent retry state handoff, if one exists."""

    now = time.monotonic()
    with _retry_handoff_lock:
        entry = _retry_handoff.pop(key, None)
    if entry is None:
        return None
    saved_at, state = entry
    if now - saved_at > _RETRY_HANDOFF_TTL_SEC:
        return None
    return state.copy()


class MeshConnectAttemptLease:
    """Process-local ownership gate for replacement-worker connect attempts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: set[object] = set()

    def try_acquire(self, key: object) -> bool:
        with self._lock:
            if key in self._owners:
                return False
            self._owners.add(key)
            return True

    def release(self, key: object) -> None:
        with self._lock:
            self._owners.discard(key)


_MESH_CONNECT_ATTEMPT_LEASE = MeshConnectAttemptLease()


def try_acquire_mesh_connect_attempt(key: object) -> bool:
    return _MESH_CONNECT_ATTEMPT_LEASE.try_acquire(key)


def release_mesh_connect_attempt(key: object) -> None:
    _MESH_CONNECT_ATTEMPT_LEASE.release(key)


def mesh_error_requires_operator_action(error: object) -> bool:
    """Return whether another automatic attempt cannot repair the BLE state."""

    text = str(error or "").casefold()
    return any(
        marker in text
        for marker in (
            "peer removed pairing information",
            "card removed its saved bluetooth pairing information",
            "cberrordomain code=14",
            "no powered bluetooth adapters found",
            "bleakbluetoothnotavailablereason.powered_off",
            "br-connection-canceled",
            "linux could not complete meshcore bluetooth pairing",
            "reconnect paused",
        )
    )
