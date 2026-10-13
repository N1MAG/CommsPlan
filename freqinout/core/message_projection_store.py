from __future__ import annotations

import hashlib
import datetime
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from freqinout.core.sqlite_utils import connect_sqlite, connect_sqlite_readonly

PROJECTION_SCHEMA_VERSION = 3
MAX_PROJECTED_MESSAGE_PAGE_SIZE = 200
MAX_PROJECTED_MESSAGE_DETAIL_ROWS = 200


def utc_now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def stable_message_id(*parts: object) -> str:
    text = "|".join(_sanitize_sql_text(part).strip() for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def content_hash(*parts: object) -> str:
    text = "\n".join(_sanitize_sql_text(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MessageSourceRecord:
    source_id: str
    source_family: str
    source_label: str = ""
    radio_id: int | None = None
    app_instance_id: str = ""
    endpoint_or_path: str = ""
    capabilities: Mapping[str, object] = field(default_factory=dict)
    provenance: Mapping[str, object] = field(default_factory=dict)
    enabled: bool = True
    last_seen_utc: str = ""
    last_ingested_utc: str = ""


@dataclass(frozen=True)
class ExternalMessageRef:
    message_id: str
    source_id: str
    external_kind: str
    external_key: str
    external_path: str = ""
    external_mtime: float = 0.0
    external_size: int = 0
    external_hash: str = ""
    delete_capability: str = ""
    read_capability: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageProjectionRecord:
    message_id: str
    canonical_key: str
    content_hash: str
    primary_source_id: str
    source_family: str
    source_label: str = ""
    radio_id: int | None = None
    app_instance_id: str = ""
    message_type: str = ""
    display_type: str = ""
    status: str = "info"
    severity: str = "info"
    read_state: str = "new"
    from_call: str = ""
    to_call: str = ""
    group_name: str = ""
    scope: str = ""
    state_code: str = ""
    grid: str = ""
    lat: float | None = None
    lon: float | None = None
    event_ts: float = 0.0
    received_ts: float = 0.0
    event_utc: str = ""
    received_utc: str = ""
    subject: str = ""
    summary: str = ""
    body_preview: str = ""
    topics: Sequence[str] = field(default_factory=tuple)
    entities: Mapping[str, object] = field(default_factory=dict)
    actionable: bool = False
    operator_attention: bool = False
    confidence: float = 0.0
    recommended_action: str = ""
    intelligence_version: int = 0
    intelligence_utc: str = ""
    intelligence: Mapping[str, object] = field(default_factory=dict)
    pinned: bool = False
    archived: bool = False
    deleted: bool = False
    deleted_utc: str = ""
    inbox_visible: bool = True
    inbox_suppression_reason: str = ""
    classification_version: int = 0
    retention_class: str = "normal"
    search_text: str = ""
    projection_version: int = PROJECTION_SCHEMA_VERSION
    projected_utc: str = ""


@dataclass(frozen=True)
class MessageArtifactRecord:
    artifact_id: str
    message_id: str
    artifact_type: str
    source_id: str = ""
    external_key: str = ""
    path: str = ""
    content_hash: str = ""
    q_id: str = ""
    block_id: str = ""
    transfer_id: str = ""
    block_count: int = 0
    missing_blocks_json: str = "[]"
    transfer_state: str = ""
    signature_state: str = ""
    verified_utc: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageProjectionCheckpoint:
    source_id: str
    last_external_key: str = ""
    last_event_ts: float = 0.0
    content_fingerprint: str = ""
    updated_utc: str = ""


@dataclass(frozen=True)
class MessageProjectionPageCursor:
    """Stable keyset cursor for the bounded Inbox projection order.

    The cursor deliberately carries the complete durable order key instead of
    an offset.  A busy station may receive new traffic while an operator pages;
    an offset would then either repeat or skip rows and force SQLite to walk
    every preceding result.  ``received_ts`` stores the effective received
    timestamp (falling back to ``event_ts`` for legacy rows).  The attention
    fields remain for cursor compatibility but are not part of the default
    newest-received order.  Projection rows own these scalar fields and the
    corresponding model indexes are installed by the startup migration.
    """

    operator_attention: int
    actionable: int
    event_ts: float
    received_ts: float
    message_id: str


@dataclass(frozen=True)
class MessageProjectionPage:
    """One bounded projection page plus its committed invalidation generation."""

    rows: tuple[sqlite3.Row, ...]
    generation: int = 0
    next_cursor: MessageProjectionPageCursor | None = None
    total_count: int | None = None


@dataclass(frozen=True)
class MessageProjectionFocusCounts:
    """Unread Inbox focus counts from one committed projection snapshot."""

    counts: Mapping[str, int]
    generation: int = 0


def _json(value: object, default: str) -> str:
    try:
        return json.dumps(_sanitize_sql_value(value), sort_keys=True, separators=(",", ":"))
    except Exception:
        return default


def _sanitize_sql_text(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


def _sanitize_sql_value(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_sql_text(value)
    if isinstance(value, dict):
        return {_sanitize_sql_text(key): _sanitize_sql_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_sql_value(item) for item in value]
    return value


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    existing = {
        str(row[1] or "")
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def ensure_message_projection_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_sources (
            source_id TEXT PRIMARY KEY,
            source_family TEXT NOT NULL,
            source_label TEXT,
            radio_id INTEGER,
            app_instance_id TEXT,
            endpoint_or_path TEXT,
            capabilities_json TEXT NOT NULL DEFAULT '{}',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            last_seen_utc TEXT,
            last_ingested_utc TEXT,
            updated_utc TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_projection (
            message_id TEXT PRIMARY KEY,
            canonical_key TEXT NOT NULL UNIQUE,
            content_hash TEXT NOT NULL,
            primary_source_id TEXT NOT NULL,
            source_family TEXT NOT NULL,
            source_label TEXT,
            radio_id INTEGER,
            app_instance_id TEXT,
            message_type TEXT,
            display_type TEXT,
            status TEXT,
            severity TEXT,
            read_state TEXT,
            from_call TEXT,
            to_call TEXT,
            group_name TEXT,
            scope TEXT,
            state_code TEXT,
            grid TEXT,
            lat REAL,
            lon REAL,
            event_ts REAL,
            received_ts REAL,
            event_utc TEXT,
            received_utc TEXT,
            subject TEXT,
            summary TEXT,
            body_preview TEXT,
            topics_json TEXT NOT NULL DEFAULT '[]',
            entities_json TEXT NOT NULL DEFAULT '{}',
            actionable INTEGER NOT NULL DEFAULT 0,
            operator_attention INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0,
            recommended_action TEXT,
            intelligence_version INTEGER NOT NULL DEFAULT 0,
            intelligence_utc TEXT,
            intelligence_json TEXT NOT NULL DEFAULT '{}',
            pinned INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            deleted_utc TEXT,
            inbox_visible INTEGER NOT NULL DEFAULT 1,
            inbox_suppression_reason TEXT,
            classification_version INTEGER NOT NULL DEFAULT 0,
            retention_class TEXT NOT NULL DEFAULT 'normal',
            search_text TEXT,
            projection_version INTEGER NOT NULL DEFAULT 1,
            projected_utc TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_external_refs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            external_kind TEXT NOT NULL,
            external_key TEXT NOT NULL,
            external_path TEXT,
            external_mtime REAL,
            external_size INTEGER,
            external_hash TEXT,
            delete_capability TEXT,
            read_capability TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_utc TEXT NOT NULL,
            UNIQUE(source_id, external_kind, external_key)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_artifacts (
            artifact_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            source_id TEXT,
            external_key TEXT,
            path TEXT,
            content_hash TEXT,
            q_id TEXT,
            block_id TEXT,
            transfer_id TEXT,
            block_count INTEGER NOT NULL DEFAULT 0,
            missing_blocks_json TEXT NOT NULL DEFAULT '[]',
            transfer_state TEXT,
            signature_state TEXT,
            verified_utc TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_utc TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_delete_queue (
            delete_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            requested_effect TEXT NOT NULL,
            requested_by TEXT,
            source_scope TEXT NOT NULL DEFAULT 'selected',
            state TEXT NOT NULL DEFAULT 'queued',
            requested_utc TEXT NOT NULL,
            completed_utc TEXT,
            result_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_delete_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delete_id TEXT,
            message_id TEXT NOT NULL,
            source_id TEXT,
            external_kind TEXT,
            external_key TEXT,
            effect TEXT NOT NULL,
            state TEXT NOT NULL,
            detail TEXT,
            audit_utc TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_projection_checkpoint (
            source_id TEXT PRIMARY KEY,
            last_external_key TEXT,
            last_event_ts REAL,
            content_fingerprint TEXT,
            updated_utc TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_projection_dirty (
            dirty_key TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            source_family TEXT NOT NULL,
            external_kind TEXT NOT NULL,
            external_key TEXT NOT NULL,
            operation TEXT NOT NULL DEFAULT 'upsert',
            priority INTEGER NOT NULL DEFAULT 0,
            source_version TEXT,
            projector_version INTEGER NOT NULL DEFAULT 0,
            first_observed_utc TEXT NOT NULL,
            last_observed_utc TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            retry_after_utc TEXT,
            last_error_code TEXT,
            lease_owner TEXT,
            lease_expires_utc TEXT,
            UNIQUE(source_id, external_kind, external_key)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_projection_source_state (
            source_id TEXT PRIMARY KEY,
            source_family TEXT NOT NULL,
            high_water_key TEXT,
            high_water_ts REAL NOT NULL DEFAULT 0,
            source_generation TEXT,
            projector_version INTEGER NOT NULL DEFAULT 0,
            classifier_version INTEGER NOT NULL DEFAULT 0,
            last_reconciled_utc TEXT,
            last_available_utc TEXT,
            availability_state TEXT NOT NULL DEFAULT 'unknown',
            diagnostic_json TEXT NOT NULL DEFAULT '{}',
            updated_utc TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_projection_generation (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            generation INTEGER NOT NULL DEFAULT 0,
            updated_utc TEXT NOT NULL
        )
        """
    )
    # File scanner state is an additive, startup-owned cache.  It records only
    # inventory metadata; source files remain authoritative and are never
    # copied, moved, or deleted by projection maintenance.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_file_scan_cache (
            cache_key TEXT PRIMARY KEY,
            watch_signature TEXT NOT NULL DEFAULT '',
            dir_mtimes_json TEXT NOT NULL DEFAULT '{}',
            inventory_fingerprint TEXT NOT NULL DEFAULT '',
            record_count INTEGER NOT NULL DEFAULT 0,
            updated_utc TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_file_scan_inventory (
            origin TEXT NOT NULL,
            path_key TEXT NOT NULL,
            path_display TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            source_label TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0,
            updated_utc TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(origin, path_key)
        )
        """
    )
    cur.execute(
        """
        INSERT OR IGNORE INTO message_projection_generation(singleton, generation, updated_utc)
        VALUES (1, 0, '')
        """
    )
    _ensure_columns(
        conn,
        "message_sources",
        {
            "source_family": "TEXT NOT NULL DEFAULT 'message'",
            "source_label": "TEXT",
            "radio_id": "INTEGER",
            "app_instance_id": "TEXT",
            "endpoint_or_path": "TEXT",
            "capabilities_json": "TEXT NOT NULL DEFAULT '{}'",
            "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "last_seen_utc": "TEXT",
            "last_ingested_utc": "TEXT",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _ensure_columns(
        conn,
        "message_projection",
        {
            "message_id": "TEXT",
            "canonical_key": "TEXT",
            "content_hash": "TEXT NOT NULL DEFAULT ''",
            "primary_source_id": "TEXT NOT NULL DEFAULT ''",
            "source_family": "TEXT NOT NULL DEFAULT 'message'",
            "source_label": "TEXT",
            "radio_id": "INTEGER",
            "app_instance_id": "TEXT",
            "message_type": "TEXT",
            "display_type": "TEXT",
            "status": "TEXT",
            "severity": "TEXT",
            "read_state": "TEXT",
            "from_call": "TEXT",
            "to_call": "TEXT",
            "group_name": "TEXT",
            "scope": "TEXT",
            "state_code": "TEXT",
            "grid": "TEXT",
            "lat": "REAL",
            "lon": "REAL",
            "event_ts": "REAL",
            "received_ts": "REAL",
            "event_utc": "TEXT",
            "received_utc": "TEXT",
            "subject": "TEXT",
            "summary": "TEXT",
            "body_preview": "TEXT",
            "topics_json": "TEXT NOT NULL DEFAULT '[]'",
            "entities_json": "TEXT NOT NULL DEFAULT '{}'",
            "actionable": "INTEGER NOT NULL DEFAULT 0",
            "operator_attention": "INTEGER NOT NULL DEFAULT 0",
            "confidence": "REAL NOT NULL DEFAULT 0",
            "recommended_action": "TEXT",
            "intelligence_version": "INTEGER NOT NULL DEFAULT 0",
            "intelligence_utc": "TEXT",
            "intelligence_json": "TEXT NOT NULL DEFAULT '{}'",
            "pinned": "INTEGER NOT NULL DEFAULT 0",
            "archived": "INTEGER NOT NULL DEFAULT 0",
            "deleted": "INTEGER NOT NULL DEFAULT 0",
            "deleted_utc": "TEXT",
            "inbox_visible": "INTEGER NOT NULL DEFAULT 1",
            "inbox_suppression_reason": "TEXT",
            "classification_version": "INTEGER NOT NULL DEFAULT 0",
            "retention_class": "TEXT NOT NULL DEFAULT 'normal'",
            "search_text": "TEXT",
            "projection_version": "INTEGER NOT NULL DEFAULT 1",
            "projected_utc": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _ensure_columns(
        conn,
        "message_external_refs",
        {
            "message_id": "TEXT NOT NULL DEFAULT ''",
            "source_id": "TEXT NOT NULL DEFAULT ''",
            "external_kind": "TEXT NOT NULL DEFAULT ''",
            "external_key": "TEXT NOT NULL DEFAULT ''",
            "external_path": "TEXT",
            "external_mtime": "REAL",
            "external_size": "INTEGER",
            "external_hash": "TEXT",
            "delete_capability": "TEXT",
            "read_capability": "TEXT",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _ensure_columns(
        conn,
        "message_artifacts",
        {
            "message_id": "TEXT NOT NULL DEFAULT ''",
            "artifact_type": "TEXT NOT NULL DEFAULT ''",
            "source_id": "TEXT",
            "external_key": "TEXT",
            "path": "TEXT",
            "content_hash": "TEXT",
            "q_id": "TEXT",
            "block_id": "TEXT",
            "transfer_id": "TEXT",
            "block_count": "INTEGER NOT NULL DEFAULT 0",
            "missing_blocks_json": "TEXT NOT NULL DEFAULT '[]'",
            "transfer_state": "TEXT",
            "signature_state": "TEXT",
            "verified_utc": "TEXT",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _ensure_columns(
        conn,
        "message_delete_queue",
        {
            "message_id": "TEXT NOT NULL DEFAULT ''",
            "requested_effect": "TEXT NOT NULL DEFAULT 'delete'",
            "requested_by": "TEXT",
            "source_scope": "TEXT NOT NULL DEFAULT 'selected'",
            "state": "TEXT NOT NULL DEFAULT 'queued'",
            "requested_utc": "TEXT NOT NULL DEFAULT ''",
            "completed_utc": "TEXT",
            "result_json": "TEXT NOT NULL DEFAULT '{}'",
        },
    )
    _ensure_columns(
        conn,
        "message_delete_audit",
        {
            "delete_id": "TEXT",
            "message_id": "TEXT NOT NULL DEFAULT ''",
            "source_id": "TEXT",
            "external_kind": "TEXT",
            "external_key": "TEXT",
            "effect": "TEXT NOT NULL DEFAULT 'delete'",
            "state": "TEXT NOT NULL DEFAULT ''",
            "detail": "TEXT",
            "audit_utc": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _ensure_columns(
        conn,
        "message_projection_checkpoint",
        {
            "last_external_key": "TEXT",
            "last_event_ts": "REAL",
            "content_fingerprint": "TEXT",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _ensure_columns(
        conn,
        "message_projection_dirty",
        {
            "source_id": "TEXT NOT NULL DEFAULT ''",
            "source_family": "TEXT NOT NULL DEFAULT ''",
            "external_kind": "TEXT NOT NULL DEFAULT ''",
            "external_key": "TEXT NOT NULL DEFAULT ''",
            "operation": "TEXT NOT NULL DEFAULT 'upsert'",
            "priority": "INTEGER NOT NULL DEFAULT 0",
            "source_version": "TEXT",
            "projector_version": "INTEGER NOT NULL DEFAULT 0",
            "first_observed_utc": "TEXT NOT NULL DEFAULT ''",
            "last_observed_utc": "TEXT NOT NULL DEFAULT ''",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "retry_after_utc": "TEXT",
            "last_error_code": "TEXT",
            "lease_owner": "TEXT",
            "lease_expires_utc": "TEXT",
        },
    )
    _ensure_columns(
        conn,
        "message_projection_source_state",
        {
            "source_family": "TEXT NOT NULL DEFAULT ''",
            "high_water_key": "TEXT",
            "high_water_ts": "REAL NOT NULL DEFAULT 0",
            "source_generation": "TEXT",
            "projector_version": "INTEGER NOT NULL DEFAULT 0",
            "classifier_version": "INTEGER NOT NULL DEFAULT 0",
            "last_reconciled_utc": "TEXT",
            "last_available_utc": "TEXT",
            "availability_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "diagnostic_json": "TEXT NOT NULL DEFAULT '{}'",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _ensure_columns(
        conn,
        "message_file_scan_cache",
        {
            "watch_signature": "TEXT NOT NULL DEFAULT ''",
            "dir_mtimes_json": "TEXT NOT NULL DEFAULT '{}'",
            "inventory_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "record_count": "INTEGER NOT NULL DEFAULT 0",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _ensure_columns(
        conn,
        "message_file_scan_inventory",
        {
            "path_display": "TEXT NOT NULL DEFAULT ''",
            "source_id": "TEXT NOT NULL DEFAULT ''",
            "source_label": "TEXT NOT NULL DEFAULT ''",
            "size": "INTEGER NOT NULL DEFAULT 0",
            "mtime": "REAL NOT NULL DEFAULT 0",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_projection_default ON message_projection(deleted, archived, event_ts DESC, received_ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_projection_inbox ON message_projection(inbox_visible, deleted, archived, operator_attention DESC, actionable DESC, event_ts DESC, received_ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_projection_source ON message_projection(source_family, event_ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_projection_group ON message_projection(group_name, event_ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_projection_status ON message_projection(status, severity, event_ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_projection_attention ON message_projection(operator_attention, actionable, event_ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_projection_calls ON message_projection(from_call, to_call, event_ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_projection_geo ON message_projection(state_code, grid, event_ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_projection_search ON message_projection(search_text)")
    # Bounded Inbox pages use a keyset cursor ordered by the following durable
    # columns.  Keep the broad default and the common equality filters in
    # separate additive indexes so a view change does not turn into a retained
    # history scan.  Search is intentionally left as a semantic substring
    # predicate here; a future FTS migration must preserve its existing search
    # grammar rather than silently changing it to a prefix-only lookup.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_default "
        "ON message_projection(inbox_visible, deleted, archived, "
        "operator_attention DESC, actionable DESC, event_ts DESC, "
        "received_ts DESC, message_id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_source "
        "ON message_projection(source_family, inbox_visible, deleted, archived, "
        "operator_attention DESC, actionable DESC, event_ts DESC, "
        "received_ts DESC, message_id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_group "
        "ON message_projection(group_name, inbox_visible, deleted, archived, "
        "operator_attention DESC, actionable DESC, event_ts DESC, "
        "received_ts DESC, message_id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_status "
        "ON message_projection(status, inbox_visible, deleted, archived, "
        "operator_attention DESC, actionable DESC, event_ts DESC, "
        "received_ts DESC, message_id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_severity "
        "ON message_projection(severity, inbox_visible, deleted, archived, "
        "operator_attention DESC, actionable DESC, event_ts DESC, "
        "received_ts DESC, message_id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_received "
        "ON message_projection(inbox_visible, deleted, archived, "
        "COALESCE(NULLIF(received_ts, 0), event_ts, 0) DESC, message_id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_received_v2 "
        "ON message_projection(inbox_visible, deleted, archived, "
        "COALESCE(NULLIF(received_ts, 0), event_ts, 0) DESC, "
        "event_ts DESC, message_id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_source_received_v2 "
        "ON message_projection(source_family, inbox_visible, deleted, archived, "
        "COALESCE(NULLIF(received_ts, 0), event_ts, 0) DESC, "
        "event_ts DESC, message_id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_from "
        "ON message_projection(from_call, inbox_visible, deleted, archived, "
        "operator_attention DESC, actionable DESC, event_ts DESC, received_ts DESC, message_id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_to "
        "ON message_projection(to_call, inbox_visible, deleted, archived, "
        "operator_attention DESC, actionable DESC, event_ts DESC, received_ts DESC, message_id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_projection_model_type "
        "ON message_projection(message_type, inbox_visible, deleted, archived, "
        "operator_attention DESC, actionable DESC, event_ts DESC, received_ts DESC, message_id DESC)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_refs_message ON message_external_refs(message_id)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_refs_file_identity "
        "ON message_external_refs(external_kind, external_path, external_mtime, external_size, message_id)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_artifacts_message ON message_artifacts(message_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_artifacts_flamp_qid ON message_artifacts(q_id, transfer_state)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_delete_queue_state ON message_delete_queue(state, requested_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_delete_audit_message ON message_delete_audit(message_id, audit_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_dirty_ready ON message_projection_dirty(retry_after_utc, priority DESC, first_observed_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_dirty_source ON message_projection_dirty(source_family, source_id, last_observed_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_file_inventory_updated ON message_file_scan_inventory(origin, updated_utc)")
    # Ops focus is part of the projection schema lifecycle. Runtime row helpers
    # assume startup (or an explicit compatibility boundary) completed this
    # migration and therefore never introspect or execute DDL per message.
    from freqinout.core.ops_focus import ensure_ops_focus_schema

    ensure_ops_focus_schema(conn)
def upsert_message_source(conn: sqlite3.Connection, source: MessageSourceRecord, *, updated_utc: str | None = None) -> str:
    stamp = updated_utc or utc_now_iso()
    conn.execute(
        """
        INSERT INTO message_sources (
            source_id, source_family, source_label, radio_id, app_instance_id,
            endpoint_or_path, capabilities_json, provenance_json, enabled,
            last_seen_utc, last_ingested_utc, updated_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            source_family=excluded.source_family,
            source_label=excluded.source_label,
            radio_id=excluded.radio_id,
            app_instance_id=excluded.app_instance_id,
            endpoint_or_path=excluded.endpoint_or_path,
            capabilities_json=excluded.capabilities_json,
            provenance_json=excluded.provenance_json,
            enabled=excluded.enabled,
            last_seen_utc=excluded.last_seen_utc,
            last_ingested_utc=excluded.last_ingested_utc,
            updated_utc=excluded.updated_utc
        """,
        (
            _sanitize_sql_text(source.source_id),
            _sanitize_sql_text(source.source_family),
            _sanitize_sql_text(source.source_label),
            source.radio_id,
            _sanitize_sql_text(source.app_instance_id),
            _sanitize_sql_text(source.endpoint_or_path),
            _json(source.capabilities, "{}"),
            _json(source.provenance, "{}"),
            1 if source.enabled else 0,
            _sanitize_sql_text(source.last_seen_utc),
            _sanitize_sql_text(source.last_ingested_utc),
            _sanitize_sql_text(stamp),
        ),
    )
    return source.source_id


def upsert_message_projection(conn: sqlite3.Connection, message: MessageProjectionRecord, *, projected_utc: str | None = None) -> str:
    stamp = projected_utc or message.projected_utc or utc_now_iso()
    conn.execute(
        """
        INSERT INTO message_projection (
            message_id, canonical_key, content_hash, primary_source_id, source_family, source_label,
            radio_id, app_instance_id, message_type, display_type, status, severity, read_state,
            from_call, to_call, group_name, scope, state_code, grid, lat, lon, event_ts, received_ts,
            event_utc, received_utc, subject, summary, body_preview, topics_json, entities_json,
            actionable, operator_attention, confidence, recommended_action, intelligence_version,
            intelligence_utc, intelligence_json, pinned, archived, deleted, deleted_utc, retention_class,
            inbox_visible, inbox_suppression_reason, classification_version,
            search_text, projection_version, projected_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            canonical_key=excluded.canonical_key,
            content_hash=excluded.content_hash,
            primary_source_id=excluded.primary_source_id,
            source_family=excluded.source_family,
            source_label=excluded.source_label,
            radio_id=excluded.radio_id,
            app_instance_id=excluded.app_instance_id,
            message_type=excluded.message_type,
            display_type=excluded.display_type,
            status=CASE
                WHEN LOWER(COALESCE(message_projection.read_state, ''))='read'
                     AND UPPER(COALESCE(excluded.status, '')) IN ('NEW', 'UNREAD')
                THEN message_projection.status
                ELSE excluded.status
            END,
            severity=excluded.severity,
            read_state=CASE
                WHEN LOWER(COALESCE(message_projection.read_state, ''))='read'
                     AND LOWER(COALESCE(excluded.read_state, '')) IN ('new', 'unread', 'info', '')
                THEN message_projection.read_state
                ELSE excluded.read_state
            END,
            from_call=excluded.from_call,
            to_call=excluded.to_call,
            group_name=excluded.group_name,
            scope=excluded.scope,
            state_code=excluded.state_code,
            grid=excluded.grid,
            lat=excluded.lat,
            lon=excluded.lon,
            event_ts=excluded.event_ts,
            received_ts=excluded.received_ts,
            event_utc=excluded.event_utc,
            received_utc=excluded.received_utc,
            subject=excluded.subject,
            summary=excluded.summary,
            body_preview=excluded.body_preview,
            topics_json=excluded.topics_json,
            entities_json=excluded.entities_json,
            actionable=excluded.actionable,
            operator_attention=excluded.operator_attention,
            confidence=excluded.confidence,
            recommended_action=excluded.recommended_action,
            intelligence_version=excluded.intelligence_version,
            intelligence_utc=excluded.intelligence_utc,
            intelligence_json=excluded.intelligence_json,
            pinned=CASE WHEN message_projection.pinned=1 THEN 1 ELSE excluded.pinned END,
            archived=CASE WHEN message_projection.archived=1 THEN 1 ELSE excluded.archived END,
            deleted=CASE WHEN message_projection.deleted=1 THEN 1 ELSE excluded.deleted END,
            deleted_utc=CASE
                WHEN message_projection.deleted=1 AND COALESCE(message_projection.deleted_utc, '') != '' THEN message_projection.deleted_utc
                ELSE excluded.deleted_utc
            END,
            retention_class=excluded.retention_class,
            inbox_visible=excluded.inbox_visible,
            inbox_suppression_reason=excluded.inbox_suppression_reason,
            classification_version=excluded.classification_version,
            search_text=excluded.search_text,
            projection_version=excluded.projection_version,
            projected_utc=excluded.projected_utc
        """,
        _message_values(message, stamp),
    )
    # Keep the compact Ops entity index current without making the UI parse or
    # scan retained message bodies. Import lazily to avoid a projection-module
    # dependency cycle at startup.
    from freqinout.core.ops_focus import index_message_for_ops_focus

    # The UPSERT intentionally preserves operator-owned state (read, pinned,
    # archived, and deleted) when an upstream replay reports stale defaults.
    # Index the durable row, not the incoming dataclass, so compact Ops focus
    # counts and attention state cannot disagree with the Inbox projection.
    cursor = conn.execute(
        "SELECT * FROM message_projection WHERE message_id=?",
        (_sanitize_sql_text(message.message_id),),
    )
    persisted = cursor.fetchone()
    if persisted is not None:
        if not hasattr(persisted, "keys"):
            columns = tuple(str(column[0]) for column in (cursor.description or ()))
            persisted = dict(zip(columns, persisted))
        index_message_for_ops_focus(conn, persisted)
    return message.message_id


def upsert_external_ref(conn: sqlite3.Connection, ref: ExternalMessageRef, *, updated_utc: str | None = None) -> str:
    stamp = updated_utc or utc_now_iso()
    previous = conn.execute(
        """
        SELECT message_id FROM message_external_refs
         WHERE source_id=? AND external_kind=? AND external_key=?
        """,
        (
            _sanitize_sql_text(ref.source_id),
            _sanitize_sql_text(ref.external_kind),
            _sanitize_sql_text(ref.external_key),
        ),
    ).fetchone()
    previous_message_id = _sanitize_sql_text(previous[0]) if previous is not None else ""
    conn.execute(
        """
        INSERT INTO message_external_refs (
            message_id, source_id, external_kind, external_key, external_path,
            external_mtime, external_size, external_hash, delete_capability,
            read_capability, metadata_json, updated_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, external_kind, external_key) DO UPDATE SET
            message_id=excluded.message_id,
            external_path=excluded.external_path,
            external_mtime=excluded.external_mtime,
            external_size=excluded.external_size,
            external_hash=excluded.external_hash,
            delete_capability=excluded.delete_capability,
            read_capability=excluded.read_capability,
            metadata_json=excluded.metadata_json,
            updated_utc=excluded.updated_utc
        """,
        (
            _sanitize_sql_text(ref.message_id),
            _sanitize_sql_text(ref.source_id),
            _sanitize_sql_text(ref.external_kind),
            _sanitize_sql_text(ref.external_key),
            _sanitize_sql_text(ref.external_path),
            float(ref.external_mtime or 0.0),
            int(ref.external_size or 0),
            _sanitize_sql_text(ref.external_hash),
            _sanitize_sql_text(ref.delete_capability),
            _sanitize_sql_text(ref.read_capability),
            _json(ref.metadata, "{}"),
            _sanitize_sql_text(stamp),
        ),
    )
    if previous_message_id and previous_message_id != _sanitize_sql_text(ref.message_id):
        _merge_relinked_projection(
            conn,
            previous_message_id=previous_message_id,
            message_id=_sanitize_sql_text(ref.message_id),
            source_id=_sanitize_sql_text(ref.source_id),
            external_key=_sanitize_sql_text(ref.external_key),
        )
    return ref.message_id


def _merge_relinked_projection(
    conn: sqlite3.Connection,
    *,
    previous_message_id: str,
    message_id: str,
    source_id: str,
    external_key: str,
) -> None:
    """Merge state and remove an orphan left by canonical-id correction.

    Re-keying never deletes a source receipt: the unique external reference has
    already moved to ``message_id``.  Only the superseded presentation row is
    removed once no references remain attached to it.
    """

    if not previous_message_id or not message_id or previous_message_id == message_id:
        return
    prior = conn.execute(
        """
        SELECT read_state, status, pinned, archived, deleted, deleted_utc
          FROM message_projection WHERE message_id=?
        """,
        (previous_message_id,),
    ).fetchone()
    current = conn.execute(
        "SELECT 1 FROM message_projection WHERE message_id=?",
        (message_id,),
    ).fetchone()
    if prior is not None and current is not None:
        conn.execute(
            """
            UPDATE message_projection
               SET read_state=CASE
                       WHEN LOWER(COALESCE(?,''))='read' THEN 'read'
                       ELSE read_state END,
                   status=CASE
                       WHEN LOWER(COALESCE(?,''))='read'
                            AND UPPER(COALESCE(status,'')) IN ('NEW','UNREAD')
                       THEN COALESCE(NULLIF(?,''),'READ') ELSE status END,
                   pinned=CASE WHEN COALESCE(?,0)=1 THEN 1 ELSE pinned END,
                   archived=CASE WHEN COALESCE(?,0)=1 THEN 1 ELSE archived END,
                   deleted=CASE WHEN COALESCE(?,0)=1 THEN 1 ELSE deleted END,
                   deleted_utc=CASE
                       WHEN COALESCE(?,0)=1 AND COALESCE(?, '') != '' THEN ?
                       ELSE deleted_utc END
             WHERE message_id=?
            """,
            (
                prior[0], prior[0], prior[1], prior[2], prior[3], prior[4],
                prior[4], prior[5], prior[5], message_id,
            ),
        )
    # Artifacts tied to this exact receipt follow its new canonical message.
    conn.execute(
        """
        UPDATE message_artifacts SET message_id=?
         WHERE message_id=? AND COALESCE(source_id,'')=? AND COALESCE(external_key,'')=?
        """,
        (message_id, previous_message_id, source_id, external_key),
    )
    remaining = conn.execute(
        "SELECT 1 FROM message_external_refs WHERE message_id=? LIMIT 1",
        (previous_message_id,),
    ).fetchone()
    if remaining is not None:
        return
    # The old id is now presentation-only debris.  Move dependent active state
    # and retained artifacts before removing it; audit rows remain immutable.
    conn.execute(
        "UPDATE message_artifacts SET message_id=? WHERE message_id=?",
        (message_id, previous_message_id),
    )
    conn.execute(
        "UPDATE message_delete_queue SET message_id=? WHERE message_id=? AND state IN ('queued','running')",
        (message_id, previous_message_id),
    )
    if _table_exists(conn, "fio_spotter_watch_matches"):
        duplicate_watch_ids = [
            int(row[0])
            for row in conn.execute(
                """
                SELECT old.watch_id
                  FROM fio_spotter_watch_matches old
                 WHERE old.message_id=?
                   AND EXISTS (
                       SELECT 1 FROM fio_spotter_watch_matches current
                        WHERE current.watch_id=old.watch_id AND current.message_id=?
                   )
                """,
                (previous_message_id, message_id),
            ).fetchall()
        ]
        conn.execute(
            """
            INSERT OR IGNORE INTO fio_spotter_watch_matches(watch_id, message_id, matched_ts)
            SELECT watch_id, ?, matched_ts FROM fio_spotter_watch_matches WHERE message_id=?
            """,
            (message_id, previous_message_id),
        )
        conn.execute(
            "DELETE FROM fio_spotter_watch_matches WHERE message_id=?",
            (previous_message_id,),
        )
        for watch_id in duplicate_watch_ids:
            conn.execute(
                """
                UPDATE fio_spotter_watches
                   SET match_count=MAX(0, COALESCE(match_count,0)-1)
                 WHERE id=?
                """,
                (watch_id,),
            )
    from freqinout.core.ops_focus import (
        index_message_for_ops_focus,
        remove_message_from_ops_focus,
    )

    remove_message_from_ops_focus(conn, previous_message_id)
    conn.execute("DELETE FROM message_projection WHERE message_id=?", (previous_message_id,))
    refreshed = conn.execute(
        "SELECT * FROM message_projection WHERE message_id=?", (message_id,)
    ).fetchone()
    if refreshed is not None:
        index_message_for_ops_focus(conn, refreshed)


def merge_message_projections(
    conn: sqlite3.Connection,
    *,
    target_message_id: str,
    duplicate_message_id: str,
) -> bool:
    """Converge one proven duplicate presentation without losing receipts.

    This is a derived-index repair seam.  Native rows and files remain
    untouched; every external reference and artifact is rehomed to the chosen
    canonical presentation before the superseded row is removed.  The shared
    relink helper preserves operator state, active delete work, Spotter watch
    state, and compact Ops indexes.
    """

    target = _sanitize_sql_text(target_message_id)
    duplicate = _sanitize_sql_text(duplicate_message_id)
    if not target or not duplicate or target == duplicate:
        return False
    target_exists = conn.execute(
        "SELECT 1 FROM message_projection WHERE message_id=?", (target,)
    ).fetchone()
    duplicate_exists = conn.execute(
        "SELECT 1 FROM message_projection WHERE message_id=?", (duplicate,)
    ).fetchone()
    if target_exists is None or duplicate_exists is None:
        return False
    conn.execute(
        "UPDATE message_external_refs SET message_id=? WHERE message_id=?",
        (target, duplicate),
    )
    conn.execute(
        "UPDATE message_artifacts SET message_id=? WHERE message_id=?",
        (target, duplicate),
    )
    _merge_relinked_projection(
        conn,
        previous_message_id=duplicate,
        message_id=target,
        source_id="",
        external_key="",
    )
    return (
        conn.execute(
            "SELECT 1 FROM message_projection WHERE message_id=?", (duplicate,)
        ).fetchone()
        is None
    )


def upsert_message_artifact(conn: sqlite3.Connection, artifact: MessageArtifactRecord, *, updated_utc: str | None = None) -> str:
    stamp = updated_utc or utc_now_iso()
    conn.execute(
        """
        INSERT INTO message_artifacts (
            artifact_id, message_id, artifact_type, source_id, external_key, path,
            content_hash, q_id, block_id, transfer_id, block_count, missing_blocks_json,
            transfer_state, signature_state, verified_utc, metadata_json, updated_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(artifact_id) DO UPDATE SET
            message_id=excluded.message_id,
            artifact_type=excluded.artifact_type,
            source_id=excluded.source_id,
            external_key=excluded.external_key,
            path=excluded.path,
            content_hash=excluded.content_hash,
            q_id=excluded.q_id,
            block_id=excluded.block_id,
            transfer_id=excluded.transfer_id,
            block_count=excluded.block_count,
            missing_blocks_json=excluded.missing_blocks_json,
            transfer_state=excluded.transfer_state,
            signature_state=excluded.signature_state,
            verified_utc=excluded.verified_utc,
            metadata_json=excluded.metadata_json,
            updated_utc=excluded.updated_utc
        """,
        (
            _sanitize_sql_text(artifact.artifact_id),
            _sanitize_sql_text(artifact.message_id),
            _sanitize_sql_text(artifact.artifact_type),
            _sanitize_sql_text(artifact.source_id),
            _sanitize_sql_text(artifact.external_key),
            _sanitize_sql_text(artifact.path),
            _sanitize_sql_text(artifact.content_hash),
            _sanitize_sql_text(artifact.q_id),
            _sanitize_sql_text(artifact.block_id),
            _sanitize_sql_text(artifact.transfer_id),
            int(artifact.block_count or 0),
            _sanitize_sql_text(artifact.missing_blocks_json),
            _sanitize_sql_text(artifact.transfer_state),
            _sanitize_sql_text(artifact.signature_state),
            _sanitize_sql_text(artifact.verified_utc),
            _json(artifact.metadata, "{}"),
            _sanitize_sql_text(stamp),
        ),
    )
    return artifact.artifact_id


def queue_message_delete(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    requested_effect: str,
    requested_by: str = "",
    source_scope: str = "selected",
    requested_utc: str | None = None,
) -> str:
    stamp = requested_utc or utc_now_iso()
    delete_id = stable_message_id("delete", message_id, requested_effect, source_scope, stamp)
    conn.execute(
        """
        INSERT INTO message_delete_queue (
            delete_id, message_id, requested_effect, requested_by, source_scope,
            state, requested_utc, result_json
        )
        VALUES (?, ?, ?, ?, ?, 'queued', ?, '{}')
        """,
        (delete_id, message_id, requested_effect, requested_by, source_scope, stamp),
    )
    conn.execute(
        """
        INSERT INTO message_delete_audit (
            delete_id, message_id, effect, state, detail, audit_utc
        )
        VALUES (?, ?, ?, 'queued', 'Delete request queued', ?)
        """,
        (delete_id, message_id, requested_effect, stamp),
    )
    conn.execute(
        "UPDATE message_projection SET deleted=1, deleted_utc=? WHERE message_id=?",
        (stamp, message_id),
    )
    return delete_id


def list_projected_messages(
    db_path: str | Path,
    *,
    source_family: str = "",
    source_families: Sequence[str] | None = None,
    group_name: str = "",
    group_names: Sequence[str] | None = None,
    status: str = "",
    statuses: Sequence[str] | None = None,
    severity: str = "",
    from_call: str = "",
    to_call: str = "",
    message_type: str = "",
    search_text: str = "",
    received_after_ts: float = 0.0,
    received_before_ts: float = 0.0,
    include_archived: bool = False,
    include_deleted: bool = False,
    include_suppressed: bool = False,
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Compatibility list API for non-model consumers.

    The historical API is now also hard-capped at 200 rows so an old caller
    cannot accidentally rebuild a retained-history widget.  Consumers that
    need more history must advance with :func:`query_projected_message_page`;
    aggregate consumers must use a dedicated count/summary query.  This helper
    is strictly read-only and never repairs projection schema on a view open.
    """
    clauses, params = _projected_message_filter_sql(
        source_family=source_family,
        source_families=source_families,
        group_name=group_name,
        group_names=group_names,
        status=status,
        statuses=statuses,
        severity=severity,
        from_call=from_call,
        to_call=to_call,
        message_type=message_type,
        search_text=search_text,
        received_after_ts=received_after_ts,
        received_before_ts=received_before_ts,
        include_archived=include_archived,
        include_deleted=include_deleted,
        include_suppressed=include_suppressed,
    )
    params.append(_bounded_int(limit, default=MAX_PROJECTED_MESSAGE_PAGE_SIZE, maximum=MAX_PROJECTED_MESSAGE_PAGE_SIZE))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return _read_projected_rows(
        db_path,
        f"""
        SELECT *
          FROM message_projection
        {where}
         ORDER BY operator_attention DESC, actionable DESC, event_ts DESC,
                  received_ts DESC, message_id DESC
         LIMIT ?
        """,
        tuple(params),
    )


def count_projected_messages(
    db_path: str | Path,
    *,
    source_family: str = "",
    source_families: Sequence[str] | None = None,
    group_name: str = "",
    group_names: Sequence[str] | None = None,
    status: str = "",
    statuses: Sequence[str] | None = None,
    severity: str = "",
    from_call: str = "",
    to_call: str = "",
    message_type: str = "",
    search_text: str = "",
    received_after_ts: float = 0.0,
    received_before_ts: float = 0.0,
    include_archived: bool = False,
    include_deleted: bool = False,
    include_suppressed: bool = False,
) -> int:
    """Return the bounded-model result count without mutating projection state."""
    clauses, params = _projected_message_filter_sql(
        source_family=source_family,
        source_families=source_families,
        group_name=group_name,
        group_names=group_names,
        status=status,
        statuses=statuses,
        severity=severity,
        from_call=from_call,
        to_call=to_call,
        message_type=message_type,
        search_text=search_text,
        received_after_ts=received_after_ts,
        received_before_ts=received_before_ts,
        include_archived=include_archived,
        include_deleted=include_deleted,
        include_suppressed=include_suppressed,
    )
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_sqlite_readonly(db_path, row_factory=sqlite3.Row)
        return _count_projected_messages_on_connection(conn, where, params)
    except sqlite3.Error:
        # Startup owns the migration.  A tab opened before that owner has
        # completed simply has no cached projection to render yet.
        return 0
    finally:
        if conn is not None:
            conn.close()


def get_message_projection_generation(db_path: str | Path) -> int:
    """Return the latest committed projection generation without repair I/O.

    A zero result is the safe pre-migration/unavailable value.  GUI callers
    compare this compact token instead of retaining message bodies or polling
    a writer from the UI thread.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_sqlite_readonly(db_path, row_factory=sqlite3.Row)
        return _message_projection_generation_on_connection(conn)
    except sqlite3.Error:
        return 0
    finally:
        if conn is not None:
            conn.close()


def query_projected_message_page(
    db_path: str | Path,
    *,
    message_ids: Sequence[str] | None = None,
    source_family: str = "",
    source_families: Sequence[str] | None = None,
    group_name: str = "",
    group_names: Sequence[str] | None = None,
    status: str = "",
    statuses: Sequence[str] | None = None,
    severity: str = "",
    from_call: str = "",
    to_call: str = "",
    message_type: str = "",
    search_text: str = "",
    received_after_ts: float = 0.0,
    received_before_ts: float = 0.0,
    include_archived: bool = False,
    include_deleted: bool = False,
    include_suppressed: bool = False,
    page_size: int = MAX_PROJECTED_MESSAGE_PAGE_SIZE,
    cursor: MessageProjectionPageCursor | None = None,
    include_total: bool = False,
) -> MessageProjectionPage:
    """Read one 200-row-at-most projection page using indexed keyset paging.

    This helper has no migration fallback by design: opening, filtering, or
    paging the Inbox must never contend with a projection writer for DDL.  The
    optional total is a separate indexed aggregate over the same filter, not a
    materialized row list.
    """
    base_clauses, base_params = _projected_message_filter_sql(
        source_family=source_family,
        source_families=source_families,
        group_name=group_name,
        group_names=group_names,
        status=status,
        statuses=statuses,
        severity=severity,
        from_call=from_call,
        to_call=to_call,
        message_type=message_type,
        search_text=search_text,
        received_after_ts=received_after_ts,
        received_before_ts=received_before_ts,
        include_archived=include_archived,
        include_deleted=include_deleted,
        include_suppressed=include_suppressed,
    )
    if message_ids is not None:
        # Explicit Ops scope: no empty-list fallback to the ordinary Inbox.
        ids = tuple(dict.fromkeys(str(value).strip() for value in message_ids if str(value).strip()))
        if len(ids) > MAX_PROJECTED_MESSAGE_PAGE_SIZE:
            raise ValueError("Explicit message scope exceeds the 200-message bound")
        if ids:
            base_clauses.append("message_id IN (" + ",".join("?" for _ in ids) + ")")
            base_params.extend(ids)
        else:
            base_clauses.append("0")
    clauses = list(base_clauses)
    params = list(base_params)
    if cursor is not None:
        clauses.append(
            """(
                COALESCE(NULLIF(received_ts, 0), event_ts, 0) < ?
                OR (
                    COALESCE(NULLIF(received_ts, 0), event_ts, 0) = ?
                    AND event_ts < ?
                )
                OR (
                    COALESCE(NULLIF(received_ts, 0), event_ts, 0) = ?
                    AND event_ts = ?
                    AND message_id < ?
                )
            )"""
        )
        params.extend(
            (
                float(cursor.received_ts or 0.0),
                float(cursor.received_ts or 0.0),
                float(cursor.event_ts or 0.0),
                float(cursor.received_ts or 0.0),
                float(cursor.event_ts or 0.0),
                str(cursor.message_id or ""),
            )
        )
    bounded_size = _bounded_int(
        page_size,
        default=MAX_PROJECTED_MESSAGE_PAGE_SIZE,
        maximum=MAX_PROJECTED_MESSAGE_PAGE_SIZE,
    )
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    conn: sqlite3.Connection | None = None
    rows: list[sqlite3.Row] = []
    total_count: int | None = None
    generation = 0
    try:
        conn = connect_sqlite_readonly(db_path, row_factory=sqlite3.Row)
        # Without an explicit transaction, SQLite may complete the page SELECT
        # before the count/generation queries begin.  A writer could then make
        # one UI result internally inconsistent.  Keep these compact reads in
        # one read-only snapshot; this never waits for or mutates the writer.
        conn.execute("BEGIN")
        rows = list(
            conn.execute(
                f"""
                SELECT *
                  FROM message_projection
                {where}
                 ORDER BY COALESCE(NULLIF(received_ts, 0), event_ts, 0) DESC,
                          event_ts DESC, message_id DESC
                 LIMIT ?
                """,
                tuple(params + [bounded_size + 1]),
            ).fetchall()
        )
        if include_total:
            base_where = " WHERE " + " AND ".join(base_clauses) if base_clauses else ""
            total_count = _count_projected_messages_on_connection(conn, base_where, base_params)
        generation = _message_projection_generation_on_connection(conn)
    except sqlite3.Error:
        rows = []
        total_count = 0 if include_total else None
        generation = 0
    finally:
        if conn is not None:
            conn.close()
    page_rows = tuple(rows[:bounded_size])
    next_cursor = _projected_page_cursor_from_row(page_rows[-1]) if len(rows) > bounded_size and page_rows else None
    return MessageProjectionPage(
        rows=page_rows,
        generation=generation,
        next_cursor=next_cursor,
        total_count=total_count,
    )


def query_projected_inbox_focus_counts(
    db_path: str | Path,
    *,
    group_name: str = "",
    group_names: Sequence[str] | None = None,
    received_after_ts: float = 0.0,
    received_before_ts: float = 0.0,
) -> MessageProjectionFocusCounts:
    """Return all Inbox focus counters with one scalar aggregate read.

    Counts follow the Inbox workspace scope (configured groups and age) but
    deliberately ignore the active focus, source refinement, search text, and
    advanced filters.  This keeps every focus chip trustworthy while the
    operator moves between views.  The query runs in the caller's background
    read lane and does not migrate, repair, or write the projection database.
    """

    clauses, params = _projected_message_filter_sql(
        source_family="",
        source_families=None,
        group_name=group_name,
        group_names=group_names,
        status="",
        statuses=None,
        severity="",
        from_call="",
        to_call="",
        message_type="",
        search_text="",
        received_after_ts=received_after_ts,
        received_before_ts=received_before_ts,
        include_archived=False,
        include_deleted=False,
        include_suppressed=False,
    )
    unread = """(
        UPPER(COALESCE(status, '')) <> 'READ'
        AND (
            UPPER(COALESCE(status, '')) IN ('NEW', 'UNREAD', 'ALERT', 'YELLOW', 'RED')
            OR LOWER(COALESCE(read_state, '')) IN ('new', 'unread', 'alert')
        )
    )"""
    clauses.append(unread)
    where = " WHERE " + " AND ".join(clauses)
    empty = {
        "all": 0,
        "new": 0,
        "forms": 0,
        "spotter": 0,
        "commstat": 0,
        "js8call": 0,
        "mesh": 0,
        "varac": 0,
        "bbs": 0,
    }
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_sqlite_readonly(db_path, row_factory=sqlite3.Row)
        conn.execute("BEGIN")
        rows = conn.execute(
            f"""
            SELECT
                LOWER(COALESCE(source_family, '')) AS family,
                COUNT(*) AS unread_count,
                SUM(CASE WHEN UPPER(COALESCE(message_type, '')) LIKE 'F!%'
                         THEN 1 ELSE 0 END) AS spotter_form_count
              FROM message_projection
            {where}
             GROUP BY LOWER(COALESCE(source_family, ''))
            """,
            tuple(params),
        ).fetchall()
        generation = _message_projection_generation_on_connection(conn)
        counts = dict(empty)
        for row in rows:
            family = str(row["family"] or "").strip().lower()
            unread_count = max(0, int(row["unread_count"] or 0))
            spotter_form_count = max(0, int(row["spotter_form_count"] or 0))
            counts["all"] += unread_count
            counts["new"] += unread_count
            if family in {"flmsg", "flamp"}:
                counts["forms"] += unread_count
            counts["spotter"] += unread_count if family == "spotter" else spotter_form_count
            if family == "commstat":
                counts["commstat"] += unread_count
            if family in {"js8", "commstat", "spotter"}:
                counts["js8call"] += unread_count
            if family in {"mesh", "meshcore", "meshtastic", "mesh_client", "local_mesh"}:
                counts["mesh"] += unread_count
            if family == "varac":
                counts["varac"] += unread_count
            if family in {"bbs", "bbs_archive"}:
                counts["bbs"] += unread_count
        return MessageProjectionFocusCounts(counts=counts, generation=generation)
    except sqlite3.Error:
        return MessageProjectionFocusCounts(counts=empty, generation=0)
    finally:
        if conn is not None:
            conn.close()


def _count_projected_messages_on_connection(
    conn: sqlite3.Connection,
    where: str,
    params: Sequence[object],
) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM message_projection{where}",
        tuple(params),
    ).fetchone()
    return int(row["count"] or 0) if row is not None else 0


def _message_projection_generation_on_connection(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT generation FROM message_projection_generation WHERE singleton=1"
        ).fetchone()
    except sqlite3.Error:
        # A projection created before the additive MIP migration still has
        # readable message rows.  The caller treats generation zero as an
        # invalidation token unavailable until startup completes migration.
        return 0
    if row is None:
        return 0
    try:
        return max(0, int(row["generation"] if hasattr(row, "keys") else row[0]))
    except (TypeError, ValueError, IndexError):
        return 0


def _projected_message_filter_sql(
    *,
    source_family: str,
    source_families: Sequence[str] | None,
    group_name: str,
    group_names: Sequence[str] | None,
    status: str,
    statuses: Sequence[str] | None,
    severity: str,
    from_call: str,
    to_call: str,
    message_type: str,
    search_text: str,
    received_after_ts: float,
    received_before_ts: float,
    include_archived: bool,
    include_deleted: bool,
    include_suppressed: bool,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    # A file projection identity changed from display-path text to a reversible
    # SQLite-safe path token. Databases that span that upgrade can contain both
    # derived rows for the exact same physical file version. Prefer the safe
    # identity (and then the newest projection) in every Inbox read model so a
    # reader click always advances to a different message. Source files and
    # historical projection evidence remain untouched.
    clauses.append(
        """
        NOT EXISTS (
            SELECT 1
              FROM message_external_refs AS current_ref
              JOIN message_external_refs AS preferred_ref
                ON preferred_ref.external_kind=current_ref.external_kind
               AND preferred_ref.external_path=current_ref.external_path
               AND COALESCE(preferred_ref.external_mtime, 0)=COALESCE(current_ref.external_mtime, 0)
               AND COALESCE(preferred_ref.external_size, 0)=COALESCE(current_ref.external_size, 0)
              JOIN message_projection AS preferred_message
                ON preferred_message.message_id=preferred_ref.message_id
             WHERE current_ref.message_id=message_projection.message_id
               AND SUBSTR(current_ref.external_kind, -5)='_file'
               AND COALESCE(current_ref.external_path, '') <> ''
               AND preferred_ref.message_id <> current_ref.message_id
               AND (
                    (
                        CASE WHEN INSTR(preferred_message.primary_source_id, '/')=0
                                   AND INSTR(preferred_message.primary_source_id, CHAR(92))=0
                             THEN 1 ELSE 0 END
                        >
                        CASE WHEN INSTR(message_projection.primary_source_id, '/')=0
                                  AND INSTR(message_projection.primary_source_id, CHAR(92))=0
                             THEN 1 ELSE 0 END
                    )
                    OR (
                        CASE WHEN INSTR(preferred_message.primary_source_id, '/')=0
                                   AND INSTR(preferred_message.primary_source_id, CHAR(92))=0
                             THEN 1 ELSE 0 END
                        =
                        CASE WHEN INSTR(message_projection.primary_source_id, '/')=0
                                  AND INSTR(message_projection.primary_source_id, CHAR(92))=0
                             THEN 1 ELSE 0 END
                        AND (
                            COALESCE(preferred_message.projected_utc, '')
                                > COALESCE(message_projection.projected_utc, '')
                            OR (
                                COALESCE(preferred_message.projected_utc, '')
                                    = COALESCE(message_projection.projected_utc, '')
                                AND preferred_message.message_id > message_projection.message_id
                            )
                        )
                    )
               )
        )
        """
    )
    if not include_deleted:
        clauses.append("deleted=0")
    if not include_archived:
        clauses.append("archived=0")
    if not include_suppressed:
        clauses.append("inbox_visible=1")
    requested_sources = [
        str(value or "").strip().lower()
        for value in (source_families or ())
        if str(value or "").strip()
    ]
    if source_family:
        requested_sources.append(str(source_family or "").strip().lower())
    requested_sources = sorted(set(requested_sources))
    if len(requested_sources) == 1:
        clauses.append("source_family=?")
        params.append(requested_sources[0])
    elif requested_sources:
        clauses.append(f"source_family IN ({','.join('?' for _ in requested_sources)})")
        params.extend(requested_sources)
    requested_groups = [
        str(value or "").strip().lstrip("@").upper()
        for value in (group_names or ())
        if str(value or "").strip().lstrip("@")
    ]
    if group_name:
        requested_groups.append(str(group_name or "").strip().lstrip("@").upper())
    requested_groups = sorted(set(requested_groups))
    if requested_groups:
        # Spotter/MCF traffic can carry a transport destination, report group,
        # and multiple query groups while the primary projection column stores
        # only one value.  Use that indexed primary value first, then admit
        # bounded canonical search-text candidates for the shared predicate to
        # verify with exact token boundaries.  This avoids source reads and
        # prevents a configured query group from disappearing before the
        # bounded 200-row page reaches the UI.
        exact_marks = ",".join("?" for _ in requested_groups)
        search_terms = " OR ".join("LOWER(COALESCE(search_text,'')) LIKE ?" for _ in requested_groups)
        clauses.append(
            f"(group_name IN ({exact_marks}) OR "
            f"(source_family IN ('spotter','sitrep') AND ({search_terms})))"
        )
        params.extend(requested_groups)
        params.extend(f"%{group.lower()}%" for group in requested_groups)
    requested_statuses = [
        str(value or "").strip().upper()
        for value in (statuses or ())
        if str(value or "").strip()
    ]
    if status:
        requested_statuses.append(str(status or "").strip().upper())
    requested_statuses = sorted(set(requested_statuses))
    if len(requested_statuses) == 1:
        clauses.append("status=?")
        params.append(requested_statuses[0])
    elif requested_statuses:
        clauses.append(f"status IN ({','.join('?' for _ in requested_statuses)})")
        params.extend(requested_statuses)
    if severity:
        clauses.append("severity=?")
        params.append(str(severity or "").strip().lower())
    if from_call:
        clauses.append("from_call=?")
        params.append(str(from_call or "").strip().upper())
    if to_call:
        clauses.append("to_call=?")
        params.append(str(to_call or "").strip().upper())
    if message_type:
        clauses.append("(message_type=? OR display_type=?)")
        normalized_type = str(message_type or "").strip().upper()
        params.extend((normalized_type, normalized_type))
    if search_text:
        clauses.append("search_text LIKE ?")
        params.append(f"%{search_text.lower()}%")
    if received_after_ts:
        clauses.append("COALESCE(NULLIF(received_ts, 0), event_ts, 0) >= ?")
        params.append(float(received_after_ts))
    if received_before_ts:
        clauses.append("COALESCE(NULLIF(received_ts, 0), event_ts, 0) <= ?")
        params.append(float(received_before_ts))
    return clauses, params


def _read_projected_rows(
    db_path: str | Path,
    sql: str,
    params: Sequence[object],
) -> list[sqlite3.Row]:
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_sqlite_readonly(db_path, row_factory=sqlite3.Row)
        return list(conn.execute(sql, tuple(params)).fetchall())
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def _projected_page_cursor_from_row(row: sqlite3.Row) -> MessageProjectionPageCursor:
    event_ts = float(row["event_ts"] or 0.0)
    received_ts = float(row["received_ts"] or 0.0)
    return MessageProjectionPageCursor(
        operator_attention=int(row["operator_attention"] or 0),
        actionable=int(row["actionable"] or 0),
        event_ts=event_ts,
        received_ts=received_ts if received_ts else event_ts,
        message_id=str(row["message_id"] or ""),
    )


def _bounded_int(value: object, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(int(maximum), parsed))


def list_projected_attention_messages(
    db_path: str | Path,
    *,
    limit: int = 250,
) -> list[sqlite3.Row]:
    # Use the Inbox's canonical visibility/duplicate authority before counting.
    clauses, params = _projected_message_filter_sql(
        source_family="", source_families=None, group_name="", group_names=None,
        status="", statuses=None, severity="", from_call="", to_call="",
        message_type="", search_text="", received_after_ts=0, received_before_ts=0,
        include_archived=False, include_deleted=False, include_suppressed=False,
    )
    clauses.append("(operator_attention=1 OR actionable=1 OR severity IN ('critical', 'warning'))")
    where = " AND ".join(clauses)
    return _read_projected_rows(
        db_path,
        f"""
        SELECT *
          FROM message_projection
         WHERE {where}
         ORDER BY
           CASE severity
               WHEN 'critical' THEN 0
               WHEN 'warning' THEN 1
               WHEN 'watch' THEN 2
               ELSE 3
           END,
           event_ts DESC,
           received_ts DESC,
           message_id DESC
         LIMIT ?
        """,
        tuple(params) + (_bounded_int(limit, default=250, maximum=1_000),),
    )


def list_projected_geo_messages(
    db_path: str | Path,
    *,
    state_code: str = "",
    group_name: str = "",
    limit: int = 500,
) -> list[sqlite3.Row]:
    clauses = ["deleted=0", "archived=0", "inbox_visible=1", "(COALESCE(state_code, '') != '' OR COALESCE(grid, '') != '' OR (lat IS NOT NULL AND lon IS NOT NULL))"]
    params: list[object] = []
    if state_code:
        clauses.append("UPPER(state_code)=?")
        params.append(str(state_code or "").strip().upper())
    if group_name:
        clauses.append("UPPER(group_name)=?")
        params.append(str(group_name or "").strip().lstrip("@").upper())
    params.append(_bounded_int(limit, default=500, maximum=2_000))
    return _read_projected_rows(
        db_path,
        f"""
        SELECT *
          FROM message_projection
         WHERE {' AND '.join(clauses)}
         ORDER BY event_ts DESC, received_ts DESC, message_id DESC
         LIMIT ?
        """,
        tuple(params),
    )


def load_projected_message_detail(db_path: str | Path, message_id: str) -> dict[str, Any]:
    clean_id = str(message_id or "").strip()
    if not clean_id:
        return {"message": None, "refs": [], "artifacts": []}
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_sqlite_readonly(db_path, row_factory=sqlite3.Row)
        message = conn.execute(
            "SELECT * FROM message_projection WHERE message_id=?",
            (clean_id,),
        ).fetchone()
        refs = conn.execute(
            """
            SELECT r.*, s.source_label AS receipt_source_label,
                   s.source_family AS receipt_source_family,
                   s.radio_id AS receipt_radio_id,
                   s.app_instance_id AS receipt_app_instance_id,
                   s.endpoint_or_path AS receipt_endpoint_or_path
              FROM message_external_refs r
              LEFT JOIN message_sources s ON s.source_id=r.source_id
             WHERE r.message_id=?
             ORDER BY r.source_id, r.external_kind, r.external_key
             LIMIT ?
            """,
            (clean_id, MAX_PROJECTED_MESSAGE_DETAIL_ROWS),
        ).fetchall()
        artifacts = conn.execute(
            """
            SELECT *
             FROM message_artifacts
             WHERE message_id=?
             ORDER BY artifact_type, q_id, block_id, path
             LIMIT ?
            """,
            (clean_id, MAX_PROJECTED_MESSAGE_DETAIL_ROWS),
        ).fetchall()
        return {"message": message, "refs": list(refs), "artifacts": list(artifacts)}
    except sqlite3.Error:
        return {"message": None, "refs": [], "artifacts": []}
    finally:
        if conn is not None:
            conn.close()


def load_projected_external_refs_for_messages(
    db_path: str | Path,
    message_ids: Sequence[str],
) -> dict[str, list[sqlite3.Row]]:
    clean_ids = list(dict.fromkeys(
        str(value or "").strip() for value in message_ids if str(value or "").strip()
    ))[:MAX_PROJECTED_MESSAGE_DETAIL_ROWS]
    if not clean_ids:
        return {}
    out: dict[str, list[sqlite3.Row]] = {message_id: [] for message_id in clean_ids}
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_sqlite_readonly(db_path, row_factory=sqlite3.Row)
        for start in range(0, len(clean_ids), 250):
            chunk = clean_ids[start : start + 250]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT r.*, s.source_label AS receipt_source_label,
                       s.source_family AS receipt_source_family,
                       s.radio_id AS receipt_radio_id,
                       s.app_instance_id AS receipt_app_instance_id,
                       s.endpoint_or_path AS receipt_endpoint_or_path
                  FROM message_external_refs r
                  LEFT JOIN message_sources s ON s.source_id=r.source_id
                 WHERE r.message_id IN ({placeholders})
                 ORDER BY r.source_id, r.external_kind, r.external_key
                """,
                tuple(chunk),
            ).fetchall()
            for row in rows:
                out.setdefault(str(row["message_id"] or ""), []).append(row)
        return out
    except sqlite3.Error:
        return out
    finally:
        if conn is not None:
            conn.close()


def mark_projected_messages_read(db_path: str | Path, message_ids: Sequence[str]) -> int:
    clean_ids = [str(value or "").strip() for value in message_ids if str(value or "").strip()]
    if not clean_ids:
        return 0
    conn = connect_sqlite(db_path, row_factory=sqlite3.Row)
    try:
        ensure_message_projection_schema(conn)
        stamp = utc_now_iso()
        with conn:
            count = 0
            for message_id in clean_ids:
                cur = conn.execute(
                    """
                    UPDATE message_projection
                       SET read_state='read',
                           status=CASE WHEN status IN ('NEW', 'UNREAD', 'ALERT') THEN 'READ' ELSE status END,
                           projected_utc=?
                     WHERE message_id=? AND deleted=0
                    """,
                    (stamp, message_id),
                )
                _mark_source_refs_read(conn, message_id, stamp)
                if cur.rowcount:
                    from freqinout.core.ops_focus import index_message_for_ops_focus

                    refreshed = conn.execute(
                        "SELECT * FROM message_projection WHERE message_id=?",
                        (message_id,),
                    ).fetchone()
                    if refreshed is not None:
                        index_message_for_ops_focus(conn, refreshed)
                count += int(cur.rowcount or 0)
            return count
    finally:
        conn.close()


def set_projected_message_attention(
    db_path: str | Path,
    message_id: str,
    enabled: bool,
) -> bool:
    """Persist the operator Flag state for one projected Inbox row.

    The projection stores attention as a boolean.  Source-specific rows may
    retain richer flag state in their native store, but the unified Inbox only
    needs a durable outlined/filled Flag contract.  This targeted update also
    advances the projection generation so an in-flight query cannot repaint a
    stale action state over the operator's choice.
    """

    clean_id = str(message_id or "").strip()
    if not clean_id:
        return False
    conn = connect_sqlite(db_path, row_factory=sqlite3.Row)
    try:
        ensure_message_projection_schema(conn)
        stamp = utc_now_iso()
        with conn:
            cur = conn.execute(
                """
                UPDATE message_projection
                   SET operator_attention=?, projected_utc=?
                 WHERE message_id=? AND deleted=0
                """,
                (1 if enabled else 0, stamp, clean_id),
            )
            if cur.rowcount:
                conn.execute(
                    """
                    UPDATE message_projection_generation
                       SET generation=generation+1, updated_utc=?
                     WHERE singleton=1
                    """,
                    (stamp,),
                )
            return bool(cur.rowcount)
    finally:
        conn.close()


def _mark_source_refs_read(conn: sqlite3.Connection, message_id: str, stamp: str) -> None:
    refs = list(
        conn.execute(
            """
            SELECT *
              FROM message_external_refs
             WHERE message_id=? AND COALESCE(read_capability, '') IN (
                'mark_read', 'js8_mark_read', 'spotter_mark_read', 'varac_mark_read'
             )
            """,
            (message_id,),
        )
    )
    read_ts = datetime_from_iso_ts(stamp)
    for ref in refs:
        kind = str(ref["external_kind"] or "").strip().lower()
        key = str(ref["external_key"] or "").strip()
        metadata = _parse_json_object(ref["metadata_json"])
        row_id = str(metadata.get("row_id") or "").strip()
        try:
            if kind == "js8_message" and _table_exists(conn, "js8_messages"):
                target = _int_text(row_id or key)
                if target > 0:
                    conn.execute(
                        "UPDATE js8_messages SET state='READ', read_ts=? WHERE id=? OR COALESCE(source_id, id)=?",
                        (read_ts, target, target),
                    )
            elif kind == "spotter_message" and _table_exists(conn, "spotter_traffic"):
                target = _int_text(row_id or key)
                if target > 0:
                    conn.execute("UPDATE spotter_traffic SET state='READ', read_ts=? WHERE id=?", (read_ts, target))
            elif kind == "varac_message" and _table_exists(conn, "varac_messages"):
                target = _int_text(row_id)
                source = str(metadata.get("source") or "").strip()
                source_key = str(metadata.get("source_key") or "").strip()
                if target > 0 and source and _table_has_column(conn, "varac_messages", "ingest_source_key"):
                    conn.execute(
                        "UPDATE varac_messages SET read_status=1 WHERE ingest_source_key=? AND source=? AND id=?",
                        (source_key, source, target),
                    )
                elif target > 0:
                    conn.execute("UPDATE varac_messages SET read_status=1 WHERE id=?", (target,))
        except Exception:
            continue


def datetime_from_iso_ts(value: str) -> float:
    try:
        return datetime.datetime.fromisoformat(str(value or "")).timestamp()
    except Exception:
        return 0.0


def get_message_projection_checkpoint(
    db_path: str | Path,
    source_id: str,
) -> MessageProjectionCheckpoint:
    clean_source = str(source_id or "").strip()
    if not clean_source:
        return MessageProjectionCheckpoint(source_id="")
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_sqlite_readonly(db_path, row_factory=sqlite3.Row)
        row = conn.execute(
            """
            SELECT source_id, last_external_key, last_event_ts, content_fingerprint, updated_utc
              FROM message_projection_checkpoint
             WHERE source_id=?
            """,
            (clean_source,),
        ).fetchone()
        if row is None:
            return MessageProjectionCheckpoint(source_id=clean_source)
        return MessageProjectionCheckpoint(
            source_id=str(row["source_id"] or ""),
            last_external_key=str(row["last_external_key"] or ""),
            last_event_ts=float(row["last_event_ts"] or 0.0),
            content_fingerprint=str(row["content_fingerprint"] or ""),
            updated_utc=str(row["updated_utc"] or ""),
        )
    except sqlite3.Error:
        # Schema lifecycle is startup-owned.  Treat an unavailable derived
        # checkpoint as empty rather than reopening the database writable from
        # a read path.
        return MessageProjectionCheckpoint(source_id=clean_source)
    finally:
        if conn is not None:
            conn.close()


def set_message_projection_checkpoint(
    conn: sqlite3.Connection,
    checkpoint: MessageProjectionCheckpoint,
    *,
    updated_utc: str | None = None,
) -> str:
    clean_source = str(checkpoint.source_id or "").strip()
    if not clean_source:
        return ""
    stamp = updated_utc or checkpoint.updated_utc or utc_now_iso()
    conn.execute(
        """
        INSERT INTO message_projection_checkpoint (
            source_id, last_external_key, last_event_ts, content_fingerprint, updated_utc
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            last_external_key=excluded.last_external_key,
            last_event_ts=excluded.last_event_ts,
            content_fingerprint=excluded.content_fingerprint,
            updated_utc=excluded.updated_utc
        """,
        (
            clean_source,
            str(checkpoint.last_external_key or ""),
            float(checkpoint.last_event_ts or 0.0),
            str(checkpoint.content_fingerprint or ""),
            stamp,
        ),
    )
    return clean_source


def process_message_delete_queue(db_path: str | Path, *, limit: int = 50) -> dict[str, int]:
    conn = connect_sqlite(db_path, row_factory=sqlite3.Row)
    try:
        ensure_message_projection_schema(conn)
        rows = list(
            conn.execute(
                """
                SELECT *
                  FROM message_delete_queue
                 WHERE state='queued'
                 ORDER BY requested_utc
                 LIMIT ?
                """,
                (max(1, min(500, int(limit or 50))),),
            )
        )
        counts = {"completed": 0, "failed": 0, "skipped": 0}
        for row in rows:
            result = _process_delete_queue_row(conn, row)
            counts[result] = counts.get(result, 0) + 1
        return counts
    finally:
        conn.close()


def _process_delete_queue_row(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    delete_id = str(row["delete_id"] or "")
    message_id = str(row["message_id"] or "")
    effect = str(row["requested_effect"] or "").strip().lower()
    stamp = utc_now_iso()
    if not delete_id or not message_id:
        return "skipped"
    try:
        if effect == "hide_fio":
            _complete_delete_queue_row(conn, delete_id, message_id, effect, "completed", "Projection hidden")
            return "completed"
        if effect == "source_delete":
            return _process_source_external_delete(conn, delete_id, message_id, effect)
        if effect == "audit_only":
            with conn:
                conn.execute(
                    """
                    UPDATE message_projection
                       SET body_preview='', search_text=LOWER(TRIM(COALESCE(subject, '') || ' ' || COALESCE(summary, ''))),
                           retention_class='audit_only', projected_utc=?
                     WHERE message_id=?
                    """,
                    (stamp, message_id),
                )
            _complete_delete_queue_row(conn, delete_id, message_id, effect, "completed", "Raw preview minimized")
            return "completed"
        if effect in {"delete_external", "delete_all_external_refs"}:
            return _process_file_external_delete(conn, delete_id, message_id, effect)
        _complete_delete_queue_row(conn, delete_id, message_id, effect, "failed", f"Unsupported delete effect: {effect}")
        return "failed"
    except Exception as exc:
        _complete_delete_queue_row(conn, delete_id, message_id, effect, "failed", str(exc))
        return "failed"


def _process_source_external_delete(
    conn: sqlite3.Connection,
    delete_id: str,
    message_id: str,
    effect: str,
) -> str:
    refs = list(
        conn.execute(
            """
            SELECT *
              FROM message_external_refs
             WHERE message_id=? AND COALESCE(delete_capability, '') IN (
                'delete_source', 'js8_delete', 'spotter_delete', 'varac_soft_delete',
                'sitrep_delete', 'commstat_delete'
             )
             ORDER BY source_id, external_kind, external_key
            """,
            (message_id,),
        )
    )
    if not refs:
        _complete_delete_queue_row(conn, delete_id, message_id, effect, "completed", "Projection hidden; no source-delete refs")
        return "completed"
    deleted = 0
    skipped = 0
    errors: list[str] = []
    with conn:
        for ref in refs:
            try:
                if _delete_source_ref(conn, ref):
                    deleted += 1
                else:
                    skipped += 1
            except Exception as exc:
                errors.append(f"{ref['external_kind']}:{ref['external_key']}: {exc}")
    detail = f"Deleted {deleted} source ref(s); skipped {skipped}"
    if errors:
        detail += "; errors: " + "; ".join(errors[:3])
    state = "failed" if errors and deleted == 0 else "completed"
    _complete_delete_queue_row(conn, delete_id, message_id, effect, state, detail)
    return "failed" if state == "failed" else "completed"


def _delete_source_ref(conn: sqlite3.Connection, ref: sqlite3.Row) -> bool:
    kind = str(ref["external_kind"] or "").strip().lower()
    key = str(ref["external_key"] or "").strip()
    metadata = _parse_json_object(ref["metadata_json"])
    row_id = str(metadata.get("row_id") or "").strip()
    if kind == "js8_message":
        if not _table_exists(conn, "js8_messages"):
            return False
        target = _int_text(row_id or key)
        if target <= 0:
            return False
        cur = conn.execute("DELETE FROM js8_messages WHERE id=? OR COALESCE(source_id, id)=?", (target, target))
        try:
            conn.execute("DELETE FROM js8_inbox_state WHERE id=? OR COALESCE(source_id, id)=?", (target, target))
        except Exception:
            pass
        return int(cur.rowcount or 0) > 0
    if kind == "spotter_message":
        if not _table_exists(conn, "spotter_traffic"):
            return False
        target = _int_text(row_id or key)
        if target <= 0:
            return False
        cur = conn.execute("DELETE FROM spotter_traffic WHERE id=?", (target,))
        return int(cur.rowcount or 0) > 0
    if kind == "varac_message":
        if not _table_exists(conn, "varac_messages"):
            return False
        target = _int_text(row_id)
        source = str(metadata.get("source") or "").strip()
        source_key = str(metadata.get("source_key") or "").strip()
        if target > 0 and source and _table_has_column(conn, "varac_messages", "ingest_source_key"):
            cur = conn.execute(
                "UPDATE varac_messages SET is_deleted=1 WHERE ingest_source_key=? AND source=? AND id=?",
                (source_key, source, target),
            )
            return int(cur.rowcount or 0) > 0
        if target > 0:
            cur = conn.execute("UPDATE varac_messages SET is_deleted=1 WHERE id=?", (target,))
            return int(cur.rowcount or 0) > 0
        if key:
            cur = conn.execute(
                "UPDATE varac_messages SET is_deleted=1 WHERE guid=? OR vmail_guid=?",
                (key, key),
            )
            return int(cur.rowcount or 0) > 0
        return False
    if kind == "sitrep_event":
        if not _table_exists(conn, "sitrep_events"):
            return False
        target = _int_text(row_id)
        if target > 0:
            cur = conn.execute("DELETE FROM sitrep_events WHERE id=?", (target,))
            return int(cur.rowcount or 0) > 0
        if key:
            cur = conn.execute("DELETE FROM sitrep_events WHERE report_key=?", (key,))
            return int(cur.rowcount or 0) > 0
        return False
    if kind == "commstat_artifact":
        if not _table_exists(conn, "commstat_artifacts"):
            return False
        cur = conn.execute("DELETE FROM commstat_artifacts WHERE artifact_key=?", (key,))
        return int(cur.rowcount or 0) > 0
    return False


def _process_file_external_delete(
    conn: sqlite3.Connection,
    delete_id: str,
    message_id: str,
    effect: str,
) -> str:
    refs = list(
        conn.execute(
            """
            SELECT *
              FROM message_external_refs
             WHERE message_id=? AND delete_capability='file_delete'
             ORDER BY external_path
            """,
            (message_id,),
        )
    )
    if not refs:
        _complete_delete_queue_row(conn, delete_id, message_id, effect, "failed", "No file-delete capable refs")
        return "failed"
    deleted = 0
    missing = 0
    errors: list[str] = []
    for ref in refs:
        path_text = str(ref["external_path"] or "").strip()
        if not path_text:
            missing += 1
            continue
        path = Path(path_text)
        try:
            if path.exists() and path.is_file():
                path.unlink()
                deleted += 1
            else:
                missing += 1
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    detail = f"Deleted {deleted} file ref(s); missing {missing}"
    if errors:
        detail += "; errors: " + "; ".join(errors[:3])
    state = "failed" if errors and deleted == 0 else "completed"
    _complete_delete_queue_row(conn, delete_id, message_id, effect, state, detail)
    return "failed" if state == "failed" else "completed"


def _parse_json_object(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_text(value: object) -> int:
    try:
        return int(float(str(value or "").strip()))
    except Exception:
        return 0


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(str(row[1] or "") == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())
    except Exception:
        return False


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
            (str(table or "").strip(),),
        ).fetchone()
    except Exception:
        return False
    return bool(row)


def _complete_delete_queue_row(
    conn: sqlite3.Connection,
    delete_id: str,
    message_id: str,
    effect: str,
    state: str,
    detail: str,
) -> None:
    stamp = utc_now_iso()
    payload = {"detail": str(detail or ""), "completed_utc": stamp}
    with conn:
        conn.execute(
            """
            UPDATE message_delete_queue
               SET state=?, completed_utc=?, result_json=?
             WHERE delete_id=?
            """,
            (state, stamp, _json(payload, "{}"), delete_id),
        )
        conn.execute(
            """
            INSERT INTO message_delete_audit (
                delete_id, message_id, effect, state, detail, audit_utc
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (delete_id, message_id, effect, state, detail, stamp),
        )


def _message_values(message: MessageProjectionRecord, projected_utc: str) -> tuple[object, ...]:
    return (
        _sanitize_sql_text(message.message_id),
        _sanitize_sql_text(message.canonical_key),
        _sanitize_sql_text(message.content_hash),
        _sanitize_sql_text(message.primary_source_id),
        _sanitize_sql_text(message.source_family),
        _sanitize_sql_text(message.source_label),
        message.radio_id,
        _sanitize_sql_text(message.app_instance_id),
        _sanitize_sql_text(message.message_type),
        _sanitize_sql_text(message.display_type),
        _sanitize_sql_text(message.status),
        _sanitize_sql_text(message.severity),
        _sanitize_sql_text(message.read_state),
        _sanitize_sql_text(message.from_call).upper(),
        _sanitize_sql_text(message.to_call).upper(),
        _sanitize_sql_text(message.group_name).lstrip("@").upper(),
        _sanitize_sql_text(message.scope),
        _sanitize_sql_text(message.state_code).upper(),
        _sanitize_sql_text(message.grid).upper(),
        message.lat,
        message.lon,
        float(message.event_ts or 0.0),
        float(message.received_ts or 0.0),
        _sanitize_sql_text(message.event_utc),
        _sanitize_sql_text(message.received_utc),
        _sanitize_sql_text(message.subject),
        _sanitize_sql_text(message.summary),
        _sanitize_sql_text(message.body_preview),
        _json(list(message.topics), "[]"),
        _json(message.entities, "{}"),
        1 if message.actionable else 0,
        1 if message.operator_attention else 0,
        float(message.confidence or 0.0),
        _sanitize_sql_text(message.recommended_action),
        int(message.intelligence_version or 0),
        _sanitize_sql_text(message.intelligence_utc),
        _json(message.intelligence, "{}"),
        1 if message.pinned else 0,
        1 if message.archived else 0,
        1 if message.deleted else 0,
        _sanitize_sql_text(message.deleted_utc),
        _sanitize_sql_text(message.retention_class or "normal"),
        1 if message.inbox_visible else 0,
        _sanitize_sql_text(message.inbox_suppression_reason),
        int(message.classification_version or 0),
        _sanitize_sql_text(message.search_text).lower(),
        int(message.projection_version or PROJECTION_SCHEMA_VERSION),
        _sanitize_sql_text(projected_utc),
    )


def upsert_projected_message(
    db_path: str | Path,
    *,
    source: MessageSourceRecord,
    message: MessageProjectionRecord,
    refs: Sequence[ExternalMessageRef] = (),
    artifacts: Sequence[MessageArtifactRecord] = (),
) -> str:
    conn = connect_sqlite(db_path)
    try:
        ensure_message_projection_schema(conn)
        with conn:
            upsert_message_source(conn, source)
            message_id = upsert_message_projection(conn, message)
            for ref in refs:
                upsert_external_ref(conn, ref)
            for artifact in artifacts:
                upsert_message_artifact(conn, artifact)
        return message_id
    finally:
        conn.close()
