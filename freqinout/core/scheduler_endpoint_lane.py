"""Failure-isolated serialized workers for scheduler endpoint control.

The lane layer owns threads and monotonic runtime state but no Qt objects,
database handles, settings, UI, timers, or protocol adapters. Callers provide a
bounded operation and marshal immutable completion results to their own thread.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import threading
import time
from typing import Callable, Dict, Mapping, Optional, Tuple

from freqinout.core.scheduler_coordination import (
    EndpointKey,
    EndpointResult,
    bounded_exponential_backoff,
)
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor


EndpointOperation = Callable[[], object]
EndpointCompletion = Callable[[EndpointResult], None]
ExecutorFactory = Callable[..., object]


@dataclass(frozen=True)
class EndpointWork:
    endpoint_key: EndpointKey
    generation: int
    occurrence_id: str
    operation: EndpointOperation
    completion: EndpointCompletion
    timeout_s: float = 8.0

    def __post_init__(self) -> None:
        if int(self.generation) <= 0:
            raise ValueError("endpoint work generation must be positive")
        if float(self.timeout_s) <= 0.0:
            raise ValueError("endpoint work timeout must be positive")
        if not callable(self.operation) or not callable(self.completion):
            raise TypeError("endpoint work operation and completion must be callable")


@dataclass(frozen=True)
class LaneSubmission:
    endpoint_key: EndpointKey
    generation: int
    disposition: str
    accepted: bool


@dataclass(frozen=True)
class EndpointLaneSnapshot:
    endpoint_key: EndpointKey
    state: str
    current_generation: int
    pending_generation: int
    failure_count: int
    backoff_until_monotonic: float
    circuit_open: bool
    half_open: bool
    timeout_reported: bool
    started_monotonic: float
    last_success_generation: int
    last_success_occurrence_id: str
    last_result_status: str
    closed: bool


class EndpointLane:
    """One serialized control lane for exactly one normalized endpoint route."""

    def __init__(
        self,
        endpoint_key: EndpointKey,
        *,
        executor_factory: ExecutorFactory = DaemonSerialExecutor,
        monotonic_fn: Callable[[], float] = time.monotonic,
        circuit_failure_threshold: int = 3,
        base_backoff_s: float = 5.0,
        max_backoff_s: float = 300.0,
    ) -> None:
        if int(circuit_failure_threshold) <= 0:
            raise ValueError("circuit_failure_threshold must be positive")
        self.endpoint_key = endpoint_key
        self._monotonic_fn = monotonic_fn
        self._threshold = int(circuit_failure_threshold)
        self._base_backoff_s = max(0.0, float(base_backoff_s))
        self._max_backoff_s = max(self._base_backoff_s, float(max_backoff_s))
        label = "".join(char if char.isalnum() else "-" for char in endpoint_key.adapter_family)
        self._executor = executor_factory(
            max_workers=1,
            thread_name_prefix=f"freqinout-endpoint-{label or 'control'}",
        )
        self._lock = threading.RLock()
        self._future: Optional[Future] = None
        self._current: Optional[EndpointWork] = None
        self._pending: Optional[EndpointWork] = None
        self._token = 0
        self._started_monotonic = 0.0
        self._timeout_reported = False
        self._failure_count = 0
        self._backoff_until = 0.0
        self._circuit_open = False
        self._half_open = False
        self._last_success_generation = 0
        self._last_success_occurrence_id = ""
        self._last_result_status = ""
        self._closed = False

    @property
    def future(self) -> Optional[Future]:
        with self._lock:
            return self._future

    @property
    def pending_occurrence_id(self) -> str:
        with self._lock:
            if self._current is not None:
                return self._current.occurrence_id
            if self._pending is not None:
                return self._pending.occurrence_id
            return ""

    def snapshot(self) -> EndpointLaneSnapshot:
        with self._lock:
            if self._closed:
                state = "stopped"
            elif self._circuit_open:
                state = "circuit_open"
            elif self._future is not None and not self._future.done():
                state = "half_open" if self._half_open else "running"
            elif self._pending is not None and self._monotonic_fn() < self._backoff_until:
                state = "backoff"
            elif self._pending is not None:
                state = "pending"
            else:
                state = "idle"
            return EndpointLaneSnapshot(
                endpoint_key=self.endpoint_key,
                state=state,
                current_generation=self._current.generation if self._current else 0,
                pending_generation=self._pending.generation if self._pending else 0,
                failure_count=self._failure_count,
                backoff_until_monotonic=self._backoff_until,
                circuit_open=self._circuit_open,
                half_open=self._half_open,
                timeout_reported=self._timeout_reported,
                started_monotonic=self._started_monotonic,
                last_success_generation=self._last_success_generation,
                last_success_occurrence_id=self._last_success_occurrence_id,
                last_result_status=self._last_result_status,
                closed=self._closed,
            )

    def submit(self, work: EndpointWork) -> LaneSubmission:
        if work.endpoint_key != self.endpoint_key:
            raise ValueError("endpoint work was submitted to the wrong lane")
        with self._lock:
            if self._closed:
                return LaneSubmission(self.endpoint_key, work.generation, "stopped", False)
            newest_generation = max(
                self._current.generation if self._current else 0,
                self._pending.generation if self._pending else 0,
            )
            if work.generation <= newest_generation:
                return LaneSubmission(self.endpoint_key, work.generation, "stale", False)
            if self._future is not None and not self._future.done():
                self._pending = work
                return LaneSubmission(self.endpoint_key, work.generation, "coalesced", True)
            if self._circuit_open:
                self._pending = work
                return LaneSubmission(self.endpoint_key, work.generation, "circuit_open", False)
            if self._monotonic_fn() < self._backoff_until:
                self._pending = work
                return LaneSubmission(self.endpoint_key, work.generation, "backoff", True)
            self._launch_locked(work)
            return LaneSubmission(self.endpoint_key, work.generation, "started", True)

    def poll(self) -> Optional[EndpointResult]:
        """Advance timeout/backoff state; safe to call from one station timer."""

        completion: Optional[EndpointCompletion] = None
        result: Optional[EndpointResult] = None
        with self._lock:
            now = self._monotonic_fn()
            if (
                not self._closed
                and self._current is not None
                and self._future is not None
                and not self._future.done()
                and not self._timeout_reported
                and now - self._started_monotonic >= float(self._current.timeout_s)
            ):
                self._timeout_reported = True
                self._record_failure_locked(now)
                self._last_result_status = "timed_out"
                completion = self._current.completion
                result = EndpointResult.create(
                    endpoint_key=self.endpoint_key,
                    generation=self._current.generation,
                    status="timed_out",
                    reason_code="operation_timeout",
                    detail="Endpoint operation exceeded its bounded timeout.",
                )
            elif (
                not self._closed
                and self._future is None
                and self._pending is not None
                and now >= self._backoff_until
            ):
                pending = self._pending
                self._pending = None
                self._half_open = bool(self._circuit_open)
                self._circuit_open = False
                self._launch_locked(pending)
        if completion is not None and result is not None:
            completion(result)
        return result

    def retry_now(self) -> bool:
        """Permit one operator-requested half-open probe for this lane only."""

        with self._lock:
            if self._closed:
                return False
            self._circuit_open = False
            self._half_open = self._failure_count >= self._threshold
            self._backoff_until = 0.0
            self._timeout_reported = False
            if self._future is not None and not self._future.done():
                return False
            if self._pending is None:
                return True
            pending = self._pending
            self._pending = None
            self._launch_locked(pending)
            return True

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._token += 1
            self._pending = None
            future = self._future
            if cancel_futures and future is not None and not future.done():
                try:
                    future.cancel()
                except Exception:
                    pass
        try:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        except TypeError:
            self._executor.shutdown(wait=wait)

    def await_termination(self, timeout_s: float) -> bool:
        """Join this lane only within the caller's remaining shutdown budget."""

        awaiter = getattr(self._executor, "await_termination", None)
        if callable(awaiter):
            return bool(awaiter(max(0.0, float(timeout_s))))
        future = self.future
        if future is not None and not future.done():
            try:
                future.result(timeout=max(0.0, float(timeout_s)))
            except Exception:
                pass
        if future is not None and not future.done():
            return False
        try:
            self._executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            self._executor.shutdown(wait=True)
        return True

    def _launch_locked(self, work: EndpointWork) -> None:
        self._token += 1
        token = self._token
        self._current = work
        self._started_monotonic = self._monotonic_fn()
        self._timeout_reported = False
        future = self._executor.submit(work.operation)
        self._future = future
        future.add_done_callback(lambda done: self._on_done(done, work, token))

    def _record_failure_locked(self, now: float) -> None:
        self._failure_count += 1
        delay = bounded_exponential_backoff(
            self._base_backoff_s,
            self._max_backoff_s,
            self._failure_count,
        )
        self._backoff_until = now + delay
        if self._failure_count >= self._threshold:
            self._circuit_open = True

    def _on_done(self, future: Future, work: EndpointWork, token: int) -> None:
        try:
            raw_result = future.result()
            if isinstance(raw_result, EndpointResult):
                if (
                    raw_result.endpoint_key != self.endpoint_key
                    or raw_result.generation != work.generation
                ):
                    raise ValueError("endpoint operation returned a result for another lane or generation")
                operation_result = raw_result
                succeeded = raw_result.status in {"applied_and_verified", "applied_unverified"}
            elif isinstance(raw_result, Mapping):
                succeeded = bool(raw_result.get("ok", True))
                actual_state = raw_result.get("actual_state")
                operation_result = EndpointResult.create(
                    endpoint_key=self.endpoint_key,
                    generation=work.generation,
                    status="applied_unverified" if succeeded else ("rejected" if raw_result.get("safety_hold") else "failed"),
                    actual_state=actual_state if isinstance(actual_state, Mapping) else None,
                    reason_code="" if succeeded else str(raw_result.get("reason_code") or "operation_failed"),
                    detail=str(raw_result.get("detail") or ""),
                )
            else:
                succeeded = bool(raw_result)
                operation_result = EndpointResult.create(
                    endpoint_key=self.endpoint_key,
                    generation=work.generation,
                    status="applied_unverified" if succeeded else "failed",
                    reason_code="" if succeeded else "operation_failed",
                )
        except Exception as exc:
            succeeded = False
            operation_result = EndpointResult.create(
                endpoint_key=self.endpoint_key,
                generation=work.generation,
                status="failed",
                reason_code="operation_exception",
                detail=f"{type(exc).__name__}: {exc}",
            )

        completion: Optional[EndpointCompletion] = None
        result: Optional[EndpointResult] = None
        launch_pending: Optional[EndpointWork] = None
        with self._lock:
            if self._closed or token != self._token or self._current is not work:
                return
            timed_out = self._timeout_reported
            superseded = self._pending is not None and self._pending.generation > work.generation
            self._future = None
            self._current = None
            self._started_monotonic = 0.0
            self._timeout_reported = False
            if not timed_out:
                completion = work.completion
                if succeeded:
                    self._failure_count = 0
                    self._backoff_until = 0.0
                    self._circuit_open = False
                    self._half_open = False
                    if superseded:
                        self._last_result_status = "superseded"
                        result = EndpointResult.create(
                            endpoint_key=self.endpoint_key,
                            generation=work.generation,
                            status="superseded",
                            reason_code="newer_generation_pending",
                        )
                    else:
                        self._last_success_generation = work.generation
                        self._last_success_occurrence_id = work.occurrence_id
                        self._last_result_status = operation_result.status
                        result = operation_result
                elif operation_result.status == "rejected":
                    # A current safety/ownership hold is not an adapter fault.
                    # The next coordinator tick re-evaluates the current intent.
                    self._last_result_status = operation_result.status
                    self._half_open = False
                    result = operation_result
                else:
                    self._record_failure_locked(self._monotonic_fn())
                    self._half_open = False
                    self._last_result_status = operation_result.status
                    result = operation_result
            if (
                self._pending is not None
                and not self._circuit_open
                and self._monotonic_fn() >= self._backoff_until
            ):
                launch_pending = self._pending
                self._pending = None
        if completion is not None and result is not None:
            completion(result)
        if launch_pending is not None:
            with self._lock:
                if not self._closed and self._future is None:
                    self._launch_locked(launch_pending)


class EndpointLaneRegistry:
    """Thread-safe owner for one lane per normalized endpoint key."""

    def __init__(
        self,
        *,
        executor_factory: ExecutorFactory = DaemonSerialExecutor,
        monotonic_fn: Callable[[], float] = time.monotonic,
        circuit_failure_threshold: int = 3,
        base_backoff_s: float = 5.0,
        max_backoff_s: float = 300.0,
    ) -> None:
        self._executor_factory = executor_factory
        self._monotonic_fn = monotonic_fn
        self._threshold = circuit_failure_threshold
        self._base_backoff_s = base_backoff_s
        self._max_backoff_s = max_backoff_s
        self._lock = threading.RLock()
        self._lanes: Dict[EndpointKey, EndpointLane] = {}
        self._generations: Dict[EndpointKey, int] = {}
        self._closed = False
        self._last_shutdown_survivors: Tuple[str, ...] = ()

    def submit(
        self,
        endpoint_key: EndpointKey,
        *,
        occurrence_id: str,
        operation: EndpointOperation,
        completion: EndpointCompletion,
        timeout_s: float = 8.0,
    ) -> LaneSubmission:
        with self._lock:
            if self._closed:
                return LaneSubmission(endpoint_key, 0, "stopped", False)
            generation = self._generations.get(endpoint_key, 0) + 1
            self._generations[endpoint_key] = generation
            lane = self._lanes.get(endpoint_key)
            if lane is None:
                lane = EndpointLane(
                    endpoint_key,
                    executor_factory=self._executor_factory,
                    monotonic_fn=self._monotonic_fn,
                    circuit_failure_threshold=self._threshold,
                    base_backoff_s=self._base_backoff_s,
                    max_backoff_s=self._max_backoff_s,
                )
                self._lanes[endpoint_key] = lane
        return lane.submit(
            EndpointWork(
                endpoint_key=endpoint_key,
                generation=generation,
                occurrence_id=str(occurrence_id or generation),
                operation=operation,
                completion=completion,
                timeout_s=timeout_s,
            )
        )

    def lane(self, endpoint_key: EndpointKey) -> Optional[EndpointLane]:
        with self._lock:
            return self._lanes.get(endpoint_key)

    def snapshots(self) -> Tuple[EndpointLaneSnapshot, ...]:
        with self._lock:
            lanes = tuple(self._lanes.values())
        return tuple(sorted((lane.snapshot() for lane in lanes), key=lambda item: item.endpoint_key.canonical))

    def poll(self) -> Tuple[EndpointResult, ...]:
        with self._lock:
            lanes = tuple(self._lanes.values())
        results = [result for lane in lanes if (result := lane.poll()) is not None]
        return tuple(results)

    def retry_now(self, endpoint_key: EndpointKey) -> bool:
        lane = self.lane(endpoint_key)
        return bool(lane and lane.retry_now())

    def remove(self, endpoint_key: EndpointKey) -> bool:
        with self._lock:
            lane = self._lanes.pop(endpoint_key, None)
            self._generations.pop(endpoint_key, None)
        if lane is None:
            return False
        future = lane.future
        lane.shutdown(
            wait=bool(future is None or future.done()),
            cancel_futures=True,
        )
        return True

    def retire_all(self) -> Tuple[str, ...]:
        """Fence and detach every current lane without closing the registry.

        Lifecycle discontinuities must not let a pre-suspend command publish a
        late completion after the current schedule has been recomputed.  The
        detached daemon workers may finish cooperatively, but their lane tokens
        are invalidated and new intent receives newly constructed lanes.
        """

        with self._lock:
            lanes = tuple(self._lanes.values())
            self._lanes.clear()
            self._generations.clear()
        active = []
        for lane in lanes:
            future = lane.future
            if future is not None and not future.done():
                active.append(lane.endpoint_key.safe_label)
            lane.shutdown(wait=False, cancel_futures=True)
        return tuple(sorted(active))

    def shutdown(
        self,
        *,
        wait: bool = False,
        cancel_futures: bool = True,
        join_timeout_s: float = 0.0,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            lanes = tuple(self._lanes.values())
        if wait:
            for lane in lanes:
                lane.shutdown(wait=True, cancel_futures=cancel_futures)
            with self._lock:
                self._last_shutdown_survivors = ()
            return
        for lane in lanes:
            lane.shutdown(
                wait=False,
                cancel_futures=cancel_futures,
            )
        deadline = self._monotonic_fn() + max(0.0, float(join_timeout_s))
        survivors = []
        for lane in lanes:
            remaining = max(0.0, deadline - self._monotonic_fn())
            if not lane.await_termination(remaining):
                survivors.append(lane.endpoint_key.safe_label)
        with self._lock:
            self._last_shutdown_survivors = tuple(sorted(survivors))

    @property
    def closed(self) -> bool:
        with self._lock:
            return bool(self._closed)

    def __len__(self) -> int:
        with self._lock:
            return len(self._lanes)

    @property
    def last_shutdown_survivors(self) -> Tuple[str, ...]:
        with self._lock:
            return self._last_shutdown_survivors
