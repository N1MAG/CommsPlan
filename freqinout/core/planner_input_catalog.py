"""Detached, revision-validated Planner inputs owned by one serial reader.

This module has no Qt dependency and performs no work during construction.
Startup/schema administration remains outside ordinary catalogue refreshes.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import sqlite3
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from freqinout.core.operating_group_frequency import ensure_operating_group_frequency_keys
from freqinout.core.schedule_configuration_cache import (
    ConfigurationChanged,
    CONFIGURATION_KEYS,
    NETS_TABLES,
    SETTINGS_TABLES,
    ScheduleRevisionTracker,
)
from freqinout.core.schedule_source_inputs import ScheduleSourceError
from freqinout.core.sqlite_utils import connect_sqlite_readonly


# Input completeness is independent of the existing 500-row presentation cap.
# These limits support at least the existing 50,000-row schedule-reader scale.
MAX_TABLE_ROWS = 50_000
MAX_TOTAL_ROWS = 250_000
MAX_CATALOG_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class PlannerInputCatalog:
    revision: tuple
    publication: int
    loaded_at: float
    loaded_utc: str
    config_dir: Path
    settings: dict[str, Any]
    tables: dict[str, tuple[dict[str, Any], ...]]
    absent: tuple[str, ...]
    columns: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def present_databases(self) -> tuple[str, ...]:
        # Presence belongs to validated file identity, even for a legacy file
        # containing none of the current source tables.
        return tuple(name for name, key in zip(("settings", "nets"), self.revision)
                     if key != ("missing",))

    def rows(self, table: str, database: str = "settings") -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.tables.get(f"{database}:{table}", ())))


class PlannerCatalogReader:
    """A lazy reader; every operation, including cleanup, has one thread owner.

    Separate database transactions are validated against the same before/after
    revision pair. This detects intervening commits; it is not a cross-file
    atomic transaction or an entity-incarnation guarantee.
    """

    settings_tables = SETTINGS_TABLES
    nets_tables = NETS_TABLES

    def __init__(self, config_dir: Path, *, max_rows: int = MAX_TABLE_ROWS,
                 max_total_rows: int = MAX_TOTAL_ROWS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config_dir = Path(config_dir)
        self.max_rows = int(max_rows)
        self.max_total_rows = int(max_total_rows)
        if self.max_rows <= 0 or self.max_total_rows <= 0:
            raise ValueError("Planner catalogue capacity must be positive")
        self.clock = clock
        self.loads = 0
        self.attempts = 0
        self.hits = 0
        self._owner: int | None = None
        self._tracker: ScheduleRevisionTracker | None = None
        self._catalog: PlannerInputCatalog | None = None
        self._usable = False
        self._closed = False

    def _check_owner(self) -> None:
        current = threading.get_ident()
        if self._owner is None:
            self._owner = current
        elif self._owner != current:
            raise RuntimeError("Planner catalogue reader used outside its owning worker")
        if self._closed:
            raise RuntimeError("Planner catalogue reader is closed")

    def revision(self) -> tuple:
        self._check_owner()
        if self._tracker is None:
            self._tracker = ScheduleRevisionTracker(self.config_dir, allow_missing=True)
        return self._tracker.key()

    def read(self, *, force: bool = False) -> PlannerInputCatalog:
        self._check_owner()
        try:
            revision = self.revision()
            if not force and self._usable and self._catalog is not None and self._catalog.revision == revision:
                self.hits += 1
                return copy.deepcopy(self._catalog)
            self.attempts += 1
            self._read_columns: dict[str, tuple[str, ...]] = {}
            settings, tables, absent = self._read_tables()
            if self._catalog is not None and "nets" in self._catalog.present_databases:
                if all(f"nets:{name}" in absent for name in self.nets_tables):
                    try:
                        (self.config_dir / "freqinout_nets.db").stat()
                    except FileNotFoundError as exc:
                        raise ScheduleSourceError("Previously available Net/SOP database is missing; repair it or reopen the intended profile") from exc
            if self.revision() != revision:
                raise ConfigurationChanged("Plan Builder inputs changed during loading; retry the current sources")
            catalog = PlannerInputCatalog(
                revision=revision,
                publication=self.loads + 1,
                loaded_at=self.clock(),
                loaded_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                config_dir=self.config_dir,
                settings=settings,
                tables=tables,
                absent=tuple(sorted(absent)),
                columns=copy.deepcopy(self._read_columns),
            )
            # A failed forced load cannot make an older same-revision cache a
            # successful recovery. A fresh complete read is required afterward.
            self._catalog = copy.deepcopy(catalog)
            self._usable = True
            self.loads += 1
            return catalog
        except (ConfigurationChanged, ScheduleSourceError):
            self._usable = False
            raise
        except Exception as exc:
            self._usable = False
            raise ScheduleSourceError(f"Cannot read Plan Builder inputs: {exc}") from exc

    def _read_tables(self) -> tuple[dict, dict, set[str]]:
        tables: dict[str, tuple[dict, ...]] = {}
        absent: set[str] = set()
        total = 0
        byte_count = 0
        # Hold each database's read transaction across all of its input reads.
        # Read-only opens never install schemas or create optional files.
        with ExitStack() as stack:
            for database, filename, names, required in (
                ("settings", "freqinout.db", self.settings_tables, True),
                ("nets", "freqinout_nets.db", self.nets_tables, False),
            ):
                path = self.config_dir / filename
                try:
                    path.stat()
                except FileNotFoundError:
                    if required:
                        raise ScheduleSourceError("Plan Builder configuration database is unavailable")
                    absent.update(f"{database}:{name}" for name in names)
                    continue
                connection = connect_sqlite_readonly(path, timeout=0.5, busy_timeout_ms=500,
                                                      row_factory=sqlite3.Row)
                stack.callback(connection.close)
                connection.execute("BEGIN")
                existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for name in names:
                    key = f"{database}:{name}"
                    if name not in existing:
                        if name == "kv":
                            raise ScheduleSourceError("Plan Builder settings schema is unavailable")
                        absent.add(key)
                        continue
                    # Expose only revision-covered settings. Bookkeeping-only
                    # timestamps are intentionally not part of source identity;
                    # the existing tracker excludes timestamp-only updates.
                    schema_columns = tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")'))
                    self._read_columns[key] = schema_columns
                    columns = [column for column in schema_columns
                               if database != "settings" or column not in {"created_utc", "updated_utc"}]
                    quoted = ["\"" + column.replace('"', '""') + "\"" for column in columns]
                    where = ""
                    parameters: tuple = ()
                    if name == "kv":
                        where = " WHERE key IN (" + ",".join("?" for _ in CONFIGURATION_KEYS) + ")"
                        parameters = tuple(CONFIGURATION_KEYS)
                    count = connection.execute(
                        f'SELECT COUNT(*) FROM (SELECT 1 FROM "{name}"{where} LIMIT ?)',
                        (*parameters, self.max_rows + 1),
                    ).fetchone()[0]
                    if count > self.max_rows:
                        raise ScheduleSourceError(f"Plan Builder input {name} exceeds supported capacity ({self.max_rows:,} rows); prior work is retained")
                    if total + count > self.max_total_rows:
                        raise ScheduleSourceError(f"Plan Builder inputs exceed supported combined capacity ({self.max_total_rows:,} rows); prior work is retained")
                    # SQLite measures large cells before Python materializes
                    # them. Stream rows afterward; never fetch a whole oversized
                    # table before discovering the capacity violation.
                    lengths = "+".join(f"COALESCE(length(CAST({column} AS BLOB)),0)" for column in quoted) or "0"
                    raw_bytes = connection.execute(
                        f'SELECT COALESCE(SUM({lengths}),0) FROM "{name}"{where}', parameters,
                    ).fetchone()[0]
                    if raw_bytes > MAX_CATALOG_BYTES - byte_count:
                        raise ScheduleSourceError("Plan Builder input size exceeds supported capacity (128 MiB); prior work is retained")
                    collected = []
                    cursor = connection.execute(f'SELECT {",".join(quoted)} FROM "{name}"{where}', parameters)
                    for raw_row in cursor:
                        row = dict(raw_row)
                        byte_count += len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
                        if byte_count > MAX_CATALOG_BYTES:
                            raise ScheduleSourceError("Plan Builder input size exceeds supported capacity (128 MiB); prior work is retained")
                        collected.append(row)
                    total += len(collected)
                    tables[key] = tuple(collected)
        settings = {}
        for row in tables.get("settings:kv", ()):
            key, value = str(row["key"]), row["value"]
            try:
                settings[key] = json.loads(value)
            except (TypeError, ValueError):
                settings[key] = value
        groups = settings.get("operating_groups")
        if isinstance(groups, list) and all(isinstance(row, dict) for row in groups):
            settings["operating_groups"] = ensure_operating_group_frequency_keys(groups)
        return settings, tables, absent

    def close(self) -> None:
        if self._closed:
            return
        self._check_owner()
        if self._tracker is not None:
            self._tracker.close()
            self._tracker = None
        self._closed = True
        self._catalog = None
        self._usable = False
