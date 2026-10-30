from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass
class DependencyHealthState:
    key: str
    owner: str = ""
    consecutive_failures: int = 0
    consecutive_slow: int = 0
    last_success_ts: float = 0.0
    last_failure_ts: float = 0.0
    issue_started_ts: float = 0.0
    last_checked_ts: float = 0.0
    last_duration_ms: float = 0.0
    cooldown_until: float = 0.0
    last_error: str = ""
    metadata: Dict[str, object] = field(default_factory=dict)

    def cooldown_remaining(self, now_ts: Optional[float] = None) -> float:
        now = time.monotonic() if now_ts is None else float(now_ts)
        return max(0.0, float(self.cooldown_until or 0.0) - now)

    def is_degraded(self, now_ts: Optional[float] = None) -> bool:
        return self.cooldown_remaining(now_ts) > 0 or self.consecutive_failures > 0 or self.consecutive_slow > 0

    def snapshot(self) -> Dict[str, object]:
        now = time.monotonic()
        return {
            "key": self.key,
            "owner": self.owner,
            "consecutive_failures": int(self.consecutive_failures),
            "consecutive_slow": int(self.consecutive_slow),
            "last_success_ts": float(self.last_success_ts or 0.0),
            "last_failure_ts": float(self.last_failure_ts or 0.0),
            "issue_started_ts": float(self.issue_started_ts or 0.0),
            "last_checked_ts": float(self.last_checked_ts or 0.0),
            "last_duration_ms": round(float(self.last_duration_ms or 0.0), 3),
            "cooldown_remaining_sec": round(self.cooldown_remaining(now), 3),
            "degraded": bool(self.is_degraded(now)),
            "last_error": self.last_error,
            "metadata": dict(self.metadata),
        }


class DependencyHealthRegistry:
    """
    Process-local health/cooldown tracker for external apps and local resources.

    The registry intentionally stays in memory. It protects the current FIO
    session from repeated slow probes without adding persistence or schema risk.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: Dict[str, DependencyHealthState] = {}

    def _state(self, key: str, owner: str = "") -> DependencyHealthState:
        normalized = str(key or "").strip() or "unknown"
        state = self._states.get(normalized)
        if state is None:
            state = DependencyHealthState(key=normalized, owner=str(owner or ""))
            self._states[normalized] = state
        elif owner and not state.owner:
            state.owner = str(owner)
        return state

    def may_run(self, key: str, *, owner: str = "", force: bool = False) -> Tuple[bool, Dict[str, object]]:
        with self._lock:
            state = self._state(key, owner=owner)
            if force:
                return True, state.snapshot()
            return state.cooldown_remaining() <= 0, state.snapshot()

    def record_success(
        self,
        key: str,
        *,
        owner: str = "",
        duration_ms: float = 0.0,
        slow_ms: float = 1000.0,
        metadata: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        with self._lock:
            now = time.monotonic()
            state = self._state(key, owner=owner)
            state.last_checked_ts = now
            state.last_success_ts = now
            state.last_duration_ms = float(duration_ms or 0.0)
            state.last_error = ""
            state.consecutive_failures = 0
            if metadata:
                state.metadata.update(metadata)
            if duration_ms >= slow_ms:
                if state.consecutive_slow <= 0 and state.issue_started_ts <= 0:
                    state.issue_started_ts = now
                state.consecutive_slow += 1
                if state.consecutive_slow >= 3:
                    state.cooldown_until = max(state.cooldown_until, now + 30.0)
                    state.last_error = f"slow response ({duration_ms:.0f} ms)"
            else:
                state.consecutive_slow = 0
                state.cooldown_until = 0.0
                state.issue_started_ts = 0.0
            return state.snapshot()

    def record_failure(
        self,
        key: str,
        *,
        owner: str = "",
        error: str = "",
        duration_ms: float = 0.0,
        cooldown_sec: Optional[float] = None,
        metadata: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        with self._lock:
            now = time.monotonic()
            state = self._state(key, owner=owner)
            state.last_checked_ts = now
            state.last_failure_ts = now
            state.last_duration_ms = float(duration_ms or 0.0)
            state.last_error = str(error or "failure")
            if state.consecutive_failures <= 0 and state.issue_started_ts <= 0:
                state.issue_started_ts = now
            state.consecutive_failures += 1
            if metadata:
                state.metadata.update(metadata)
            if cooldown_sec is None:
                if state.consecutive_failures >= 6:
                    cooldown_sec = 120.0
                elif state.consecutive_failures >= 3:
                    cooldown_sec = 30.0
                else:
                    cooldown_sec = 0.0
            if cooldown_sec and cooldown_sec > 0:
                state.cooldown_until = max(state.cooldown_until, now + float(cooldown_sec))
            return state.snapshot()

    def discard(self, key: str) -> None:
        """Retire a lifecycle-owned optional source without retaining stale identity."""
        with self._lock:
            self._states.pop(str(key), None)

    def snapshot(self, key: Optional[str] = None) -> Dict[str, object]:
        with self._lock:
            if key is not None:
                return self._state(str(key)).snapshot()
            return {name: state.snapshot() for name, state in sorted(self._states.items())}


_REGISTRY = DependencyHealthRegistry()


def get_dependency_health_registry() -> DependencyHealthRegistry:
    return _REGISTRY
