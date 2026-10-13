from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from freqinout.core.config_backup import ConfigBackupResult, create_config_backup
from freqinout.core.config_paths import get_config_dir
from freqinout.core.logger import log
from freqinout.core.sqlite_utils import connect_sqlite

BBS_LIBRARY_SCHEMA_VERSION = 2
BBS_RECONCILE_BATCH_SIZE = 128
DEFAULT_BBS_RETENTION_DAYS = 14

RETENTION_MANUAL = "manual"
RETENTION_GLOBAL_DEFAULT = "global_default"
RETENTION_EXPIRE_AFTER_DAYS = "expire_after_days"
RETENTION_CLASS_KEEP = "keep"
RETENTION_CLASS_NORMAL = "normal"
SOURCE_PRESENT = "present"
SOURCE_MISSING = "missing"
SOURCE_DELETED = "deleted"
DISABLED_OPERATOR = "operator_disabled"
DISABLED_RETENTION = "retention_expired"


@dataclass(frozen=True)
class BbsLibraryManifestRow:
    artifact_id: str
    source_path: str
    display_name: str
    live_name: str
    size: int
    mtime_ns: int
    content_hash: str = ""
    q_id: str = ""
    block_id: str = ""
    metadata: Mapping[str, object] | None = None


@dataclass(frozen=True)
class BbsLocationRecord:
    location_id: str
    name: str
    source_dir: str
    enabled: bool
    parent_location_id: str = ""
    access_rule: str = "public"
    retention_mode: str = RETENTION_GLOBAL_DEFAULT
    retention_days: int = 0
    metadata: Mapping[str, object] | None = None


@dataclass(frozen=True)
class BbsArtifactAdminRow:
    artifact_id: str
    source_path: str
    source_kind: str
    display_name: str
    size: int
    mtime_ns: int
    source_state: str
    location_id: str
    location_name: str
    published: bool
    publication_state: str
    disabled_reason: str
    retention_mode: str
    retention_days: int
    retention_class: str
    expires_utc: str
    modified_utc: str
    age_days: int


@dataclass(frozen=True)
class BbsReconcileResult:
    checked: int = 0
    missing: int = 0
    restored: int = 0
    expired: int = 0
    remaining: int = 0


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def default_bbs_library_db_path() -> Path:
    return get_config_dir() / "config" / "freqinout.db"


def bbs_library_db_path_from_settings(settings: object) -> Path:
    for attr in ("db_path", "_config_path"):
        value = getattr(settings, attr, None)
        if value:
            return Path(value)
    fallback = getattr(settings, "fallback_settings", None)
    for attr in ("db_path", "_config_path"):
        value = getattr(fallback, attr, None)
        if value:
            return Path(value)
    return default_bbs_library_db_path()


def load_station_bbs_sweeper_rules(conn: sqlite3.Connection) -> list[dict[str, object]] | None:
    """Return the station-owned automation rules, or ``None`` before cutover.

    ``None`` deliberately differs from an empty list: it lets one release of
    runtime code fall back to the legacy radio/settings copy only when the
    station-owned value has never been created.  Once an operator saves an
    empty rule set, automatic copying stays disabled instead of resurrecting a
    stale profile value.
    """

    ensure_bbs_library_schema(conn)
    row = conn.execute(
        "SELECT value FROM bbs_library_meta WHERE key='station_sweeper_rules_json' LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row[0] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def save_station_bbs_sweeper_rules(
    conn: sqlite3.Connection,
    rules: Sequence[Mapping[str, object]],
) -> None:
    """Persist the canonical station-owned BBS automation rule list."""

    ensure_bbs_library_schema(conn)
    payload = [dict(item) for item in rules if isinstance(item, Mapping)]
    conn.execute(
        "INSERT OR REPLACE INTO bbs_library_meta(key, value) VALUES('station_sweeper_rules_json', ?)",
        (_json(payload, default="[]"),),
    )


def stable_bbs_artifact_id(*parts: object) -> str:
    text = "|".join(str(part or "").strip() for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def upsert_bbs_artifact_path(
    conn: sqlite3.Connection,
    *,
    source_path: object,
    source_kind: object = "operator_file",
    source_id: object = "",
    display_name: object = "",
    metadata: Mapping[str, object] | None = None,
) -> str:
    ensure_bbs_library_schema(conn)
    path = Path(str(source_path or "")).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(str(path))
    stat = path.stat()
    resolved = str(path.resolve())
    artifact_id = stable_bbs_artifact_id("file", resolved)
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO bbs_artifacts(
            artifact_id, source_kind, source_id, source_path, display_name,
            size, mtime_ns, content_hash, q_id, block_id, metadata_json,
            deleted, created_utc, updated_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, '', '', '', ?, 0, ?, ?)
        ON CONFLICT(artifact_id) DO UPDATE SET
            source_kind=excluded.source_kind,
            source_id=excluded.source_id,
            source_path=excluded.source_path,
            display_name=excluded.display_name,
            size=excluded.size,
            mtime_ns=excluded.mtime_ns,
            metadata_json=excluded.metadata_json,
            deleted=0,
            source_state='present',
            missing_since_utc=NULL,
            last_reconciled_utc=excluded.updated_utc,
            updated_utc=excluded.updated_utc
        """,
        (
            artifact_id,
            str(source_kind or "operator_file").strip() or "operator_file",
            str(source_id or "").strip(),
            resolved,
            str(display_name or path.name).strip() or path.name,
            int(stat.st_size or 0),
            int(stat.st_mtime_ns or 0),
            _json(metadata or {}),
            now,
            now,
        ),
    )
    _refresh_artifact_mapping_expiries(conn, artifact_id)
    return artifact_id


def upsert_bbs_location(
    conn: sqlite3.Connection,
    *,
    location_id: object,
    name: object,
    source_dir: object = "",
    enabled: bool = True,
    parent_location_id: object = "",
    access_rule: object = "public",
    retention_mode: object = RETENTION_GLOBAL_DEFAULT,
    retention_days: int = 0,
    metadata: Mapping[str, object] | None = None,
) -> str:
    ensure_bbs_library_schema(conn)
    location_key = str(location_id or "").strip()
    if not location_key:
        raise ValueError("location_id is required")
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO bbs_locations(
            location_id, name, source_dir, enabled, metadata_json, updated_utc,
            parent_location_id, access_rule, retention_mode, retention_days
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(location_id) DO UPDATE SET
            name=excluded.name,
            source_dir=excluded.source_dir,
            enabled=excluded.enabled,
            metadata_json=excluded.metadata_json,
            parent_location_id=excluded.parent_location_id,
            access_rule=excluded.access_rule,
            retention_mode=excluded.retention_mode,
            retention_days=excluded.retention_days,
            updated_utc=excluded.updated_utc
        """,
        (
            location_key,
            str(name or location_key).strip() or location_key,
            str(source_dir or "").strip(),
            1 if enabled else 0,
            _json(metadata or {}),
            now,
            str(parent_location_id or "").strip(),
            _normalize_access_rule(access_rule),
            _normalize_retention_mode(retention_mode),
            max(0, int(retention_days or 0)),
        ),
    )
    _refresh_location_mapping_expiries(conn, location_key)
    return location_key


def set_bbs_location_artifact(
    conn: sqlite3.Connection,
    *,
    location_id: object,
    artifact_id: object,
    live_name: object = "",
    sort_order: int = 0,
    visibility_rule: object = "public",
    retention_class: object = "normal",
    publish_enabled: bool = True,
    expires_utc: object = "",
) -> None:
    ensure_bbs_library_schema(conn)
    location_key = str(location_id or "").strip()
    artifact_key = str(artifact_id or "").strip()
    if not location_key or not artifact_key:
        raise ValueError("location_id and artifact_id are required")
    now = utc_now_iso()
    retention_class_key = _normalize_retention_class(retention_class)
    resolved_expiry = str(expires_utc or "").strip()
    if retention_class_key == RETENTION_CLASS_KEEP:
        resolved_expiry = ""
    elif publish_enabled and not resolved_expiry:
        resolved_expiry = _mapping_expiry_utc(
            conn,
            location_key,
            artifact_key,
            retention_class=retention_class_key,
        )
    conn.execute(
        """
        INSERT INTO bbs_location_artifacts(
            location_id, artifact_id, live_name, sort_order, visibility_rule,
            retention_class, publish_enabled, created_utc, updated_utc,
            disabled_reason, expires_utc, last_reconciled_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(location_id, artifact_id) DO UPDATE SET
            live_name=excluded.live_name,
            sort_order=excluded.sort_order,
            visibility_rule=excluded.visibility_rule,
            retention_class=excluded.retention_class,
            publish_enabled=excluded.publish_enabled,
            disabled_reason=excluded.disabled_reason,
            expires_utc=excluded.expires_utc,
            updated_utc=excluded.updated_utc
        """,
        (
            location_key,
            artifact_key,
            str(live_name or "").strip(),
            int(sort_order or 0),
            str(visibility_rule or "public").strip() or "public",
            retention_class_key,
            1 if publish_enabled else 0,
            now,
            now,
            "" if publish_enabled else DISABLED_OPERATOR,
            resolved_expiry or None,
            now,
        ),
    )


def unpublish_bbs_artifact_path_from_location(
    conn: sqlite3.Connection,
    *,
    source_path: object,
    location_id: object,
) -> int:
    ensure_bbs_library_schema(conn)
    location_key = str(location_id or "").strip()
    if not location_key:
        return 0
    try:
        resolved = str(Path(str(source_path or "")).expanduser().resolve())
    except Exception:
        resolved = str(source_path or "").strip()
    row = conn.execute(
        "SELECT artifact_id FROM bbs_artifacts WHERE source_path=? LIMIT 1",
        (resolved,),
    ).fetchone()
    if not row:
        return 0
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE bbs_location_artifacts
           SET publish_enabled=0,
               disabled_reason=?,
               updated_utc=?
         WHERE location_id=?
           AND artifact_id=?
           AND publish_enabled=1
        """,
        (DISABLED_OPERATOR, now, location_key, str(row[0] or "")),
    )
    return int(cur.rowcount or 0)


def remove_bbs_artifact_from_all_locations(
    conn: sqlite3.Connection,
    *,
    artifact_id: object,
) -> int:
    """Disable every BBS mapping for an artifact without touching its source."""

    ensure_bbs_library_schema(conn)
    artifact_key = str(artifact_id or "").strip()
    if not artifact_key:
        return 0
    if not conn.execute(
        "SELECT 1 FROM bbs_artifacts WHERE artifact_id=? LIMIT 1", (artifact_key,)
    ).fetchone():
        return 0
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE bbs_location_artifacts
           SET publish_enabled=0,
               retention_class=?,
               disabled_reason=?,
               expires_utc=NULL,
               updated_utc=?
         WHERE artifact_id=?
           AND (publish_enabled<>0 OR disabled_reason<>? OR retention_class<>?)
        """,
        (
            RETENTION_CLASS_NORMAL,
            DISABLED_OPERATOR,
            now,
            artifact_key,
            DISABLED_OPERATOR,
            RETENTION_CLASS_NORMAL,
        ),
    )
    return int(cur.rowcount or 0)


def set_bbs_artifact_keep(
    conn: sqlite3.Connection,
    *,
    artifact_id: object,
    location_id: object,
    keep: bool = True,
) -> str:
    """Set or clear a per-location keep override and return the resulting expiry."""

    ensure_bbs_library_schema(conn)
    artifact_key = str(artifact_id or "").strip()
    location_key = str(location_id or "").strip()
    if not artifact_key or not location_key:
        raise ValueError("artifact_id and location_id are required")
    if not conn.execute(
        "SELECT 1 FROM bbs_artifacts WHERE artifact_id=? LIMIT 1", (artifact_key,)
    ).fetchone():
        raise ValueError(f"Unknown BBS artifact id: {artifact_key}")
    if not conn.execute(
        "SELECT 1 FROM bbs_locations WHERE location_id=? LIMIT 1", (location_key,)
    ).fetchone():
        raise ValueError(f"Unknown BBS location id: {location_key}")

    mapping = conn.execute(
        """
        SELECT live_name, sort_order, visibility_rule, publish_enabled, disabled_reason
        FROM bbs_location_artifacts
        WHERE location_id=? AND artifact_id=?
        LIMIT 1
        """,
        (location_key, artifact_key),
    ).fetchone()
    if keep:
        set_bbs_location_artifact(
            conn,
            location_id=location_key,
            artifact_id=artifact_key,
            live_name=str(mapping[0] or "") if mapping else "",
            sort_order=int(mapping[1] or 0) if mapping else 0,
            visibility_rule=str(mapping[2] or "public") if mapping else "public",
            retention_class=RETENTION_CLASS_KEEP,
            publish_enabled=True,
            expires_utc="",
        )
        return ""

    if mapping is None:
        return ""
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE bbs_location_artifacts
           SET retention_class=?, updated_utc=?
         WHERE location_id=? AND artifact_id=?
        """,
        (
            RETENTION_CLASS_NORMAL,
            now,
            location_key,
            artifact_key,
        ),
    )
    expiry = _mapping_expiry_utc(conn, location_key, artifact_key)
    conn.execute(
        """
        UPDATE bbs_location_artifacts
           SET expires_utc=?, updated_utc=?
         WHERE location_id=? AND artifact_id=?
        """,
        (expiry or None, utc_now_iso(), location_key, artifact_key),
    )
    return expiry


def republish_bbs_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_id: object,
    location_id: object,
    now_utc: dt.datetime | None = None,
) -> str:
    """Republish a mapping with a fresh retention window, without touching the source."""

    ensure_bbs_library_schema(conn)
    artifact_key = str(artifact_id or "").strip()
    location_key = str(location_id or "").strip()
    if not artifact_key or not location_key:
        raise ValueError("artifact_id and location_id are required")
    if not conn.execute(
        "SELECT 1 FROM bbs_artifacts WHERE artifact_id=? LIMIT 1", (artifact_key,)
    ).fetchone():
        raise ValueError(f"Unknown BBS artifact id: {artifact_key}")
    if not conn.execute(
        "SELECT 1 FROM bbs_locations WHERE location_id=? LIMIT 1", (location_key,)
    ).fetchone():
        raise ValueError(f"Unknown BBS location id: {location_key}")

    now = now_utc or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)
    mode, days = _resolved_location_retention(conn, location_key)
    expiry = "" if mode == RETENTION_MANUAL or days <= 0 else (now + dt.timedelta(days=days)).isoformat()
    mapping = conn.execute(
        """
        SELECT live_name, sort_order, visibility_rule
        FROM bbs_location_artifacts
        WHERE location_id=? AND artifact_id=?
        LIMIT 1
        """,
        (location_key, artifact_key),
    ).fetchone()
    if mapping is None:
        set_bbs_location_artifact(
            conn,
            location_id=location_key,
            artifact_id=artifact_key,
            retention_class=RETENTION_CLASS_NORMAL,
            publish_enabled=True,
            expires_utc=expiry,
        )
    else:
        conn.execute(
            """
            UPDATE bbs_location_artifacts
               SET retention_class=?, publish_enabled=1, disabled_reason='',
                   expires_utc=?, updated_utc=?
             WHERE location_id=? AND artifact_id=?
            """,
            (
                RETENTION_CLASS_NORMAL,
                expiry or None,
                utc_now_iso(),
                location_key,
                artifact_key,
            ),
        )
    return expiry


def _json(value: object, default: str = "{}") -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except Exception:
        return default


def _normalize_retention_mode(value: object) -> str:
    raw = str(value or "").strip().lower().replace(" ", "_")
    aliases = {
        "keep_until_manually_removed": RETENTION_MANUAL,
        "use_global_bbs_archive_policy": RETENTION_GLOBAL_DEFAULT,
        "archive_this_location_by_age": RETENTION_EXPIRE_AFTER_DAYS,
    }
    raw = aliases.get(raw, raw)
    if raw not in {RETENTION_MANUAL, RETENTION_GLOBAL_DEFAULT, RETENTION_EXPIRE_AFTER_DAYS}:
        return RETENTION_GLOBAL_DEFAULT
    return raw


def _normalize_retention_class(value: object) -> str:
    raw = str(value or "").strip().lower()
    return RETENTION_CLASS_KEEP if raw == RETENTION_CLASS_KEEP else RETENTION_CLASS_NORMAL


def _normalize_access_rule(value: object) -> str:
    raw = " ".join(str(value or "Public").strip().replace("_", " ").lower().split())
    aliases = {
        "public": "Public",
        "allowed callsigns": "Allowed callsigns only",
        "allowed callsigns only": "Allowed callsigns only",
        "access code": "Access code required",
        "access code required": "Access code required",
        "allowed callsigns access code": "Allowed callsigns + access code",
        "allowed callsigns + access code": "Allowed callsigns + access code",
    }
    return aliases.get(raw, raw or "Public")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1] or "") for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _connection_db_path(conn: sqlite3.Connection) -> Path | None:
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if str(row[1] or "") != "main":
                continue
            raw = str(row[2] or "").strip()
            if raw and raw != ":memory:":
                return Path(raw)
    except Exception:
        return None
    return None


def _backup_v1_database(
    conn: sqlite3.Connection,
    *,
    reason: str = "bbs-library-schema-v1-to-v2",
) -> ConfigBackupResult | None:
    db_path = _connection_db_path(conn)
    if db_path is None or not db_path.exists():
        return None
    config_root = get_config_dir()
    try:
        db_path.resolve().relative_to(config_root.resolve())
        backup_root = config_root / "backups"
    except (OSError, ValueError):
        backup_root = db_path.parent / "backups"
    result = create_config_backup(
        [db_path],
        reason=reason,
        backup_root=backup_root,
    )
    primary = next(
        (
            item
            for item in result.items
            if Path(item.original_path).expanduser().resolve() == db_path.expanduser().resolve()
        ),
        None,
    )
    if primary is None or primary.status != "backed_up" or not Path(primary.backup_path).is_file():
        detail = primary.error if primary is not None else "backup result omitted the database"
        raise RuntimeError(f"CommsPlan Managed BBS migration backup failed: {detail}")
    try:
        with sqlite3.connect(primary.backup_path) as backup_conn:
            conn.backup(backup_conn)
    except Exception as exc:
        raise RuntimeError(f"CommsPlan Managed BBS migration backup verification failed: {exc}") from exc
    return result


def _legacy_location_policy(metadata_json: object) -> tuple[str, int, str, str]:
    try:
        payload = json.loads(str(metadata_json or "{}"))
    except Exception:
        payload = {}
    if not isinstance(payload, Mapping):
        payload = {}
    mode = _normalize_retention_mode(payload.get("retention_mode") or payload.get("retention_policy"))
    try:
        days = max(0, int(payload.get("retention_days", 0) or 0))
    except (TypeError, ValueError):
        days = 0
    parent_id = str(payload.get("parent_location_id", "") or "").strip()
    access = _normalize_access_rule(payload.get("access_rule") or payload.get("open_rule") or payload.get("visibility_rule"))
    return mode, days, parent_id, access


def _parse_utc(value: object) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _mtime_utc(mtime_ns: object) -> dt.datetime:
    try:
        timestamp = max(0.0, int(mtime_ns or 0) / 1_000_000_000.0)
        return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)


def _global_retention_days(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM bbs_library_meta WHERE key='global_retention_days' LIMIT 1"
    ).fetchone()
    try:
        return max(0, int(row[0])) if row else DEFAULT_BBS_RETENTION_DAYS
    except (TypeError, ValueError):
        return DEFAULT_BBS_RETENTION_DAYS


def _resolved_location_retention(conn: sqlite3.Connection, location_id: str) -> tuple[str, int]:
    row = conn.execute(
        "SELECT retention_mode, retention_days FROM bbs_locations WHERE location_id=? LIMIT 1",
        (location_id,),
    ).fetchone()
    if not row:
        return RETENTION_GLOBAL_DEFAULT, _global_retention_days(conn)
    mode = _normalize_retention_mode(row[0])
    try:
        days = max(0, int(row[1] or 0))
    except (TypeError, ValueError):
        days = 0
    if mode == RETENTION_GLOBAL_DEFAULT:
        days = _global_retention_days(conn)
    if mode == RETENTION_MANUAL:
        days = 0
    return mode, days


def _mapping_expiry_utc(
    conn: sqlite3.Connection,
    location_id: str,
    artifact_id: str,
    *,
    retention_class: object | None = None,
) -> str:
    if retention_class is None:
        retention_row = conn.execute(
            """
            SELECT retention_class
            FROM bbs_location_artifacts
            WHERE location_id=? AND artifact_id=?
            LIMIT 1
            """,
            (location_id, artifact_id),
        ).fetchone()
        effective_class = _normalize_retention_class(retention_row[0]) if retention_row else RETENTION_CLASS_NORMAL
    else:
        effective_class = _normalize_retention_class(retention_class)
    if effective_class == RETENTION_CLASS_KEEP:
        return ""
    mode, days = _resolved_location_retention(conn, location_id)
    if mode == RETENTION_MANUAL or days <= 0:
        return ""
    row = conn.execute(
        "SELECT mtime_ns FROM bbs_artifacts WHERE artifact_id=? LIMIT 1",
        (artifact_id,),
    ).fetchone()
    if not row:
        return ""
    return (_mtime_utc(row[0]) + dt.timedelta(days=days)).isoformat()


def _refresh_location_mapping_expiries(conn: sqlite3.Connection, location_id: str) -> None:
    """Recalculate existing mappings after a location retention policy changes."""

    now = utc_now_iso()
    artifact_ids = conn.execute(
        "SELECT artifact_id FROM bbs_location_artifacts WHERE location_id=?",
        (location_id,),
    ).fetchall()
    for (artifact_id,) in artifact_ids:
        artifact_key = str(artifact_id or "")
        expiry = _mapping_expiry_utc(conn, location_id, artifact_key)
        conn.execute(
            """
            UPDATE bbs_location_artifacts
               SET expires_utc=?, updated_utc=?
             WHERE location_id=? AND artifact_id=?
            """,
            (expiry or None, now, location_id, artifact_key),
        )


def _refresh_artifact_mapping_expiries(conn: sqlite3.Connection, artifact_id: str) -> None:
    """Recalculate mapped expiry when a source file's modification time changes."""

    now = utc_now_iso()
    location_ids = conn.execute(
        "SELECT location_id FROM bbs_location_artifacts WHERE artifact_id=?",
        (artifact_id,),
    ).fetchall()
    for (location_id,) in location_ids:
        location_key = str(location_id or "")
        expiry = _mapping_expiry_utc(conn, location_key, artifact_id)
        conn.execute(
            """
            UPDATE bbs_location_artifacts
               SET expires_utc=?, updated_utc=?
             WHERE location_id=? AND artifact_id=?
            """,
            (expiry or None, now, location_key, artifact_id),
        )


def ensure_bbs_library_schema(conn: sqlite3.Connection) -> None:
    existing_tables = {
        str(row[0] or "")
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'bbs_%'"
        ).fetchall()
    }
    existing_version = 0
    if "bbs_library_meta" in existing_tables:
        row = conn.execute(
            "SELECT value FROM bbs_library_meta WHERE key='schema_version' LIMIT 1"
        ).fetchone()
        try:
            existing_version = int(row[0]) if row else 1
        except (TypeError, ValueError):
            existing_version = 1
    elif existing_tables:
        existing_version = 1
    if existing_version > BBS_LIBRARY_SCHEMA_VERSION:
        raise RuntimeError(
            f"CommsPlan Managed BBS schema {existing_version} is newer than supported {BBS_LIBRARY_SCHEMA_VERSION}."
        )
    if existing_version == BBS_LIBRARY_SCHEMA_VERSION:
        return
    if existing_version == 1:
        _backup_v1_database(conn, reason="bbs-library-schema-v1-to-v2")

    savepoint = "fio_bbs_schema_v2"
    conn.execute(f"SAVEPOINT {savepoint}")
    cur = conn.cursor()
    try:
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS bbs_library_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
        )
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS bbs_artifacts (
            artifact_id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            source_id TEXT,
            source_path TEXT UNIQUE,
            display_name TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            mtime_ns INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT,
            q_id TEXT,
            block_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            deleted INTEGER NOT NULL DEFAULT 0,
            source_state TEXT NOT NULL DEFAULT 'present',
            missing_since_utc TEXT,
            last_reconciled_utc TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """
        )
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS bbs_locations (
            location_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source_dir TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            parent_location_id TEXT NOT NULL DEFAULT '',
            access_rule TEXT NOT NULL DEFAULT 'public',
            retention_mode TEXT NOT NULL DEFAULT 'global_default',
            retention_days INTEGER NOT NULL DEFAULT 0,
            updated_utc TEXT NOT NULL
        )
        """
        )
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS bbs_location_artifacts (
            location_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            live_name TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            visibility_rule TEXT NOT NULL DEFAULT 'public',
            retention_class TEXT NOT NULL DEFAULT 'normal',
            publish_enabled INTEGER NOT NULL DEFAULT 1,
            disabled_reason TEXT NOT NULL DEFAULT '',
            expires_utc TEXT,
            last_reconciled_utc TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            PRIMARY KEY(location_id, artifact_id)
        )
        """
        )
        additions = {
            "bbs_artifacts": {
                "source_state": "TEXT NOT NULL DEFAULT 'present'",
                "missing_since_utc": "TEXT",
                "last_reconciled_utc": "TEXT",
            },
            "bbs_locations": {
                "parent_location_id": "TEXT NOT NULL DEFAULT ''",
                "access_rule": "TEXT NOT NULL DEFAULT 'public'",
                "retention_mode": "TEXT NOT NULL DEFAULT 'global_default'",
                "retention_days": "INTEGER NOT NULL DEFAULT 0",
            },
            "bbs_location_artifacts": {
                "disabled_reason": "TEXT NOT NULL DEFAULT ''",
                "expires_utc": "TEXT",
                "last_reconciled_utc": "TEXT",
            },
        }
        for table, columns in additions.items():
            present = _table_columns(conn, table)
            for column, declaration in columns.items():
                if column not in present:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        if existing_version == 1:
            cur.execute(
                "UPDATE bbs_artifacts SET source_state=CASE WHEN deleted<>0 THEN 'deleted' ELSE 'present' END"
            )
            cur.execute(
                "UPDATE bbs_location_artifacts "
                "SET disabled_reason=CASE WHEN publish_enabled=0 THEN ? ELSE '' END",
                (DISABLED_OPERATOR,),
            )
            rows = cur.execute(
                "SELECT location_id, metadata_json FROM bbs_locations"
            ).fetchall()
            for location_id, metadata_json in rows:
                mode, days, parent_id, access = _legacy_location_policy(metadata_json)
                cur.execute(
                    "UPDATE bbs_locations SET retention_mode=?, retention_days=?, "
                    "parent_location_id=?, access_rule=? WHERE location_id=?",
                    (mode, days, parent_id, access, str(location_id or "")),
                )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_bbs_artifacts_path ON bbs_artifacts(source_path)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bbs_artifacts_qid ON bbs_artifacts(q_id, block_id)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_bbs_artifacts_source_state "
            "ON bbs_artifacts(source_state, last_reconciled_utc)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_bbs_location_artifacts_publish "
            "ON bbs_location_artifacts(location_id, publish_enabled, sort_order)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_bbs_location_artifacts_expiry "
            "ON bbs_location_artifacts(publish_enabled, expires_utc)"
        )
        cur.execute(
            "INSERT OR REPLACE INTO bbs_library_meta(key, value) VALUES('schema_version', ?)",
            (str(BBS_LIBRARY_SCHEMA_VERSION),),
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def ensure_bbs_library_db(db_path: str | Path | None = None) -> Path:
    path = Path(db_path) if db_path else default_bbs_library_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_sqlite(path) as conn:
        ensure_bbs_library_schema(conn)
    return path


def _profile_json_list(value: object) -> list[Mapping[str, object]]:
    payload = value
    if isinstance(value, str):
        try:
            payload = json.loads(value or "[]")
        except Exception:
            payload = []
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray)):
        return []
    return [item for item in payload if isinstance(item, Mapping)]


def import_legacy_station_bbs_profiles(
    conn: sqlite3.Connection,
    profiles: Sequence[Mapping[str, object]],
) -> int:
    """Copy legacy radio-owned BBS policy into the station catalog once.

    Legacy profile fields remain untouched for downgrade/recovery. Existing station
    catalog rows always win, so reruns and divergent radio copies cannot overwrite
    an operator's station-level edits.
    """

    ensure_bbs_library_schema(conn)
    marker = conn.execute(
        "SELECT value FROM bbs_library_meta WHERE key='legacy_profile_import_v1' LIMIT 1"
    ).fetchone()
    if marker and str(marker[0] or "") == "complete":
        return 0
    ordered = sorted(
        (dict(profile) for profile in profiles if isinstance(profile, Mapping)),
        key=lambda profile: (
            -int(profile.get("runtime_primary", 0) or 0),
            -int(profile.get("runtime_active", 0) or 0),
            int(profile.get("display_order", 0) or 0),
            int(profile.get("id", 0) or 0),
        ),
    )
    candidates: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for profile in ordered:
        for location in _profile_json_list(profile.get("varac_bbs_vault_locations_v1", [])):
            candidates.append((profile, location))
    if ordered or candidates:
        _backup_v1_database(conn, reason="bbs-station-ownership-import-v1")
    now = utc_now_iso()
    imported = 0
    for _profile, location in candidates:
        location_id = str(location.get("id", "") or "").strip()
        if not location_id:
            continue
        if conn.execute("SELECT 1 FROM bbs_locations WHERE location_id=?", (location_id,)).fetchone():
            continue
        mode = _normalize_retention_mode(
            location.get("retention_mode") or location.get("retention_policy")
        )
        try:
            retention_days = max(0, int(location.get("retention_days", 0) or 0))
        except (TypeError, ValueError):
            retention_days = 0
        conn.execute(
            """
            INSERT INTO bbs_locations(
                location_id, name, source_dir, enabled, metadata_json, updated_utc,
                parent_location_id, access_rule, retention_mode, retention_days
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                location_id,
                str(location.get("name", location_id) or location_id).strip() or location_id,
                str(location.get("source_dir", "") or "").strip(),
                1 if bool(location.get("enabled", True)) else 0,
                _json(dict(location)),
                now,
                str(location.get("parent_location_id", "") or "").strip(),
                _normalize_access_rule(
                    location.get("access_rule") or location.get("open_rule") or location.get("visibility_rule")
                ),
                mode,
                retention_days,
            ),
        )
        imported += 1
    if ordered:
        primary = ordered[0]
        station_values = {
            "station_enabled": "1" if bool(primary.get("varac_bbs_vault_enabled", False)) else "0",
            "station_managed_root": str(primary.get("varac_bbs_vault_managed_root", "") or ""),
            "station_default_location_id": str(primary.get("varac_bbs_vault_default_location_id", "") or ""),
            "station_global_code_policy": str(primary.get("varac_bbs_vault_global_code_policy", "") or ""),
            "station_allowed_callsigns": str(primary.get("varac_bbs_allowed_callsigns", "") or ""),
            "station_limit_access_enabled": "1" if bool(primary.get("varac_bbs_limit_access_enabled", False)) else "0",
            "station_sweeper_rules_json": _json(
                _profile_json_list(primary.get("varac_bbs_sweeper_rules_v1", [])), default="[]"
            ),
        }
        try:
            station_values["global_retention_days"] = str(
                max(0, int(primary.get("varac_bbs_auto_archive_days", DEFAULT_BBS_RETENTION_DAYS) or 0))
            )
        except (TypeError, ValueError):
            station_values["global_retention_days"] = str(DEFAULT_BBS_RETENTION_DAYS)
        for key, value in station_values.items():
            conn.execute(
                "INSERT OR IGNORE INTO bbs_library_meta(key, value) VALUES(?, ?)",
                (key, value),
            )
    conn.execute(
        "INSERT OR REPLACE INTO bbs_library_meta(key, value) VALUES('legacy_profile_import_v1', 'complete')"
    )
    return imported


def sync_bbs_location_from_folder(
    conn: sqlite3.Connection,
    *,
    location_id: object,
    name: object,
    source_dir: object,
    enabled: bool = True,
    metadata: Mapping[str, object] | None = None,
) -> int:
    ensure_bbs_library_schema(conn)
    location_key = str(location_id or "").strip()
    if not location_key:
        return 0
    source_path = Path(str(source_dir or "")).expanduser()
    now = utc_now_iso()
    metadata_value = dict(metadata or {})
    retention_mode = _normalize_retention_mode(
        metadata_value.get("retention_mode") or metadata_value.get("retention_policy")
    )
    try:
        retention_days = max(0, int(metadata_value.get("retention_days", 0) or 0))
    except (TypeError, ValueError):
        retention_days = 0
    conn.execute(
        """
        INSERT INTO bbs_locations(
            location_id, name, source_dir, enabled, metadata_json, updated_utc,
            parent_location_id, access_rule, retention_mode, retention_days
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(location_id) DO UPDATE SET
            updated_utc=excluded.updated_utc
        """,
        (
            location_key,
            str(name or location_key).strip() or location_key,
            str(source_path),
            1 if enabled else 0,
            _json(metadata_value),
            now,
            str(metadata_value.get("parent_location_id", "") or "").strip(),
            _normalize_access_rule(
                metadata_value.get("access_rule")
                or metadata_value.get("open_rule")
                or metadata_value.get("visibility_rule")
            ),
            retention_mode,
            retention_days,
        ),
    )
    if not enabled or not source_path.exists() or not source_path.is_dir():
        return 0
    resolved_retention_mode, resolved_retention_days = _resolved_location_retention(conn, location_key)

    existing_rows = {
        str(row[0] or ""): str(row[1] or "")
        for row in conn.execute(
            """
            SELECT artifact_id, source_path
            FROM bbs_artifacts
            WHERE source_kind='managed_location_file'
              AND source_id=?
            """,
            (location_key,),
        ).fetchall()
    }
    seen_artifact_ids: set[str] = set()
    added = 0
    for order, child in enumerate(sorted(source_path.iterdir(), key=lambda item: item.name.lower()), start=1):
        if not child.is_file():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        resolved = str(child.resolve())
        artifact_id = stable_bbs_artifact_id("file", resolved)
        seen_artifact_ids.add(artifact_id)
        expiry = None
        if resolved_retention_mode != RETENTION_MANUAL and resolved_retention_days > 0:
            expiry = (
                _mtime_utc(stat.st_mtime_ns) + dt.timedelta(days=resolved_retention_days)
            ).isoformat()
        conn.execute(
            """
            INSERT INTO bbs_artifacts(
                artifact_id, source_kind, source_id, source_path, display_name,
                size, mtime_ns, content_hash, q_id, block_id, metadata_json,
                deleted, created_utc, updated_utc
            )
            VALUES(?, 'managed_location_file', ?, ?, ?, ?, ?, '', '', '', '{}', 0, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                source_id=excluded.source_id,
                source_path=excluded.source_path,
                display_name=excluded.display_name,
                size=excluded.size,
                mtime_ns=excluded.mtime_ns,
                deleted=0,
                source_state='present',
                missing_since_utc=NULL,
                last_reconciled_utc=excluded.updated_utc,
                updated_utc=excluded.updated_utc
            """,
            (
                artifact_id,
                location_key,
                resolved,
                child.name,
                int(stat.st_size or 0),
                int(stat.st_mtime_ns or 0),
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO bbs_location_artifacts(
                location_id, artifact_id, live_name, sort_order, visibility_rule,
                retention_class, publish_enabled, created_utc, updated_utc,
                disabled_reason, expires_utc, last_reconciled_utc
            )
            VALUES(?, ?, '', ?, 'public', 'normal', 1, ?, ?, '', ?, ?)
            ON CONFLICT(location_id, artifact_id) DO UPDATE SET
                sort_order=excluded.sort_order,
                expires_utc=excluded.expires_utc,
                last_reconciled_utc=excluded.last_reconciled_utc,
                updated_utc=excluded.updated_utc
            """,
            (location_key, artifact_id, order, now, now, expiry, now),
        )
        added += 1
    if existing_rows:
        missing_now = utc_now_iso()
        for artifact_id, source_path_text in existing_rows.items():
            if artifact_id in seen_artifact_ids:
                continue
            candidate = Path(source_path_text) if source_path_text else None
            if candidate is not None and candidate.exists() and candidate.is_file():
                continue
            conn.execute(
                """
                UPDATE bbs_artifacts
                   SET source_state='missing',
                       missing_since_utc=COALESCE(missing_since_utc, ?),
                       last_reconciled_utc=?,
                       updated_utc=?
                 WHERE artifact_id=?
                   AND source_state<>'deleted'
                """,
                (missing_now, missing_now, missing_now, artifact_id),
            )
    return added


def sync_bbs_locations_from_folders(
    db_path: str | Path,
    locations: Sequence[Mapping[str, object]],
) -> int:
    path = ensure_bbs_library_db(db_path)
    synced = 0
    with connect_sqlite(path) as conn:
        ensure_bbs_library_schema(conn)
        with conn:
            for location in locations:
                synced += sync_bbs_location_from_folder(
                    conn,
                    location_id=location.get("id"),
                    name=location.get("name"),
                    source_dir=location.get("source_dir"),
                    enabled=bool(location.get("enabled", True)),
                    metadata=location,
                )
    return synced


def location_has_bbs_catalog(conn: sqlite3.Connection, location_id: object) -> bool:
    ensure_bbs_library_schema(conn)
    key = str(location_id or "").strip()
    if not key:
        return False
    row = conn.execute("SELECT 1 FROM bbs_locations WHERE location_id=? LIMIT 1", (key,)).fetchone()
    return bool(row)


def bbs_location_catalog_source_dir(conn: sqlite3.Connection, location_id: object) -> str:
    ensure_bbs_library_schema(conn)
    key = str(location_id or "").strip()
    if not key:
        return ""
    row = conn.execute("SELECT source_dir FROM bbs_locations WHERE location_id=? LIMIT 1", (key,)).fetchone()
    return str(row[0] or "").strip() if row else ""


def list_bbs_location_manifest_rows(
    conn: sqlite3.Connection,
    location_id: object,
    *,
    include_disabled: bool = False,
) -> list[BbsLibraryManifestRow]:
    ensure_bbs_library_schema(conn)
    key = str(location_id or "").strip()
    if not key:
        return []
    enabled_clause = "" if include_disabled else (
        "AND l.enabled=1 AND la.publish_enabled=1 AND a.deleted=0 "
        "AND a.source_state='present' "
        "AND (la.expires_utc IS NULL OR la.expires_utc='' OR la.expires_utc>?)"
    )
    params: tuple[object, ...] = (key,) if include_disabled else (key, utc_now_iso())
    rows = conn.execute(
        f"""
        SELECT
            a.artifact_id,
            a.source_path,
            a.display_name,
            COALESCE(NULLIF(la.live_name, ''), a.display_name) AS live_name,
            a.size,
            a.mtime_ns,
            a.content_hash,
            a.q_id,
            a.block_id,
            a.metadata_json
        FROM bbs_location_artifacts la
        JOIN bbs_artifacts a ON a.artifact_id = la.artifact_id
        JOIN bbs_locations l ON l.location_id = la.location_id
        WHERE la.location_id=?
          {enabled_clause}
          AND COALESCE(a.source_path, '') != ''
        ORDER BY la.sort_order ASC, live_name COLLATE NOCASE ASC, a.artifact_id ASC
        """,
        params,
    ).fetchall()
    result: list[BbsLibraryManifestRow] = []
    for row in rows:
        try:
            metadata = json.loads(row[9] or "{}")
        except Exception:
            metadata = {}
        result.append(
            BbsLibraryManifestRow(
                artifact_id=str(row[0] or ""),
                source_path=str(row[1] or ""),
                display_name=str(row[2] or ""),
                live_name=str(row[3] or ""),
                size=int(row[4] or 0),
                mtime_ns=int(row[5] or 0),
                content_hash=str(row[6] or ""),
                q_id=str(row[7] or ""),
                block_id=str(row[8] or ""),
                metadata=metadata if isinstance(metadata, Mapping) else {},
            )
        )
    return result


def list_bbs_locations(conn: sqlite3.Connection, *, include_disabled: bool = True) -> list[BbsLocationRecord]:
    ensure_bbs_library_schema(conn)
    disabled_clause = "" if include_disabled else "WHERE enabled=1"
    rows = conn.execute(
        f"""
        SELECT location_id, name, source_dir, enabled, parent_location_id,
               access_rule, retention_mode, retention_days, metadata_json
        FROM bbs_locations
        {disabled_clause}
        ORDER BY name COLLATE NOCASE ASC, location_id ASC
        """
    ).fetchall()
    result: list[BbsLocationRecord] = []
    for row in rows:
        try:
            metadata = json.loads(row[8] or "{}")
        except Exception:
            metadata = {}
        result.append(
            BbsLocationRecord(
                location_id=str(row[0] or ""),
                name=str(row[1] or row[0] or ""),
                source_dir=str(row[2] or ""),
                enabled=bool(row[3]),
                parent_location_id=str(row[4] or ""),
                access_rule=_normalize_access_rule(row[5]),
                retention_mode=_normalize_retention_mode(row[6]),
                retention_days=max(0, int(row[7] or 0)),
                metadata=metadata if isinstance(metadata, Mapping) else {},
            )
        )
    return result


def list_bbs_artifact_location_ids(conn: sqlite3.Connection, artifact_id: object) -> tuple[str, ...]:
    ensure_bbs_library_schema(conn)
    key = str(artifact_id or "").strip()
    if not key:
        return ()
    rows = conn.execute(
        """
        SELECT location_id
        FROM bbs_location_artifacts
        WHERE artifact_id=? AND publish_enabled=1
        ORDER BY location_id ASC
        """,
        (key,),
    ).fetchall()
    return tuple(str(row[0] or "") for row in rows if str(row[0] or ""))


def set_bbs_artifact_locations(
    conn: sqlite3.Connection,
    *,
    artifact_id: object,
    location_ids: Iterable[object],
) -> tuple[str, ...]:
    """Atomically replace an artifact's operator-selected managed locations."""

    ensure_bbs_library_schema(conn)
    artifact_key = str(artifact_id or "").strip()
    if not artifact_key:
        raise ValueError("artifact_id is required")
    requested = {str(value or "").strip() for value in location_ids}
    requested.discard("")
    known = {
        str(row[0] or "")
        for row in conn.execute("SELECT location_id FROM bbs_locations").fetchall()
    }
    unknown = requested - known
    if unknown:
        raise ValueError(f"Unknown BBS location id(s): {', '.join(sorted(unknown))}")
    if not conn.execute("SELECT 1 FROM bbs_artifacts WHERE artifact_id=?", (artifact_key,)).fetchone():
        raise ValueError(f"Unknown BBS artifact id: {artifact_key}")
    now = utc_now_iso()
    existing = {
        str(row[0] or "")
        for row in conn.execute(
            "SELECT location_id FROM bbs_location_artifacts WHERE artifact_id=?",
            (artifact_key,),
        ).fetchall()
    }
    for location_id in sorted(requested):
        expiry = _mapping_expiry_utc(conn, location_id, artifact_key)
        if location_id in existing:
            conn.execute(
                """
                UPDATE bbs_location_artifacts
                   SET publish_enabled=1, disabled_reason='', expires_utc=?, updated_utc=?
                 WHERE location_id=? AND artifact_id=?
                """,
                (expiry or None, now, location_id, artifact_key),
            )
        else:
            set_bbs_location_artifact(
                conn,
                location_id=location_id,
                artifact_id=artifact_key,
                publish_enabled=True,
                expires_utc=expiry,
            )
    for location_id in sorted(existing - requested):
        conn.execute(
            """
            UPDATE bbs_location_artifacts
               SET publish_enabled=0, disabled_reason=?, updated_utc=?
             WHERE location_id=? AND artifact_id=?
            """,
            (DISABLED_OPERATOR, now, location_id, artifact_key),
        )
    return tuple(sorted(requested))


def list_bbs_admin_rows(
    conn: sqlite3.Connection,
    *,
    location_id: object = "",
    limit: int = 200,
    offset: int = 0,
    now_utc: dt.datetime | None = None,
) -> list[BbsArtifactAdminRow]:
    ensure_bbs_library_schema(conn)
    location_key = str(location_id or "").strip()
    where = "WHERE la.location_id=?" if location_key else ""
    params: list[object] = [location_key] if location_key else []
    params.extend([max(1, min(500, int(limit or 200))), max(0, int(offset or 0))])
    rows = conn.execute(
        f"""
        SELECT a.artifact_id, a.source_path, a.source_kind, a.display_name, a.size, a.mtime_ns,
               a.source_state, la.location_id, l.name, la.publish_enabled,
               la.disabled_reason, l.retention_mode, l.retention_days,
               la.retention_class, la.expires_utc, a.deleted
        FROM bbs_location_artifacts la
        JOIN bbs_artifacts a ON a.artifact_id=la.artifact_id
        JOIN bbs_locations l ON l.location_id=la.location_id
        {where}
        ORDER BY a.mtime_ns DESC, a.display_name COLLATE NOCASE ASC, la.location_id ASC
        LIMIT ? OFFSET ?
        """,
        tuple(params),
    ).fetchall()
    now = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    global_days = _global_retention_days(conn)
    result: list[BbsArtifactAdminRow] = []
    for row in rows:
        modified = _mtime_utc(row[5])
        source_state = str(row[6] or SOURCE_PRESENT)
        enabled = bool(row[9])
        disabled_reason = str(row[10] or "")
        retention_class = _normalize_retention_class(row[13])
        expiry = _parse_utc(row[14])
        if not enabled:
            state = disabled_reason or DISABLED_OPERATOR
        elif source_state == SOURCE_MISSING:
            state = "source_missing"
        elif source_state == SOURCE_DELETED or bool(row[15]):
            state = SOURCE_DELETED
        elif retention_class != RETENTION_CLASS_KEEP and expiry is not None and expiry <= now:
            state = DISABLED_RETENTION
        else:
            state = "published"
        mode = _normalize_retention_mode(row[11])
        try:
            days = max(0, int(row[12] or 0))
        except (TypeError, ValueError):
            days = 0
        if mode == RETENTION_GLOBAL_DEFAULT:
            days = global_days
        elif mode == RETENTION_MANUAL:
            days = 0
        result.append(
            BbsArtifactAdminRow(
                artifact_id=str(row[0] or ""),
                source_path=str(row[1] or ""),
                source_kind=str(row[2] or ""),
                display_name=str(row[3] or ""),
                size=int(row[4] or 0),
                mtime_ns=int(row[5] or 0),
                source_state=source_state,
                location_id=str(row[7] or ""),
                location_name=str(row[8] or row[7] or ""),
                published=state == "published",
                publication_state=state,
                disabled_reason=disabled_reason,
                retention_mode=mode,
                retention_days=days,
                retention_class=retention_class,
                expires_utc=str(row[14] or ""),
                modified_utc=modified.isoformat(),
                age_days=max(0, int((now - modified).total_seconds() // 86400)),
            )
        )
    return result


def reconcile_bbs_publications(
    conn: sqlite3.Connection,
    *,
    batch_size: int = BBS_RECONCILE_BATCH_SIZE,
    now_utc: dt.datetime | None = None,
) -> BbsReconcileResult:
    """Bounded source/retention reconciliation; never walks source directories."""

    ensure_bbs_library_schema(conn)
    limit = max(1, min(1000, int(batch_size or BBS_RECONCILE_BATCH_SIZE)))
    now = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    now_iso = now.isoformat()
    without_expiry = conn.execute(
        """
        SELECT la.location_id, la.artifact_id
        FROM bbs_location_artifacts la
        WHERE la.publish_enabled=1 AND (la.expires_utc IS NULL OR la.expires_utc='')
        """
    ).fetchall()
    for location_id, artifact_id in without_expiry:
        expiry = _mapping_expiry_utc(conn, str(location_id or ""), str(artifact_id or ""))
        if expiry:
            conn.execute(
                "UPDATE bbs_location_artifacts SET expires_utc=? WHERE location_id=? AND artifact_id=?",
                (expiry, str(location_id or ""), str(artifact_id or "")),
            )
    # A keep override is intentionally expiry-free, even if an older row
    # retained a stale timestamp from before the override was applied.
    conn.execute(
        """
        UPDATE bbs_location_artifacts
           SET expires_utc=NULL
         WHERE retention_class=? AND expires_utc IS NOT NULL AND expires_utc<>''
        """,
        (RETENTION_CLASS_KEEP,),
    )
    expired_cur = conn.execute(
        """
        UPDATE bbs_location_artifacts
           SET publish_enabled=0, disabled_reason=?, last_reconciled_utc=?, updated_utc=?
         WHERE publish_enabled=1
           AND retention_class<>?
           AND expires_utc IS NOT NULL AND expires_utc<>'' AND expires_utc<=?
        """,
        (DISABLED_RETENTION, now_iso, now_iso, RETENTION_CLASS_KEEP, now_iso),
    )
    rows = conn.execute(
        """
        SELECT artifact_id, source_path, source_state
        FROM bbs_artifacts
        WHERE deleted=0 AND COALESCE(source_path, '')<>''
          AND (
              source_state='missing'
              OR artifact_id IN (
                  SELECT artifact_id FROM bbs_location_artifacts WHERE publish_enabled=1
              )
          )
        ORDER BY CASE WHEN last_reconciled_utc IS NULL OR last_reconciled_utc='' THEN 0 ELSE 1 END,
                 last_reconciled_utc ASC, artifact_id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    missing = restored = 0
    for artifact_id, source_path, old_state in rows:
        present = False
        try:
            present = Path(str(source_path or "")).is_file()
        except OSError:
            present = False
        next_state = SOURCE_PRESENT if present else SOURCE_MISSING
        if next_state == SOURCE_MISSING and str(old_state or SOURCE_PRESENT) != SOURCE_MISSING:
            missing += 1
        if next_state == SOURCE_PRESENT and str(old_state or SOURCE_PRESENT) == SOURCE_MISSING:
            restored += 1
        conn.execute(
            """
            UPDATE bbs_artifacts
               SET source_state=?,
                   missing_since_utc=CASE WHEN ?='missing' THEN COALESCE(missing_since_utc, ?) ELSE NULL END,
                   last_reconciled_utc=?, updated_utc=?
             WHERE artifact_id=?
            """,
            (next_state, next_state, now_iso, now_iso, now_iso, str(artifact_id or "")),
        )
    remaining_row = conn.execute(
        """
        SELECT COUNT(DISTINCT a.artifact_id)
        FROM bbs_artifacts a
        JOIN bbs_location_artifacts la ON la.artifact_id=a.artifact_id
        WHERE a.deleted=0 AND la.publish_enabled=1
          AND (a.last_reconciled_utc IS NULL OR a.last_reconciled_utc<>?)
        """,
        (now_iso,),
    ).fetchone()
    return BbsReconcileResult(
        checked=len(rows),
        missing=missing,
        restored=restored,
        expired=int(expired_cur.rowcount or 0),
        remaining=max(0, int(remaining_row[0] or 0)) if remaining_row else 0,
    )


def log_bbs_library_sync_failure(exc: Exception) -> None:
    log.debug("varac_bbs_library: DB-backed manifest sync unavailable: %s", exc)
