"""Bounded SQLite repository for the LN-1 canonical resource catalog.

Schema creation is explicit: callers must invoke :func:`create_resource_catalog_schema`
from startup/migration ownership.  Read APIs use SQLite's read-only connection
and intentionally return empty/missing results if the catalog has not been
initialized; they never create a database, table, journal, or migration state.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from freqinout.core.resource_catalog_models import (
    CatalogSource,
    CatalogValidationError,
    FieldDiff,
    FrequencyResource,
    NetDirectoryEntry,
    NetDirectorySession,
    ReadOnlyResourceError,
    ReferencedResourceError,
    ResourceUsage,
    VersionComparison,
)
from freqinout.core.sqlite_utils import connect_sqlite, connect_sqlite_readonly, table_exists


CATALOG_TABLES = (
    "resource_catalog_sources",
    "frequency_resources",
    "frequency_resource_group_links",
    "net_directory_entries",
    "net_directory_entry_group_links",
    "net_directory_sessions",
    "legacy_net_resource_map",
    "resource_catalog_migration_state",
)
MAX_RESULTS = 200
MAX_BATCH_KEYS = 2_000
STATION_MANUAL_SOURCE_KEY = "source_station_manual"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _new_key(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


def _limit(value: int) -> int:
    try:
        return max(1, min(MAX_RESULTS, int(value)))
    except (TypeError, ValueError):
        return MAX_RESULTS


def _optional(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _execute_statements(conn: sqlite3.Connection, script: str) -> None:
    """Execute this schema's simple DDL without ``executescript``'s implicit commit."""
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)


def ensure_resource_catalog_schema(conn: sqlite3.Connection) -> None:
    """Create the additive catalog schema on an existing transaction owner.

    Startup and migration use this form so schema, imported rows, and the
    authority checkpoint commit or roll back together.
    """
    conn.execute("PRAGMA foreign_keys=ON")
    _execute_statements(
        conn,
        """
        CREATE TABLE IF NOT EXISTS resource_catalog_sources (
            source_key TEXT PRIMARY KEY NOT NULL,
            label TEXT NOT NULL,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('bundled','station','imported')),
            jurisdiction TEXT, version TEXT, effective_date TEXT, source_uri TEXT,
            last_verified_utc TEXT, content_hash TEXT NOT NULL,
            read_only INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
            created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS frequency_resources (
            frequency_resource_key TEXT PRIMARY KEY NOT NULL,
            source_key TEXT NOT NULL REFERENCES resource_catalog_sources(source_key),
            resource_kind TEXT NOT NULL CHECK(resource_kind IN ('band_range','channel','simplex','repeater')),
            service TEXT NOT NULL, jurisdiction TEXT, band TEXT, channel TEXT, label TEXT NOT NULL,
            lower_hz INTEGER, upper_hz INTEGER, receive_hz INTEGER, transmit_hz INTEGER,
            center_hz INTEGER, offset_hz INTEGER, mode TEXT, bandwidth_hz INTEGER, tone TEXT,
            locality TEXT, grid TEXT, coverage TEXT, notes TEXT, provenance TEXT,
            content_version TEXT, content_hash TEXT NOT NULL, version_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, retired INTEGER NOT NULL DEFAULT 0,
            replacement_frequency_resource_key TEXT, created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS frequency_resource_group_links (
            frequency_resource_key TEXT NOT NULL REFERENCES frequency_resources(frequency_resource_key),
            operating_group_key TEXT NOT NULL,
            group_name_snapshot TEXT,
            PRIMARY KEY(frequency_resource_key, operating_group_key)
        );
        CREATE TABLE IF NOT EXISTS net_directory_entries (
            net_entry_key TEXT PRIMARY KEY NOT NULL,
            source_key TEXT NOT NULL REFERENCES resource_catalog_sources(source_key),
            name TEXT NOT NULL, description TEXT, scope TEXT, contact_info TEXT,
            last_verified_utc TEXT, content_hash TEXT NOT NULL, version_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, retired INTEGER NOT NULL DEFAULT 0,
            replacement_net_entry_key TEXT, created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS net_directory_entry_group_links (
            net_entry_key TEXT NOT NULL REFERENCES net_directory_entries(net_entry_key),
            operating_group_key TEXT NOT NULL, group_name_snapshot TEXT,
            PRIMARY KEY(net_entry_key, operating_group_key)
        );
        CREATE TABLE IF NOT EXISTS net_directory_sessions (
            net_session_key TEXT PRIMARY KEY NOT NULL,
            net_entry_key TEXT NOT NULL REFERENCES net_directory_entries(net_entry_key),
            source_key TEXT NOT NULL REFERENCES resource_catalog_sources(source_key),
            service TEXT NOT NULL, frequency_resource_key TEXT REFERENCES frequency_resources(frequency_resource_key),
            recurrence TEXT, local_start_time TEXT, duration_minutes INTEGER, timezone TEXT,
            day_utc TEXT,
            effective_start_date TEXT, effective_end_date TEXT, exception_dates_json TEXT NOT NULL DEFAULT '[]',
            reminder_minutes INTEGER, mode TEXT, mode_details TEXT,
            fldigi_tuning_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL, version_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, retired INTEGER NOT NULL DEFAULT 0,
            replacement_net_session_key TEXT, created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS legacy_net_resource_map (
            legacy_table_name TEXT NOT NULL, legacy_resource_id TEXT NOT NULL,
            frequency_resource_key TEXT, net_entry_key TEXT, net_session_key TEXT,
            classification TEXT NOT NULL, source_row_hash TEXT NOT NULL,
            diagnostic_state TEXT, diagnostics_json TEXT, migrated_utc TEXT NOT NULL,
            PRIMARY KEY(legacy_table_name, legacy_resource_id)
        );
        CREATE TABLE IF NOT EXISTS resource_catalog_migration_state (
            state_key TEXT PRIMARY KEY NOT NULL, authority_state TEXT NOT NULL,
            schema_version TEXT NOT NULL, details_json TEXT, updated_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_frequency_resources_search
            ON frequency_resources(active, retired, service, resource_kind, source_key, label);
        CREATE INDEX IF NOT EXISTS idx_frequency_resources_hz
            ON frequency_resources(center_hz, receive_hz, lower_hz, upper_hz);
        CREATE INDEX IF NOT EXISTS idx_net_directory_entries_search
            ON net_directory_entries(active, retired, source_key, name);
        CREATE INDEX IF NOT EXISTS idx_net_directory_sessions_frequency
            ON net_directory_sessions(frequency_resource_key, active, retired);
        CREATE INDEX IF NOT EXISTS idx_net_directory_sessions_entry
            ON net_directory_sessions(net_entry_key, active, retired);
        """,
    )
    session_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(net_directory_sessions)")}
    if "fldigi_tuning_json" not in session_columns:
        conn.execute("ALTER TABLE net_directory_sessions ADD COLUMN fldigi_tuning_json TEXT NOT NULL DEFAULT '{}'")
    if "day_utc" not in session_columns:
        conn.execute("ALTER TABLE net_directory_sessions ADD COLUMN day_utc TEXT")


def create_resource_catalog_schema(db_path: str | Path) -> None:
    """Create only LN-1 additive tables and indexes in one transaction.

    This is intentionally separate from ``ResourceCatalogStore`` construction
    and all query methods.  It does not rename, alter, or delete legacy tables.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(path, row_factory=sqlite3.Row)
    try:
        with conn:
            ensure_resource_catalog_schema(conn)
    finally:
        conn.close()


class ResourceCatalogStore:
    """Canonical source/frequency/directory store with bounded read APIs."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _write_connection(self) -> sqlite3.Connection:
        conn = connect_sqlite(self.db_path, row_factory=sqlite3.Row)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _write(self) -> Iterable[sqlite3.Connection]:
        """Open, commit/roll back, and close one write connection per operation."""
        conn = self._write_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _read_connection(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        try:
            return connect_sqlite_readonly(self.db_path, row_factory=sqlite3.Row)
        except sqlite3.Error:
            return None

    @staticmethod
    def _has_catalog(conn: sqlite3.Connection) -> bool:
        return table_exists(conn, "resource_catalog_sources")

    # ---- explicit schema and source ownership ---------------------------------
    def create_schema(self) -> None:
        create_resource_catalog_schema(self.db_path)

    def create_source(self, source: CatalogSource) -> CatalogSource:
        return self._write_source(source, create=True)

    def update_source(self, source: CatalogSource) -> CatalogSource:
        return self._write_source(source, create=False)

    def _write_source(self, source: CatalogSource, *, create: bool) -> CatalogSource:
        now = _utc_now()
        content_hash = source.content_hash or _hash({
            "source_key": source.source_key, "label": source.label, "source_kind": source.source_kind,
            "jurisdiction": source.jurisdiction, "version": source.version, "effective_date": source.effective_date,
            "source_uri": source.source_uri, "last_verified_utc": source.last_verified_utc,
            "read_only": source.read_only, "enabled": source.enabled,
        })
        with self._write() as conn:
            if not self._has_catalog(conn):
                raise CatalogValidationError("resource catalog schema is not initialized")
            old = conn.execute("SELECT * FROM resource_catalog_sources WHERE source_key=?", (source.source_key,)).fetchone()
            if create and old:
                raise CatalogValidationError(f"source already exists: {source.source_key}")
            if not create and not old:
                raise CatalogValidationError(f"source not found: {source.source_key}")
            if old and bool(old["read_only"]):
                raise ReadOnlyResourceError(f"source is read-only: {source.source_key}")
            values = (source.source_key, source.label, source.source_kind, source.jurisdiction, source.version,
                      source.effective_date, source.source_uri, source.last_verified_utc, content_hash,
                      int(source.read_only), int(source.enabled), old["created_utc"] if old else now, now)
            conn.execute(
                """INSERT INTO resource_catalog_sources
                   (source_key,label,source_kind,jurisdiction,version,effective_date,source_uri,last_verified_utc,
                    content_hash,read_only,enabled,created_utc,updated_utc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_key) DO UPDATE SET label=excluded.label,source_kind=excluded.source_kind,
                    jurisdiction=excluded.jurisdiction,version=excluded.version,effective_date=excluded.effective_date,
                    source_uri=excluded.source_uri,last_verified_utc=excluded.last_verified_utc,content_hash=excluded.content_hash,
                    read_only=excluded.read_only,enabled=excluded.enabled,updated_utc=excluded.updated_utc""", values)
        return dataclasses.replace(source, content_hash=content_hash, created_utc=old["created_utc"] if old else now, updated_utc=now)

    def get_source(self, source_key: str) -> CatalogSource | None:
        return self._read_one("resource_catalog_sources", "source_key", source_key, self._source_from_row)

    def sources_by_keys(self, keys: Iterable[str]) -> Mapping[str, CatalogSource]:
        """Load source presentation metadata for a bounded result set at once."""
        return self._read_by_keys(
            "resource_catalog_sources",
            "source_key",
            keys,
            self._source_from_row,
        )

    def list_sources(
        self,
        *,
        enabled: bool | None = True,
        limit: int = MAX_RESULTS,
    ) -> tuple[CatalogSource, ...]:
        """Return bounded catalog-source metadata for operator-facing selectors."""
        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        if enabled is not None:
            clauses.append("enabled=?")
            params.append(int(enabled))
        params.append(_limit(limit))
        return self._read_many(
            "resource_catalog_sources",
            f"SELECT * FROM resource_catalog_sources WHERE {' AND '.join(clauses)} "
            "ORDER BY label COLLATE NOCASE, source_key LIMIT ?",
            params,
            self._source_from_row,
        )

    def ensure_station_source(
        self,
        source_key: str = STATION_MANUAL_SOURCE_KEY,
        label: str = "Station Resources",
    ) -> CatalogSource:
        """Return the stable mutable source used for operator-created records."""
        key = str(source_key or STATION_MANUAL_SOURCE_KEY).strip()
        with self._write() as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM resource_catalog_sources WHERE source_key=?",
                (key,),
            ).fetchone()
            if row is None:
                now = _utc_now()
                source = CatalogSource(key, str(label or "Station Resources"), "station")
                content_hash = _hash({
                    "source_key": source.source_key,
                    "label": source.label,
                    "source_kind": source.source_kind,
                    "read_only": False,
                    "enabled": True,
                })
                conn.execute(
                    """INSERT INTO resource_catalog_sources
                    (source_key,label,source_kind,content_hash,read_only,enabled,created_utc,updated_utc)
                    VALUES (?,?,?,?,0,1,?,?)""",
                    (source.source_key, source.label, source.source_kind, content_hash, now, now),
                )
            elif bool(row["read_only"]) or str(row["source_kind"] or "").lower() != "station":
                raise CatalogValidationError(f"station source is not mutable: {key}")
        source = self.get_source(key)
        if source is None:
            raise CatalogValidationError(f"station source unavailable: {key}")
        return source

    # ---- frequency resources ---------------------------------------------------
    def create_frequency(self, resource: FrequencyResource, *, group_keys: Iterable[str] | Mapping[str, str] = ()) -> FrequencyResource:
        return self._write_frequency(resource, group_keys=group_keys, create=True)

    def update_frequency(self, resource: FrequencyResource, *, group_keys: Iterable[str] | Mapping[str, str] | None = None) -> FrequencyResource:
        return self._write_frequency(resource, group_keys=group_keys, create=False)

    def _write_frequency(self, resource: FrequencyResource, *, group_keys: Iterable[str] | Mapping[str, str] | None, create: bool) -> FrequencyResource:
        now = _utc_now()
        with self._write() as conn:
            self._require_schema(conn)
            old = conn.execute("SELECT * FROM frequency_resources WHERE frequency_resource_key=?", (resource.frequency_resource_key,)).fetchone()
            if create and old:
                raise CatalogValidationError(f"frequency exists: {resource.frequency_resource_key}")
            if not create and not old:
                raise CatalogValidationError(f"frequency not found: {resource.frequency_resource_key}")
            if create:
                self._require_source_exists(conn, resource.source_key)
            else:
                self._require_mutable_source(conn, resource.source_key, old["source_key"])
            revision = self._revision(old["content_version"] if old else None)
            content_hash = _hash(self._frequency_payload(resource))
            version_hash = _hash({"content_hash": content_hash, "revision": revision})
            created = old["created_utc"] if old else now
            values = self._frequency_values(resource, revision, content_hash, version_hash, created, now)
            conn.execute(self._frequency_upsert_sql(), values)
            if group_keys is not None:
                conn.execute("DELETE FROM frequency_resource_group_links WHERE frequency_resource_key=?", (resource.frequency_resource_key,))
                conn.executemany(
                    """INSERT INTO frequency_resource_group_links
                    (frequency_resource_key,operating_group_key,group_name_snapshot) VALUES (?,?,?)""",
                    [(resource.frequency_resource_key, key, name) for key, name in self._group_links(group_keys)],
                )
        return dataclasses.replace(resource, content_version=revision, content_hash=content_hash, version_hash=version_hash, created_utc=created, updated_utc=now)

    def get_frequency(self, frequency_resource_key: str) -> FrequencyResource | None:
        return self._read_one("frequency_resources", "frequency_resource_key", frequency_resource_key, self._frequency_from_row)

    def frequencies_by_keys(self, keys: Iterable[str]) -> Mapping[str, FrequencyResource]:
        """Load a bounded set of frequencies with one read connection.

        Presentation code uses this instead of opening one SQLite connection per
        result row.  Unknown keys are intentionally absent from the result.
        """
        return self._read_by_keys(
            "frequency_resources",
            "frequency_resource_key",
            keys,
            self._frequency_from_row,
        )

    def list_frequencies(self, *, search: str = "", service: str | None = None, source_key: str | None = None,
                         active: bool | None = True, limit: int = MAX_RESULTS, offset: int = 0) -> tuple[FrequencyResource, ...]:
        clauses, params = ["1=1"], []
        if search.strip():
            needle = f"%{search.strip().lower()}%"
            clauses.append("(LOWER(label) LIKE ? OR LOWER(COALESCE(band,'')) LIKE ? OR LOWER(COALESCE(channel,'')) LIKE ? OR LOWER(COALESCE(locality,'')) LIKE ? OR LOWER(COALESCE(coverage,'')) LIKE ? OR LOWER(COALESCE(notes,'')) LIKE ?)")
            params.extend([needle] * 6)
        if service:
            clauses.append("service=?")
            params.append(str(service).strip().upper())
        if source_key:
            clauses.append("source_key=?")
            params.append(str(source_key).strip())
        if active is not None:
            clauses.append("active=? AND retired=?")
            params.extend([int(active), int(not active)])
        return self._read_many("frequency_resources", f"SELECT * FROM frequency_resources WHERE {' AND '.join(clauses)} ORDER BY label COLLATE NOCASE, frequency_resource_key LIMIT ? OFFSET ?", (*params, _limit(limit), max(0, int(offset))), self._frequency_from_row)

    def retire_frequency(self, frequency_resource_key: str, *, replacement_frequency_resource_key: str | None = None) -> FrequencyResource:
        resource = self._must_frequency(frequency_resource_key)
        return self.update_frequency(dataclasses.replace(resource, active=False, retired=True, replacement_frequency_resource_key=_optional(replacement_frequency_resource_key)))

    def delete_frequency_if_unreferenced(self, frequency_resource_key: str) -> None:
        usage = self.frequency_usage(frequency_resource_key)
        if usage.is_referenced:
            raise ReferencedResourceError(f"frequency is referenced by {usage.total_references} record(s)")
        with self._write() as conn:
            self._require_schema(conn)
            row = conn.execute("SELECT source_key FROM frequency_resources WHERE frequency_resource_key=?", (frequency_resource_key,)).fetchone()
            if not row:
                return
            self._require_mutable_source(conn, row["source_key"])
            conn.execute("DELETE FROM frequency_resources WHERE frequency_resource_key=?", (frequency_resource_key,))

    delete_frequency = delete_frequency_if_unreferenced
    search_frequencies = list_frequencies

    def frequency_group_links(self, frequency_resource_key: str) -> tuple[tuple[str, str | None], ...]:
        return self._read_group_links(
            "frequency_resource_group_links", "frequency_resource_key", frequency_resource_key
        )

    # ---- directory identities and sessions -------------------------------------
    def create_net_entry(self, entry: NetDirectoryEntry, *, group_keys: Iterable[str] | Mapping[str, str] = ()) -> NetDirectoryEntry:
        return self._write_entry(entry, group_keys=group_keys, create=True)

    def update_net_entry(self, entry: NetDirectoryEntry, *, group_keys: Iterable[str] | Mapping[str, str] | None = None) -> NetDirectoryEntry:
        return self._write_entry(entry, group_keys=group_keys, create=False)

    def _write_entry(self, entry: NetDirectoryEntry, *, group_keys: Iterable[str] | Mapping[str, str] | None, create: bool) -> NetDirectoryEntry:
        now = _utc_now()
        with self._write() as conn:
            self._require_schema(conn)
            old = conn.execute("SELECT * FROM net_directory_entries WHERE net_entry_key=?", (entry.net_entry_key,)).fetchone()
            if create and old: raise CatalogValidationError(f"net entry exists: {entry.net_entry_key}")
            if not create and not old: raise CatalogValidationError(f"net entry not found: {entry.net_entry_key}")
            if create:
                self._require_source_exists(conn, entry.source_key)
            else:
                self._require_mutable_source(conn, entry.source_key, old["source_key"])
            revision = self._revision(None) if not old else self._revision_from_hash(old["version_hash"])
            content_hash = _hash(self._entry_payload(entry))
            version_hash = _hash({"content_hash": content_hash, "revision": revision})
            created = old["created_utc"] if old else now
            conn.execute("""INSERT INTO net_directory_entries(net_entry_key,source_key,name,description,scope,contact_info,last_verified_utc,content_hash,version_hash,active,retired,replacement_net_entry_key,created_utc,updated_utc)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(net_entry_key) DO UPDATE SET source_key=excluded.source_key,name=excluded.name,description=excluded.description,scope=excluded.scope,contact_info=excluded.contact_info,last_verified_utc=excluded.last_verified_utc,content_hash=excluded.content_hash,version_hash=excluded.version_hash,active=excluded.active,retired=excluded.retired,replacement_net_entry_key=excluded.replacement_net_entry_key,updated_utc=excluded.updated_utc""",
                         (entry.net_entry_key,entry.source_key,entry.name,entry.description,entry.scope,entry.contact_info,entry.last_verified_utc,content_hash,version_hash,int(entry.active),int(entry.retired),entry.replacement_net_entry_key,created,now))
            if group_keys is not None:
                conn.execute("DELETE FROM net_directory_entry_group_links WHERE net_entry_key=?", (entry.net_entry_key,))
                conn.executemany(
                    """INSERT INTO net_directory_entry_group_links
                    (net_entry_key,operating_group_key,group_name_snapshot) VALUES (?,?,?)""",
                    [(entry.net_entry_key, key, name) for key, name in self._group_links(group_keys)],
                )
        return dataclasses.replace(entry, content_hash=content_hash, version_hash=version_hash, created_utc=created, updated_utc=now)

    def get_net_entry(self, net_entry_key: str) -> NetDirectoryEntry | None:
        return self._read_one("net_directory_entries", "net_entry_key", net_entry_key, self._entry_from_row)

    def net_entries_by_keys(self, keys: Iterable[str]) -> Mapping[str, NetDirectoryEntry]:
        """Load directory identities for a bounded UI result set."""
        return self._read_by_keys(
            "net_directory_entries",
            "net_entry_key",
            keys,
            self._entry_from_row,
        )

    def net_entry_group_links(self, net_entry_key: str) -> tuple[tuple[str, str | None], ...]:
        return self._read_group_links(
            "net_directory_entry_group_links", "net_entry_key", net_entry_key
        )

    def list_net_entries(self, *, search: str = "", active: bool | None = True, limit: int = MAX_RESULTS, offset: int = 0) -> tuple[NetDirectoryEntry, ...]:
        clauses, params = ["1=1"], []
        if search.strip():
            needle = f"%{search.strip().lower()}%"; clauses.append("(LOWER(name) LIKE ? OR LOWER(COALESCE(description,'')) LIKE ? OR LOWER(COALESCE(scope,'')) LIKE ?)"); params.extend([needle] * 3)
        if active is not None: clauses.append("active=? AND retired=?"); params.extend([int(active), int(not active)])
        return self._read_many("net_directory_entries", f"SELECT * FROM net_directory_entries WHERE {' AND '.join(clauses)} ORDER BY name COLLATE NOCASE, net_entry_key LIMIT ? OFFSET ?", (*params, _limit(limit), max(0, int(offset))), self._entry_from_row)

    def create_session(self, session: NetDirectorySession) -> NetDirectorySession:
        return self._write_session(session, create=True)

    def update_session(self, session: NetDirectorySession) -> NetDirectorySession:
        return self._write_session(session, create=False)

    def _write_session(self, session: NetDirectorySession, *, create: bool) -> NetDirectorySession:
        now = _utc_now()
        with self._write() as conn:
            self._require_schema(conn)
            old = conn.execute("SELECT * FROM net_directory_sessions WHERE net_session_key=?", (session.net_session_key,)).fetchone()
            if create and old: raise CatalogValidationError(f"session exists: {session.net_session_key}")
            if not create and not old: raise CatalogValidationError(f"session not found: {session.net_session_key}")
            if not conn.execute("SELECT 1 FROM net_directory_entries WHERE net_entry_key=?", (session.net_entry_key,)).fetchone():
                raise CatalogValidationError(f"net entry not found: {session.net_entry_key}")
            if session.frequency_resource_key and not conn.execute("SELECT 1 FROM frequency_resources WHERE frequency_resource_key=?", (session.frequency_resource_key,)).fetchone():
                raise CatalogValidationError(f"frequency not found: {session.frequency_resource_key}")
            if create:
                self._require_source_exists(conn, session.source_key)
            else:
                self._require_mutable_source(conn, session.source_key, old["source_key"])
            revision = self._revision(None) if not old else self._revision_from_hash(old["version_hash"])
            content_hash = _hash(self._session_payload(session)); version_hash = _hash({"content_hash": content_hash, "revision": revision}); created = old["created_utc"] if old else now
            conn.execute("""INSERT INTO net_directory_sessions(net_session_key,net_entry_key,source_key,service,frequency_resource_key,recurrence,local_start_time,duration_minutes,timezone,effective_start_date,effective_end_date,exception_dates_json,reminder_minutes,mode,mode_details,day_utc,content_hash,version_hash,active,retired,replacement_net_session_key,created_utc,updated_utc,fldigi_tuning_json)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(net_session_key) DO UPDATE SET net_entry_key=excluded.net_entry_key,source_key=excluded.source_key,service=excluded.service,frequency_resource_key=excluded.frequency_resource_key,recurrence=excluded.recurrence,local_start_time=excluded.local_start_time,duration_minutes=excluded.duration_minutes,timezone=excluded.timezone,effective_start_date=excluded.effective_start_date,effective_end_date=excluded.effective_end_date,exception_dates_json=excluded.exception_dates_json,reminder_minutes=excluded.reminder_minutes,mode=excluded.mode,mode_details=excluded.mode_details,day_utc=excluded.day_utc,content_hash=excluded.content_hash,version_hash=excluded.version_hash,active=excluded.active,retired=excluded.retired,replacement_net_session_key=excluded.replacement_net_session_key,updated_utc=excluded.updated_utc,fldigi_tuning_json=excluded.fldigi_tuning_json""",
                         (session.net_session_key,session.net_entry_key,session.source_key,session.service,session.frequency_resource_key,session.recurrence,session.local_start_time,session.duration_minutes,session.timezone,session.effective_start_date,session.effective_end_date,_canonical_json(session.exception_dates),session.reminder_minutes,session.mode,session.mode_details,session.day_utc,content_hash,version_hash,int(session.active),int(session.retired),session.replacement_net_session_key,created,now,session.fldigi_tuning_json))
        return dataclasses.replace(session, content_hash=content_hash, version_hash=version_hash, created_utc=created, updated_utc=now)

    def get_session(self, net_session_key: str) -> NetDirectorySession | None:
        return self._read_one("net_directory_sessions", "net_session_key", net_session_key, self._session_from_row)

    def sessions_by_keys(self, keys: Iterable[str]) -> Mapping[str, NetDirectorySession]:
        """Load directory sessions for a bounded UI result set."""
        return self._read_by_keys(
            "net_directory_sessions",
            "net_session_key",
            keys,
            self._session_from_row,
        )

    def list_sessions(self, *, net_entry_key: str | None = None, frequency_resource_key: str | None = None, active: bool | None = True, limit: int = MAX_RESULTS, offset: int = 0) -> tuple[NetDirectorySession, ...]:
        clauses, params = ["1=1"], []
        if net_entry_key: clauses.append("net_entry_key=?"); params.append(net_entry_key)
        if frequency_resource_key: clauses.append("frequency_resource_key=?"); params.append(frequency_resource_key)
        if active is not None: clauses.append("active=? AND retired=?"); params.extend([int(active), int(not active)])
        return self._read_many("net_directory_sessions", f"SELECT * FROM net_directory_sessions WHERE {' AND '.join(clauses)} ORDER BY local_start_time, net_session_key LIMIT ? OFFSET ?", (*params, _limit(limit), max(0, int(offset))), self._session_from_row)

    def retire_net_entry(self, net_entry_key: str, *, replacement_net_entry_key: str | None = None) -> NetDirectoryEntry:
        entry = self.get_net_entry(net_entry_key)
        if not entry: raise CatalogValidationError(f"net entry not found: {net_entry_key}")
        return self.update_net_entry(dataclasses.replace(entry, active=False, retired=True, replacement_net_entry_key=_optional(replacement_net_entry_key)))

    def retire_session(self, net_session_key: str, *, replacement_net_session_key: str | None = None) -> NetDirectorySession:
        session = self.get_session(net_session_key)
        if not session: raise CatalogValidationError(f"session not found: {net_session_key}")
        return self.update_session(dataclasses.replace(session, active=False, retired=True, replacement_net_session_key=_optional(replacement_net_session_key)))

    def delete_session_if_unreferenced(self, net_session_key: str) -> None:
        usage = self.session_usage(net_session_key)
        if usage.is_referenced:
            raise ReferencedResourceError(f"session is referenced by {usage.total_references} record(s)")
        with self._write() as conn:
            self._require_schema(conn)
            row = conn.execute("SELECT source_key FROM net_directory_sessions WHERE net_session_key=?", (net_session_key,)).fetchone()
            if not row: return
            self._require_mutable_source(conn, row["source_key"])
            conn.execute("DELETE FROM net_directory_sessions WHERE net_session_key=?", (net_session_key,))

    def delete_net_entry_if_unreferenced(self, net_entry_key: str) -> None:
        usage = self.net_entry_usage(net_entry_key)
        if usage.is_referenced:
            raise ReferencedResourceError(f"net entry is referenced by {usage.total_references} record(s)")
        with self._write() as conn:
            self._require_schema(conn)
            row = conn.execute("SELECT source_key FROM net_directory_entries WHERE net_entry_key=?", (net_entry_key,)).fetchone()
            if not row: return
            self._require_mutable_source(conn, row["source_key"])
            conn.execute("DELETE FROM net_directory_entries WHERE net_entry_key=?", (net_entry_key,))

    delete_session = delete_session_if_unreferenced
    delete_net_entry = delete_net_entry_if_unreferenced

    # ---- computed usage and accepted-version review ----------------------------
    def frequency_usage(self, frequency_resource_key: str) -> ResourceUsage:
        return self._usage(frequency_resource_key, (("net_directory_sessions", "frequency_resource_key", "directory_sessions"), ("local_net_schedules", "frequency_resource_key", "local_net_schedules")))

    def net_entry_usage(self, net_entry_key: str) -> ResourceUsage:
        return self._usage(net_entry_key, (("net_directory_sessions", "net_entry_key", "directory_sessions"), ("local_net_schedules", "net_entry_key", "local_net_schedules")))

    def session_usage(self, net_session_key: str) -> ResourceUsage:
        return self._usage(net_session_key, (("local_net_schedules", "net_session_key", "local_net_schedules"), ("net_schedule", "net_session_key", "hf_schedules"), ("net_schedule_tab", "net_session_key", "hf_schedules")))

    def compare_frequency_version(self, frequency_resource_key: str, accepted_version_hash: str | None, accepted_snapshot: Mapping[str, Any] | None = None) -> VersionComparison:
        resource = self._must_frequency(frequency_resource_key)
        return self._comparison(resource.frequency_resource_key, accepted_version_hash, resource.version_hash, accepted_snapshot, self._frequency_payload(resource))

    def compare_session_version(self, net_session_key: str, accepted_version_hash: str | None, accepted_snapshot: Mapping[str, Any] | None = None) -> VersionComparison:
        session = self.get_session(net_session_key)
        if not session: raise CatalogValidationError(f"session not found: {net_session_key}")
        return self._comparison(session.net_session_key, accepted_version_hash, session.version_hash, accepted_snapshot, self._session_payload(session))

    # ---- internal helpers -------------------------------------------------------
    def _read_one(self, table: str, key_column: str, key: str, decoder: Any) -> Any | None:
        conn = self._read_connection()
        if conn is None: return None
        try:
            if not table_exists(conn, table): return None
            row = conn.execute(f"SELECT * FROM {table} WHERE {key_column}=?", (str(key).strip(),)).fetchone()
            return decoder(row) if row else None
        except sqlite3.Error:
            return None
        finally: conn.close()

    def _read_many(self, table: str, sql: str, params: Sequence[Any], decoder: Any) -> tuple[Any, ...]:
        conn = self._read_connection()
        if conn is None: return ()
        try:
            if not table_exists(conn, table): return ()
            return tuple(decoder(row) for row in conn.execute(sql, tuple(params)).fetchall())
        except sqlite3.Error:
            return ()
        finally: conn.close()

    def _read_by_keys(
        self,
        table: str,
        key_column: str,
        keys: Iterable[str],
        decoder: Any,
    ) -> Mapping[str, Any]:
        distinct = tuple(dict.fromkeys(str(key).strip() for key in keys if str(key).strip()))[
            :MAX_BATCH_KEYS
        ]
        if not distinct:
            return {}
        conn = self._read_connection()
        if conn is None:
            return {}
        try:
            if not table_exists(conn, table):
                return {}
            result: dict[str, Any] = {}
            for offset in range(0, len(distinct), 400):
                batch = distinct[offset : offset + 400]
                marks = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE {key_column} IN ({marks})",
                    batch,
                ).fetchall()
                for row in rows:
                    result[str(row[key_column])] = decoder(row)
            return result
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    @staticmethod
    def _key(value: object, field_name: str) -> str:
        text = str(value or "").strip()
        if not text: raise CatalogValidationError(f"{field_name} is required")
        return text

    @classmethod
    def _group_links(
        cls, values: Iterable[str] | Mapping[str, str]
    ) -> tuple[tuple[str, str | None], ...]:
        if isinstance(values, Mapping):
            pairs = ((key, _optional(name)) for key, name in values.items())
        else:
            iterable = (values,) if isinstance(values, str) else values
            pairs = ((key, None) for key in iterable)
        normalized = {(cls._key(key, "operating_group_key"), name) for key, name in pairs}
        return tuple(sorted(normalized))

    def _read_group_links(
        self, table: str, owner_column: str, owner_key: str
    ) -> tuple[tuple[str, str | None], ...]:
        conn = self._read_connection()
        if conn is None:
            return ()
        try:
            if not table_exists(conn, table):
                return ()
            return tuple(
                (str(row["operating_group_key"]), _optional(row["group_name_snapshot"]))
                for row in conn.execute(
                    f"""SELECT operating_group_key,group_name_snapshot FROM {table}
                    WHERE {owner_column}=? ORDER BY group_name_snapshot COLLATE NOCASE,operating_group_key""",
                    (str(owner_key).strip(),),
                )
            )
        except sqlite3.Error:
            return ()
        finally:
            conn.close()

    def _require_schema(self, conn: sqlite3.Connection) -> None:
        if not self._has_catalog(conn): raise CatalogValidationError("resource catalog schema is not initialized")

    def _require_mutable_source(self, conn: sqlite3.Connection, source_key: str, old_source_key: str | None = None) -> None:
        source = self._require_source_exists(conn, source_key)
        if bool(source["read_only"]): raise ReadOnlyResourceError(f"source is read-only: {source_key}")
        if old_source_key and old_source_key != source_key:
            old = conn.execute("SELECT read_only FROM resource_catalog_sources WHERE source_key=?", (old_source_key,)).fetchone()
            if old and bool(old["read_only"]): raise ReadOnlyResourceError(f"source is read-only: {old_source_key}")

    @staticmethod
    def _require_source_exists(conn: sqlite3.Connection, source_key: str) -> sqlite3.Row:
        source = conn.execute("SELECT read_only FROM resource_catalog_sources WHERE source_key=?", (source_key,)).fetchone()
        if not source:
            raise CatalogValidationError(f"source not found: {source_key}")
        return source

    @staticmethod
    def _revision(value: str | None) -> str: return str((int(value or "0") if str(value or "0").isdigit() else 0) + 1)
    @staticmethod
    def _revision_from_hash(_: str | None) -> str: return "1"  # version hash is opaque; content changes remain detectable

    def _must_frequency(self, key: str) -> FrequencyResource:
        value = self.get_frequency(key)
        if not value: raise CatalogValidationError(f"frequency not found: {key}")
        return value

    def _usage(self, key: str, targets: Sequence[tuple[str, str, str]]) -> ResourceUsage:
        conn = self._read_connection()
        if conn is None: return ResourceUsage(key, 0, {})
        try:
            counts: Counter[str] = Counter()
            for table, column, label in targets:
                if table_exists(conn, table):
                    try: counts[label] += int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (key,)).fetchone()[0])
                    except sqlite3.Error: pass
            return ResourceUsage(key, sum(counts.values()), dict(counts))
        finally: conn.close()

    @staticmethod
    def _comparison(key: str, accepted_hash: str | None, current_hash: str | None, snapshot: Mapping[str, Any] | None, current: Mapping[str, Any]) -> VersionComparison:
        # An accepted snapshot may be deliberately partial (for example, an HF
        # schedule stores only fields it subscribed to).  Missing accepted fields
        # are unknown, not changes, so diff only the captured field set.
        diffs = () if snapshot is None else tuple(
            FieldDiff(name, snapshot.get(name), current.get(name))
            for name in sorted(snapshot)
            if _canonical_json(snapshot.get(name)) != _canonical_json(current.get(name))
        )
        return VersionComparison(key, accepted_hash, current_hash, bool(accepted_hash and accepted_hash != current_hash), diffs)

    @staticmethod
    def _frequency_payload(value: FrequencyResource) -> dict[str, Any]:
        payload = dataclasses.asdict(value)
        for name in ("content_hash", "version_hash", "content_version", "created_utc", "updated_utc"): payload.pop(name, None)
        return payload
    @staticmethod
    def _entry_payload(value: NetDirectoryEntry) -> dict[str, Any]:
        payload = dataclasses.asdict(value)
        for name in ("content_hash", "version_hash", "created_utc", "updated_utc"): payload.pop(name, None)
        return payload
    @staticmethod
    def _session_payload(value: NetDirectorySession) -> dict[str, Any]:
        payload = dataclasses.asdict(value)
        for name in ("content_hash", "version_hash", "created_utc", "updated_utc"): payload.pop(name, None)
        return payload

    @staticmethod
    def _frequency_values(value: FrequencyResource, revision: str, content_hash: str, version_hash: str, created: str, now: str) -> tuple[Any, ...]:
        return (value.frequency_resource_key,value.source_key,value.resource_kind,value.service,value.jurisdiction,value.band,value.channel,value.label,value.lower_hz,value.upper_hz,value.receive_hz,value.transmit_hz,value.center_hz,value.offset_hz,value.mode,value.bandwidth_hz,value.tone,value.locality,value.grid,value.coverage,value.notes,value.provenance,revision,content_hash,version_hash,int(value.active),int(value.retired),value.replacement_frequency_resource_key,created,now)
    @staticmethod
    def _frequency_upsert_sql() -> str:
        return """INSERT INTO frequency_resources(frequency_resource_key,source_key,resource_kind,service,jurisdiction,band,channel,label,lower_hz,upper_hz,receive_hz,transmit_hz,center_hz,offset_hz,mode,bandwidth_hz,tone,locality,grid,coverage,notes,provenance,content_version,content_hash,version_hash,active,retired,replacement_frequency_resource_key,created_utc,updated_utc)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(frequency_resource_key) DO UPDATE SET source_key=excluded.source_key,resource_kind=excluded.resource_kind,service=excluded.service,jurisdiction=excluded.jurisdiction,band=excluded.band,channel=excluded.channel,label=excluded.label,lower_hz=excluded.lower_hz,upper_hz=excluded.upper_hz,receive_hz=excluded.receive_hz,transmit_hz=excluded.transmit_hz,center_hz=excluded.center_hz,offset_hz=excluded.offset_hz,mode=excluded.mode,bandwidth_hz=excluded.bandwidth_hz,tone=excluded.tone,locality=excluded.locality,grid=excluded.grid,coverage=excluded.coverage,notes=excluded.notes,provenance=excluded.provenance,content_version=excluded.content_version,content_hash=excluded.content_hash,version_hash=excluded.version_hash,active=excluded.active,retired=excluded.retired,replacement_frequency_resource_key=excluded.replacement_frequency_resource_key,updated_utc=excluded.updated_utc"""

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> CatalogSource: return CatalogSource(**{name: row[name] for name in CatalogSource.__dataclass_fields__})
    @staticmethod
    def _frequency_from_row(row: sqlite3.Row) -> FrequencyResource: return FrequencyResource(**{name: row[name] for name in FrequencyResource.__dataclass_fields__})
    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> NetDirectoryEntry: return NetDirectoryEntry(**{name: row[name] for name in NetDirectoryEntry.__dataclass_fields__})
    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> NetDirectorySession:
        data = {name: row[name] for name in NetDirectorySession.__dataclass_fields__ if name != "exception_dates" and name in row.keys()}
        try: data["exception_dates"] = tuple(json.loads(row["exception_dates_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError): data["exception_dates"] = ()
        return NetDirectorySession(**data)


__all__ = [
    "CATALOG_TABLES",
    "MAX_RESULTS",
    "STATION_MANUAL_SOURCE_KEY",
    "ResourceCatalogStore",
    "create_resource_catalog_schema",
    "ensure_resource_catalog_schema",
    "_new_key",
]
