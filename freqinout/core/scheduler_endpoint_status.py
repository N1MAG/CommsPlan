"""Bounded, endpoint-scoped status reads for the multi-endpoint scheduler.

This module is deliberately Qt-, database-, settings-, and adapter-free.  It
owns only immutable cached status snapshots and one serialized worker per
normalized endpoint.  Callers supply pollers, consume cached snapshots, and
marshal UI updates in their own layer.

The registry never replaces an executor after a timeout.  A slow endpoint can
therefore make only its own status stale; it cannot create unbounded threads or
block cached reads for any other endpoint.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, replace
import threading
import time
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple

from freqinout.core.scheduler_coordination import (
    EndpointKey,
    FrozenFields,
    bounded_exponential_backoff,
    freeze_fields,
    thaw_fields,
)
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor


StatusPoller = Callable[[], Mapping[str, object]]
StatusCompletion = Callable[["EndpointStatusSnapshot"], None]
ExecutorFactory = Callable[..., object]


def _as_positive_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _as_vfo(value: object) -> Optional[str]:
    token = str(value or "").strip().upper()[:1]
    return token if token in {"A", "B"} else None


def _error_fields(value: object) -> FrozenFields:
    if not isinstance(value, Mapping):
        return ()
    return freeze_fields(
        {
            str(key): str(item or "").strip()
            for key, item in value.items()
            if str(key or "").strip() and str(item or "").strip()
        }
    )


@dataclass(frozen=True)
class EndpointStatusSnapshot:
    """An immutable cached read for exactly one endpoint route."""

    endpoint_key: EndpointKey
    generation: int = 0
    collected_wall_time: float = 0.0
    collected_monotonic: float = 0.0
    frequency_hz: Optional[int] = None
    ptt_active: bool = False
    ptt_known: bool = False
    vfo: Optional[str] = None
    js8_busy: bool = False
    js8_frequency_hz: Optional[int] = None
    js8_offset_hz: Optional[int] = None
    fldigi_available: bool = False
    fldigi_mode: Optional[str] = None
    fldigi_offset_hz: Optional[int] = None
    varac_status_fields: FrozenFields = ()
    error_fields: FrozenFields = ()
    stale: bool = True
    inflight: bool = False
    timed_out: bool = False
    backoff_until_monotonic: float = 0.0
    failure_count: int = 0
    invalidated: bool = False
    closed: bool = False
    source: str = ""
    activity_collected_monotonic: float = 0.0
    activity_known: bool = False
    # Presentation-only history; safety consumers continue to use fields above.
    # Individual observation times prevent partial reads from renewing companions.
    display_observation_fields: FrozenFields = ()

    def __post_init__(self) -> None:
        if int(self.generation) < 0:
            raise ValueError("status generation cannot be negative")
        if float(self.collected_wall_time) < 0.0 or float(self.collected_monotonic) < 0.0:
            raise ValueError("status timestamps cannot be negative")
        if int(self.failure_count) < 0:
            raise ValueError("status failure count cannot be negative")
        object.__setattr__(self, "generation", int(self.generation))
        object.__setattr__(self, "failure_count", int(self.failure_count))
        object.__setattr__(self, "vfo", _as_vfo(self.vfo))
        object.__setattr__(self, "varac_status_fields", tuple(self.varac_status_fields or ()))
        object.__setattr__(self, "error_fields", tuple(self.error_fields or ()))
        object.__setattr__(self, "display_observation_fields", tuple(self.display_observation_fields or ()))
        object.__setattr__(self, "source", str(self.source or "").strip())

    @property
    def varac_status(self) -> Dict[str, object]:
        return thaw_fields(self.varac_status_fields)

    @property
    def errors(self) -> Dict[str, object]:
        return thaw_fields(self.error_fields)

    @property
    def known(self) -> bool:
        return bool(self.generation and not self.stale and not self.error_fields and not self.invalidated)

    def age_seconds(self, *, now_monotonic: Optional[float] = None) -> float:
        if self.collected_monotonic <= 0.0:
            return float("inf")
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        return max(0.0, now - self.collected_monotonic)

    def is_fresh(self, ttl_s: float, *, now_monotonic: Optional[float] = None) -> bool:
        return bool(
            self.known
            and self.age_seconds(now_monotonic=now_monotonic) <= max(0.0, float(ttl_s))
        )

    def as_dict(self) -> Dict[str, object]:
        """Return a plain, detached projection suitable for safety/UI code."""

        return {
            "endpoint_key": self.endpoint_key.canonical,
            "generation": self.generation,
            "collected_wall_time": self.collected_wall_time,
            "collected_monotonic": self.collected_monotonic,
            "frequency_hz": self.frequency_hz,
            "ptt_active": self.ptt_active,
            "ptt_known": self.ptt_known,
            "vfo": self.vfo,
            "js8_busy": self.js8_busy,
            "js8_frequency_hz": self.js8_frequency_hz,
            "js8_offset_hz": self.js8_offset_hz,
            "fldigi_available": self.fldigi_available,
            "fldigi_mode": self.fldigi_mode,
            "fldigi_offset_hz": self.fldigi_offset_hz,
            "varac_status": self.varac_status,
            "errors": self.errors,
            "stale": self.stale,
            "inflight": self.inflight,
            "timed_out": self.timed_out,
            "backoff_until_monotonic": self.backoff_until_monotonic,
            "failure_count": self.failure_count,
            "invalidated": self.invalidated,
            "closed": self.closed,
            "source": self.source,
            "activity_collected_monotonic": self.activity_collected_monotonic,
            "activity_known": self.activity_known,
            "display_observations": thaw_fields(self.display_observation_fields),
        }


@dataclass(frozen=True)
class EndpointStatusMetrics:
    endpoint_count: int = 0
    inflight_count: int = 0
    cache_hits: int = 0
    backoff_hits: int = 0
    singleflight_hits: int = 0
    polls_started: int = 0
    polls_succeeded: int = 0
    polls_failed: int = 0
    polls_timed_out: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "endpoint_count": self.endpoint_count,
            "inflight_count": self.inflight_count,
            "cache_hits": self.cache_hits,
            "backoff_hits": self.backoff_hits,
            "singleflight_hits": self.singleflight_hits,
            "polls_started": self.polls_started,
            "polls_succeeded": self.polls_succeeded,
            "polls_failed": self.polls_failed,
            "polls_timed_out": self.polls_timed_out,
        }


@dataclass(frozen=True)
class EndpointStatusRequest:
    endpoint_key: EndpointKey
    generation: int
    disposition: str
    accepted: bool


class _EndpointStatusLane:
    """Private state owner for one endpoint; all public access is registry based."""

    def __init__(
        self,
        endpoint_key: EndpointKey,
        *,
        executor_factory: ExecutorFactory,
        wall_clock: Callable[[], float],
        monotonic_clock: Callable[[], float],
        base_backoff_s: float,
        max_backoff_s: float,
    ) -> None:
        self.endpoint_key = endpoint_key
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._base_backoff_s = max(0.0, float(base_backoff_s))
        self._max_backoff_s = max(self._base_backoff_s, float(max_backoff_s))
        label = "".join(char if char.isalnum() else "-" for char in endpoint_key.adapter_family)
        self._executor = executor_factory(
            max_workers=1,
            thread_name_prefix=f"freqinout-status-{label or 'endpoint'}",
        )
        self._lock = threading.RLock()
        self._future: Optional[Future] = None
        self._token = 0
        self._started_monotonic = 0.0
        self._timeout_s = 0.0
        self._failure_count = 0
        self._backoff_until = 0.0
        self._closed = False
        self._snapshot = EndpointStatusSnapshot(endpoint_key=endpoint_key)
        self._completion: Optional[StatusCompletion] = None
        self._polls_succeeded = 0
        self._polls_failed = 0

    def await_termination(self, timeout_s: float) -> bool:
        awaiter = getattr(self._executor, "await_termination", None)
        if callable(awaiter):
            return bool(awaiter(max(0.0, float(timeout_s))))
        future = self._future
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

    def cached(self) -> EndpointStatusSnapshot:
        with self._lock:
            return self._snapshot

    def completion_counts(self) -> Tuple[int, int]:
        with self._lock:
            return (self._polls_succeeded, self._polls_failed)

    def refresh(
        self,
        poller: StatusPoller,
        *,
        ttl_s: float,
        timeout_s: float,
        completion: Optional[StatusCompletion] = None,
    ) -> str:
        if not callable(poller):
            raise TypeError("status poller must be callable")
        if float(timeout_s) <= 0.0:
            raise ValueError("status timeout must be positive")
        immediate: Optional[EndpointStatusSnapshot] = None
        with self._lock:
            now = self._monotonic_clock()
            if self._closed:
                self._snapshot = self._replace_snapshot_locked(closed=True, stale=True, inflight=False)
                disposition = "stopped"
            elif self._snapshot.is_fresh(ttl_s, now_monotonic=now):
                immediate = self._snapshot
                disposition = "cached"
            elif self._future is not None and not self._future.done():
                self._snapshot = self._replace_snapshot_locked(stale=True, inflight=True)
                disposition = "singleflight"
            elif now < self._backoff_until:
                self._snapshot = self._replace_snapshot_locked(
                    stale=True,
                    inflight=False,
                    backoff_until_monotonic=self._backoff_until,
                )
                disposition = "backoff"
            else:
                self._completion = completion
                self._start_locked(poller, timeout_s=float(timeout_s))
                disposition = "started"
        if immediate is not None and completion is not None:
            completion(immediate)
        return disposition

    def poll_timeout(self) -> bool:
        """Mark a slow read stale; never replace the serialized worker."""

        completion: Optional[StatusCompletion] = None
        snapshot: Optional[EndpointStatusSnapshot] = None
        with self._lock:
            if self._closed or self._future is None or self._future.done() or self._timeout_s <= 0.0:
                return False
            now = self._monotonic_clock()
            if now - self._started_monotonic < self._timeout_s or self._snapshot.timed_out:
                return False
            self._token += 1  # fence a late completion from the timed-out request.
            self._failure_count += 1
            self._backoff_until = now + self._backoff_for_failure_locked()
            self._snapshot = self._replace_snapshot_locked(
                stale=True,
                inflight=True,
                timed_out=True,
                failure_count=self._failure_count,
                backoff_until_monotonic=self._backoff_until,
                error_fields=freeze_fields({"timeout": "Endpoint status read exceeded its bounded timeout."}),
            )
            completion = self._completion
            self._completion = None
            snapshot = self._snapshot
        if completion is not None and snapshot is not None:
            completion(snapshot)
        return True

    def invalidate(self) -> None:
        with self._lock:
            self._token += 1
            self._completion = None
            inflight = self._future is not None and not self._future.done()
            self._snapshot = self._replace_snapshot_locked(
                stale=True,
                inflight=inflight,
                invalidated=True,
                timed_out=False,
                error_fields=(),
                display_observation_fields=(),
            )

    def publish(
        self,
        raw: Mapping[str, object],
        *,
        generation: Optional[int],
        source: str,
    ) -> EndpointStatusSnapshot:
        """Store a caller-provided fresh read without submitting endpoint I/O."""

        if not isinstance(raw, Mapping):
            raise TypeError("published endpoint status must be a mapping")
        with self._lock:
            if self._closed:
                return self._snapshot
            requested_generation = int(generation) if generation is not None else self._snapshot.generation + 1
            if requested_generation < self._snapshot.generation:
                return self._snapshot
            # A command-lane verifier is authoritative for this generation.
            # Fence a concurrent lower-level status poll so its late completion
            # cannot overwrite the inline endpoint read.
            self._token += 1
            self._completion = None
            # Keep physical I/O serialized, but detach publication ownership.
            # A later refresh queues behind this read instead of coalescing
            # against a completion that can no longer publish fresh status.
            self._future = None
            self._started_monotonic = 0.0
            now_mono = self._monotonic_clock()
            error_fields = _error_fields(raw.get("errors"))
            if error_fields:
                self._polls_failed += 1
                self._failure_count += 1
                self._backoff_until = now_mono + self._backoff_for_failure_locked()
                self._snapshot = self._replace_snapshot_locked(
                    generation=requested_generation,
                    collected_wall_time=self._wall_clock(),
                    collected_monotonic=now_mono,
                    stale=True,
                    inflight=False,
                    timed_out=False,
                    invalidated=False,
                    failure_count=self._failure_count,
                    backoff_until_monotonic=self._backoff_until,
                    error_fields=error_fields,
                    source=source,
                    display_observation_fields=self._display_observations_from_raw_locked(raw, now_mono),
                )
                return self._snapshot
            self._failure_count = 0
            self._backoff_until = 0.0
            self._snapshot = self._snapshot_from_raw_locked(
                raw,
                generation=requested_generation,
                collected_monotonic=now_mono,
                source=source,
            )
            return self._snapshot

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._token += 1
            future = self._future
            self._snapshot = self._replace_snapshot_locked(
                closed=True, stale=True, inflight=False, display_observation_fields=(),
            )
            if cancel_futures and future is not None and not future.done():
                try:
                    future.cancel()
                except Exception:
                    pass
        try:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        except TypeError:
            self._executor.shutdown(wait=wait)

    def _start_locked(self, poller: StatusPoller, *, timeout_s: float) -> None:
        self._token += 1
        token = self._token
        generation = max(self._snapshot.generation, 0) + 1
        started = self._monotonic_clock()
        self._started_monotonic = started
        self._timeout_s = timeout_s
        self._snapshot = self._replace_snapshot_locked(
            generation=generation,
            stale=True,
            inflight=True,
            timed_out=False,
            invalidated=False,
            backoff_until_monotonic=self._backoff_until,
        )

        def _task() -> Mapping[str, object]:
            return dict(poller() or {})

        future = self._executor.submit(_task)
        self._future = future

        def _done(done: Future) -> None:
            self._complete(done, token=token, generation=generation)

        future.add_done_callback(_done)

    def _complete(self, done: Future, *, token: int, generation: int) -> None:
        try:
            raw = dict(done.result() or {})
            error_fields = _error_fields(raw.get("errors"))
        except Exception as exc:
            raw = {}
            error_fields = freeze_fields({"poll": str(exc or "endpoint status poll failed")})
        completion: Optional[StatusCompletion] = None
        snapshot: Optional[EndpointStatusSnapshot] = None
        with self._lock:
            owns_request = self._future is done
            if owns_request:
                self._future = None
            if self._closed:
                return
            if token != self._token:
                if owns_request:
                    # Timeout/invalidation fenced this request, with no newer
                    # owner. Retire its inflight marker without touching a
                    # replacement poll or authoritative publish.
                    self._snapshot = self._replace_snapshot_locked(inflight=False)
                return
            now_mono = self._monotonic_clock()
            if error_fields:
                self._polls_failed += 1
                self._failure_count += 1
                self._backoff_until = now_mono + self._backoff_for_failure_locked()
                self._snapshot = self._replace_snapshot_locked(
                    generation=generation,
                    collected_wall_time=self._wall_clock(),
                    collected_monotonic=now_mono,
                    stale=True,
                    inflight=False,
                    timed_out=False,
                    invalidated=False,
                    failure_count=self._failure_count,
                    backoff_until_monotonic=self._backoff_until,
                    error_fields=error_fields,
                    display_observation_fields=self._display_observations_from_raw_locked(raw, now_mono),
                )
            else:
                self._failure_count = 0
                self._backoff_until = 0.0
                self._polls_succeeded += 1
                self._snapshot = self._snapshot_from_raw_locked(
                    raw,
                    generation=generation,
                    collected_monotonic=now_mono,
                    source="poll",
                )
            completion = self._completion
            self._completion = None
            snapshot = self._snapshot
        if completion is not None and snapshot is not None:
            completion(snapshot)

    @staticmethod
    def _optional_int(value: object) -> Optional[int]:
        if isinstance(value, bool) or value in (None, ""):
            return None
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _snapshot_from_raw_locked(
        self,
        raw: Mapping[str, object],
        *,
        generation: int,
        collected_monotonic: float,
        source: str,
    ) -> EndpointStatusSnapshot:
        previous = self._snapshot
        has_activity = any(key in raw for key in ("js8_busy", "varac_status"))
        return EndpointStatusSnapshot(
            endpoint_key=self.endpoint_key,
            generation=generation,
            collected_wall_time=self._wall_clock(),
            collected_monotonic=collected_monotonic,
            frequency_hz=_as_positive_int(raw.get("frequency_hz")),
            ptt_active=bool(raw.get("ptt_active", False)),
            ptt_known=bool(raw.get("ptt_known", False)),
            vfo=_as_vfo(raw.get("vfo")),
            js8_busy=bool(raw.get("js8_busy", previous.js8_busy)),
            js8_frequency_hz=_as_positive_int(raw.get("js8_frequency_hz")),
            js8_offset_hz=self._optional_int(raw.get("js8_offset_hz")),
            fldigi_available=bool(raw.get("fldigi_available", False)),
            fldigi_mode=str(raw.get("fldigi_mode") or "").strip().upper() or None,
            fldigi_offset_hz=self._optional_int(raw.get("fldigi_offset_hz")),
            varac_status_fields=freeze_fields(
                raw.get("varac_status") if isinstance(raw.get("varac_status"), Mapping) else previous.varac_status
            ),
            error_fields=(),
            stale=False,
            inflight=False,
            timed_out=False,
            backoff_until_monotonic=0.0,
            failure_count=0,
            invalidated=False,
            closed=False,
            source=source,
            activity_collected_monotonic=collected_monotonic if has_activity else previous.activity_collected_monotonic,
            activity_known=bool(raw.get("activity_known", False)) if has_activity else previous.activity_known,
            display_observation_fields=self._display_observations_from_raw_locked(raw, collected_monotonic),
        )

    def _display_observations_from_raw_locked(
        self, raw: Mapping[str, object], collected_monotonic: float,
    ) -> FrozenFields:
        """Keep bounded successful field history without changing safety evidence."""
        observations = thaw_fields(self._snapshot.display_observation_fields)
        errors = raw.get("errors") if isinstance(raw.get("errors"), Mapping) else {}
        fields = (
            "frequency_hz", "ptt_active", "vfo", "js8_frequency_hz",
            "js8_offset_hz", "js8_busy", "fldigi_available", "fldigi_mode",
            "fldigi_offset_hz", "varac_status",
        )
        for key in fields:
            if key not in raw or raw[key] is None or raw[key] == "":
                continue
            if any(str(error).lower() in {"poll", "timeout"} for error in errors):
                continue
            domain = ("fldigi" if key.startswith("fldigi") else "js8" if key.startswith("js8")
                      else "varac" if key == "varac_status" else "rig")
            if any(domain in str(error).lower() for error in errors):
                continue
            if key in {"js8_busy", "varac_status"} and "companion_busy" in errors:
                continue
            if key == "ptt_active" and not raw.get("ptt_known"):
                continue
            if key in {"fldigi_mode", "fldigi_offset_hz"} and not raw.get("fldigi_available"):
                continue
            value = raw[key]
            if key.endswith("frequency_hz"):
                value = _as_positive_int(value)
                if value is None:
                    continue
            if key.endswith("offset_hz"):
                value = self._optional_int(value)
                if value is None:
                    continue
            if key == "varac_status" and not isinstance(value, Mapping):
                continue
            observations[key] = {"value": value, "collected_monotonic": collected_monotonic}
        return freeze_fields(observations)

    def _backoff_for_failure_locked(self) -> float:
        return bounded_exponential_backoff(
            self._base_backoff_s,
            self._max_backoff_s,
            self._failure_count,
        )

    def _replace_snapshot_locked(self, **values: object) -> EndpointStatusSnapshot:
        # Preserve companion fields and their original freshness when changing
        # lane lifecycle flags; a status refresh cannot manufacture new reads.
        return replace(self._snapshot, **values)


class EndpointStatusRegistry:
    """Endpoint-scoped, nonblocking status cache and refresh coordinator."""

    def __init__(
        self,
        *,
        executor_factory: ExecutorFactory = DaemonSerialExecutor,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        default_ttl_s: float = 0.8,
        default_timeout_s: float = 8.0,
        base_backoff_s: float = 4.0,
        max_backoff_s: float = 300.0,
        monotonic_fn: Optional[Callable[[], float]] = None,
        ttl_s: Optional[float] = None,
        retry_s: Optional[float] = None,
    ) -> None:
        if monotonic_fn is not None:
            monotonic_clock = monotonic_fn
        if ttl_s is not None:
            default_ttl_s = float(ttl_s)
        if retry_s is not None:
            base_backoff_s = float(retry_s)
        if float(default_ttl_s) < 0.0:
            raise ValueError("default status TTL cannot be negative")
        if float(default_timeout_s) <= 0.0:
            raise ValueError("default status timeout must be positive")
        self._executor_factory = executor_factory
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self.default_ttl_s = float(default_ttl_s)
        self.default_timeout_s = float(default_timeout_s)
        self._base_backoff_s = max(0.0, float(base_backoff_s))
        self._max_backoff_s = max(self._base_backoff_s, float(max_backoff_s))
        self._lock = threading.RLock()
        self._lanes: Dict[EndpointKey, _EndpointStatusLane] = {}
        self._closed = False
        self._cache_hits = 0
        self._backoff_hits = 0
        self._singleflight_hits = 0
        self._polls_started = 0
        self._polls_timed_out = 0
        self._last_shutdown_survivors: Tuple[str, ...] = ()

    def get_cached(self, endpoint_key: EndpointKey) -> EndpointStatusSnapshot:
        """Return immediately from memory; this method never performs endpoint I/O."""

        with self._lock:
            lane = self._lanes.get(endpoint_key)
        if lane is not None:
            return lane.cached()
        return EndpointStatusSnapshot(endpoint_key=endpoint_key, closed=self._closed)

    def latest(
        self,
        endpoint_key: EndpointKey,
        *,
        stale_after_s: Optional[float] = None,
    ) -> EndpointStatusSnapshot:
        """Read a cached endpoint snapshot, optionally projecting age as stale."""

        snapshot = self.get_cached(endpoint_key)
        if stale_after_s is None or snapshot.stale:
            return snapshot
        if snapshot.age_seconds(now_monotonic=self._monotonic_clock()) > max(0.0, float(stale_after_s)):
            return replace(snapshot, stale=True)
        return snapshot

    def publish(
        self,
        endpoint_key: EndpointKey,
        raw_mapping: Mapping[str, object],
        *,
        generation: Optional[int] = None,
        source: str = "inline",
    ) -> EndpointStatusSnapshot:
        """Publish an inline command/readback result without scheduling I/O."""

        with self._lock:
            if self._closed:
                return EndpointStatusSnapshot(endpoint_key=endpoint_key, closed=True)
            lane = self._lanes.get(endpoint_key)
            if lane is None:
                lane = _EndpointStatusLane(
                    endpoint_key,
                    executor_factory=self._executor_factory,
                    wall_clock=self._wall_clock,
                    monotonic_clock=self._monotonic_clock,
                    base_backoff_s=self._base_backoff_s,
                    max_backoff_s=self._max_backoff_s,
                )
                self._lanes[endpoint_key] = lane
        return lane.publish(raw_mapping, generation=generation, source=source)

    def refresh(
        self,
        endpoint_key: EndpointKey,
        poller: StatusPoller,
        *,
        ttl_s: Optional[float] = None,
        timeout_s: Optional[float] = None,
    ) -> EndpointStatusSnapshot:
        """Request one bounded refresh and immediately return the cached snapshot."""

        with self._lock:
            if self._closed:
                return EndpointStatusSnapshot(endpoint_key=endpoint_key, closed=True)
            lane = self._lanes.get(endpoint_key)
            if lane is None:
                lane = _EndpointStatusLane(
                    endpoint_key,
                    executor_factory=self._executor_factory,
                    wall_clock=self._wall_clock,
                    monotonic_clock=self._monotonic_clock,
                    base_backoff_s=self._base_backoff_s,
                    max_backoff_s=self._max_backoff_s,
                )
                self._lanes[endpoint_key] = lane
        disposition = lane.refresh(
            poller,
            ttl_s=self.default_ttl_s if ttl_s is None else float(ttl_s),
            timeout_s=self.default_timeout_s if timeout_s is None else float(timeout_s),
        )
        with self._lock:
            if disposition == "cached":
                self._cache_hits += 1
            elif disposition == "backoff":
                self._backoff_hits += 1
            elif disposition == "singleflight":
                self._singleflight_hits += 1
            elif disposition == "started":
                self._polls_started += 1
        return lane.cached()

    def request(
        self,
        endpoint_key: EndpointKey,
        *,
        poller: StatusPoller,
        completion: Optional[StatusCompletion] = None,
        ttl_s: Optional[float] = None,
        timeout_s: Optional[float] = None,
    ) -> EndpointStatusRequest:
        """Request a refresh and optionally receive its immutable completion."""

        with self._lock:
            if self._closed:
                return EndpointStatusRequest(endpoint_key, 0, "stopped", False)
            lane = self._lanes.get(endpoint_key)
            if lane is None:
                lane = _EndpointStatusLane(
                    endpoint_key,
                    executor_factory=self._executor_factory,
                    wall_clock=self._wall_clock,
                    monotonic_clock=self._monotonic_clock,
                    base_backoff_s=self._base_backoff_s,
                    max_backoff_s=self._max_backoff_s,
                )
                self._lanes[endpoint_key] = lane
        disposition = lane.refresh(
            poller,
            ttl_s=self.default_ttl_s if ttl_s is None else float(ttl_s),
            timeout_s=self.default_timeout_s if timeout_s is None else float(timeout_s),
            completion=completion,
        )
        with self._lock:
            if disposition == "cached":
                self._cache_hits += 1
            elif disposition == "backoff":
                self._backoff_hits += 1
            elif disposition == "singleflight":
                self._singleflight_hits += 1
            elif disposition == "started":
                self._polls_started += 1
        snapshot = lane.cached()
        return EndpointStatusRequest(
            endpoint_key=endpoint_key,
            generation=snapshot.generation,
            disposition=disposition,
            accepted=disposition in {"started", "cached", "singleflight", "backoff"},
        )

    def poll(self) -> Tuple[EndpointStatusSnapshot, ...]:
        """Advance endpoint timeouts and return only snapshots that changed."""

        with self._lock:
            lanes = tuple(self._lanes.values())
        changed = []
        for lane in lanes:
            if lane.poll_timeout():
                changed.append(lane.cached())
                with self._lock:
                    self._polls_timed_out += 1
        return tuple(changed)

    def invalidate(self, endpoint_key: Optional[EndpointKey] = None) -> bool:
        with self._lock:
            if endpoint_key is None:
                lanes: Iterable[_EndpointStatusLane] = tuple(self._lanes.values())
            else:
                lane = self._lanes.get(endpoint_key)
                lanes = (lane,) if lane is not None else ()
        changed = False
        for lane in lanes:
            lane.invalidate()
            changed = True
        return changed

    def remove(self, endpoint_key: EndpointKey) -> bool:
        """Retire one status lane and fence any late completion from it."""

        with self._lock:
            lane = self._lanes.pop(endpoint_key, None)
        if lane is None:
            return False
        lane.invalidate()
        lane.shutdown(wait=False, cancel_futures=True)
        return True

    def retire_all(self) -> Tuple[str, ...]:
        """Fence and detach all status lanes while keeping the registry open."""

        with self._lock:
            lanes = tuple(self._lanes.values())
            self._lanes.clear()
        active = []
        for lane in lanes:
            snapshot = lane.cached()
            if snapshot.inflight:
                active.append(lane.endpoint_key.safe_label)
            lane.invalidate()
            lane.shutdown(wait=False, cancel_futures=True)
        return tuple(sorted(active))

    def snapshots(self) -> Tuple[EndpointStatusSnapshot, ...]:
        with self._lock:
            lanes = tuple(self._lanes.values())
        return tuple(sorted((lane.cached() for lane in lanes), key=lambda item: item.endpoint_key.canonical))

    def metrics_snapshot(self) -> EndpointStatusMetrics:
        with self._lock:
            lanes = tuple(self._lanes.values())
            completed_successes = sum(lane.completion_counts()[0] for lane in lanes)
            completed_failures = sum(lane.completion_counts()[1] for lane in lanes)
            return EndpointStatusMetrics(
                endpoint_count=len(lanes),
                inflight_count=sum(1 for lane in lanes if lane.cached().inflight),
                cache_hits=self._cache_hits,
                backoff_hits=self._backoff_hits,
                singleflight_hits=self._singleflight_hits,
                polls_started=self._polls_started,
                polls_succeeded=completed_successes,
                polls_failed=completed_failures + self._polls_timed_out,
                polls_timed_out=self._polls_timed_out,
            )

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
        deadline = self._monotonic_clock() + max(0.0, float(join_timeout_s))
        survivors = []
        for lane in lanes:
            remaining = max(0.0, deadline - self._monotonic_clock())
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
