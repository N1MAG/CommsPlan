"""Worker-owned schedule revisions; traffic commits never invalidate schedule inputs.

Counters and triggers are additive and transactionally updated by *all* writers,
including another FIO process. No timer/UI callback may construct or read these.
"""
from __future__ import annotations

import copy
import sqlite3
import threading
from pathlib import Path
from typing import Callable


SETTINGS_TABLES = (
    'kv', 'device_profiles', 'operating_profiles',
    'operating_profile_assignments', 'assigned_plans', 'frequency_plans',
    'daily_schedule_tab', 'runtime_policies', 'js8_instances',
    'fast_light_configs', 'varac_nodes', 'operator_schedule_authorizations', 'radio_software_identity_records', 'radio_launch_bundle_items',
)
NETS_TABLES = ('net_schedule_tab', 'net_schedule', 'sop_profiles',
               'sop_schedule_layer', 'sop_net_conflict_policy', 'net_resources')
_COUNTER = '_fio_schedule_revision'
CONFIGURATION_KEYS = (
    'hf_schedule', 'daily_schedule', 'net_schedule', 'operating_groups',
    'sop_schedule_layer_enabled', 'sop_frequency_variant_references',
    'radio_operator_controls', 'freqplanner_hf_daily_schedule_sets',
    'freqplanner_hf_net_schedule_sets', 'freqplanner_selected_hf_daily_schedule_set_id',
    'freqplanner_selected_hf_net_schedule_set_id',
    'freqplanner_selected_sop_schedule_plan_id',
)



class ConfigurationChanged(RuntimeError):
    """A configuration load crossed an edit; its rows must not be published."""


class _DatabaseRevision:
    def __init__(self, path: Path, tables: tuple[str, ...], *, allow_missing=False):
        self.path, self.tables = Path(path), tables
        self.allow_missing = allow_missing
        self.connection = None
        self.identity = None
        self.schema = None
        self.owner = threading.get_ident()

    def _check_owner(self):
        if self.owner != threading.get_ident():
            raise RuntimeError('Schedule revision connection used outside its worker')

    def close(self):
        self._check_owner()
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _install(self):
        conn = self.connection
        conn.execute('PRAGMA query_only=OFF')
        try:
            with conn:
                conn.execute(f'CREATE TABLE IF NOT EXISTS {_COUNTER} '
                             '(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)')
                conn.execute(f'INSERT OR IGNORE INTO {_COUNTER} VALUES (1, 0)')
                existing = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for table in self.tables:
                    if table not in existing:
                        continue
                    for operation in ('INSERT', 'UPDATE', 'DELETE'):
                        condition = ''
                        if table == 'kv':
                            keys = ','.join("'" + key + "'" for key in CONFIGURATION_KEYS)
                            references = ('NEW',) if operation == 'INSERT' else ('OLD',) if operation == 'DELETE' else ('OLD', 'NEW')
                            condition = ' WHEN ' + ' OR '.join(f'{ref}.key IN ({keys})' for ref in references)
                        name = f'_fio_schedule_{table}_{operation}'
                        definition = (f'CREATE TRIGGER {name} '
                                      f'AFTER {operation} ON "{table}"{condition} BEGIN '
                                      f'UPDATE {_COUNTER} SET revision=revision+1 WHERE id=1; END')
                        existing_trigger = conn.execute(
                            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
                        if existing_trigger is not None and existing_trigger[0] != definition:
                            conn.execute(f'DROP TRIGGER {name}')
                            existing_trigger = None
                        if existing_trigger is None:
                            conn.execute(definition)
        finally:
            conn.execute('PRAGMA query_only=ON')
        self.schema = conn.execute('PRAGMA schema_version').fetchone()[0]

    def key(self):
        self._check_owner()
        try:
            stat = self.path.stat()  # Missing/replaced databases cannot reuse authority.
        except FileNotFoundError:
            self.close()
            if self.allow_missing:
                return ("missing",)
            raise
        identity = (stat.st_dev, stat.st_ino)
        if self.connection is None or identity != self.identity:
            self.close()
            self.connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw',
                                              uri=True, timeout=0.5)
            self.identity = identity
            self._install()
        schema = self.connection.execute('PRAGMA schema_version').fetchone()[0]
        if schema != self.schema:
            self._install()  # Track newly created/replaced authoring tables too.
        revision = self.connection.execute(f'SELECT revision FROM {_COUNTER} WHERE id=1').fetchone()
        if revision is None:
            raise RuntimeError('Schedule revision counter is unavailable')
        return (*identity, self.schema, int(revision[0]))


class ScheduleRevisionTracker:
    def __init__(self, config_dir: Path, *, allow_missing=False):
        self.databases = (_DatabaseRevision(Path(config_dir) / 'freqinout.db', SETTINGS_TABLES, allow_missing=allow_missing),
                          _DatabaseRevision(Path(config_dir) / 'freqinout_nets.db', NETS_TABLES, allow_missing=allow_missing))

    def key(self):
        return tuple(database.key() for database in self.databases)

    def close(self):
        for database in self.databases:
            database.close()


class ScheduleConfigurationCache:
    """One serial worker owns replacement; consumers receive detached copies."""
    def __init__(self):
        self.revision = None
        self.rows = None
        self.loads = 0
        self.hits = 0
        self.tracker = None

    def read(self, key: Callable[[], object], load: Callable[[], object], *, force=False, prepare=None):
        try:
            return self._read(key, load, force=force, prepare=prepare)
        except Exception:
            self.rows = None
            self.revision = None
            raise

    def _read(self, key, load, *, force, prepare):
        revision = key()
        if not force and self.rows is not None and revision == self.revision:
            self.hits += 1
            return copy.deepcopy(self.rows)
        if prepare is not None:
            # Preparation may write derived plans. It returns the exact key
            # it validated; a later probe must never stamp stale derivations
            # with a concurrent author's newer revision.
            revision = prepare()
        rows = load()
        if key() != revision:
            raise ConfigurationChanged('Schedule configuration changed during loading')
        self.rows = copy.deepcopy(rows)
        self.revision = revision
        self.loads += 1
        return copy.deepcopy(self.rows)

    def close(self):
        if self.tracker is not None:
            self.tracker.close()
