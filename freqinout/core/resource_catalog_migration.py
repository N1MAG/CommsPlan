"""Backup-first, idempotent legacy migration for the Local Nets resource catalog.

LN-1 deliberately leaves the legacy tables authoritative.  This migrator builds
and refreshes a canonical shadow, records every source row, and checkpoints the
catalog as ``shadow_ready``.  It never exposes UI or changes scheduler inputs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from freqinout.core.config_backup import ConfigBackupResult, create_config_backup
from freqinout.core.operating_group_identity import (
    ensure_operating_group_keys,
    operating_group_key_for_name,
)
from freqinout.core.resource_catalog_store import ensure_resource_catalog_schema
from freqinout.core.resource_reference_validation import load_bundled_reference_manifest
from freqinout.core.sqlite_utils import connect_sqlite, connect_sqlite_readonly, table_exists


SCHEMA_VERSION = "1"
STATE_KEY = "resource_catalog"
_KEY_NAMESPACE = uuid.UUID("ea3a25ae-7f15-5a8a-a8ce-93cc8346f119")
_GENERIC_NET_NAMES = {
    "",
    "default calling frequencies",
    "calling frequencies",
    "working frequencies",
    "digital frequencies",
    "default",
}


@dataclass(frozen=True, slots=True)
class MigrationClassification:
    legacy_table_name: str
    legacy_resource_id: str
    classification: str
    source_row_hash: str
    frequency_resource_key: str | None = None
    net_entry_key: str | None = None
    net_session_key: str | None = None
    diagnostics: tuple[str, ...] = ()
    legacy_row: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResourceCatalogMigrationReport:
    total_legacy_rows: int
    migrated_frequency_count: int
    migrated_net_entry_count: int
    migrated_session_count: int
    review_required_count: int
    unchanged_count: int
    group_rows_updated: int
    classifications: tuple[MigrationClassification, ...]
    backup_dir: str | None = None
    authority_state: str = "legacy"


class ResourceCatalogBackupError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _stable_key(prefix: str, identity: str) -> str:
    return f"{prefix}_{uuid.uuid5(_KEY_NAMESPACE, identity)}"


def _text(value: object) -> str:
    return str(value or "").strip()


def _frequency_hz(value: object) -> int | None:
    text = _text(value).replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    # Legacy UI persists MHz.  Values already expressed as Hz remain Hz.
    hz = round(number if abs(number) >= 100_000 else number * 1_000_000)
    return hz if hz > 0 else None


def _duration_minutes(start: object, end: object) -> int | None:
    def minutes(value: object) -> int | None:
        parts = _text(value).split(":")
        if len(parts) < 2:
            return None
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    first, last = minutes(start), minutes(end)
    if first is None or last is None:
        return None
    delta = last - first
    return delta if delta > 0 else delta + 24 * 60


def _service(row: Mapping[str, Any]) -> str:
    band = _text(row.get("band")).upper()
    return "GMRS" if "GMRS" in band else "AMATEUR"


def _source_kind(value: object) -> str:
    normalized = _text(value).casefold()
    if normalized in {"builtin", "bundled"}:
        return "bundled"
    if normalized == "imported":
        return "imported"
    return "station"


def _source_key(row: Mapping[str, Any]) -> str:
    identity = "|".join(
        (
            _source_kind(row.get("source_type")),
            _text(row.get("resource_set")).casefold(),
            _text(row.get("source_ref")).casefold(),
        )
    )
    return _stable_key("source", f"legacy-source:{identity}")


def _is_credible_session(row: Mapping[str, Any], hz: int | None) -> bool:
    name = _text(row.get("net_name")).casefold()
    return bool(
        hz
        and name not in _GENERIC_NET_NAMES
        and _text(row.get("band"))
        and _text(row.get("day_utc"))
        and _text(row.get("start_utc"))
        and _duration_minutes(row.get("start_utc"), row.get("end_utc")) is not None
    )


def _classify_legacy_row(table: str, row_id: str, row: Mapping[str, Any]) -> MigrationClassification:
    payload = dict(row)
    source_hash = _hash(payload)
    identity = f"{table}:{row_id}"
    hz = _frequency_hz(row.get("frequency"))
    diagnostics: list[str] = []
    frequency_key = _stable_key("frequency", identity) if hz else None
    if not hz:
        diagnostics.append("frequency is missing or invalid")
    credible = _is_credible_session(row, hz)
    entry_key = _stable_key(
        "net",
        "legacy-net:"
        + "|".join(
            (
                _source_key(row),
                _text(row.get("net_name")).casefold(),
                _text(row.get("group_name")).casefold(),
            )
        ),
    ) if credible else None
    session_key = _stable_key("session", identity) if credible else None
    if credible:
        classification = "frequency_and_session"
    elif hz:
        classification = "frequency_only"
        if _text(row.get("net_name")) and _text(row.get("net_name")).casefold() not in _GENERIC_NET_NAMES:
            diagnostics.append("net/session fields are incomplete")
    else:
        classification = "review_required"
    diagnostic_state = "review_required" if diagnostics else "ready"
    if diagnostic_state == "review_required" and classification != "review_required":
        classification = "review_required_frequency_only"
    return MigrationClassification(
        legacy_table_name=table,
        legacy_resource_id=row_id,
        classification=classification,
        source_row_hash=source_hash,
        frequency_resource_key=frequency_key,
        net_entry_key=entry_key,
        net_session_key=session_key,
        diagnostics=tuple(diagnostics),
        legacy_row=payload,
    )


def _read_legacy_rows(nets_db_path: Path) -> list[MigrationClassification]:
    if not nets_db_path.exists():
        return []
    conn = connect_sqlite_readonly(nets_db_path, row_factory=sqlite3.Row)
    try:
        if not table_exists(conn, "net_resources"):
            return []
        rows = conn.execute("SELECT * FROM net_resources ORDER BY id").fetchall()
        return [_classify_legacy_row("net_resources", str(row["id"]), dict(row)) for row in rows]
    finally:
        conn.close()


def _read_settings_rows(settings_db_path: Path | None) -> tuple[list[dict[str, Any]], list[MigrationClassification]]:
    if settings_db_path is None or not settings_db_path.exists():
        return [], []
    conn = connect_sqlite_readonly(settings_db_path, row_factory=sqlite3.Row)
    try:
        if not table_exists(conn, "kv"):
            return [], []
        values = {row["key"]: row["value"] for row in conn.execute(
            "SELECT key,value FROM kv WHERE key IN ('operating_groups','local_net_profiles')"
        )}
    finally:
        conn.close()
    try:
        groups = json.loads(values.get("operating_groups", "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        groups = []
    if not isinstance(groups, list):
        groups = []
    try:
        profiles = json.loads(values.get("local_net_profiles", "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        profiles = []
    classified: list[MigrationClassification] = []
    for index, raw in enumerate(profiles if isinstance(profiles, list) else []):
        if not isinstance(raw, Mapping):
            raw = {"raw_value": raw}
        row = dict(raw)
        target_hz = _frequency_hz(row.get("target"))
        diagnostics = () if target_hz else ("target is not a safely parseable frequency",)
        row_hash = _hash(row)
        row_id = f"{index}:{row_hash[:16]}"
        classified.append(
            MigrationClassification(
                legacy_table_name="settings.local_net_profiles",
                legacy_resource_id=row_id,
                classification="review_required_frequency_only" if target_hz else "review_required",
                source_row_hash=row_hash,
                frequency_resource_key=_stable_key("frequency", f"settings.local_net_profiles:{row_id}") if target_hz else None,
                diagnostics=diagnostics,
                legacy_row=row,
            )
        )
    return [dict(row) for row in groups if isinstance(row, Mapping)], classified


def dry_run_resource_catalog_migration(
    nets_db_path: str | Path,
    settings_db_path: str | Path | None = None,
) -> ResourceCatalogMigrationReport:
    """Classify legacy inputs without creating a file, table, journal, or backup."""
    nets_path = Path(nets_db_path)
    settings_path = Path(settings_db_path) if settings_db_path is not None else None
    rows = _read_legacy_rows(nets_path)
    groups, profile_rows = _read_settings_rows(settings_path)
    rows.extend(profile_rows)
    ensured_groups = ensure_operating_group_keys(groups)
    group_updates = sum(1 for before, after in zip(groups, ensured_groups) if before != after)
    return _report(rows, group_updates=group_updates, authority_state="legacy")


def _report(
    rows: Sequence[MigrationClassification],
    *,
    group_updates: int,
    authority_state: str,
    backup_dir: str | None = None,
    unchanged: int = 0,
) -> ResourceCatalogMigrationReport:
    return ResourceCatalogMigrationReport(
        total_legacy_rows=len(rows),
        migrated_frequency_count=sum(bool(row.frequency_resource_key) for row in rows),
        migrated_net_entry_count=sum(bool(row.net_entry_key) for row in rows),
        migrated_session_count=sum(bool(row.net_session_key) for row in rows),
        review_required_count=sum(row.classification.startswith("review_required") for row in rows),
        unchanged_count=unchanged,
        group_rows_updated=group_updates,
        classifications=tuple(rows),
        backup_dir=backup_dir,
        authority_state=authority_state,
    )


def _validate_backup(result: ConfigBackupResult, required_paths: Iterable[Path]) -> None:
    required = {str(path) for path in required_paths if path.exists()}
    backed_up = {item.original_path for item in result.items if item.status == "backed_up"}
    missing = required.difference(backed_up)
    if missing:
        raise ResourceCatalogBackupError("Required backup failed: " + ", ".join(sorted(missing)))


def _ensure_hf_subscription_columns(conn: sqlite3.Connection) -> None:
    for table_name in ("net_schedule_tab", "net_schedule"):
        if not table_exists(conn, table_name):
            continue
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
        for name, ddl in (
            ("net_session_key", "TEXT"),
            ("accepted_session_version_hash", "TEXT"),
            ("accepted_resource_version_hash", "TEXT"),
            ("accepted_snapshot_json", "TEXT"),
        ):
            if name not in existing:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}")


def _upsert_source(conn: sqlite3.Connection, row: Mapping[str, Any], now: str) -> str:
    source_key = _source_key(row)
    kind = _source_kind(row.get("source_type"))
    label = _text(row.get("resource_set")) or {"bundled": "Bundled", "imported": "Imported", "station": "Station"}[kind]
    payload_hash = _hash({"label": label, "kind": kind, "ref": _text(row.get("source_ref"))})
    conn.execute(
        """INSERT INTO resource_catalog_sources
        (source_key,label,source_kind,source_uri,content_hash,read_only,enabled,created_utc,updated_utc)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_key) DO UPDATE SET label=excluded.label,source_uri=excluded.source_uri,
        content_hash=excluded.content_hash,updated_utc=excluded.updated_utc""",
        (source_key, label, kind, _text(row.get("source_ref")) or None, payload_hash,
         int(bool(row.get("readonly")) or kind != "station"), 1, now, now),
    )
    return source_key


def _upsert_frequency(
    conn: sqlite3.Connection,
    item: MigrationClassification,
    source_key: str,
    now: str,
) -> None:
    row = item.legacy_row
    hz = _frequency_hz(row.get("frequency") if item.legacy_table_name == "net_resources" else row.get("target"))
    if not item.frequency_resource_key or not hz:
        return
    label = _text(row.get("net_name")) or _text(row.get("resource")) or _text(row.get("target")) or f"Legacy frequency {item.legacy_resource_id}"
    payload = {
        "resource_kind": "simplex", "service": _service(row), "band": _text(row.get("band")) or None,
        "label": label, "center_hz": hz, "mode": _text(row.get("mode")) or None,
        "coverage": _text(row.get("coverage")) or None, "notes": _text(row.get("comment")) or _text(row.get("notes")) or None,
        "provenance": f"Migrated from {item.legacy_table_name}:{item.legacy_resource_id}",
    }
    content_hash = _hash(payload)
    version_hash = _hash({"content_hash": content_hash, "source_row_hash": item.source_row_hash})
    conn.execute(
        """INSERT INTO frequency_resources
        (frequency_resource_key,source_key,resource_kind,service,band,label,center_hz,mode,coverage,notes,provenance,
         content_version,content_hash,version_hash,active,retired,created_utc,updated_utc)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(frequency_resource_key) DO UPDATE SET source_key=excluded.source_key,service=excluded.service,
        band=excluded.band,label=excluded.label,center_hz=excluded.center_hz,mode=excluded.mode,
        coverage=excluded.coverage,notes=excluded.notes,provenance=excluded.provenance,
        content_hash=excluded.content_hash,version_hash=excluded.version_hash,updated_utc=excluded.updated_utc""",
        (item.frequency_resource_key, source_key, "simplex", payload["service"], payload["band"], label, hz,
         payload["mode"], payload["coverage"], payload["notes"], payload["provenance"], "legacy-1",
         content_hash, version_hash, 1, 0, now, now),
    )
    group_name = _text(row.get("group_name") or row.get("group"))
    if group_name:
        conn.execute(
            """INSERT OR REPLACE INTO frequency_resource_group_links
            (frequency_resource_key,operating_group_key,group_name_snapshot) VALUES (?,?,?)""",
            (item.frequency_resource_key, operating_group_key_for_name(group_name), group_name),
        )


def _upsert_net(conn: sqlite3.Connection, item: MigrationClassification, source_key: str, now: str) -> None:
    if not item.net_entry_key or not item.net_session_key or not item.frequency_resource_key:
        return
    row = item.legacy_row
    entry_payload = {"name": _text(row.get("net_name")), "scope": _text(row.get("coverage")) or None}
    entry_hash = _hash(entry_payload)
    entry_version = _hash({"content_hash": entry_hash, "source_row_hash": item.source_row_hash})
    conn.execute(
        """INSERT INTO net_directory_entries
        (net_entry_key,source_key,name,scope,content_hash,version_hash,active,retired,created_utc,updated_utc)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(net_entry_key) DO UPDATE SET name=excluded.name,scope=excluded.scope,
        content_hash=excluded.content_hash,version_hash=excluded.version_hash,updated_utc=excluded.updated_utc""",
        (item.net_entry_key, source_key, entry_payload["name"], entry_payload["scope"], entry_hash,
         entry_version, 1, 0, now, now),
    )
    group_name = _text(row.get("group_name"))
    if group_name:
        conn.execute(
            """INSERT OR REPLACE INTO net_directory_entry_group_links
            (net_entry_key,operating_group_key,group_name_snapshot) VALUES (?,?,?)""",
            (item.net_entry_key, operating_group_key_for_name(group_name), group_name),
        )
    session_payload = {
        "service": _service(row), "frequency_resource_key": item.frequency_resource_key,
        "recurrence": _text(row.get("recurrence")) or "Weekly",
        "day_utc": _text(row.get("day_utc")) or None,
        "local_start_time": _text(row.get("start_utc")) or None,
        "duration_minutes": _duration_minutes(row.get("start_utc"), row.get("end_utc")),
        "timezone": "UTC", "reminder_minutes": int(row.get("early_checkin") or 0),
        "mode": _text(row.get("mode")) or None,
        "mode_details": _text(row.get("fldigi_mode")) or _text(row.get("comment")) or None,
        "fldigi_tuning_json": row.get("fldigi_tuning_json") or "{}",
    }
    content_hash = _hash(session_payload)
    version_hash = _hash({"content_hash": content_hash, "source_row_hash": item.source_row_hash})
    conn.execute(
        """INSERT INTO net_directory_sessions
        (net_session_key,net_entry_key,source_key,service,frequency_resource_key,recurrence,local_start_time,
         duration_minutes,timezone,exception_dates_json,reminder_minutes,mode,mode_details,day_utc,content_hash,version_hash,
         active,retired,created_utc,updated_utc,fldigi_tuning_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(net_session_key) DO UPDATE SET net_entry_key=excluded.net_entry_key,
        frequency_resource_key=excluded.frequency_resource_key,recurrence=excluded.recurrence,
        local_start_time=excluded.local_start_time,duration_minutes=excluded.duration_minutes,
        timezone=excluded.timezone,reminder_minutes=excluded.reminder_minutes,mode=excluded.mode,
        mode_details=excluded.mode_details,day_utc=excluded.day_utc,content_hash=excluded.content_hash,
        version_hash=excluded.version_hash,updated_utc=excluded.updated_utc,fldigi_tuning_json=excluded.fldigi_tuning_json""",
        (item.net_session_key, item.net_entry_key, source_key, session_payload["service"],
         item.frequency_resource_key, session_payload["recurrence"], session_payload["local_start_time"],
         session_payload["duration_minutes"], "UTC", "[]", session_payload["reminder_minutes"],
         session_payload["mode"], session_payload["mode_details"], session_payload["day_utc"], content_hash, version_hash, 1, 0, now, now, session_payload["fldigi_tuning_json"]),
    )


def _remove_stale_shadow_rows(
    conn: sqlite3.Connection,
    expected: set[tuple[str, str]],
) -> None:
    stale = conn.execute(
        "SELECT * FROM legacy_net_resource_map"
    ).fetchall()
    for row in stale:
        identity = (str(row["legacy_table_name"]), str(row["legacy_resource_id"]))
        if identity in expected:
            continue
        session_key = _text(row["net_session_key"])
        frequency_key = _text(row["frequency_resource_key"])
        entry_key = _text(row["net_entry_key"])
        if session_key:
            conn.execute("DELETE FROM net_directory_sessions WHERE net_session_key=?", (session_key,))
        if entry_key and not conn.execute(
            "SELECT 1 FROM legacy_net_resource_map WHERE net_entry_key=? AND NOT (legacy_table_name=? AND legacy_resource_id=?)",
            (entry_key, *identity),
        ).fetchone():
            conn.execute("DELETE FROM net_directory_entry_group_links WHERE net_entry_key=?", (entry_key,))
            conn.execute("DELETE FROM net_directory_entries WHERE net_entry_key=?", (entry_key,))
        if frequency_key and not conn.execute(
            "SELECT 1 FROM legacy_net_resource_map WHERE frequency_resource_key=? AND NOT (legacy_table_name=? AND legacy_resource_id=?)",
            (frequency_key, *identity),
        ).fetchone():
            conn.execute(
                "DELETE FROM frequency_resource_group_links WHERE frequency_resource_key=?",
                (frequency_key,),
            )
            conn.execute("DELETE FROM frequency_resources WHERE frequency_resource_key=?", (frequency_key,))
        conn.execute(
            "DELETE FROM legacy_net_resource_map WHERE legacy_table_name=? AND legacy_resource_id=?",
            identity,
        )


def _link_hf_subscription_snapshots(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "net_schedule_tab"):
        return
    conn.execute(
        """UPDATE net_schedule_tab
        SET net_session_key=(
            SELECT mapping.net_session_key FROM legacy_net_resource_map AS mapping
            WHERE mapping.legacy_table_name='net_resources'
              AND mapping.legacy_resource_id=CAST(net_schedule_tab.resource_id AS TEXT)
        )
        WHERE resource_id IS NOT NULL"""
    )
    rows = conn.execute(
        """SELECT schedule.id,schedule.net_session_key,session.version_hash AS session_hash,
                  frequency.version_hash AS frequency_hash,session.frequency_resource_key,
                  session.recurrence,session.local_start_time,session.duration_minutes,
                  session.timezone,session.mode,session.mode_details
           FROM net_schedule_tab AS schedule
           LEFT JOIN net_directory_sessions AS session ON session.net_session_key=schedule.net_session_key
           LEFT JOIN frequency_resources AS frequency
             ON frequency.frequency_resource_key=session.frequency_resource_key
           WHERE schedule.net_session_key IS NOT NULL"""
    ).fetchall()
    for row in rows:
        snapshot = {
            "frequency_resource_key": row["frequency_resource_key"],
            "recurrence": row["recurrence"],
            "local_start_time": row["local_start_time"],
            "duration_minutes": row["duration_minutes"],
            "timezone": row["timezone"],
            "mode": row["mode"],
            "mode_details": row["mode_details"],
        }
        conn.execute(
            """UPDATE net_schedule_tab SET accepted_session_version_hash=?,
               accepted_resource_version_hash=?,accepted_snapshot_json=? WHERE id=?""",
            (row["session_hash"], row["frequency_hash"], _canonical(snapshot), row["id"]),
        )
def _upsert_bundled_reference(conn: sqlite3.Connection, now: str) -> None:
    manifest = load_bundled_reference_manifest()
    source_key = manifest["catalog_key"]
    conn.execute(
        """INSERT INTO resource_catalog_sources
        (source_key,label,source_kind,jurisdiction,version,effective_date,source_uri,last_verified_utc,
         content_hash,read_only,enabled,created_utc,updated_utc)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_key) DO UPDATE SET label=excluded.label,jurisdiction=excluded.jurisdiction,
        version=excluded.version,effective_date=excluded.effective_date,source_uri=excluded.source_uri,
        last_verified_utc=excluded.last_verified_utc,content_hash=excluded.content_hash,
        read_only=1,enabled=excluded.enabled,updated_utc=excluded.updated_utc""",
        (source_key, "US FCC Reference", "bundled", manifest["jurisdiction"], manifest["content_version"],
         manifest["effective_date"], manifest["sources"][0]["url"], manifest["verified_date"],
         manifest["content_sha256"], 1, 1, now, now),
    )
    for ref in manifest["references"]:
        content_hash = _hash(ref)
        conn.execute(
            """INSERT INTO frequency_resources
            (frequency_resource_key,source_key,resource_kind,service,jurisdiction,band,channel,label,
             lower_hz,upper_hz,center_hz,notes,provenance,content_version,content_hash,version_hash,
             active,retired,created_utc,updated_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(frequency_resource_key) DO UPDATE SET source_key=excluded.source_key,
            resource_kind=excluded.resource_kind,service=excluded.service,jurisdiction=excluded.jurisdiction,
            band=excluded.band,channel=excluded.channel,label=excluded.label,lower_hz=excluded.lower_hz,
            upper_hz=excluded.upper_hz,center_hz=excluded.center_hz,notes=excluded.notes,
            provenance=excluded.provenance,content_version=excluded.content_version,
            content_hash=excluded.content_hash,version_hash=excluded.version_hash,
            active=1,retired=0,updated_utc=excluded.updated_utc""",
            (ref["reference_key"], source_key, ref["kind"], ref["service"], manifest["jurisdiction"],
             ref.get("band"), ref.get("channel"), ref["label"], ref.get("lower_hz"), ref.get("upper_hz"),
             ref.get("center_hz"), ref.get("purpose"), ref.get("citation"), manifest["content_version"],
             content_hash, _hash({"content_hash": content_hash, "version": manifest["content_version"]}),
             1, 0, now, now),
        )


def apply_resource_catalog_migration(
    nets_db_path: str | Path,
    settings_db_path: str | Path | None = None,
    *,
    backup_factory: Callable[..., ConfigBackupResult] = create_config_backup,
    fail_after_rows: int | None = None,
    authority_state: str = "shadow_ready",
) -> ResourceCatalogMigrationReport:
    """Back up inputs and transactionally refresh the LN-1 canonical shadow."""
    if authority_state not in {"shadow_ready", "canonical"}:
        raise ValueError("authority_state must be shadow_ready or canonical")
    nets_path = Path(nets_db_path)
    settings_path = Path(settings_db_path) if settings_db_path is not None else None
    dry = dry_run_resource_catalog_migration(nets_path, settings_path)
    backup_paths = [
        path for path in (nets_path, settings_path)
        if path is not None and path.exists() and (dry.total_legacy_rows or dry.group_rows_updated)
    ]
    backup_dir: str | None = None
    if backup_paths:
        result = backup_factory(backup_paths, reason="pre-resource-catalog-ln1")
        _validate_backup(result, backup_paths)
        backup_dir = result.backup_dir

    nets_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(nets_path, row_factory=sqlite3.Row)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        ensure_resource_catalog_schema(conn)
        _ensure_hf_subscription_columns(conn)
        now = _now()
        _upsert_bundled_reference(conn, now)
        previous = {
            (row["legacy_table_name"], row["legacy_resource_id"]): row["source_row_hash"]
            for row in conn.execute("SELECT legacy_table_name,legacy_resource_id,source_row_hash FROM legacy_net_resource_map")
        }
        expected_identities = {
            (item.legacy_table_name, item.legacy_resource_id) for item in dry.classifications
        }
        _remove_stale_shadow_rows(conn, expected_identities)
        unchanged = 0
        for index, item in enumerate(dry.classifications, start=1):
            if previous.get((item.legacy_table_name, item.legacy_resource_id)) == item.source_row_hash:
                unchanged += 1
            row = item.legacy_row
            if item.legacy_table_name == "net_resources":
                source_key = _upsert_source(conn, row, now)
            else:
                source_key = _stable_key("source", "settings-local-net-profiles")
                conn.execute(
                    """INSERT OR IGNORE INTO resource_catalog_sources
                    (source_key,label,source_kind,content_hash,read_only,enabled,created_utc,updated_utc)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (source_key, "Legacy Local Net Profiles", "station", _hash("local_net_profiles"), 0, 1, now, now),
                )
            _upsert_frequency(conn, item, source_key, now)
            _upsert_net(conn, item, source_key, now)
            diagnostics_payload = {"diagnostics": list(item.diagnostics), "legacy_row": dict(row)}
            conn.execute(
                """INSERT INTO legacy_net_resource_map
                (legacy_table_name,legacy_resource_id,frequency_resource_key,net_entry_key,net_session_key,
                 classification,source_row_hash,diagnostic_state,diagnostics_json,migrated_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(legacy_table_name,legacy_resource_id) DO UPDATE SET
                frequency_resource_key=excluded.frequency_resource_key,net_entry_key=excluded.net_entry_key,
                net_session_key=excluded.net_session_key,classification=excluded.classification,
                source_row_hash=excluded.source_row_hash,diagnostic_state=excluded.diagnostic_state,
                diagnostics_json=excluded.diagnostics_json,migrated_utc=excluded.migrated_utc""",
                (item.legacy_table_name, item.legacy_resource_id, item.frequency_resource_key,
                 item.net_entry_key, item.net_session_key, item.classification, item.source_row_hash,
                 "review_required" if item.classification.startswith("review_required") else "ready",
                 _canonical(diagnostics_payload), now),
            )
            if fail_after_rows is not None and index >= fail_after_rows:
                raise RuntimeError("injected resource catalog migration failure")

        _link_hf_subscription_snapshots(conn)

        group_rows, _ = _read_settings_rows(settings_path)
        ensured_groups = ensure_operating_group_keys(group_rows)
        if settings_path is not None and settings_path.exists() and group_rows != list(ensured_groups):
            conn.execute("ATTACH DATABASE ? AS settingsdb", (str(settings_path),))
            conn.execute(
                "INSERT OR REPLACE INTO settingsdb.kv(key,value) VALUES ('operating_groups',?)",
                (json.dumps(list(ensured_groups)),),
            )
        details = {
            "total_legacy_rows": dry.total_legacy_rows,
            "review_required_count": dry.review_required_count,
            "backup_dir": backup_dir,
        }
        conn.execute(
            """INSERT OR REPLACE INTO resource_catalog_migration_state
            (state_key,authority_state,schema_version,details_json,updated_utc) VALUES (?,?,?,?,?)""",
            (STATE_KEY, authority_state, SCHEMA_VERSION, _canonical(details), now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _report(
        dry.classifications,
        group_updates=dry.group_rows_updated,
        authority_state=authority_state,
        backup_dir=backup_dir,
        unchanged=unchanged,
    )


def resource_catalog_authority_state(db_path: str | Path) -> str:
    path = Path(db_path)
    if not path.exists():
        return "legacy"
    conn = connect_sqlite_readonly(path, row_factory=sqlite3.Row)
    try:
        if not table_exists(conn, "resource_catalog_migration_state"):
            return "legacy"
        row = conn.execute(
            "SELECT authority_state FROM resource_catalog_migration_state WHERE state_key=?",
            (STATE_KEY,),
        ).fetchone()
        return _text(row["authority_state"] if row else "legacy") or "legacy"
    finally:
        conn.close()


def resource_catalog_migration_needed(
    nets_db_path: str | Path,
    settings_db_path: str | Path | None = None,
) -> bool:
    """Return whether startup needs a first or delta shadow import."""
    nets_path = Path(nets_db_path)
    state = resource_catalog_authority_state(nets_path)
    if state == "canonical":
        return False
    dry = dry_run_resource_catalog_migration(nets_path, settings_db_path)
    return _migration_needed_from_dry(nets_path, state, dry)


def _migration_needed_from_dry(
    nets_path: Path,
    state: str,
    dry: ResourceCatalogMigrationReport,
) -> bool:
    if state != "shadow_ready" or dry.group_rows_updated:
        return True
    conn = connect_sqlite_readonly(nets_path, row_factory=sqlite3.Row)
    try:
        mapped = {
            (row["legacy_table_name"], row["legacy_resource_id"]): row["source_row_hash"]
            for row in conn.execute(
                "SELECT legacy_table_name,legacy_resource_id,source_row_hash FROM legacy_net_resource_map"
            )
        }
        expected = {
            (item.legacy_table_name, item.legacy_resource_id): item.source_row_hash
            for item in dry.classifications
        }
        manifest = load_bundled_reference_manifest()
        source = conn.execute(
            "SELECT version,content_hash FROM resource_catalog_sources WHERE source_key=?",
            (manifest["catalog_key"],),
        ).fetchone()
        return mapped != expected or not source or source["version"] != manifest["content_version"] \
            or source["content_hash"] != manifest["content_sha256"]
    finally:
        conn.close()


def ensure_resource_catalog_shadow(
    nets_db_path: str | Path,
    settings_db_path: str | Path | None = None,
) -> ResourceCatalogMigrationReport:
    """Startup-owned first/delta import with a zero-write fast path."""
    nets_path = Path(nets_db_path)
    state = resource_catalog_authority_state(nets_path)
    if state == "canonical":
        return _report((), group_updates=0, authority_state=state)
    dry = dry_run_resource_catalog_migration(nets_path, settings_db_path)
    if _migration_needed_from_dry(nets_path, state, dry):
        return apply_resource_catalog_migration(nets_db_path, settings_db_path)
    return _report(
        dry.classifications,
        group_updates=0,
        authority_state=resource_catalog_authority_state(nets_db_path),
        unchanged=dry.total_legacy_rows,
    )


def synchronize_legacy_rows_in_connection(
    conn: sqlite3.Connection,
    *,
    authority_state: str = "canonical",
) -> ResourceCatalogMigrationReport:
    """Refresh legacy projections inside a caller-owned write transaction.

    This is the single cutover seam used by compatibility writers.  It performs
    no backup and never commits; the caller owns both the legacy mutation and
    canonical refresh atomically.
    """
    if authority_state not in {"shadow_ready", "canonical"}:
        raise ValueError("invalid catalog authority state")
    conn.row_factory = sqlite3.Row
    ensure_resource_catalog_schema(conn)
    _ensure_hf_subscription_columns(conn)
    rows = [
        _classify_legacy_row("net_resources", str(row["id"]), dict(row))
        for row in conn.execute("SELECT * FROM net_resources ORDER BY id").fetchall()
    ] if table_exists(conn, "net_resources") else []
    expected = {(item.legacy_table_name, item.legacy_resource_id) for item in rows}
    # Compatibility writes own only net_resources; Settings profile mappings
    # remain intact until their own migration/cutover path changes them.
    stale = conn.execute(
        "SELECT legacy_resource_id FROM legacy_net_resource_map WHERE legacy_table_name='net_resources'"
    ).fetchall()
    stale_identities = {
        ("net_resources", str(row["legacy_resource_id"])) for row in stale
    } - expected
    if stale_identities:
        retained = {
            (str(row["legacy_table_name"]), str(row["legacy_resource_id"]))
            for row in conn.execute("SELECT legacy_table_name,legacy_resource_id FROM legacy_net_resource_map")
        } - stale_identities
        _remove_stale_shadow_rows(conn, retained)
    now = _now()
    _upsert_bundled_reference(conn, now)
    unchanged = 0
    for item in rows:
        previous = conn.execute(
            """SELECT source_row_hash FROM legacy_net_resource_map
            WHERE legacy_table_name=? AND legacy_resource_id=?""",
            (item.legacy_table_name, item.legacy_resource_id),
        ).fetchone()
        if previous and previous["source_row_hash"] == item.source_row_hash:
            unchanged += 1
        source_key = _upsert_source(conn, item.legacy_row, now)
        _upsert_frequency(conn, item, source_key, now)
        _upsert_net(conn, item, source_key, now)
        conn.execute(
            """INSERT INTO legacy_net_resource_map
            (legacy_table_name,legacy_resource_id,frequency_resource_key,net_entry_key,net_session_key,
             classification,source_row_hash,diagnostic_state,diagnostics_json,migrated_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(legacy_table_name,legacy_resource_id) DO UPDATE SET
            frequency_resource_key=excluded.frequency_resource_key,net_entry_key=excluded.net_entry_key,
            net_session_key=excluded.net_session_key,classification=excluded.classification,
            source_row_hash=excluded.source_row_hash,diagnostic_state=excluded.diagnostic_state,
            diagnostics_json=excluded.diagnostics_json,migrated_utc=excluded.migrated_utc""",
            (item.legacy_table_name, item.legacy_resource_id, item.frequency_resource_key,
             item.net_entry_key, item.net_session_key, item.classification, item.source_row_hash,
             "review_required" if item.classification.startswith("review_required") else "ready",
             _canonical({"diagnostics": list(item.diagnostics), "legacy_row": dict(item.legacy_row)}), now),
        )
    _link_hf_subscription_snapshots(conn)
    conn.execute(
        """INSERT OR REPLACE INTO resource_catalog_migration_state
        (state_key,authority_state,schema_version,details_json,updated_utc) VALUES (?,?,?,?,?)""",
        (STATE_KEY, authority_state, SCHEMA_VERSION,
         _canonical({"total_legacy_rows": len(rows), "compatibility_projection": True}), now),
    )
    return _report(rows, group_updates=0, authority_state=authority_state, unchanged=unchanged)


def cutover_resource_catalog_to_canonical(
    nets_db_path: str | Path,
    settings_db_path: str | Path | None = None,
) -> ResourceCatalogMigrationReport:
    """Perform the final backup/delta import and atomically claim authority."""
    if resource_catalog_authority_state(nets_db_path) == "canonical":
        dry = dry_run_resource_catalog_migration(nets_db_path, settings_db_path)
        return _report(dry.classifications, group_updates=0, authority_state="canonical", unchanged=dry.total_legacy_rows)
    return apply_resource_catalog_migration(
        nets_db_path,
        settings_db_path,
        authority_state="canonical",
    )


def read_legacy_compatibility_rows(db_path: str | Path) -> tuple[dict[str, Any], ...]:
    """Reproduce legacy rows from the lossless audit payload without writes."""
    path = Path(db_path)
    if not path.exists():
        return ()
    conn = connect_sqlite_readonly(path, row_factory=sqlite3.Row)
    try:
        if not table_exists(conn, "legacy_net_resource_map"):
            return ()
        result: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT diagnostics_json FROM legacy_net_resource_map WHERE legacy_table_name='net_resources' ORDER BY CAST(legacy_resource_id AS INTEGER)"
        ):
            try:
                payload = json.loads(row["diagnostics_json"] or "{}")
                legacy = payload.get("legacy_row")
                if isinstance(legacy, dict):
                    result.append(legacy)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(result)
    finally:
        conn.close()


__all__ = [
    "MigrationClassification",
    "ResourceCatalogBackupError",
    "ResourceCatalogMigrationReport",
    "apply_resource_catalog_migration",
    "cutover_resource_catalog_to_canonical",
    "dry_run_resource_catalog_migration",
    "ensure_resource_catalog_shadow",
    "read_legacy_compatibility_rows",
    "resource_catalog_authority_state",
    "resource_catalog_migration_needed",
    "synchronize_legacy_rows_in_connection",
]
