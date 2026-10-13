from __future__ import annotations

import datetime
import json
import sqlite3
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from freqinout.core.config_paths import get_config_dir
from freqinout.core.logger import log
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor
from freqinout.core.sqlite_utils import connect_sqlite


# Scheduler evidence is operationally useful for several days.  Keep the
# limit high enough for a busy station while still bounding the database work
# and the amount of data presented to diagnostics.
SCHEDULER_EVENT_RETENTION_DAYS = 7
MAX_SCHEDULER_EVENTS = 20_000
REPETITIVE_EVENT_COALESCE_SECONDS = 60

_REPETITIVE_EVENT_TYPES = frozenset({"skip", "status", "hold", "drift"})
_schema_initialized_paths: Set[str] = set()


def _db_path() -> Path:
    cfg = get_config_dir() / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg / "freqinout.db"


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _parse_utc_iso(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _utc_cutoff_iso(now_iso: str, *, seconds: int = 0, days: int = 0) -> str:
    try:
        now = _parse_utc_iso(now_iso)
    except Exception:
        now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(seconds=int(seconds), days=int(days))
    return cutoff.isoformat(timespec="seconds")


def _is_repetitive_event(event_type: str) -> bool:
    event_type_text = str(event_type or "").strip().lower()
    return event_type_text in _REPETITIVE_EVENT_TYPES


def _repetitive_event_where_sql() -> str:
    return "event_type IN ('skip', 'status', 'hold', 'drift')"


def _prune_scheduler_events(conn: sqlite3.Connection, now_iso: str) -> None:
    """Apply age and count limits while protecting durable scheduler evidence."""
    conn.execute(
        "DELETE FROM scheduler_events WHERE ts_utc < ?",
        (_utc_cutoff_iso(now_iso, days=SCHEDULER_EVENT_RETENTION_DAYS),),
    )

    count_row = conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()
    overflow = max(0, int(count_row[0] if count_row else 0) - MAX_SCHEDULER_EVENTS)
    if not overflow:
        return

    # Repeated status/hold/skip/drift evidence is useful for a short window,
    # but must not evict commands, transitions, or failures from the bounded
    # history.  Only if repetitive rows are exhausted do we remove durable
    # rows as a final hard-cap safeguard.
    conn.execute(
        f"""
        DELETE FROM scheduler_events
        WHERE id IN (
            SELECT id FROM scheduler_events
            WHERE {_repetitive_event_where_sql()}
            ORDER BY id ASC
            LIMIT ?
        )
        """,
        (overflow,),
    )

    remaining_row = conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()
    remaining = max(0, int(remaining_row[0] if remaining_row else 0) - MAX_SCHEDULER_EVENTS)
    if remaining:
        conn.execute(
            """
            DELETE FROM scheduler_events
            WHERE id IN (
                SELECT id FROM scheduler_events ORDER BY id ASC LIMIT ?
            )
            """,
            (remaining,),
        )


def _has_recent_repetitive_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    code: str,
    source: str,
    action: str,
    detail: str,
    radio_profile_id: str,
    frequency_hz: Optional[int],
    band: str,
    mode: str,
    vfo: str,
    schedule_key: str,
    metadata_json: str,
    now_iso: str,
) -> bool:
    """Return whether this repetitive signal was recorded in the coalesce window."""
    row = conn.execute(
        """
        SELECT 1
        FROM scheduler_events
        WHERE event_type = ?
          AND code = ?
          AND source = ?
          AND action = ?
          AND detail = ?
          AND radio_profile_id = ?
          AND frequency_hz IS ?
          AND band = ?
          AND mode = ?
          AND vfo = ?
          AND schedule_key = ?
          AND metadata_json = ?
          AND ts_utc >= ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            event_type,
            code,
            source,
            action,
            detail,
            radio_profile_id,
            frequency_hz,
            band,
            mode,
            vfo,
            schedule_key,
            metadata_json,
            _utc_cutoff_iso(now_iso, seconds=REPETITIVE_EVENT_COALESCE_SECONDS),
        ),
    ).fetchone()
    return row is not None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    db_key = str(_db_path())
    if db_key in _schema_initialized_paths:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            event_type TEXT NOT NULL,
            code TEXT NOT NULL,
            source TEXT,
            action TEXT,
            detail TEXT,
            radio_profile_id TEXT,
            frequency_hz INTEGER,
            band TEXT,
            mode TEXT,
            vfo TEXT,
            schedule_key TEXT,
            metadata_json TEXT
        )
        """
    )
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(scheduler_events)").fetchall()}
    if "radio_profile_id" not in cols:
        conn.execute("ALTER TABLE scheduler_events ADD COLUMN radio_profile_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduler_events_ts ON scheduler_events(ts_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduler_events_code ON scheduler_events(code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduler_events_radio_profile_id ON scheduler_events(radio_profile_id)")
    _schema_initialized_paths.add(db_key)


def record_scheduler_event(
    *,
    event_type: str,
    code: str,
    source: str = "",
    action: str = "",
    detail: str = "",
    radio_profile_id: str = "",
    frequency_hz: Optional[int] = None,
    band: str = "",
    mode: str = "",
    vfo: str = "",
    schedule_key: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    timestamp_utc: Optional[str] = None,
) -> None:
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = connect_sqlite(_db_path(), timeout=0.6, busy_timeout_ms=600)
        _ensure_schema(conn)
        event_type_text = str(event_type or "").strip() or "event"
        code_text = str(code or "").strip() or "unknown"
        source_text = str(source or "").strip()
        action_text = str(action or "").strip()
        detail_text = str(detail or "").strip()
        radio_profile_text = str(radio_profile_id or "").strip()
        band_text = str(band or "").strip()
        mode_text = str(mode or "").strip()
        vfo_text = str(vfo or "").strip()
        schedule_key_text = str(schedule_key or "").strip()
        metadata_json = json.dumps(metadata or {}, sort_keys=True)
        now_iso = timestamp_utc or _utc_now_iso()

        if _is_repetitive_event(event_type_text) and _has_recent_repetitive_event(
            conn,
            event_type=event_type_text,
            code=code_text,
            source=source_text,
            action=action_text,
            detail=detail_text,
            radio_profile_id=radio_profile_text,
            frequency_hz=int(frequency_hz) if frequency_hz is not None else None,
            band=band_text,
            mode=mode_text,
            vfo=vfo_text,
            schedule_key=schedule_key_text,
            metadata_json=metadata_json,
            now_iso=now_iso,
        ):
            _prune_scheduler_events(conn, now_iso)
            conn.commit()
            return

        conn.execute(
            """
            INSERT INTO scheduler_events(
                ts_utc, event_type, code, source, action, detail, radio_profile_id, frequency_hz,
                band, mode, vfo, schedule_key, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso,
                event_type_text,
                code_text,
                source_text,
                action_text,
                detail_text,
                radio_profile_text,
                int(frequency_hz) if frequency_hz is not None else None,
                band_text,
                mode_text,
                vfo_text,
                schedule_key_text,
                metadata_json,
            ),
        )
        _prune_scheduler_events(conn, now_iso)
        conn.commit()
    except Exception as exc:
        log.debug("Scheduler event persistence failed: %s", exc)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def load_recent_scheduler_events(limit: int = 25) -> List[Dict[str, Any]]:
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = connect_sqlite(_db_path(), timeout=0.8, row_factory=sqlite3.Row, busy_timeout_ms=800)
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT id, ts_utc, event_type, code, source, action, detail, frequency_hz,
                   radio_profile_id, band, mode, vfo, schedule_key, metadata_json
            FROM scheduler_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(100, int(limit or 25))),),
        ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(str(item.pop("metadata_json", "") or "{}"))
            except Exception:
                item["metadata"] = {}
            events.append(item)
        return events
    except Exception as exc:
        log.debug("Scheduler event load failed: %s", exc)
        return []
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


class AsyncSchedulerEventWriter:
    """Bounded FIFO journal owner; callers never wait for SQLite or its worker."""

    def __init__(self, *, limit: int = 1024, recorder=None):
        self._executor = DaemonSerialExecutor(thread_name_prefix="fio-scheduler-events")
        self._lock = threading.Lock()
        self._pending = deque()
        self._limit = max(1, int(limit))
        self._draining = False
        self._closed = False
        self._recorder = recorder or record_scheduler_event

    def submit(self, **payload) -> bool:
        # Copy nested metadata before returning to the mutable caller.
        payload = dict(payload)
        payload["metadata"] = json.loads(json.dumps(payload.get("metadata") or {}, default=str))
        payload["timestamp_utc"] = payload.get("timestamp_utc") or _utc_now_iso()
        with self._lock:
            if self._closed:
                return False
            if len(self._pending) >= self._limit:
                # Prefer keeping command/failure evidence over repetitive status.
                discard = next((i for i, row in enumerate(self._pending)
                                if _is_repetitive_event(row.get("event_type", ""))), None)
                if discard is None:
                    log.warning("Scheduler event queue full; event not retained: %s", payload.get("code"))
                    return False
                del self._pending[discard]
            self._pending.append(payload)
            if not self._draining:
                self._draining = True
                self._executor.submit(self._drain)
        return True

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._draining = False
                    return
                payload = self._pending.popleft()
            try:
                self._recorder(**payload)
            except Exception as exc:
                log.warning("Scheduler event worker failed: %s", exc)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        # Drain already accepted evidence without blocking Qt shutdown. The
        # daemon worker cannot keep a stuck storage operation holding app exit.
        self._executor.shutdown(wait=False, cancel_futures=False)
