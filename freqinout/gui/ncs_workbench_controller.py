"""Bounded worker boundary for the JS8 NCS working view.

Qt owns drafts and navigation. SQLite and guarded RF submission run on one lazy
worker; completion never navigates or replaces a newer net's working context.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMessageBox

from freqinout.core.ncs_workbench_store import NcsWorkbenchStore
from freqinout.core.ncs_workbench_model import NetReportScope, query_net_reports, query_net_roster, query_net_prior_work, query_net_summary
from freqinout.core.ncs_net_messages import (
    NetMessagesStore, claim_net_end, release_net_end, form_query_command,
    saved_expect_compose_intent, send_ncs_command, expand_net_message, resolve_ncs_js8_source,
)
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor


class NcsWorkbenchController(QObject):
    completed = Signal(object, object, object)
    ingest_completed = Signal(object)

    def __init__(self, tab, panel):
        super().__init__(tab)
        self.tab, self.panel = tab, panel
        self.path = Path(tab.settings.db_path).with_name('freqinout_nets.db')
        self.config_path = Path(tab.settings.db_path)
        self.store = NcsWorkbenchStore(self.path)
        self.messages = NetMessagesStore(self.config_path)
        self.legacy_message_controls = bool(getattr(panel, '_legacy_message_controls', True))
        self.executor = DaemonSerialExecutor(thread_name_prefix='fio-ncs-workbench')
        self.ingest_executor = DaemonSerialExecutor(thread_name_prefix='fio-ncs-api-ingest')
        registry = getattr(tab.window(), '_shutdown_registry', None)
        if registry is not None:
            registry.register(f'ncs-workbench:{id(self)}', request_stop=self.close,
                is_stopped=self.shutdown_complete)
        self.ingest_busy = False
        self.ingest_pending = []
        self.ingest_bytes = 0
        self.ingest_completed.connect(self._ingest_finished)
        self.generation = 0
        self.busy = False
        self.closed = False
        self.run = None
        self.pending_open_run = None
        self.snapshot = {}
        self.view = 'net'
        self.cursor = None
        self.cursor_stack = []
        self.roster_cursor = ''
        self.roster_stack = []
        self.roster_filters = dict(callsign='', group='', state='', region='')
        self.detail_station = ''
        self._detail_pending = False
        self.identity_store = getattr(getattr(tab, '_fcc_identity_broker', None), 'store', None)
        self.expect_cursor = 0
        self.expect_stack = []
        self._search_pending = False
        self.message_search = ''
        self.entries_cursor = ''
        self.entries_stack = []
        self.history_cursor = None
        self.history_stack = []
        self.anchor = None
        self.drafts = {}
        self.dirty_drafts = {}
        self.draft_error = False
        self.excluded = set()
        self.undo = None
        self.pending_label = None
        self.label_error = False
        self.observations = {}
        self._end_lease = None
        self._end_retry = None
        self.completed.connect(self._completed)
        panel.action_requested.connect(self.action)
        panel.refresh_requested.connect(self.refresh)
        panel.view_changed.connect(self.change_view)
        panel.draft_changed.connect(self.retain_message_editor)
        filters_signal = getattr(panel, 'roster_filters_changed', None)
        if filters_signal is not None:
            filters_signal.connect(self.filter_roster)
        search_signal = getattr(panel, 'message_search_requested', None)
        if search_signal is not None:
            search_signal.connect(self.search_messages)
        query_selector = getattr(tab, 'query_group_selector', None)
        if query_selector is not None:
            query_selector.currentTextChanged.connect(self.target_group_changed)
        label_finished = getattr(tab.net_name_edit, 'editingFinished', None)
        if label_finished is not None:
            label_finished.connect(self.rename)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.draft_timer = QTimer(self)
        self.draft_timer.setSingleShot(True)
        self.draft_timer.setInterval(400)
        self.draft_timer.timeout.connect(self.flush_drafts)
        panel.publish({'status': 'Start a net, or refresh to review retained net records.', 'writable': False})

    def search_messages(self, text):
        self.message_search = str(text).strip()[:512]
        self._search_pending = True
        if not self.busy:
            self._search_pending = False
            self.refresh(keep_anchor=True)

    def _current_group(self):
        callback = getattr(self.tab, '_query_group_destination', None)
        return str(callback() if callable(callback) else self.tab._group_target or '').strip().upper()

    def target_group_changed(self, *_args):
        self.snapshot.pop('preview', None)
        self.snapshot.pop('preview_identity', None)
        self.snapshot['outgoing_group'] = self._current_group()
        if hasattr(self.panel, '_snapshot'):
            self.panel._snapshot['outgoing_group'] = self._current_group()
        if getattr(self.panel, '_target_mode', '') == 'group':
            self.panel.set_group_target(self._current_group())
        if hasattr(self.panel, 'preview'):
            self.panel.preview.clear()

    def _message_target(self, data):
        mode = data.get('target_mode', 'manual')
        if mode == 'group':
            target = self._current_group()
        elif mode == 'station':
            row = data.get('target_station') or {}
            selected = self.panel._selected_roster() or {}
            target = str(row.get('from_call') or row.get('callsign') or '').strip().upper()
            if (not target or target != str(selected.get('from_call') or selected.get('callsign') or '').upper()
                    or row.get('_ncs_run_id') != (self.run or {}).get('run_id')
                    or row.get('_ncs_generation') != self.generation):
                raise ValueError('Select the intended station in this net again; nothing sent')
        else:
            target = str(data.get('target') or '').strip().upper()
        if not target or any(ch.isspace() for ch in target):
            raise ValueError('Choose a group or net station destination')
        return target

    def _status(self, text):
        self.snapshot['outgoing_group'] = self._current_group()
        library = getattr(self.panel, 'saved_library', None)
        if library is not None and library.editing is not None and ('Could not save' in text or 'Work is pending' in text):
            library.status.setText(text)
        self.snapshot['status'] = text
        self.snapshot['can_undo'] = bool(self.undo)
        self.panel.publish(dict(self.snapshot, writable=self.writable, has_previous=bool(self.cursor_stack)))

    @property
    def writable(self):
        return bool(self.run and self.run['state'] == 'active'
                    and self.run['run_id'] == self.tab._ncs_session_id
                    and self.tab._net_in_progress and not self._end_lease)

    def _submit(self, work, accept, *, status='Saving…', allow_stale=False):
        if self.closed:
            return False
        if self.busy:
            self._status('Work is pending; your draft is retained. Retry when it finishes.')
            return False
        self.busy = True
        token = (self.generation, accept, allow_stale)
        if status:
            self._status(status)
        future = self.executor.submit(work)
        def done(result):
            try:
                value, error = result.result(), None
            except Exception as exc:
                value, error = None, str(exc)
            if self.closed:
                self._release_end()
                return
            try:
                self.completed.emit(token, value, error)
            except RuntimeError:
                pass
        future.add_done_callback(done)
        return True

    def _completed(self, token, value, error):
        self.busy = False
        generation, accept, allow_stale = token
        if self.closed:
            self._release_end()
            return
        if generation != self.generation and not allow_stale:
            self._status('Earlier work completed; the current net view was retained.')
        elif error:
            if self._end_lease:
                self._end_retry = (self._end_lease, not self.tab._net_in_progress)
            if self.pending_label:
                self.label_error = True
            self._status(f'Could not save or verify: {error}. Work retained; retry explicitly.')
        else:
            try:
                accept(value)
            except Exception as exc:
                if self._end_lease:
                    self._end_retry = (self._end_lease, not self.tab._net_in_progress)
                self._status(f'Work retained; completion needs review: {exc}')
        if self.dirty_drafts and not error and not self.draft_error:
            self.draft_timer.start()
        elif error and self.dirty_drafts:
            self.draft_error = True
        if self.pending_open_run and not self.busy and not self.dirty_drafts and not error:
            target = self.pending_open_run
            self.pending_open_run = None
            self._load_run(target)
        if (self._search_pending or self._detail_pending) and not self.busy and not self.closed:
            self._search_pending = False
            self._detail_pending = False
            self.refresh(keep_anchor=True)


    def on_started(self):
        self.detail_station = ''
        self.generation += 1
        self.cursor, self.anchor, self.undo = None, None, None
        self.cursor_stack.clear()
        self.roster_cursor = ''
        self.roster_stack.clear()
        self.excluded.clear()
        self.observations.clear()
        if self.run is not None:
            self.retain_message_editor(self.panel._draft())
            self._initial_editor = {'kind': 'custom', 'target': self.tab._group_target, 'pending_notes': {}}
            self.panel.restore_message_draft(self._initial_editor)
        else:
            self._initial_editor = self.panel._draft()
        self.run = None
        self.ensure_run()

    def ensure_run(self):
        if self.run or not self.tab._net_in_progress or self.busy:
            return
        tab = self.tab
        scope = getattr(tab, '_preparation_scope', None)
        radio = int(tab._ncs_session_radio_id or 0)
        profile = tab.settings.get('_preparation_profile', {})
        instance = str(getattr(scope, 'instance_id', '') or profile.get('js8_instance_id') or radio).split(':')[-1]
        ident = tab._ncs_session_id
        label = tab.net_name_edit.text().strip()
        group = tab._group_target
        source = {'operator_call': tab._my_callsign(),
                  'radio_name': str(getattr(scope, 'radio_name', '') or profile.get('name') or f'Radio {radio}'),
                  'operating_group_key': str((getattr(getattr(tab, '_prepared_receiver', None), 'prepared', None).request.source.values.values().get('operating_group_key', '')) if getattr(getattr(tab, '_prepared_receiver', None), 'prepared', None) else '')}
        if scope is not None:
            source.update(host=tab.settings.get('js8_host', '127.0.0.1'),
                          port=int(tab.settings.get('js8_port', 2442) or 2442))
        stamp = getattr(tab, '_ncs_workbench_started_ts', None) or time.time()
        if not getattr(tab, '_ncs_workbench_started_ts', None) and tab._net_start_utc:
            import datetime
            stamp = datetime.datetime.fromisoformat(tab._net_start_utc).replace(tzinfo=datetime.timezone.utc).timestamp()
        def work():
            existing = self.store.get_run(ident)
            if not existing:
                pinned_source = dict(source)
                pinned_instance = instance
                if scope is None:
                    resolved = resolve_ncs_js8_source(self.config_path, radio)
                    pinned_instance = resolved.pop('instance_id')
                    pinned_source.update(resolved)
                self.store.create_run(protocol='JS8', radio_id=radio, instance_id=pinned_instance,
                                      run_id=ident, label=label, group_name=group,
                                      started_ts=stamp, source=pinned_source)
            return self.store.get_run(ident)
        def accepted(run):
            self.run = run
            initial = getattr(self, '_initial_editor', None)
            if initial:
                self.retain_message_editor(initial)
                self._initial_editor = None
            self._status('Net record saved. Listening; no message sent.')
            self.refresh()
        self._submit(work, accepted, status='Saving net record; message actions are pending…')

    def _scope(self, run):
        source = json.loads(run['source_json'])
        return NetReportScope(run['run_id'], run['radio_id'], run['instance_id'],
                              run['group_name'], str(source.get('operator_call', '')),
                              run['started_ts'], run.get('ended_ts') or 0)

    def _read(self, run, view, cursor, anchor, roster_cursor='', roster_previous=False, allowed_codes=None, entries_cursor='', history_cursor=None, expect_cursor=0, roster_filters=None, message_search='', detail_station=''):
        from freqinout.core.js8_expect_store import list_expect_entries
        saved = list_expect_entries(db_path=self.path, limit=201, net_forms_only=True,
            static_form_codes=allowed_codes, after_id=expect_cursor)
        result = {'run': run, 'runs': self.store.list_runs(), 'entries': self.messages.list(after=entries_cursor) if self.legacy_message_controls else [],
                  'saved_expect': saved[:200],
                  'expect_next_cursor': saved[199]['id'] if len(saved) > 200 else None}
        quick = self.messages.search(message_search, limit=201)
        result.update(quick_entries=quick[:200], message_search=message_search, message_search_more=len(quick) > 200)
        result['entries_next_cursor'] = result['entries'][-1]['entry_id'] if len(result['entries']) == 200 else None
        if not run:
            return result
        run = self.store.get_run(run['run_id'])
        if not run:
            raise ValueError('Net record was removed')
        result['run'] = run
        scope = self._scope(run)
        page = query_net_reports(self.path, scope, view=view, cursor=cursor, anchor_ts=anchor, from_call=detail_station)
        result['detail_station'] = detail_station
        # The report page owns a stable cursor; roster is independently bounded.
        filters = dict(roster_filters or {})
        facets = {}
        roster, roster_next, roster_total = query_net_roster(self.path, scope, view=view,
            after=roster_cursor, anchor_ts=page.anchor_ts, facets=facets, identity_store=self.identity_store,
            callsign_filter=filters.get('callsign', ''), group_filter=filters.get('group', ''), state_filter=filters.get('state', ''), region_filter=filters.get('region', ''))
        result.update(roster_groups=facets.get('groups', []), roster_states=facets.get('states', []),
                      roster_regions=facets.get('regions', []),
                      table_traffic_total=facets.get('traffic_total', page.total),
                      roster_unfiltered_total=facets.get('total', roster_total), roster_filters=filters)
        result.update(reports=list(page.rows), roster=list(roster), roster_next_cursor=roster_next,
                      roster_total=roster_total, roster_has_previous=roster_previous, next_cursor=page.next_cursor,
                      anchor_ts=page.anchor_ts, newer_available=page.newer_available,
                      total=page.total, history=self.store.submissions(run['run_id'], before=history_cursor))
        latest = result['history'][:1] if history_cursor is None else self.store.submissions(run['run_id'], limit=1)
        result['latest_submission'] = latest[0] if latest else None
        result['history_next_cursor'] = result['history'][-1]['sequence'] if len(result['history']) == 200 else None
        result['query_groups'] = self.store.group_action_history(run['run_id'])[0]
        result['summary'] = query_net_summary(self.path, scope, view=view, anchor_ts=page.anchor_ts)
        for key in ('unreviewed', 'follow_up_stations'):
            if key in facets:
                result['summary'][key] = facets[key]
        # Advisory receive cache is bounded to the loaded pages. SQL joins and
        # atomic participant counting remain the exclusion authority for all rows.
        result['excluded_calls'] = sorted({str(row.get('from_call') or '').upper()
            for row in (*result['roster'], *result['reports']) if row.get('station_excluded')})
        result['drafts'] = json.loads(run['draft_json']).get('compose_drafts', {})
        return result

    def refresh(self, *, keep_anchor=False):
        if not keep_anchor and self.label_error and self.pending_label and not self.busy:
            self.label_error = False
            self.flush_label()
            return
        if not keep_anchor and self._end_retry and not self.busy:
            ident, ended = self._end_retry
            self._end_retry = None
            self._finish_end(ident, ended)
            return
        if self._end_lease:
            self._status('End is pending; current work is retained.')
            return
        if not keep_anchor and self.draft_error and self.dirty_drafts and not self.busy:
            self.draft_error = False
            self.flush_drafts()
            return
        if self.busy:
            return
        if not keep_anchor:
            self.expect_cursor = 0
            self.expect_stack.clear()
            self.cursor = None
            self.cursor_stack.clear()
            self.roster_cursor = ''
            self.roster_stack.clear()
        run = copy.deepcopy(self.run)
        if self.tab._net_in_progress and (not run or run['run_id'] != self.tab._ncs_session_id):
            self.ensure_run()
            return
        view, cursor, anchor = self.view, self.cursor, self.anchor if keep_anchor else None
        roster_cursor, roster_previous = self.roster_cursor, bool(self.roster_stack)
        allowed = frozenset(self.tab._available_net_form_codes)
        entries_cursor, history_cursor = self.entries_cursor, self.history_cursor
        expect_cursor = self.expect_cursor
        filters = dict(self.roster_filters)
        message_search = self.message_search
        detail_station = self.detail_station
        self._submit(lambda: self._read(run, view, cursor, anchor, roster_cursor, roster_previous, allowed, entries_cursor, history_cursor, expect_cursor, filters, message_search, detail_station), self._publish,
                     status=None if keep_anchor else 'Checking for updates…')

    def _publish(self, snapshot):
        if snapshot.get('detail_station', self.detail_station) != self.detail_station:
            self.refresh(keep_anchor=True)
            return
        if snapshot.get('message_search', self.message_search) != self.message_search:
            self.refresh(keep_anchor=True)
            return
        snapshot['outgoing_group'] = self._current_group()
        if 'roster_filters' in snapshot and snapshot['roster_filters'] != self.roster_filters:
            self.refresh(keep_anchor=True)
            return
        snapshot['can_undo'] = bool(self.undo)
        for field in ('roster', 'reports'):
            snapshot[field] = [dict(row, _ncs_run_id=(snapshot.get('run') or {}).get('run_id'),
                                    _ncs_generation=self.generation) for row in snapshot.get(field, ())]
        snapshot['expect_has_previous'] = bool(self.expect_stack)
        snapshot['entries_has_previous'] = bool(self.entries_stack)
        snapshot['history_has_previous'] = bool(self.history_stack)
        self.run = snapshot.get('run')
        self.excluded = set(snapshot.get('excluded_calls', ()))
        ident = self.run['run_id'] if self.run else ''
        for key, value in snapshot.get('drafts', {}).items():
            if (ident, key) not in self.dirty_drafts:
                cache_key = ident + '|' + key if key == '__net_message_editor__' else key
                self.drafts[cache_key] = value
        self.anchor = snapshot.get('anchor_ts')
        snapshot['message_draft'] = self.drafts.get(ident + '|__net_message_editor__', {})
        source_name = str(json.loads(self.run['source_json']).get('radio_name') or f"Radio {self.run['radio_id']}") if self.run else ''
        status = source_name + ' · Listening · NCS decides what to keep.'
        latest = snapshot.get('latest_submission')
        if latest is None and self.history_cursor is None:
            latest = next(iter(snapshot.get('history', ())), None)
        if self.writable and latest:
            from freqinout.core.ncs_activity_presentation import submission_outcome_label
            status = source_name + ' · Listening · Last send: ' + submission_outcome_label(latest)
            if latest.get('outcome') != 'accepted' and latest.get('detail'):
                status += ': ' + str(latest['detail'])
        self.snapshot = dict(snapshot, status=status if self.writable else
                             'Retained net record · no listening or automatic transmission.')
        for row in snapshot.get('roster', ()):
            call = str(row.get('from_call') or '').upper()
            if self.writable and self.view == 'net' and call and call not in self.excluded:
                # Canonical CommStat/Spotter reports also establish participation.
                self.tab._checkins.setdefault(call, {'status': row.get('kind_label', 'Traffic')})
        self.panel.publish(dict(self.snapshot, writable=self.writable, has_previous=bool(self.cursor_stack)))

    def poll(self):
        if self.closed or self.busy or self._end_lease or (not self.tab.isVisible() and not self.tab._net_in_progress):
            return
        if self.tab._net_in_progress and (not self.run or self.run['run_id'] != self.tab._ncs_session_id):
            self.ensure_run()
            return
        if self.dirty_drafts:
            if not self.draft_error:
                self.flush_drafts()
            return
        if self.pending_label:
            self.flush_label()
            return
        if self.run and self.writable:
            changed = {call: copy.deepcopy(data) for call, data in self.tab._checkins.items()
                       if self.observations.get(call) != data}
            if changed:
                batch = dict(list(changed.items())[:200])
                run_id = self.run['run_id']
                def accept(_):
                    self.observations.update(batch)
                    self.refresh(keep_anchor=True)
                self._submit(lambda: self.store.observe_stations(run_id, batch), accept, status='Retaining station observations…')
                return
        if self.run:
            self._follow_live_head()
            self.refresh(keep_anchor=True)

    def _follow_live_head(self):
        # Release only the newest unselected page. Older pages and the report
        # being read retain their watermark and signal newer arrivals instead.
        station = getattr(self.panel, '_selected_roster', lambda: None)()
        report = getattr(self.panel, '_selected_report', lambda: None)()
        if (self.writable and self.view == 'net' and self.cursor is None
                and not self.cursor_stack and not self.roster_cursor and not self.roster_stack
                and not station and not report):
            self.anchor = None

    def filter_roster(self, filters):
        self.roster_filters = {key: str(filters.get(key) or '').strip() for key in ('callsign', 'group', 'state', 'region')}
        self.detail_station = ''
        self.cursor = None
        self.cursor_stack.clear()
        self.roster_cursor = ''
        self.roster_stack.clear()
        self.refresh(keep_anchor=True)

    def change_view(self, view):
        if view not in {'net', 'other', 'excluded'}:
            return
        self.view = view
        self.detail_station = ''
        self.generation += 1
        self.cursor = self.anchor = None
        self.cursor_stack.clear()
        self.roster_cursor = ''
        self.roster_stack.clear()
        self.refresh()

    def open_run(self, ident):
        if self.tab._net_in_progress and ident != self.tab._ncs_session_id:
            self._status('End the current net before opening a retained net record.')
            return
        if self.run and ident != self.run['run_id']:
            self.panel._draft_timer.stop()
            self.retain_message_editor(self.panel._draft())
            self.pending_open_run = str(ident)
            self.flush_drafts()
            return
        self._load_run(ident)

    def _load_run(self, ident):
        self.detail_station = ''
        self.generation += 1
        def accept(run):
            self.run = run
            self.undo = None
            self.excluded.clear()
            self.cursor = self.anchor = None
            self.cursor_stack.clear()
            self.roster_cursor = ''
            self.roster_stack.clear()
            self.history_cursor = None
            self.history_stack.clear()
            self.refresh()
        self._submit(lambda: self.store.get_run(str(ident)), accept, status='Opening retained net record…')

    def annotate(self, kind, row, changes):
        if not self.run:
            return
        if row.get('_ncs_run_id', self.run['run_id']) != self.run['run_id'] or row.get('_ncs_generation', self.generation) != self.generation:
            self._status('The net view changed; refresh before applying this decision. Your work is retained.')
            return
        if kind == 'report' and (not row.get('canonical_key') or not row.get('content_hash')):
            self._status('Select an identified report before changing its net-local review state.')
            return
        run = copy.deepcopy(self.run)
        entity = str(row.get('from_call') or row.get('callsign') or '').upper() if kind == 'station' else str(row['canonical_key'])
        evidence = 'station' if kind == 'station' else str(row['content_hash'])
        expected = int(row.get('station_version' if kind == 'station' else 'annotation_version') or 0)
        def work():
            old = self.store.annotation(run['run_id'], kind, entity, evidence_version=evidence)
            # Match the displayed version; never silently merge another window's write.
            if (old or {}).get('version', 0) != expected:
                raise ValueError('This decision changed; refresh before retrying')
            before = dict((old or {}).get('values', {}))
            values = dict(before, **changes)
            version = self.store.annotate(run['run_id'], kind, entity, evidence_version=evidence,
                expected_version=expected, values=values, verify_projection=kind == 'report',
                expected_run_version=run['version'])
            return dict(run_id=run['run_id'], kind=kind, entity=entity, evidence=evidence, version=version, before=before)
        def accept(undo):
            self.undo = undo
            if kind == 'report' and 'note' in changes and hasattr(self.panel, 'mark_note_saved'):
                self.panel.mark_note_saved(row, str(changes['note']))
            self.refresh(keep_anchor=True)
        self._submit(work, accept)

    def restore_last(self):
        undo = copy.deepcopy(self.undo)
        if not undo or not self.run:
            return
        run_id = self.run['run_id']
        if undo.get('run_id') != run_id:
            self.undo = None
            self._status('Undo belongs to an earlier net. Current decisions were retained.')
            return
        def work():
            self.store.annotate(run_id, undo['kind'], undo['entity'], evidence_version=undo['evidence'],
                                expected_version=undo['version'], values=undo['before'])
        def accept(_):
            self.undo = None
            self.refresh(keep_anchor=True)
        self._submit(work, accept)

    def rename(self):
        if not self.writable:
            return
        label = self.tab.net_name_edit.text().strip()
        if label == self.run['label']:
            return
        if not label:
            self._status('Net label is required; the saved label has been retained.')
            return
        self.label_error = False
        self.pending_label = (self.run['run_id'], label)
        self.flush_label()

    def flush_label(self):
        if self.busy or self.label_error or not self.pending_label or not self.run:
            return
        ident, label = self.pending_label
        if ident != self.run['run_id']:
            return
        run = copy.deepcopy(self.run)
        def work():
            self.store.update_run(ident, expected_version=run['version'], label=label,
                                  draft=json.loads(run['draft_json']))
            return self.store.get_run(ident)
        def accept(updated):
            if self.run and self.run['run_id'] == ident:
                self.run = updated
            if self.pending_label == (ident, label):
                self.pending_label = None
            self.refresh(keep_anchor=True)
        self._submit(work, accept, status='Saving net label…')

    def retain_message_editor(self, draft):
        if self.run:
            self.retain_compose_draft({'run_id': self.run['run_id'], 'traffic_db_path': str(self.path)},
                                      '__net_message_editor__', draft)

    def capture_context(self):
        return dict(run=copy.deepcopy(self.run), view=self.view, pending_label=self.pending_label, label_error=self.label_error,
                    message_draft=self.panel._draft(), note=self.panel.note_edit.text(),
                    excluded=sorted(self.excluded), drafts=copy.deepcopy(self.drafts),
                    report_key=(self.panel._selected_report() or {}).get('canonical_key'),
                    station=(self.panel._selected_roster() or {}).get('from_call'))

    def restore_context(self, context):
        self.detail_station = ''
        self.generation += 1
        self.run = copy.deepcopy(context.get('run'))
        self.undo = None
        self.view = context.get('view', 'net')
        self.pending_label = tuple(context['pending_label']) if context.get('pending_label') else None
        self.label_error = bool(context.get('label_error'))
        self.cursor = self.anchor = None
        self.cursor_stack.clear()
        self.roster_cursor = ''
        self.roster_stack.clear()
        self.excluded = set(context.get('excluded', ()))
        self.drafts.update(copy.deepcopy(context.get('drafts', {})))
        self.panel.restore_message_draft(context.get('message_draft', {}))
        self.panel.note_edit.setText(str(context.get('note', '')))
        self.snapshot = dict(run=self.run, status='Retained working context · refresh to load reports.', writable=False)
        self.panel.publish(self.snapshot)

    def scope_changed(self):
        self.detail_station = ''
        self._detail_pending = False
        self.generation += 1
        self.pending_open_run = None
        self.pending_label = None
        self.label_error = False
        self.undo = None
        self.run = None
        self.excluded.clear()
        self.panel.restore_message_draft({})
        self.snapshot = dict(status='Prepared source · Start to establish a new net record.', writable=False)
        self.panel.publish(self.snapshot)

    def retain_compose_draft(self, context, key, snapshot):
        ident = str(context.get('run_id') or '')
        if not ident or str(context.get('traffic_db_path')) != str(self.path):
            raise ValueError('Compose draft belongs to another NCS profile')
        self.draft_error = False
        cache_key = ident + '|' + str(key) if key == '__net_message_editor__' else str(key)
        self.drafts[cache_key] = copy.deepcopy(snapshot)
        self.dirty_drafts[(ident, str(key))] = copy.deepcopy(snapshot)
        self.draft_timer.start()

    def clear_compose_draft(self, key):
        if not self.run:
            return
        self.draft_error = False
        cache_key = self.run['run_id'] + '|' + str(key) if key == '__net_message_editor__' else str(key)
        self.drafts.pop(cache_key, None)
        self.dirty_drafts[(self.run['run_id'], str(key))] = None
        self.draft_timer.start()

    def flush_drafts(self):
        if self.busy or self._end_lease or not self.dirty_drafts:
            return
        pending = copy.deepcopy(self.dirty_drafts)
        def accept(runs):
            for key, value in pending.items():
                if self.dirty_drafts.get(key) == value:
                    self.dirty_drafts.pop(key, None)
            if self.run and self.run['run_id'] in runs:
                self.run = runs[self.run['run_id']]
            self._status('Working draft saved; no message sent.')
        self._submit(lambda: self._write_drafts(pending), accept, status='Saving working draft…', allow_stale=True)

    def _write_drafts(self, pending):
        grouped = {}
        for (ident, key), value in pending.items():
            grouped.setdefault(ident, {})[key] = value
        result = {}
        for ident, values in grouped.items():
            run = self.store.get_run(ident)
            if not run:
                raise ValueError('Net draft record is unavailable')
            draft = json.loads(run['draft_json'])
            bank = draft.setdefault('compose_drafts', {})
            for key, value in values.items():
                if value is None:
                    bank.pop(key, None)
                else:
                    bank[key] = value
            self.store.update_run(ident, expected_version=run['version'], label=run['label'], draft=draft)
            result[ident] = self.store.get_run(ident)
        return result

    def context(self):
        if not self.writable:
            raise ValueError('Start this net before sending; retained work remains available')
        source = json.loads(self.run['source_json'])
        return dict(run_id=self.run['run_id'], traffic_db_path=str(self.path),
                    radio_id=self.run['radio_id'], instance_id=self.run['instance_id'],
                    host=source['host'], port=source['port'], label=self.run['label'])

    def send(self, text, *, kind='custom', destination='', on_sent=None, ack_calls=None, target_check=None):
        if not self.writable:
            self._status('Start this net before sending; your draft is retained.')
            return
        run_id, generation = self.run['run_id'], self.generation
        source_json = self.run['source_json']
        source = json.loads(source_json)
        source_name = str(source.get('radio_name') or f"Radio {self.run['radio_id']}")
        if QMessageBox.question(self.tab, 'Send Net Message', f"Send for {self.run['label']} through {source_name} · JS8Call?\n\n{text}",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        # A modal dialog runs a nested event loop. Its confirmation grants no
        # authority to a replacement run/source that appeared while it was open.
        if (not self.writable or self.run['run_id'] != run_id or self.generation != generation
                or self.run['source_json'] != source_json):
            self._status('Net context changed during confirmation; nothing was submitted. Review and send again explicitly.')
            return
        if target_check is not None and not target_check():
            self._status('Destination changed during confirmation; review and send again explicitly.')
            return
        manager = getattr(self.tab.window(), 'station_runtime_manager', None)
        action = dict(kind=kind, destination=destination, content=text)
        if ack_calls is not None:
            action['ack_calls'] = list(ack_calls)
        self._submit(lambda: send_ncs_command(self.store, run_id=run_id, intent_id=uuid.uuid4().hex,
                                            action=action, runtime_manager=manager),
                     lambda result: self._sent(result, on_sent=on_sent, action=action), status='Submitting once; waiting for JS8Call…')

    def _sent(self, result, *, on_sent=None, action=None):
        if result.outcome == 'accepted' and action and action.get('kind') == 'query':
            target = str(action.get('destination') or '')
            if target.startswith('@'):
                self.snapshot['query_groups'] = tuple(dict.fromkeys((target, *self.snapshot.get('query_groups', ()))))
        if on_sent is not None and result.outcome == 'accepted':
            on_sent()
        self.refresh(keep_anchor=True)
        from freqinout.core.ncs_activity_presentation import submission_outcome_label
        self._status(submission_outcome_label({'outcome': result.outcome, 'action': action or {}}) + (': ' + result.detail if result.outcome != 'accepted' and result.detail else ''))

    def acknowledge(self, selected=None, *, destination=None, expected_run=None):
        if not self.writable:
            self._status('Start this net before acknowledging traffic.')
            return
        if expected_run is not None and self.run['run_id'] != expected_run:
            self._status('Net changed; choose the acknowledgment group again.')
            return
        stations = copy.deepcopy(self.tab._checkins)
        run = copy.deepcopy(self.run)
        target = str(destination or '').strip().upper()
        if not selected and not target:
            choices = self.snapshot.get('query_groups') or ()
            target = choices[0] if choices else ''
        if not selected and not target:
            self._status('Submit an Expect group query before acknowledging a group.')
            return
        def eligible():
            groups, acknowledged = self.store.group_action_history(run['run_id'])
            if not selected and target not in groups:
                raise ValueError('This group has no accepted Expect query in this net; nothing sent')
            requested = {selected} if selected else None
            items = list(stations.items())
            for start in range(0, len(items), 200):
                self.store.observe_stations(run['run_id'], dict(items[start:start+200]))
            after, calls, anchor = '', [], time.time()
            while True:
                rows, after, _ = query_net_roster(self.path, self._scope(run), after=after, anchor_ts=anchor)
                calls.extend(row['from_call'] for row in rows if
                    (row['from_call'] in requested if requested is not None else row['from_call'] not in acknowledged.get(target, set())))
                if after is None:
                    return tuple(calls)
        def confirm(calls):
            if not self.writable or self.run['run_id'] != run['run_id']:
                self._status('Net context changed; no acknowledgment was sent.')
                return
            if not calls:
                self._status('No eligible check-ins selected; excluded traffic was retained.')
                return
            ack_target = calls[0] if selected else target
            codes = calls[0] if selected else self.tab._build_short_code_summary(list(dict.fromkeys(call.split('/', 1)[0] for call in calls)))
            def accepted():
                if self.writable and self.run['run_id'] == run['run_id']:
                    for call in calls:
                        if call in self.tab._checkins:
                            self.tab._checkins[call]['status'] = 'ACKED'
                            self.tab._update_row(call, self.tab._checkins[call])
            self.send(f'{self.tab._my_callsign()}: {ack_target} ACK {codes}', kind='ack',
                      destination=ack_target, on_sent=accepted, ack_calls=calls)
        self._submit(eligible, confirm, status='Checking net-local acknowledgment targets…')

    def query(self, destination):
        code = self.tab._spotter_form
        if code not in self.tab._available_net_form_codes:
            self._status('Review form tools and select an available form. Your choice is retained.')
            return
        try:
            command = form_query_command(self.tab._my_callsign(), destination, code)
        except ValueError as exc:
            self._status(str(exc))
            return
        self.send(command, kind='query', destination=destination)

    def _save_participants_worker(self, run, stations, already_counted):
        items = list(stations.items())
        for start in range(0, len(items), 200):
            self.store.observe_stations(run['run_id'], dict(items[start:start+200]))
        # Independently page the complete net roster; the visible 200-row page
        # must never determine Save or End membership.
        scope = self._scope(run)
        after, calls, anchor = '', set(), time.time()
        while True:
            rows, after, _ = query_net_roster(self.path, scope, after=after, anchor_ts=anchor)
            calls.update(row['from_call'] for row in rows)
            if after is None:
                break
        counted = self.store.count_participants(run['run_id'], calls, already_counted=already_counted)
        return dict(calls=tuple(sorted(calls)), counted=counted)

    def _accept_participants(self, result):
        for call in result['calls']:
            self.tab._checkins.setdefault(call, {'status': 'Received traffic'})
        self.tab._checkins_saved.update(result['counted'])

    def save_participants(self):
        if not self.writable or self.busy:
            self._status('Start this net and wait for pending work before Save Checkins.')
            return
        run = copy.deepcopy(self.run)
        stations = copy.deepcopy(self.tab._checkins)
        counted = tuple(self.tab._checkins_saved)
        def accept(result):
            if self.tab._ncs_session_id == run['run_id']:
                self._accept_participants(result)
                self._status('Check-ins recorded once for this net.')
        self._submit(lambda: self._save_participants_worker(run, stations, counted), accept,
                     status='Recording check-ins…', allow_stale=True)

    def request_end(self, finish):
        if self._end_retry:
            ident, ended = self._end_retry
            self._end_retry = None
            self._finish_end(ident, ended)
            return
        if not self.run or not self.writable or self.busy or self.dirty_drafts or self.pending_label:
            self._status('Wait for the net record and drafts to finish saving, then retry End.')
            return
        run = copy.deepcopy(self.run)
        ident = run['run_id']
        if not claim_net_end(self.path, ident):
            self._status('A message or End is pending; wait for its outcome before ending the net.')
            return
        self._end_lease = ident
        draft = json.loads(run['draft_json'])
        draft['end_pending'] = True
        stations = copy.deepcopy(self.tab._checkins)
        counted = tuple(self.tab._checkins_saved)
        def work():
            participants = self._save_participants_worker(run, stations, counted)
            self.store.update_run(ident, expected_version=run['version'], label=run['label'], draft=draft)
            return self.store.get_run(ident), participants
        def ready(value):
            updated, participants = value
            if self.tab._ncs_session_id != ident:
                self._finish_end(ident, False)
                return
            self.run = updated
            self._accept_participants(participants)
            self.tab._end_participant_calls = participants['calls']
            try:
                finish()
            finally:
                del self.tab._end_participant_calls
                ended = not self.tab._net_in_progress
                self._finish_end(ident, ended)
        if not self._submit(work, ready, status='Retaining net work before End…', allow_stale=True):
            self._release_end()

    def _finish_end(self, ident, ended):
        def work():
            run = self.store.get_run(ident)
            draft = json.loads(run['draft_json'])
            draft.pop('end_pending', None)
            self.store.update_run(ident, expected_version=run['version'], label=run['label'], draft=draft, end=ended)
            return self.store.get_run(ident)
        def accept(run):
            if self.run and self.run['run_id'] == ident:
                self.run = run
            self._release_end()
            self.refresh()
        if not self._submit(work, accept, status='Saving ended net record…' if ended else 'Keeping net active…', allow_stale=True):
            self._end_retry = (ident, ended)

    def _release_end(self):
        if self._end_lease:
            release_net_end(self.path, self._end_lease)
            self._end_lease = None

    def action(self, action, payload):
        if action == 'open_run':
            self.open_run(str(payload or ''))
            return
        data = dict(payload) if isinstance(payload, dict) else {}
        row = dict(data.get('row') or data.get('report') or data)
        if ('_ncs_run_id' in row or '_ncs_generation' in row) and (
                row.get('_ncs_run_id') != (self.run or {}).get('run_id')
                or row.get('_ncs_generation') != self.generation):
            self._status('The net view changed; select the intended station or report again.')
            return
        if action in {'view_station', 'close_detail'}:
            self.detail_station = str(row.get('from_call') or row.get('callsign') or '').strip().upper() if action == 'view_station' else ''
            self.cursor = None
            self.cursor_stack.clear()
            self._detail_pending = self.busy
            self.refresh(keep_anchor=True)
            return
        aliases = {'exclude_station':'station_exclude', 'restore_station':'station_restore',
                   'review':'report_review', 'follow_up':'report_followup', 'save_note':'report_note'}
        action = aliases.get(action, action)
        if action == 'report_exclude_restore':
            action = 'report_restore' if row.get('report_excluded') else 'report_exclude'
        entry_actions = {'save_entry':'save', 'delete_entry':'delete', 'preview_entry':'preview', 'send_entry':'send'}
        if action == 'save_library_entry':
            submitted = copy.deepcopy(data)
            def save_library():
                ident = self.messages.save(name=str(submitted.get('name') or ''),
                    kind=str(submitted.get('kind') or 'custom'), definition=submitted.get('definition') or {},
                    entry_id=submitted.get('entry_id'), expected_version=int(submitted.get('version') or 0))
                return self.messages.get(ident)
            def saved_library(row):
                self.panel.saved_library.saved(row, submitted)
                self.refresh(keep_anchor=True)
                self._status('Saved message. Outgoing draft retained.')
            self._submit(save_library, saved_library, status='Saving reusable message…')
            return
        if action == 'use_group_target':
            self.panel.set_group_target(self._current_group())
            return
        if action == 'utility_query':
            command = str(data.get('command') or '')
            if command not in {'SNR?', 'QUERY MSGS'}:
                return
            try:
                target = self._message_target(data)
                self.send(f'{self.tab._my_callsign()}: {target} {command}', kind='utility', destination=target,
                          target_check=lambda: self._target_unchanged(data, target))
            except ValueError as exc:
                self._status(str(exc))
            return
        if action in entry_actions:
            self.message_action(entry_actions[action], dict(data, entry=data if data.get('entry_id') else {}))
            return
        if action in {'station_exclude', 'station_restore'}:
            self.annotate('station', row, {'excluded': action.endswith('exclude')})
        elif action in {'report_exclude', 'report_restore'}:
            self.annotate('report', row, {'excluded': action.endswith('exclude')})
        elif action == 'report_review':
            self.annotate('report', row, {'reviewed': not bool(row.get('reviewed'))})
        elif action == 'report_followup':
            self.annotate('report', row, {'follow_up': not bool(row.get('follow_up'))})
        elif action == 'report_note':
            self.annotate('report', row, {'note': str(data.get('note', ''))})
        elif action == 'report_include':
            self.annotate('report', row, {'association': 'net'})
        elif action in {'undo', 'restore_last'}:
            self.restore_last()
        elif action == 'open_run':
            self.open_run(data.get('run_id', ''))
        elif action == 'next_page' and self.snapshot.get('next_cursor'):
            self.cursor_stack.append(self.cursor)
            self.cursor = self.snapshot['next_cursor']
            self.refresh(keep_anchor=True)
        elif action == 'previous_page' and self.cursor_stack:
            self.cursor = self.cursor_stack.pop()
            self.refresh(keep_anchor=True)
        elif action == 'roster_next_page' and self.snapshot.get('roster_next_cursor'):
            self.roster_stack.append(self.roster_cursor)
            self.roster_cursor = self.snapshot['roster_next_cursor']
            self.refresh(keep_anchor=True)
        elif action == 'roster_previous_page' and self.roster_stack:
            self.roster_cursor = self.roster_stack.pop()
            self.refresh(keep_anchor=True)
        elif action == 'expect_next_page' and self.snapshot.get('expect_next_cursor'):
            self.expect_stack.append(self.expect_cursor)
            self.expect_cursor = self.snapshot['expect_next_cursor']
            self.refresh(keep_anchor=True)
        elif action == 'expect_previous_page' and self.expect_stack:
            self.expect_cursor = self.expect_stack.pop()
            self.refresh(keep_anchor=True)
        elif action == 'entries_next_page' and self.snapshot.get('entries_next_cursor'):
            self.entries_stack.append(self.entries_cursor)
            self.entries_cursor = self.snapshot['entries_next_cursor']
            self.refresh(keep_anchor=True)
        elif action == 'entries_previous_page' and self.entries_stack:
            self.entries_cursor = self.entries_stack.pop()
            self.refresh(keep_anchor=True)
        elif action == 'history_next_page' and self.snapshot.get('history_next_cursor'):
            self.history_stack.append(self.history_cursor)
            self.history_cursor = self.snapshot['history_next_cursor']
            self.refresh(keep_anchor=True)
        elif action == 'history_previous_page' and self.history_stack:
            self.history_cursor = self.history_stack.pop()
            self.refresh(keep_anchor=True)
        elif action == 'prior_work':
            self.open_prior_work(row)
        elif action == 'query_group':
            destination = getattr(self.tab, '_query_group_destination', None)
            self.query(destination() if callable(destination) else self.tab._group_target)
        elif action == 'query_station':
            self.query(str(row.get('from_call') or row.get('callsign') or ''))
        elif action == 'open_report':
            if not row.get('message_id'):
                self._status('Select an identified report before opening its source.')
                return
            host = self.tab.window()
            if hasattr(host, 'open_messages_section'):
                context = dict(run_id=self.run['run_id'], traffic_db_path=str(self.path), label=self.run['label'], message_id=str(row['message_id'])) if self.run else {}
                host.open_messages_section('inbox', traffic_message_ids=[str(row['message_id'])], ncs_reader_context=context)
        elif action == 'clear_form_draft':
            key = str(data.get('draft_key') or '')
            if key and self.run and QMessageBox.question(self.tab, 'Clear form working copy',
                    'Clear this retained form draft? The saved Expect form and other drafts are retained.',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
                callback = getattr(self.tab.window(), 'clear_ncs_compose_draft', None)
                if callable(callback):
                    callback(dict(run_id=self.run['run_id'], traffic_db_path=str(self.path)), key)
                else:
                    self.clear_compose_draft(key)
        elif action == 'clear_draft':
            if data.get('draft_key') == '__net_message_editor__':
                self.retain_message_editor(self.panel._draft())
            elif data.get('draft_key'):
                self.clear_compose_draft(str(data['draft_key']))
            else:
                self._clear_current_compose_draft()
        elif action.startswith('message_'):
            self.message_action(action[8:], data)

    def open_prior_work(self, row=None):
        if not self.run:
            self._status('Select a retained or active net to review earlier work.')
            return
        from freqinout.gui.ncs_prior_work_dialog import NcsPriorWorkDialog
        previous = getattr(self, '_prior_dialog', None)
        if previous is not None:
            previous.close()
        self._prior_dialog = NcsPriorWorkDialog(self.tab)
        self._prior_run_id = self.run['run_id']
        self._prior_call = str((row or {}).get('from_call') or '')
        self._prior_after, self._prior_stack = ('', ''), []
        self._prior_dialog.action_requested.connect(self.prior_action)
        self._prior_dialog.show()
        self.load_prior_work()

    def load_prior_work(self):
        ident, call, after = self._prior_run_id, self._prior_call, self._prior_after
        dialog, has_previous = self._prior_dialog, bool(self._prior_stack)
        def accept(result):
            rows, cursor = result
            if dialog.isVisible():
                dialog.publish(rows, cursor, has_previous,
                    'NCS notes and decisions · ' + (call or 'All stations'))
        self._submit(lambda: query_net_prior_work(self.path, ident, from_call=call, after=after),
                     accept, status='Loading earlier net work…')

    def prior_action(self, action, row):
        dialog = self._prior_dialog
        if not self.run or self.run['run_id'] != self._prior_run_id:
            dialog.status_label.setText('Net context changed; reopen earlier work from the intended net.')
            return
        if isinstance(row, dict) and row.get('run_id', self._prior_run_id) != self._prior_run_id:
            dialog.status_label.setText('This earlier-work action belongs to another net; reopen its record.')
            return
        if action == 'next_page' and row:
            self._prior_stack.append(self._prior_after)
            self._prior_after = tuple(row)
            self.load_prior_work()
        elif action == 'previous_page' and self._prior_stack:
            self._prior_after = self._prior_stack.pop()
            self.load_prior_work()
        elif action == 'open_source' and row and row.get('message_id'):
            host = self.tab.window()
            if hasattr(host, 'open_messages_section'):
                dialog.close()
                host.open_messages_section('inbox', traffic_message_ids=[str(row['message_id'])],
                    ncs_reader_context=dict(run_id=self.run['run_id'], traffic_db_path=str(self.path), message_id=str(row['message_id'])))
        elif action in {'resolve_followup', 'review'} and row:
            values = dict(row['values'])
            values['follow_up' if action == 'resolve_followup' else 'reviewed'] = action == 'review'
            ident, run_version = self._prior_run_id, self.run['version']
            def work():
                self.store.annotate(ident, 'report', row['entity_id'], evidence_version=row['evidence_version'],
                    expected_version=row['version'], values=values,
                    expected_run_version=run_version,
                    verify_projection=bool(row.get('current_content_hash') == row['evidence_version']))
            self._submit(work, lambda _: self.load_prior_work(), status='Saving earlier-work decision…')

    def _target_unchanged(self, data, target):
        try:
            return self._message_target(data) == target
        except ValueError:
            return False

    def message_action(self, action, data):
        entry = dict(data.get('entry') or {})
        definition = dict(data.get('definition') or entry.get('definition') or {})
        target = str(data.get('target') or self._current_group()).strip().upper()
        kind = str(data.get('kind') or entry.get('kind') or 'custom')
        if action == 'save':
            editor_generation = getattr(self.panel, '_editor_generation', 0)
            def save():
                ident = self.messages.save(name=str(data.get('name', '')), kind=kind,
                    definition=definition, entry_id=entry.get('entry_id'), expected_version=int(entry.get('version') or 0))
                return self.messages.get(ident)
            def accepted(saved):
                current = self.panel._draft()
                if current.get('entry_id') == data.get('entry_id') and getattr(self.panel, '_editor_generation', 0) == editor_generation:
                    # Publish the saved identity/version without replacing edits
                    # typed while the worker was committing the previous snapshot.
                    self.panel._editing = dict(saved)
                self.refresh()
            self._submit(save, accepted, status='Saving Net Message…')
        elif action == 'delete' and entry:
            if QMessageBox.question(self.tab, 'Delete Net Message', 'Delete this reusable Net Message? Sent history is retained.',
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
                self._submit(lambda: self.messages.delete(entry['entry_id'], expected_version=entry['version']),
                             lambda _: self.refresh())
        elif action in {'preview', 'send'}:
            try:
                target = self._message_target(data)
                target_group = self._current_group()
                if kind == 'saved_expect':
                    from freqinout.core.js8_expect_store import list_expect_entries
                    context = {**self.context(), 'entry_id': entry.get('entry_id') or f"expect:{definition.get('expect_entry_id')}"}
                    allowed = frozenset(self.tab._available_net_form_codes)
                    radio_id = self.run['radio_id']
                    def read_saved():
                        matches = list_expect_entries(db_path=self.path, net_forms_only=True,
                            static_form_codes=allowed, entry_id=int(definition.get('expect_entry_id') or 0), limit=1)
                        if not matches:
                            raise ValueError('Saved Expect entry is unavailable; the draft is retained')
                        return matches[0]
                    def preview_saved(saved):
                        if (not self.writable or self.run['run_id'] != context['run_id']
                                or saved['expect_key'] not in self.tab._available_net_form_codes
                                or not self._target_unchanged(data, target) or self._current_group() != target_group):
                            self._status('Net or form availability changed; the draft is retained. Review before reopening Compose.')
                            return
                        intent = saved_expect_compose_intent(saved, destination=target,
                            radio_id=radio_id, context=context)
                        self.tab.window().open_messages_section('compose', compose_intent=intent)
                    self._submit(read_saved, preview_saved, status='Loading saved form working copy…')
                    return
                if kind == 'query':
                    from freqinout.core.js8_spotter_forms import normalize_form_code
                    if normalize_form_code(definition.get('form_code')) not in self.tab._available_net_form_codes:
                        raise ValueError('Select a form enabled for nets; review form tools if it changed')
                    text = form_query_command(self.tab._my_callsign(), target, definition.get('form_code', ''))
                else:
                    body = expand_net_message(str(data.get('text') or definition.get('text') or ''),
                                              target_group=target_group)
                    if not target or not body:
                        raise ValueError('Enter destination and message text')
                    text = f'{self.tab._my_callsign()}: {target} {body}'
                if action == 'send':
                    self.send(text, kind=kind, destination=target,
                        target_check=lambda: self._target_unchanged(data, target) and self._current_group() == target_group)
                else:
                    self.snapshot['preview'] = text
                    self.snapshot['preview_identity'] = self.panel.preview_identity(data)
                    self._status('Preview ready · review the destination and message before Send.')
            except (ValueError, AttributeError) as exc:
                self._status(str(exc))
        elif action == 'clear_draft':
            self.clear_compose_draft(str(data.get('draft_key', '')))

    def _clear_current_compose_draft(self):
        host = self.tab.window()
        callback = getattr(host, 'clear_ncs_compose_draft', None)
        if callable(callback):
            callback(self.context())

    def enqueue_events(self, events, source):
        if self.closed:
            return
        values = self.tab.settings.all()
        groups = list(values.get('primary_js8_groups') or [])
        groups.extend(self.snapshot.get('query_groups') or ())
        if self.tab._group_target:
            groups.append(self.tab._group_target)
        values['primary_js8_groups'] = list(dict.fromkeys(groups))
        detached = copy.deepcopy(list(events))
        size = len(json.dumps(detached, default=str).encode('utf-8'))
        if len(self.ingest_pending) >= 8 or self.ingest_bytes + size > 1048576:
            self._status('Report import is busy; receiver metadata is retained. Source logs may contain additional traffic.')
            return
        self.ingest_pending.append((values, detached, dict(source), size))
        self.ingest_bytes += size
        self._ingest_next()

    def _ingest_next(self):
        if self.ingest_busy or not self.ingest_pending or self.closed:
            return
        values, events, source, size = self.ingest_pending.pop(0)
        self.ingest_bytes -= size
        self.ingest_busy = True
        from freqinout.core.ncs_receiver_ingest import ingest_ncs_events
        future = self.ingest_executor.submit(ingest_ncs_events, self.path, values, events, source)
        def done(result):
            try:
                result.result()
                error = None
            except Exception as exc:
                error = str(exc)
            if self.closed:
                return
            try:
                self.ingest_completed.emit(error)
            except RuntimeError:
                pass
        future.add_done_callback(done)

    def _ingest_finished(self, error):
        self.ingest_busy = False
        if self.closed:
            return
        if error:
            self._status('Report import unavailable: ' + error + '. Receiver metadata retained.')
        elif not self.busy:
            # API arrivals publish through the existing bounded worker; no
            # dependence on configured JS8 speed or legacy file polling.
            self.poll()
        self._ingest_next()

    def shutdown_complete(self):
        if not self.executor.thread_alive and not self.ingest_executor.thread_alive:
            return True
        started = getattr(self, '_shutdown_started', None)
        if started is not None and time.monotonic() - started >= 2.5:
            if not getattr(self, '_shutdown_deadline_reported', False):
                self._shutdown_deadline_reported = True
                from freqinout.core.logger import log
                log.error('NCS shutdown worker exceeded 2.5s; final draft durability is unverified. No action will be replayed.')
            return True  # bounded daemon abandonment; never destroy a live Qt worker
        return False

    def close(self):
        if self.closed:
            return
        # Snapshot the last keystrokes before stopping the debounce timers.
        # The host's existing graceful-close registry observes the serial lane;
        # no SQLite or endpoint operation runs on this Qt callback.
        ident = (self.run or {}).get('run_id') or (self.tab._ncs_session_id if self.tab._net_in_progress else '')
        if ident and hasattr(self.panel, '_draft'):
            self.retain_compose_draft({'run_id': ident, 'traffic_db_path': str(self.path)},
                                      '__net_message_editor__', self.panel._draft())
        viewer = getattr(self.tab.window(), 'message_viewer_tab', None)
        if viewer is not None:
            try:
                from freqinout.gui.ncs_compose_handoff import retain
                retain(viewer)
            except Exception as exc:
                from freqinout.core.logger import log
                log.error('NCS closing form draft could not be captured: %s', exc)
        self.closed = True
        self._shutdown_started = time.monotonic()
        prior = getattr(self, '_prior_dialog', None)
        if prior is not None:
            prior.close()
        panel_timer = getattr(self.panel, '_draft_timer', None)
        if panel_timer is not None:
            panel_timer.stop()
        self.generation += 1
        self.timer.stop()
        self.draft_timer.stop()
        pending = copy.deepcopy(self.dirty_drafts)
        if pending:
            future = self.executor.submit(self._write_drafts, pending)
            def retained(result):
                try:
                    result.result()
                except Exception as exc:
                    from freqinout.core.logger import log
                    log.error('NCS closing draft could not be saved: %s', exc)
            future.add_done_callback(retained)
        self.executor.shutdown(wait=False, cancel_futures=False)
        self.ingest_pending.clear()
        self.ingest_executor.shutdown(wait=False, cancel_futures=True)
        if not self.busy:
            self._release_end()
