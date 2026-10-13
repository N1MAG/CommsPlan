"""Thread-safe command fences and nonblocking shared-resource reservations.

No Qt, settings, database, or endpoint I/O lives here. A reservation protects
only related radios; a busy shared resource never blocks an unrelated lane.
"""
from __future__ import annotations

from threading import Lock


class CommandSafetyGate:
    def __init__(self) -> None:
        self._lock = Lock()
        self._revision = 0
        self._radio_revisions: dict[int, int] = {}
        self._claims: set[str] = set()

    def invalidate(self, radio_id: int | None = None) -> None:
        with self._lock:
            if radio_id is None:
                self._revision += 1
            else:
                self._radio_revisions[radio_id] = self._radio_revisions.get(radio_id, 0) + 1

    def stamp(self, radio_id: int | None) -> tuple[int, int]:
        with self._lock:
            return self._revision, self._radio_revisions.get(radio_id, 0)

    def current(self, radio_id: int | None, stamp: tuple[int, int]) -> bool:
        return self.stamp(radio_id) == stamp

    def acquire(self, resources: tuple[str, ...]) -> bool:
        with self._lock:
            if self._claims.intersection(resources):
                return False
            self._claims.update(resources)
            return True

    def release(self, resources: tuple[str, ...]) -> None:
        with self._lock:
            self._claims.difference_update(resources)


def checked_ptt(client: object | None) -> bool:
    """Unknown/failed PTT is a hold, never an implied receive state."""
    reader = getattr(client, "get_ptt_checked", None)
    if not callable(reader):
        reader = getattr(client, "get_ptt", None)
    if not callable(reader):
        raise RuntimeError("PTT evidence unavailable")
    value = reader()
    if value is None:
        raise RuntimeError("PTT evidence unavailable")
    return bool(value)
