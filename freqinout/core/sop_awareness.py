"""Single-flight publication of SOP reminders; shell reads never start I/O."""
from __future__ import annotations

import copy
import datetime as dt
import time
from typing import Callable
from dataclasses import dataclass, replace

from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor


@dataclass(frozen=True)
class SopAttentionItem:
    profile_id: int
    action_id: int
    group_name: str
    label: str
    status: str
    due_utc: str
    description: str


@dataclass(frozen=True)
class SopAttentionSnapshot:
    state: str = "pending"
    actionable_count: int = 0
    overdue_count: int = 0
    upcoming_count: int = 0
    items: tuple[SopAttentionItem, ...] = ()


def _publication(reader):
    rows = reader()
    seen = set()
    items = []
    actionable = overdue = upcoming = 0
    for row in sorted(rows, key=lambda row: (row.get("status") not in {"Due Now", "Overdue"}, str(row.get("next_due_utc") or ""))):
        status = str(row.get("status") or "")
        if status == "Completed" or row.get("is_completed"):
            continue
        due = str(row.get("next_due_utc") or "")
        try:
            parsed = dt.datetime.fromisoformat(due.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                due = parsed.astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            pass
        key = tuple(str(row.get(k) or "") for k in (
            "profile_id", "action_id", "operating_group", "target_scope",
            "target_device_profile_id", "target_operating_profile_id")) + (due,)
        if key in seen:
            continue
        seen.add(key)
        actionable += int(status in {"Due Now", "Overdue"})
        overdue += int(status == "Overdue")
        upcoming += int(status == "Upcoming")
        if len(items) < 256:
            items.append(SopAttentionItem(int(row.get("profile_id") or 0), int(row.get("action_id") or 0),
                str(row.get("operating_group") or ""), str(row.get("action_label") or row.get("action_key") or "SOP action"),
                status, due, str(row.get("description") or "")[:1200]))
    # Prioritize actionable rows in the bounded display, not just oldest source rows.
    items.sort(key=lambda item: (item.status not in {"Due Now", "Overdue"}, item.due_utc))
    return tuple(dict(row) for row in rows[:256]), SopAttentionSnapshot(
        "partial" if len(seen) > 256 else "fresh", actionable, overdue, upcoming, tuple(items))


class SopAwarenessCache:
    def __init__(self) -> None:
        self._executor = DaemonSerialExecutor(thread_name_prefix="fio-sop-awareness")
        self._future = None
        self._epoch = 0
        self._request_epoch = 0
        self._next_refresh = 0.0
        self._published_at = 0.0
        self._rows: tuple[dict, ...] = ()
        self._attention = SopAttentionSnapshot()
        self._closed = False

    def invalidate(self) -> None:
        self._epoch += 1
        self._next_refresh = 0.0
        self._published_at = 0.0
        self._attention = replace(self._attention, state="pending")

    def refresh(self, reader_factory: Callable[[], Callable[[], list[dict]]], *, now: float | None = None) -> None:
        """Called by a lifecycle timer, never by rendering or selection."""
        if self._closed:
            return
        stamp = time.monotonic() if now is None else now
        if self._future is not None:
            if not self._future.done():
                return
            future, self._future = self._future, None
            try:
                rows, attention = future.result()
            except Exception:
                if self._request_epoch == self._epoch:
                    self._attention = replace(self._attention, state="unavailable")
            else:
                if self._request_epoch == self._epoch:
                    self._rows = rows
                    self._attention = attention
                    self._published_at = stamp
            if self._request_epoch == self._epoch:
                self._next_refresh = stamp + 30.0
        if stamp >= self._next_refresh:
            self._request_epoch = self._epoch
            reader = reader_factory()
            self._future = self._executor.submit(lambda: _publication(reader))

    def rows(self, *, now: float | None = None) -> tuple[dict, ...]:
        stamp = time.monotonic() if now is None else now
        if self._closed or self._attention.state in {"pending", "unavailable"} or not self._published_at or stamp - self._published_at > 65.0:
            return ()
        return tuple(dict(row) for row in self._rows)

    def attention_snapshot(self, *, now: float | None = None) -> SopAttentionSnapshot:
        stamp = time.monotonic() if now is None else now
        if self._closed:
            return replace(self._attention, state="unavailable")
        if self._attention.state != "unavailable" and self._published_at and stamp - self._published_at > 65.0:
            return replace(self._attention, state="stale")
        return self._attention

    def close(self) -> None:
        self._closed = True
        self._epoch += 1
        self._executor.shutdown(wait=False, cancel_futures=True)


def detached_sop_reader(manager: object) -> Callable[[], list[dict]]:
    """Copy cached settings so the reader never uses a Qt-owned DB connection."""
    detached = copy.copy(manager)
    original = manager.settings
    values = copy.deepcopy(getattr(original, "_data", {}))

    class CachedSettings:
        _config_path = getattr(original, "_config_path", None)

        def get(self, key, default=None):
            return values.get(key, default)

    detached.settings = CachedSettings()
    detached._active_hf_conflicts_cache = None
    detached._active_hf_conflicts_cache_monotonic = 0.0
    return lambda: detached.build_upcoming_actions(horizon_hours=12, only_active=True)
