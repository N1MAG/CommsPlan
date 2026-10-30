"""One worker's input reader and compatibility model; no widgets or live writes."""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from freqinout.core.config_paths import get_config_dir
from freqinout.core.multi_radio_store import (
    FIO_EXISTING_USE_IGNORED_KEYS, MULTI_RIG_MIGRATION_COMPLETED_AT_KEY,
    MULTI_RIG_MIGRATION_SUMMARY_PREFIX,
)
from freqinout.core.multi_rig_runtime_status import build_multi_rig_runtime_status
from freqinout.core.planner_catalog_model import PlannerModelCompiler, _MemoryStore
from freqinout.core.schedule_configuration_cache import ConfigurationChanged
from freqinout.core.schedule_source_inputs import ScheduleSourceError
from freqinout.core.sqlite_utils import connect_sqlite_readonly


class PlannerCatalogWorker:
    def __init__(self, reader, *, fixed_root=False):
        self.fixed_root = bool(fixed_root)
        self.reader = reader
        self.compiler = PlannerModelCompiler()
        self._established_meaningful_usage = None

    def _check_root(self):
        root = get_config_dir() / 'config'
        if root != self.reader.config_dir:
            if self.fixed_root or self.reader._owner is not None:
                raise ScheduleSourceError('Active configuration profile changed; reopen Plan Builder')
            self.reader.config_dir = root

    def _runtime_status(self, catalog):
        # Presence of any meaningful legacy key matters to startup classification,
        # but its value is deliberately not part of the Planner catalogue.
        ignored = tuple(FIO_EXISTING_USE_IGNORED_KEYS | {MULTI_RIG_MIGRATION_COMPLETED_AT_KEY})
        conn = connect_sqlite_readonly(catalog.config_dir / 'freqinout.db') if self._established_meaningful_usage is None else None
        try:
            marks = ','.join('?' for _ in ignored)
            meaningful = self._established_meaningful_usage
            if conn is not None:
                meaningful = bool(conn.execute(
                f'SELECT 1 FROM kv WHERE key NOT IN ({marks}) AND substr(key,1,?) != ? LIMIT 1',
                (*ignored, len(MULTI_RIG_MIGRATION_SUMMARY_PREFIX), MULTI_RIG_MIGRATION_SUMMARY_PREFIX),
                ).fetchone())
                self._established_meaningful_usage = meaningful
        finally:
            if conn is not None:
                conn.close()
        connection = self.compiler._copy_database(catalog, 'settings')
        try:
            values = dict(catalog.settings)
            if meaningful:
                values['_planner_private_existing_usage'] = True
            return build_multi_rig_runtime_status(_MemoryStore(connection), settings_values=values)
        finally:
            connection.close()

    def load(self, *, force=False, source_ids=None, validation_requests=(), runtime_status=None,
             guard_context=None, guard_payloads=(), baseline_catalog=None, accepted_mutations=()):
        self._check_root()
        catalog = self.reader.read(force=force)
        status = runtime_status or self._runtime_status(catalog)
        model = self.compiler.build(catalog, source_ids=source_ids,
                                    validation_requests=validation_requests, runtime_status=status,
                                    guard_context=guard_context, guard_payloads=guard_payloads)
        if self.reader.revision() != catalog.revision:
            raise ConfigurationChanged('Plan Builder inputs changed during preparation; retry inputs')
        if accepted_mutations:
            from dataclasses import replace
            model = replace(model, acknowledged_mutation=only_expected_mutations(baseline_catalog, catalog, accepted_mutations))
        return model

    def probe(self):
        self._check_root()
        now = datetime.datetime.now(datetime.timezone.utc)
        sunday = now.date() - datetime.timedelta(days=(now.weekday() + 1) % 7)
        return self.reader.revision(), sunday

    def close(self):
        self.compiler.close()
        self.reader.close()

    def verify_action(self, *, expected_revision, plan_id=0, source_update=None, **kwargs):
        model = self.load(force=True, **kwargs)
        if model.catalog.revision != expected_revision:
            raise ConfigurationChanged('Inputs changed since this draft was reviewed. Review current inputs, then choose the action again.')
        if source_update is not None:
            from dataclasses import replace
            from freqinout.core.planner_catalog_model import DetachedSettings
            from freqinout.core.schedule_source_sets import assigned_plan_rf_guard_impacts_for_source_update
            category, set_id, rows = source_update
            impacts = assigned_plan_rf_guard_impacts_for_source_update(
                DetachedSettings(model.catalog.settings, model.catalog.config_dir), category, set_id, rows,
                store=self.compiler._store)
            model = replace(model, source_impacts=tuple(impacts))
        expected = None
        if plan_id:
            conn = connect_sqlite_readonly(model.catalog.config_dir / 'freqinout.db', row_factory=sqlite3.Row)
            try:
                row = conn.execute('SELECT * FROM frequency_plans WHERE id=?', (int(plan_id),)).fetchone()
                expected = dict(row) if row is not None else None
            finally:
                conn.close()
            if expected is None:
                raise ConfigurationChanged('The selected plan no longer exists. Review current inputs before saving.')
        if self.reader.revision() != expected_revision:
            raise ConfigurationChanged('Inputs changed during action verification; review and try again')
        return model, expected


def only_expected_mutations(before, after, mutations):
    """Recognize acknowledged local writes without excusing unrelated changes."""
    import json
    if before is None or not mutations or before.absent != after.absent or before.columns != after.columns:
        return False
    changes = {(table, str(identity)): value for table, identity, value in mutations}
    for table in before.tables.keys() | after.tables.keys():
        identity_key = 'key' if table == 'settings:kv' else 'id'
        left = {str(row.get(identity_key)): row for row in before.tables.get(table, ())}
        right = {str(row.get(identity_key)): row for row in after.tables.get(table, ())}
        if len(left) != len(before.tables.get(table, ())) or len(right) != len(after.tables.get(table, ())):
            return False
        for (changed_table, identity), expected in changes.items():
            if changed_table != table:
                continue
            actual = right.pop(identity, None)
            left.pop(identity, None)
            if expected is None:
                if actual is not None:
                    return False
            elif table == 'settings:kv':
                try:
                    value = json.loads(actual['value']) if actual is not None else object()
                except (ValueError, TypeError):
                    return False
                if value != expected:
                    return False
            else:
                clean = {key: value for key, value in expected.items()
                         if key not in {'created_utc', 'updated_utc'} and not key.startswith('_catalog_')}
                if actual != clean:
                    return False
        if left != right:
            return False
    return True
