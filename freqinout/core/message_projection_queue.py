"""Durable, coalescing work queue for normalized message projection.

Schema ownership remains in :mod:`message_projection_store`.  These helpers
perform bounded runtime DML only and never parse messages or create schema.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from freqinout.core.message_projection_store import stable_message_id
from freqinout.core.sqlite_utils import connect_sqlite_readonly, connect_sqlite_runtime_write


_SOURCE_TRIGGER_SPECS = {
    "js8_messages": {
        "family": "js8",
        "kind": "js8_message",
        "source_id": (
            "'js8:' || CASE "
            "WHEN COALESCE({row}.source_key,'') != '' THEN "
            "CASE WHEN LOWER({row}.source_key || ' ' || COALESCE({row}.source_path,'')) LIKE '%directed%' THEN 'directed_txt' "
            "WHEN LOWER({row}.source_key || ' ' || COALESCE({row}.source_path,'')) LIKE '%inbox%' "
            "OR LOWER({row}.source_key || ' ' || COALESCE({row}.source_path,'')) LIKE '%.db3%' THEN 'inbox_db' "
            "WHEN LOWER({row}.source_key) LIKE '%api%' OR COALESCE({row}.source_path,'') = '' THEN 'api' ELSE 'file' END "
            "|| ':' || {row}.source_key "
            "WHEN COALESCE({row}.source_path,'') != '' THEN "
            "CASE WHEN LOWER({row}.source_path) LIKE '%directed%' THEN 'directed_txt' "
            "WHEN LOWER({row}.source_path) LIKE '%inbox%' OR LOWER({row}.source_path) LIKE '%.db3%' THEN 'inbox_db' "
            "ELSE 'file' END || ':legacy:' || {row}.source_path "
            "ELSE 'api:' || COALESCE(NULLIF({row}.js8_instance_id,''), 'legacy') END"
        ),
        "key": "CAST(COALESCE({row}.source_id, {row}.id) AS TEXT)",
        "version": "printf('%s:%s:%s:%s', COALESCE({row}.id,0), COALESCE({row}.read_ts,0), COALESCE({row}.state,''), COALESCE({row}.flag_state,0))",
        "required": {"id", "source_key", "source_id", "js8_instance_id", "source_path", "read_ts", "state", "flag_state"},
    },
    "spotter_traffic": {
        "family": "spotter",
        "kind": "spotter_message",
        "source_id": "'spotter:' || COALESCE(NULLIF({row}.js8_instance_id,''), NULLIF(CAST({row}.source_radio_id AS TEXT),''), 'legacy')",
        "key": "CAST({row}.id AS TEXT)",
        "version": "printf('%s:%s:%s:%s', COALESCE({row}.id,0), COALESCE({row}.read_ts,0), COALESCE({row}.state,''), COALESCE({row}.flag_state,0))",
        "required": {"id", "js8_instance_id", "source_radio_id", "read_ts", "state", "flag_state"},
    },
    "varac_messages": {
        "family": "varac",
        "kind": "varac_message",
        "source_id": "'varac:' || COALESCE(NULLIF({row}.ingest_source_key,''), 'legacy') || ':' || COALESCE(NULLIF({row}.source,''), 'varac')",
        "key": "COALESCE(NULLIF({row}.guid,''), NULLIF({row}.vmail_guid,''), CAST({row}.id AS TEXT))",
        "version": "printf('%s:%s:%s:%s', COALESCE({row}.id,0), COALESCE({row}.ts,0), COALESCE({row}.read_status,0), COALESCE({row}.is_deleted,0))",
        "required": {"id", "ingest_source_key", "source", "guid", "vmail_guid", "ts", "read_status", "is_deleted"},
    },
    "sitrep_events": {
        "family": "sitrep",
        "kind": "sitrep_event",
        "source_id": "'sitrep:fused'",
        "key": "COALESCE(NULLIF({row}.report_key,''), CAST({row}.id AS TEXT))",
        "version": "printf('%s:%s:%s', COALESCE({row}.id,0), COALESCE({row}.event_ts,0), COALESCE({row}.updated_ts,0))",
        "required": {"id", "report_key", "event_ts", "updated_ts"},
    },
    "commstat_artifacts": {
        "family": "commstat",
        "kind": "commstat_artifact",
        "source_id": "'commstat:artifacts'",
        "key": "COALESCE(NULLIF({row}.artifact_key,''), CAST({row}.id AS TEXT))",
        "version": "printf('%s:%s:%s', COALESCE({row}.id,0), COALESCE({row}.event_ts,0), COALESCE({row}.updated_ts,0))",
        "required": {"id", "artifact_key", "event_ts", "updated_ts"},
    },
    "commstat_artifact_deletions": {
        "family": "commstat",
        "kind": "commstat_artifact",
        "source_id": "'commstat:artifacts'",
        "key": "CAST({row}.artifact_key AS TEXT)",
        "version": "printf('%s:%s', COALESCE({row}.artifact_key,''), COALESCE({row}.deleted_ts,0))",
        "required": {"artifact_key", "deleted_ts"},
        "operations": {
            "insert": ("NEW", "delete"),
            "update": ("NEW", "delete"),
            "delete": ("OLD", "upsert"),
        },
    },
}


def _text(value: object) -> str:
    raw = str(value or "")
    return raw.encode("utf-8", "replace").decode("utf-8", "replace") if raw else ""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _utc_after(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(0.0, seconds))).isoformat()


def _utc_plus(value: str, seconds: float) -> str:
    try:
        base = dt.datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        if base.tzinfo is None:
            base = base.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return _utc_after(seconds)
    return (base.astimezone(dt.timezone.utc) + dt.timedelta(seconds=max(0.0, seconds))).isoformat()


def _json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except Exception:
        return "{}"


@dataclass(frozen=True)
class DirtyProjectionItem:
    source_id: str
    source_family: str
    external_kind: str
    external_key: str
    operation: str = "upsert"
    priority: int = 0
    source_version: str = ""
    projector_version: int = 0
    dirty_key: str = ""
    first_observed_utc: str = ""
    last_observed_utc: str = ""
    attempt_count: int = 0
    retry_after_utc: str = ""
    last_error_code: str = ""
    lease_owner: str = ""
    lease_expires_utc: str = ""

    @property
    def stable_key(self) -> str:
        return _text(self.dirty_key) or stable_message_id(
            "dirty", self.source_id, self.external_kind, self.external_key
        )


@dataclass(frozen=True)
class SourceProjectionState:
    source_id: str
    source_family: str
    high_water_key: str = ""
    high_water_ts: float = 0.0
    source_generation: str = ""
    projector_version: int = 0
    classifier_version: int = 0
    last_reconciled_utc: str = ""
    last_available_utc: str = ""
    availability_state: str = "unknown"
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    updated_utc: str = ""


def ensure_source_dirty_triggers(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Install additive per-identity dirty triggers during startup migration."""

    queue_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_projection_dirty'"
    ).fetchone()
    if queue_exists is None:
        return ()
    installed: list[str] = []
    stamp = "strftime('%Y-%m-%dT%H:%M:%f+00:00','now')"
    for table, spec in _SOURCE_TRIGGER_SPECS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            continue
        columns = {str(row[1] or "") for row in conn.execute(f"PRAGMA table_info({table})")}
        if not set(spec["required"]).issubset(columns):
            continue
        operations = spec.get("operations") or {
            "insert": ("NEW", "upsert"),
            "update": ("NEW", "upsert"),
            "delete": ("OLD", "delete"),
        }
        for operation, (row_name, dirty_operation) in operations.items():
            key_expr = str(spec["key"]).format(row=row_name)
            source_expr = str(spec["source_id"]).format(row=row_name)
            version_expr = str(spec["version"]).format(row=row_name)
            trigger_name = f"trg_mip_dirty_{table}_{operation}"
            # Ingest workers also call this assurance. Recreating unchanged
            # triggers changes schema_version and can starve concurrent schedule
            # loads even though no schedule input changed.
            definition = f"""CREATE TRIGGER {trigger_name}
                AFTER {operation.upper()} ON {table}
                BEGIN
                    INSERT INTO message_projection_dirty (
                        dirty_key, source_id, source_family, external_kind,
                        external_key, operation, priority, source_version,
                        projector_version, first_observed_utc, last_observed_utc,
                        attempt_count
                    ) VALUES (
                        'trigger:{table}:' || ({source_expr}) || ':' || ({key_expr}),
                        ({source_expr}), '{spec['family']}', '{spec['kind']}',
                        ({key_expr}), '{dirty_operation}', 0, ({version_expr}), 0,
                        {stamp}, {stamp}, 0
                    )
                    ON CONFLICT(source_id, external_kind, external_key) DO UPDATE SET
                        source_family=excluded.source_family,
                        operation=excluded.operation,
                        priority=MAX(message_projection_dirty.priority, excluded.priority),
                        source_version=excluded.source_version,
                        last_observed_utc=excluded.last_observed_utc,
                        retry_after_utc=NULL,
                        last_error_code=NULL,
                        lease_owner=NULL,
                        lease_expires_utc=NULL;
                END"""
            existing = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()
            if existing is None or existing[0] != definition:
                # DDL must replace atomically even on a connection without a
                # caller-owned write transaction. Never expose a missing trigger.
                conn.execute("SAVEPOINT mip_dirty_trigger_upgrade")
                try:
                    conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
                    conn.execute(definition)
                except Exception:
                    conn.execute("ROLLBACK TO mip_dirty_trigger_upgrade")
                    raise
                finally:
                    conn.execute("RELEASE mip_dirty_trigger_upgrade")
            installed.append(trigger_name)
    return tuple(installed)


def enqueue_dirty_conn(
    conn: sqlite3.Connection,
    item: DirtyProjectionItem,
    *,
    observed_utc: str = "",
) -> str:
    """Coalesce one identity to its newest source version in the caller transaction."""

    source_id = _text(item.source_id).strip()
    external_kind = _text(item.external_kind).strip()
    external_key = _text(item.external_key).strip()
    if not source_id or not external_kind or not external_key:
        raise ValueError("dirty source_id, external_kind, and external_key are required")
    dirty_key = item.stable_key
    stamp = _text(observed_utc) or _text(item.last_observed_utc) or _utc_now()
    first = _text(item.first_observed_utc) or stamp
    conn.execute(
        """
        INSERT INTO message_projection_dirty (
            dirty_key, source_id, source_family, external_kind, external_key,
            operation, priority, source_version, projector_version,
            first_observed_utc, last_observed_utc, attempt_count,
            retry_after_utc, last_error_code, lease_owner, lease_expires_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL)
        ON CONFLICT(source_id, external_kind, external_key) DO UPDATE SET
            operation=excluded.operation,
            priority=MAX(message_projection_dirty.priority, excluded.priority),
            source_family=excluded.source_family,
            source_version=excluded.source_version,
            projector_version=excluded.projector_version,
            last_observed_utc=excluded.last_observed_utc,
            retry_after_utc=NULL,
            last_error_code=NULL,
            lease_owner=NULL,
            lease_expires_utc=NULL
        """,
        (
            dirty_key,
            source_id,
            _text(item.source_family).strip().lower(),
            external_kind,
            external_key,
            _text(item.operation).strip().lower() or "upsert",
            int(item.priority or 0),
            _text(item.source_version),
            int(item.projector_version or 0),
            first,
            stamp,
        ),
    )
    row = conn.execute(
        """SELECT dirty_key FROM message_projection_dirty
             WHERE source_id=? AND external_kind=? AND external_key=?""",
        (source_id, external_kind, external_key),
    ).fetchone()
    return _text(row[0] if row else dirty_key)


def enqueue_dirty(db_path: str | Path, item: DirtyProjectionItem) -> str:
    conn = connect_sqlite_runtime_write(db_path, timeout=0.25, busy_timeout_ms=250)
    try:
        with conn:
            return enqueue_dirty_conn(conn, item)
    finally:
        conn.close()


def _item_from_row(row: sqlite3.Row) -> DirtyProjectionItem:
    return DirtyProjectionItem(
        dirty_key=_text(row["dirty_key"]),
        source_id=_text(row["source_id"]),
        source_family=_text(row["source_family"]),
        external_kind=_text(row["external_kind"]),
        external_key=_text(row["external_key"]),
        operation=_text(row["operation"]),
        priority=int(row["priority"] or 0),
        source_version=_text(row["source_version"]),
        projector_version=int(row["projector_version"] or 0),
        first_observed_utc=_text(row["first_observed_utc"]),
        last_observed_utc=_text(row["last_observed_utc"]),
        attempt_count=int(row["attempt_count"] or 0),
        retry_after_utc=_text(row["retry_after_utc"]),
        last_error_code=_text(row["last_error_code"]),
        lease_owner=_text(row["lease_owner"]),
        lease_expires_utc=_text(row["lease_expires_utc"]),
    )


def claim_ready_conn(
    conn: sqlite3.Connection,
    *,
    owner: str,
    limit: int = 100,
    lease_seconds: float = 30.0,
    now_utc: str = "",
) -> tuple[DirtyProjectionItem, ...]:
    clean_owner = _text(owner).strip()
    if not clean_owner:
        raise ValueError("lease owner is required")
    stamp = _text(now_utc) or _utc_now()
    expires = _utc_plus(stamp, lease_seconds)
    bounded = max(1, min(100, int(limit or 100)))
    candidates = conn.execute(
        """
        SELECT dirty_key FROM message_projection_dirty
         WHERE (retry_after_utc IS NULL OR retry_after_utc<=?)
           AND (lease_owner IS NULL OR lease_owner='' OR lease_expires_utc IS NULL OR lease_expires_utc<=?)
         ORDER BY priority DESC, first_observed_utc, dirty_key
         LIMIT ?
        """,
        (stamp, stamp, bounded),
    ).fetchall()
    keys = [_text(row[0]) for row in candidates]
    claimed: list[DirtyProjectionItem] = []
    for key in keys:
        changed = conn.execute(
            """
            UPDATE message_projection_dirty
               SET lease_owner=?, lease_expires_utc=?
             WHERE dirty_key=?
               AND (lease_owner IS NULL OR lease_owner='' OR lease_expires_utc IS NULL OR lease_expires_utc<=?)
            """,
            (clean_owner, expires, key, stamp),
        ).rowcount
        if changed:
            row = conn.execute(
                "SELECT * FROM message_projection_dirty WHERE dirty_key=?",
                (key,),
            ).fetchone()
            if row is not None:
                claimed.append(_item_from_row(row))
    return tuple(claimed)


def claim_ready(
    db_path: str | Path,
    *,
    owner: str,
    limit: int = 100,
    lease_seconds: float = 30.0,
) -> tuple[DirtyProjectionItem, ...]:
    conn = connect_sqlite_runtime_write(db_path, timeout=0.25, row_factory=sqlite3.Row, busy_timeout_ms=250)
    try:
        with conn:
            return claim_ready_conn(conn, owner=owner, limit=limit, lease_seconds=lease_seconds)
    finally:
        conn.close()


def complete_dirty_conn(
    conn: sqlite3.Connection,
    dirty_keys: Sequence[str],
    *,
    owner: str = "",
) -> int:
    keys = tuple(dict.fromkeys(_text(key) for key in dirty_keys if _text(key)))
    count = 0
    for key in keys:
        if owner:
            cur = conn.execute(
                "DELETE FROM message_projection_dirty WHERE dirty_key=? AND lease_owner=?",
                (key, _text(owner)),
            )
        else:
            cur = conn.execute("DELETE FROM message_projection_dirty WHERE dirty_key=?", (key,))
        count += int(cur.rowcount or 0)
    return count


def retry_dirty_conn(
    conn: sqlite3.Connection,
    dirty_keys: Sequence[str],
    *,
    owner: str,
    delay_seconds: float,
    error_code: str,
) -> int:
    retry_at = _utc_after(min(2.0, max(0.05, float(delay_seconds))))
    count = 0
    for key in dict.fromkeys(_text(value) for value in dirty_keys if _text(value)):
        cur = conn.execute(
            """
            UPDATE message_projection_dirty
               SET attempt_count=attempt_count+1, retry_after_utc=?,
                   last_error_code=?, lease_owner=NULL, lease_expires_utc=NULL
             WHERE dirty_key=? AND lease_owner=?
            """,
            (retry_at, _text(error_code)[:80], key, _text(owner)),
        )
        count += int(cur.rowcount or 0)
    return count


def release_owner_leases_conn(conn: sqlite3.Connection, owner: str) -> int:
    cur = conn.execute(
        """UPDATE message_projection_dirty
              SET lease_owner=NULL, lease_expires_utc=NULL
            WHERE lease_owner=?""",
        (_text(owner),),
    )
    return int(cur.rowcount or 0)


def upsert_source_state_conn(conn: sqlite3.Connection, state: SourceProjectionState) -> None:
    stamp = _text(state.updated_utc) or _utc_now()
    conn.execute(
        """
        INSERT INTO message_projection_source_state (
            source_id, source_family, high_water_key, high_water_ts,
            source_generation, projector_version, classifier_version,
            last_reconciled_utc, last_available_utc, availability_state,
            diagnostic_json, updated_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            source_family=excluded.source_family,
            high_water_key=excluded.high_water_key,
            high_water_ts=excluded.high_water_ts,
            source_generation=excluded.source_generation,
            projector_version=excluded.projector_version,
            classifier_version=excluded.classifier_version,
            last_reconciled_utc=excluded.last_reconciled_utc,
            last_available_utc=excluded.last_available_utc,
            availability_state=excluded.availability_state,
            diagnostic_json=excluded.diagnostic_json,
            updated_utc=excluded.updated_utc
        """,
        (
            _text(state.source_id),
            _text(state.source_family).lower(),
            _text(state.high_water_key),
            float(state.high_water_ts or 0.0),
            _text(state.source_generation),
            int(state.projector_version or 0),
            int(state.classifier_version or 0),
            _text(state.last_reconciled_utc),
            _text(state.last_available_utc),
            _text(state.availability_state) or "unknown",
            _json(dict(state.diagnostics)),
            stamp,
        ),
    )


def get_source_state_conn(conn: sqlite3.Connection, source_id: str) -> SourceProjectionState | None:
    row = conn.execute(
        "SELECT * FROM message_projection_source_state WHERE source_id=?",
        (_text(source_id),),
    ).fetchone()
    if row is None:
        return None
    try:
        diagnostics = json.loads(_text(row["diagnostic_json"]) or "{}")
    except Exception:
        diagnostics = {}
    return SourceProjectionState(
        source_id=_text(row["source_id"]),
        source_family=_text(row["source_family"]),
        high_water_key=_text(row["high_water_key"]),
        high_water_ts=float(row["high_water_ts"] or 0.0),
        source_generation=_text(row["source_generation"]),
        projector_version=int(row["projector_version"] or 0),
        classifier_version=int(row["classifier_version"] or 0),
        last_reconciled_utc=_text(row["last_reconciled_utc"]),
        last_available_utc=_text(row["last_available_utc"]),
        availability_state=_text(row["availability_state"]),
        diagnostics=diagnostics if isinstance(diagnostics, dict) else {},
        updated_utc=_text(row["updated_utc"]),
    )


def get_source_state(db_path: str | Path, source_id: str) -> SourceProjectionState | None:
    conn = connect_sqlite_readonly(db_path, row_factory=sqlite3.Row)
    try:
        return get_source_state_conn(conn, source_id)
    finally:
        conn.close()


def queue_diagnostics(db_path: str | Path) -> dict[str, object]:
    """Return bounded cache-safe diagnostics without message identities/content."""

    conn = connect_sqlite_readonly(db_path, row_factory=sqlite3.Row)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS depth, MIN(first_observed_utc) AS oldest,
                   SUM(CASE WHEN COALESCE(lease_owner,'')<>'' THEN 1 ELSE 0 END) AS leased,
                   SUM(CASE WHEN retry_after_utc IS NOT NULL THEN 1 ELSE 0 END) AS delayed
              FROM message_projection_dirty
            """
        ).fetchone()
        return {
            "depth": int(row["depth"] or 0),
            "oldest_utc": _text(row["oldest"]),
            "leased": int(row["leased"] or 0),
            "delayed": int(row["delayed"] or 0),
        }
    finally:
        conn.close()
