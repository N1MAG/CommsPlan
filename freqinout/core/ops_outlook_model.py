"""Worker-only projection of local schedule and SOP reminder inputs."""
from __future__ import annotations

import datetime as dt
import math
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace

from freqinout.core.ops_outlook_inputs import OutlookInputReader
from freqinout.core.ops_outlook_rules import OutlookRules
from freqinout.core.planner_catalog_model import PlannerModelCompiler, DetachedSettings, _BorrowedConnection
from freqinout.core.schedule_configuration_cache import ConfigurationChanged
from freqinout.core.schedule_source_inputs import ScheduleSourceError
from freqinout.core.schedule_targeting import normalize_schedule_target_fields, schedule_row_matches_target_context
from freqinout.core.sop_manager import SOPManager
from freqinout.utils.timezones import get_timezone


class _ReadOnlySOP(SOPManager):
    def __init__(self, connection, settings):
        # Do not call SOPManager.__init__: ordinary observation never assures
        # tables, normalizes active profiles or writes completion state.
        self.connection = connection
        self.settings = settings

    def _connect(self):
        return _BorrowedConnection(self.connection)

    def resolve_primary_contacts(self, *_args, **_kwargs):
        # Upcoming reminders do not consume resolved roster contacts.
        return {'hub': [], 'ncs': [], 'peer': []}

    def diagnose_due_alignment(self, *_args, **_kwargs):
        # Alignment is not displayed or used to authorize these reminder rows.
        # Avoid querying unrelated schedule/roster dependencies for unused data.
        return {'aligned': True, 'reason': 'not_evaluated', 'has_windows': False}


@dataclass(frozen=True)
class OutlookPublication:
    context_key: tuple
    inputs: object
    today: tuple
    tomorrow: tuple
    propagation: tuple
    daily_rows: tuple
    net_rows: tuple
    resolved_target: tuple
    loaded_at: float
    evaluated_at: dt.datetime
    valid_until: dt.datetime
    worker_ms: float
    action_token: tuple


class _Projector(OutlookRules):
    def __init__(self, inputs, context, sop):
        self.settings = DetachedSettings(inputs.catalog.settings, inputs.catalog.config_dir)
        # A captured display basis wins over a subsequently read preference;
        # publication is fenced against the current Qt context before acceptance.
        self.settings._data['timezone'] = context['timezone']
        self._show_local = context['show_local']
        self._sop_manager = sop
        self.group_combo = SimpleNamespace(currentData=lambda: context['group'])
        self.search_edit = SimpleNamespace(text=lambda: context['search'])
        target = context['target']
        if target == (None, None):
            devices = inputs.catalog.rows('device_profiles')
            primary = sorted((r for r in devices if r.get('runtime_active') and r.get('runtime_primary')),
                             key=lambda r: (r.get('display_order') or 0, r['id']))
            if primary:
                device_id = primary[0]['id']
                operating_ids = {r['id'] for r in inputs.catalog.rows('operating_profiles')}
                assignments = sorted((r for r in inputs.catalog.rows('operating_profile_assignments')
                                      if r.get('device_profile_id') == device_id and
                                      r.get('operating_profile_id') in operating_ids and
                                      r.get('assignment_state') in {'active', 'temporary_override'}),
                                     key=lambda r: r['id'])
                target = (device_id, assignments[0].get('operating_profile_id') if assignments else None)
        self.resolved_target = target
        def filtered(rows):
            return tuple(normalize_schedule_target_fields(r) for r in rows
                         if schedule_row_matches_target_context(normalize_schedule_target_fields(r),
                            device_profile_id=target[0], operating_profile_id=target[1]))
        daily, nets = self._assigned_rows(inputs, target[0])
        self.daily = filtered(daily)
        self.nets = filtered(nets)

    @staticmethod
    def _assigned_rows(inputs, device_id):
        """Resolve the scheduler's effective assigned source in this snapshot.

        Empty/broken assigned plans never fall through to unrelated raw rows.
        No live store or additional database read is permitted here.
        """
        from freqinout.core.multi_radio_store import EFFECTIVE_ASSIGNMENT_STATES
        assignments = sorted((row for row in inputs.catalog.rows('assigned_plans')
                              if row.get('device_profile_id') == device_id and
                              row.get('assignment_state') in EFFECTIVE_ASSIGNMENT_STATES),
                             key=lambda row: row['id']) if device_id else []
        if not assignments:
            return inputs.daily_rows, inputs.net_rows
        assignment = assignments[0]
        plan = next((row for row in inputs.catalog.rows('frequency_plans')
                     if row.get('id') == assignment.get('frequency_plan_id')), None)
        if plan is None:
            raise ScheduleSourceError('Assigned frequency plan is missing; review the radio plan assignment')
        try:
            refs = json.loads(str(plan.get('schedule_refs_json') or '[]'))
        except (TypeError, ValueError) as exc:
            raise ScheduleSourceError('Assigned frequency plan has invalid schedule rows; review it in Plan Builder') from exc
        if not isinstance(refs, list):
            raise ScheduleSourceError('Assigned frequency plan has invalid schedule rows; review it in Plan Builder')
        daily, nets = [], []
        saved_nets = {row.get('id'): row for row in inputs.net_rows}
        for ref in refs:
            if not isinstance(ref, dict) or ref.get('enabled') in (False, 0, '0', 'false'):
                continue
            row = dict(ref)
            row.update(target_scope='device_profile', target_device_profile_id=device_id)
            row.setdefault('frequency_plan_id', plan.get('id'))
            row.setdefault('frequency_plan_name', plan.get('name') or '')
            source = str(row.get('source') or row.get('source_type') or '').strip().upper()
            table = str(row.get('source_table') or '').strip().lower()
            if source in {'SOP', 'SOP_LAYER'} or table == 'sop_schedule_layer':
                continue  # Live SOP state, not a composed/clipped HF occurrence.
            if source == 'NET' or 'net' in table:
                try:
                    source_id = int(row.get('source_row_id') or row.get('id') or 0)
                except (TypeError, ValueError):
                    source_id = 0
                saved = saved_nets.get(source_id) if table in {'', 'net_schedule_tab', 'net_schedule'} else None
                if saved is not None:
                    from freqinout.core.net_source_identity import net_source_identity
                    # Generated refs intentionally omit app/recurrence details;
                    # validate the fields they carry, then bind omitted values
                    # to the canonical source in this revision. Split days,
                    # clipped times and radio targets are composition metadata.
                    comparison = dict(saved, **ref)
                    for key in ('day_utc', 'start_utc', 'end_utc', 'target_scope',
                                'target_device_profile_id', 'target_operating_profile_id'):
                        comparison[key] = saved.get(key)
                    if net_source_identity(comparison) != net_source_identity(saved):
                        saved = None
                if saved is not None:
                    # Keep canonical recurrence auxiliaries and app values
                    # omitted by ScheduleSegment.to_schedule_ref(), while the
                    # assigned window/day/target continue to win.
                    row = dict(saved, **row)
                row['_outlook_source_row'] = saved
                nets.append(row)
            else:
                daily.append(row)
        return daily, nets

    def _daily_schedule_rows(self):
        return self.daily

    def _net_schedule_rows(self):
        return self.nets


class OutlookWorker:
    def __init__(self, config_dir):
        self.reader = OutlookInputReader(config_dir)

    def load(self, context, *, force=False):
        import time
        started = time.monotonic()
        if context.get('config_root', str(self.reader.config_dir)) != str(self.reader.config_dir):
            raise ScheduleSourceError('The configuration profile changed; restart CommsPlan with the intended profile')
        if context.get('target_error'):
            raise ScheduleSourceError('The published radio context is unavailable; refresh the radio context and retry')
        inputs = self.reader.read(force=force, include_sop=context['card'])
        connection = None
        try:
            sop = None
            if context['card'] and 'nets:sop_profiles' in inputs.catalog.tables:
                tables = dict(inputs.catalog.tables)
                columns = dict(inputs.catalog.columns)
                tables.update({f'nets:{k}': v for k, v in inputs.sop_tables.items()})
                columns.update({f'nets:{k}': v for k, v in inputs.sop_columns.items()})
                copy_catalog = replace(inputs.catalog, tables=tables, columns=columns)
                connection = PlannerModelCompiler()._copy_database(copy_catalog, 'nets')
                for table, fields in (('sop_actions', 'profile_id'),
                                      ('sop_action_state', 'profile_id, action_id'),
                                      ('sop_schedule_layer', 'profile_id')):
                    connection.execute(f'CREATE INDEX _outlook_{table} ON {table} ({fields})')
                connection.execute('PRAGMA query_only=ON')
                sop = _ReadOnlySOP(connection, DetachedSettings(inputs.catalog.settings, inputs.catalog.config_dir))
            projector = _Projector(inputs, context, sop)
            now = context['now']
            tz = projector._get_display_tz()
            day_end = now.astimezone(tz).replace(hour=23, minute=59, second=59, microsecond=0)
            today_end = day_end.astimezone(dt.timezone.utc)
            tomorrow_start = (day_end + dt.timedelta(seconds=1)).astimezone(dt.timezone.utc)
            tomorrow_end = (day_end + dt.timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0).astimezone(dt.timezone.utc)
            today, tomorrow = [], []
            if context['card']:
                today_sop = sop.build_upcoming_actions(
                    horizon_hours=max(1, math.ceil((today_end-now).total_seconds()/3600)),
                    only_active=True, now_utc=now) if sop else []
                tomorrow_sop = projector._build_sop_actions_in_window(tomorrow_start, tomorrow_end) if sop else []
                today = projector._collect_schedule_rows(now, today_end, resolve_multiday=context['show_local'],
                                                         preloaded_sop_actions=today_sop)
                tomorrow = projector._collect_schedule_rows(tomorrow_start, tomorrow_end, include_day=True,
                                                            include_hf=False, preloaded_sop_actions=tomorrow_sop)
                tomorrow = [r for r in tomorrow if not isinstance(r.get('when_utc'), dt.datetime) or
                            tomorrow_start <= r['when_utc'] <= tomorrow_end]
            # Preserve propagation's existing local/UTC-day semantics separately.
            prop_tz = get_timezone(context['timezone'])
            propagation = projector._load_today_schedule_local(now.astimezone(prop_tz), prop_tz) if context['propagation'] else []
            if self.reader.revision() != inputs.revision:
                raise ConfigurationChanged('Outlook sources changed while projecting; retry')
            # Detect completion/source changes during potentially slow projection.
            if context['card'] and self.reader._status(inputs.catalog)[2] != inputs.status_identity:
                raise ConfigurationChanged('SOP state changed while projecting; retry')
            valid_until = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
            if context['card']:
                valid_until = min(valid_until, now + dt.timedelta(seconds=5))
            token = (context['generation'], context['key'], inputs.revision,
                     inputs.status_identity, int(now.timestamp()//60))
            today = tuple(dict(r, _outlook_generation=token) for r in today)
            tomorrow = tuple(dict(r, _outlook_generation=token) for r in tomorrow)
            return OutlookPublication(context['key'], inputs, today, tomorrow, tuple(propagation),
                projector.daily, projector.nets, projector.resolved_target, inputs.catalog.loaded_at,
                now, valid_until, (time.monotonic()-started)*1000, token)
        finally:
            if connection is not None:
                connection.close()

    def close(self):
        self.reader.close()
