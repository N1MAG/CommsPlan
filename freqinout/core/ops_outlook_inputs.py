"""Serial-worker outlook inputs, independent of roster and traffic publications."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from freqinout.core.planner_input_catalog import PlannerCatalogReader, PlannerInputCatalog, MAX_CATALOG_BYTES
from freqinout.core.schedule_configuration_cache import ConfigurationChanged
from freqinout.core.schedule_source_inputs import ScheduleSourceError
from freqinout.core.sqlite_utils import connect_sqlite_readonly


class _ScheduleReader(PlannerCatalogReader):
    # Revision discovery remains shared; acquiring this consumer's inputs does
    # Includes assigned plans so Ops and the per-radio scheduler use the same
    # schedule source; launch configuration and roster tables remain unrelated.
    settings_tables = ('kv', 'daily_schedule_tab', 'device_profiles', 'operating_profiles',
                       'operating_profile_assignments', 'assigned_plans', 'frequency_plans')
    nets_tables = ('net_schedule_tab', 'sop_profiles', 'sop_schedule_layer')


@dataclass(frozen=True)
class OutlookInputs:
    catalog: PlannerInputCatalog
    sop_tables: dict[str, tuple[dict, ...]]
    sop_columns: dict[str, tuple[str, ...]]
    status_identity: str | None

    @property
    def revision(self):
        return self.catalog.revision

    @property
    def daily_rows(self):
        return self.catalog.rows('daily_schedule_tab')

    @property
    def net_rows(self):
        return self.catalog.rows('net_schedule_tab', 'nets')


class OutlookInputReader:
    """Lazy owner-bound reader. Construct on Qt; call/close only on its worker.

    Runtime SOP tables are checked independently of the configuration counter.
    A second short snapshot detects a completion/source edit during acquisition;
    this is bounded observation, not atomicity across the two database files.
    """
    def __init__(self, config_dir: Path, *, max_rows=50_000, max_total_rows=250_000, clock=None):
        kwargs = dict(max_rows=max_rows, max_total_rows=max_total_rows)
        if clock is not None:
            kwargs['clock'] = clock
        self.raw = _ScheduleReader(config_dir, **kwargs)
        self.config_dir = Path(config_dir)
        self.max_rows = max_rows
        self.status_reads = 0
        self._present_tables = set()

    @property
    def loads(self):
        return self.raw.loads

    def revision(self):
        return self.raw.revision()

    def _status(self, catalog):
        self.raw._check_owner()
        if 'nets' not in catalog.present_databases:
            return {}, {}, 'absent'
        conn = connect_sqlite_readonly(self.config_dir / 'freqinout_nets.db',
                                       timeout=.5, busy_timeout_ms=500, row_factory=sqlite3.Row)
        tables, columns = {}, {}
        size = 0
        try:
            conn.execute('BEGIN')
            existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            sop_names = {'sop_profiles', 'sop_schedule_layer', 'sop_actions', 'sop_action_state'}
            present = existing & sop_names
            if present and present != sop_names:
                missing = ', '.join(sorted(sop_names - present))
                raise ScheduleSourceError(f'SOP schema is incomplete ({missing}); repair it and retry')
            for name in ('sop_actions', 'sop_action_state'):
                if name not in existing:
                    continue
                count = conn.execute(f'SELECT COUNT(*) FROM (SELECT 1 FROM "{name}" LIMIT ?)',
                                     (self.max_rows + 1,)).fetchone()[0]
                if count > self.max_rows:
                    raise ScheduleSourceError(f'SOP input {name} exceeds supported capacity ({self.max_rows:,} rows)')
                columns[name] = tuple(r[1] for r in conn.execute(f'PRAGMA table_info("{name}")'))
                quoted = [f'"{c.replace(chr(34), chr(34)*2)}"' for c in columns[name]]
                lengths = '+'.join(f'COALESCE(length(CAST({c} AS BLOB)),0)' for c in quoted)
                raw_size = conn.execute(f'SELECT COALESCE(SUM({lengths}),0) FROM "{name}"').fetchone()[0]
                if raw_size > MAX_CATALOG_BYTES - size:
                    raise ScheduleSourceError('SOP input exceeds supported size (128 MiB)')
                rows = []
                for row in conn.execute(f'SELECT * FROM "{name}" LIMIT ?', (self.max_rows + 1,)):
                    if len(rows) >= self.max_rows:
                        raise ScheduleSourceError(f'SOP input {name} exceeds supported capacity ({self.max_rows:,} rows)')
                    value = dict(row)
                    size += len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
                    if size > MAX_CATALOG_BYTES:
                        raise ScheduleSourceError('SOP input exceeds supported size (128 MiB)')
                    rows.append(value)
                tables[name] = tuple(rows)
            self.status_reads += 1
            identity = hashlib.sha256(json.dumps((columns, tables), sort_keys=True, default=str).encode()).hexdigest()
            return tables, columns, identity
        finally:
            conn.close()

    def read(self, *, force=False, include_sop=True):
        try:
            catalog = self.raw.read(force=force)
            present_tables = set(catalog.tables)
            if self._present_tables - present_tables:
                raise ScheduleSourceError('A previously available outlook source table is missing; repair it and retry')
            if include_sop:
                tables, columns, identity = self._status(catalog)
                if self._status(catalog)[2] != identity:
                    raise ConfigurationChanged('SOP completion or source changed during reading; retry')
            else:
                tables, columns, identity = {}, {}, None
            if self.revision() != catalog.revision:
                raise ConfigurationChanged('Outlook inputs changed during reading; retry')
            self._present_tables = present_tables
            return OutlookInputs(catalog, copy.deepcopy(tables), columns, identity)
        except Exception as exc:
            if isinstance(exc, ConfigurationChanged):
                raise
            if isinstance(exc, ScheduleSourceError):
                raise ScheduleSourceError(str(exc).replace('Plan Builder', 'Schedule outlook').replace('Planner catalogue', 'Schedule outlook')) from exc
            raise ScheduleSourceError(f'Cannot read schedule outlook: {exc}') from exc

    def close(self):
        self.raw.close()
