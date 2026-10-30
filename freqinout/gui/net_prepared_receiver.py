"""In-memory NCS preparation adapter. No source discovery on the Qt thread."""
from __future__ import annotations

import copy
import datetime as dt
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QObject, QSignalBlocker, Signal, QTimer
from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QSpinBox, QTextEdit, QTableWidget

from freqinout.core.net_preparation import (
    DraftCapacity, DraftControl, InactiveDraft, PreparationError, PreparedNet,
    Protocol, RetainedDrafts, StartIntents, encode_value,
)
from freqinout.core.net_preparation_inputs import PreparationReader
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor
from freqinout.core.ncs_session_contract import NCS_SESSION_SNAPSHOTS_KEY


class ScopeSettings:
    """Published settings view; scoped edits never project onto runtime primary.

    Snapshot/checkpoint writes retain their established shared keys. Other local
    preferences are namespaced to radio/service rather than changing selectors or
    another radio's legacy projection. Prepare itself forbids every write.
    """
    def __init__(self, values, config_dir, *, writer=None, scope=None):
        self._data = copy.deepcopy(values)
        self.db_path = Path(config_dir) / 'freqinout.db'
        self.writer, self.scope = writer, scope
        self.frozen = True

    def get(self, key, default=None):
        if key == NCS_SESSION_SNAPSHOTS_KEY and self.writer is not None:
            return copy.deepcopy(self.writer.get(key, default))
        return copy.deepcopy(self._data.get(key, default))

    def all(self):
        return copy.deepcopy(self._data)

    def reload(self):
        # A selected immutable scope never follows mutable station-primary state.
        return None

    def set(self, key, value):
        if self.frozen:
            raise PreparationError('Preparation cannot write settings or session records')
        if self.writer is None or self.scope is None:
            raise PreparationError('Scoped settings writer is unavailable')
        shared = key == NCS_SESSION_SNAPSHOTS_KEY or key.startswith('js8_ncs_')
        scoped_key = key if shared else f'ncs_scope_{self.scope.protocol.value}_{self.scope.radio_id}_{key}'
        self.writer.set(scoped_key, value)
        self._data[key] = copy.deepcopy(value)

    def save(self):
        return None

    write = save


JS8_STATE = ('_group_target', '_spotter_form', '_expected_form', '_available_net_form_codes', '_checkins', '_checkin_rows',
             '_status_mismatch', '_checkins_saved', '_qsy_options', '_query_destination_session')
FLDIGI_STATE = ('_known_op_autofilled_prefix', '_known_op_autofill_consumed', '_known_op_tab_stage',
                '_known_op_pending_focus', '_roster_action_scope', '_roster_action_scope_user_selected',
                '_next_roster_seq', '_roster_dirty', '_compare_missing_text',
                '_compare_reference_missing_entries', '_ncs_partner_call', '_ancs_partner_call',
                '_workspace_bucket_defaults', '_workspace_visible_bucket_ids', '_custom_bucket_sources')
SET_FIELDS = {'_checkins_saved', '_workspace_visible_bucket_ids', '_available_net_form_codes'}
CONTROL_NAMES = (
    'role_combo', 'net_name_edit', 'net_name_combo', 'refresh_spin', 'group_edit', 'spotter_combo',
    'qsy_combo', 'hold_duration_combo', 'query_group_selector', 'checkin_filter_combo', 'partner_primary_edit',
    'joiner_ncs_edit', 'joiner_ancs_edit', 'log_assisted_enable_chk', 'known_op_edit',
    'macro_profile_combo', 'macro_profile_edit', 'main_text', 'late_text', 'qru_text',
    'reference_text', 'compare_results_text',
)


class PreparedReceiver(QObject):
    checked = Signal(object, object)
    changed = Signal()

    def __init__(self, tab, protocol, *, writer=None):
        super().__init__(tab)
        self.tab, self.protocol = tab, Protocol(protocol)
        self.writer = writer if writer is not None else tab.settings
        self.initial_settings = tab.settings
        self.prepared = None
        self.manual = False
        self.pending = None
        self.retained = RetainedDrafts()
        self.pre_start_recovery = None
        self.generation = 0
        self.lifecycle = 0
        self.navigation = 0
        self.operator_edited = False
        self.applying = False
        self.dispatching = False
        self.closed = False
        self.status, self.detail = 'Checking selected net', ''
        self.start_intents = StartIntents()
        self.reader = PreparationReader(Path(tab.settings.db_path).parent)
        self.executor = DaemonSerialExecutor(thread_name_prefix='fio-net-start')
        self.inflight = False
        self.latest = None
        self.clock = time.monotonic
        self.checked.connect(self._checked)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(lambda: self.cancel_start('Checking timed out; work retained, review setup and click Start again'))
        tab.destroyed.connect(self.close)
        self._tracked_controls = set()
        self._connect_edits()

    def _controls(self):
        output, seen = {}, set()
        for name in CONTROL_NAMES:
            widget = getattr(self.tab, name, None)
            if widget is not None and id(widget) not in seen:
                output[name] = widget
                seen.add(id(widget))
        for name in ('review_card',):
            card = getattr(self.tab, name, None)
            if card is not None:
                output[name] = card.text_edit
        for key, card in getattr(self.tab, '_custom_bucket_cards', {}).items():
            output['custom:' + key] = card.text_edit
        return output

    def _connect_edits(self):
        for widget in self._controls().values():
            if id(widget) in self._tracked_controls:
                continue
            self._tracked_controls.add(id(widget))
            if isinstance(widget, QLineEdit):
                widget.textEdited.connect(self.edited)
            elif isinstance(widget, QComboBox):
                widget.activated.connect(self.edited)
                if widget.isEditable():
                    widget.lineEdit().textEdited.connect(self.edited)
            elif isinstance(widget, QTextEdit):
                widget.textChanged.connect(self.edited)
            elif isinstance(widget, QCheckBox):
                widget.clicked.connect(self.edited)
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(self.edited)
        table = getattr(self.tab, 'roster_table', None)
        if table is not None and id(table) not in self._tracked_controls:
            self._tracked_controls.add(id(table))
            table.itemChanged.connect(self.edited)
            table.model().dataChanged.connect(self.edited)

    def edited(self, *_):
        if self.applying:
            return
        self.operator_edited = True
        self.generation += 1
        self.cancel_start('Inputs changed; review and click Start again')

    def _set_status(self, status, detail=''):
        self.status, self.detail = status, str(detail)
        self.changed.emit()

    @contextmanager
    def blocked(self):
        self.applying = True
        widgets = list(self._controls().values()) + self.tab.findChildren(QTableWidget)
        widgets += [widget.lineEdit() for widget in list(widgets)
                    if isinstance(widget, QComboBox) and widget.isEditable()]
        widgets += [table.model() for table in self.tab.findChildren(QTableWidget)]
        blockers = [QSignalBlocker(widget) for widget in widgets]
        previous_loading = getattr(self.tab, '_roster_loading', False)
        previous_syncing = getattr(self.tab, '_roster_syncing', False)
        self.tab._roster_loading = self.tab._roster_syncing = True
        try:
            yield
        finally:
            self.tab._roster_loading, self.tab._roster_syncing = previous_loading, previous_syncing
            for blocker in reversed(blockers):
                blocker.unblock()
            self.applying = False

    def capture(self):
        if getattr(self.tab, '_net_in_progress', False):
            raise PreparationError('Active net work cannot be retained as an inactive draft')
        controls = []
        for name, widget in self._controls().items():
            if isinstance(widget, QLineEdit):
                kind, value = 'line', widget.text()
            elif isinstance(widget, QComboBox):
                kind, value = 'combo', {'text': widget.currentText(), 'index': widget.currentIndex(),
                                        'items': [(widget.itemText(i), widget.itemData(i)) for i in range(widget.count())]}
            elif isinstance(widget, QTextEdit):
                kind, value = 'text', widget.toPlainText()
            elif isinstance(widget, QCheckBox):
                kind, value = 'check', widget.isChecked()
            elif isinstance(widget, QSpinBox):
                kind, value = 'spin', widget.value()
            else:
                continue
            controls.append(DraftControl.make(name, kind, value))
        fields = JS8_STATE if self.protocol == Protocol.JS8 else FLDIGI_STATE
        state = {key: copy.deepcopy(getattr(self.tab, key)) for key in fields if hasattr(self.tab, key)}
        for key in SET_FIELDS & state.keys():
            state[key] = sorted(state[key])
        workbench = getattr(self.tab, 'workbench', None)
        if self.protocol == Protocol.JS8 and workbench is not None:
            state['_workbench_context'] = workbench.capture_context()
        state['_manual_context'] = self.manual
        state['_binding'] = {key: str(getattr(self.tab, key)) if key == '_directed_path' and getattr(self.tab, key, None) else getattr(self.tab, key, None)
                             for key in ('_directed_path', '_directed_offset_key', '_all_offset_key',
                                         '_startup_directed_size', '_last_directed_size', '_last_all_size',
                                         '_ncs_session_radio_id', '_net_start_utc', '_net_end_utc', '_preparation_shell')}
        state['_local_preferences'] = {key: self.tab.settings.get(key) for key in ('fldigi_selected_macro_profile', 'fldigi_macro_profiles_v1', 'js8_refresh_sec')
                                       if key in self.tab.settings.all()}
        if self.protocol == Protocol.FLDIGI:
            state['roster_rows'] = self.tab._roster_table_rows()
        for name in ('checkin_table', 'roster_table'):
            table = getattr(self.tab, name, None)
            if table is not None:
                state['table_position'] = [table.currentRow(), table.currentColumn(), table.verticalScrollBar().value()]
        radio = self.prepared.scope.radio_id if self.prepared else int(getattr(self.tab, '_ncs_session_radio_id', 0) or getattr(self.tab, '_draft_radio_id', 0) or 0)
        draft = InactiveDraft(self.protocol, radio, tuple(controls), encode_value(state),
                              self.operator_edited, self.generation, self.prepared)
        if draft.size > self.retained.max_bytes:
            raise DraftCapacity('This draft exceeds retained capacity; save/review existing work before preparing another net')
        return draft

    def has_work(self):
        self._connect_edits()
        workbench = getattr(self.tab, 'workbench', None)
        if workbench is not None and (workbench.panel.note_edit.text().strip() or workbench.panel._draft().get('editing')):
            return True
        if self.operator_edited or getattr(self.tab, '_roster_dirty', False):
            return True
        if any(widget.toPlainText().strip() for widget in self._controls().values() if isinstance(widget, QTextEdit)):
            return True
        if getattr(self.tab, '_checkins', {}):
            return True
        table = getattr(self.tab, 'roster_table', None)
        return bool(table is not None and table.rowCount())

    def _restore(self, draft):
        if self.protocol != draft.protocol:
            raise PreparationError('The retained draft belongs to a different workspace')
        widgets = self._controls()
        if draft.preparation:
            self._bind(draft.preparation.scope)
        else:
            self.tab.settings = self.initial_settings
            self.tab._preparation_scope = None
        with self.blocked():
            for control in draft.controls:
                widget = widgets.get(control.name)
                if widget is None:
                    raise PreparationError('A retained field is unavailable; existing work has been kept')
                value = control.value()
                if control.kind == 'combo':
                    widget.clear()
                    for text, data in value['items']:
                        widget.addItem(text, data)
                    widget.setCurrentIndex(value['index'])
                    if widget.isEditable():
                        widget.setEditText(value['text'])
                elif control.kind in {'line', 'text'}:
                    (widget.setText if control.kind == 'line' else widget.setPlainText)(value)
                elif control.kind == 'check':
                    widget.setChecked(value)
                elif control.kind == 'spin':
                    widget.setValue(value)
            state = draft.state()
            for key, value in state.items():
                if key not in {'roster_rows', 'table_position', '_binding', '_local_preferences', '_manual_context', '_workbench_context'}:
                    setattr(self.tab, key, set(value) if key in SET_FIELDS else value)
            workbench = getattr(self.tab, 'workbench', None)
            if workbench is not None and '_workbench_context' in state:
                workbench.restore_context(state['_workbench_context'])
            if isinstance(self.tab.settings, ScopeSettings):
                self.tab.settings._data.update(copy.deepcopy(state.get('_local_preferences', {})))
            for key, value in state.get('_binding', {}).items():
                setattr(self.tab, key, Path(value) if key == '_directed_path' and value else value)
            if self.protocol == Protocol.FLDIGI:
                self.tab._roster_rebuild_rows(state.get('roster_rows', []))
            else:
                self.tab._clear_table()
                ordering = dict(self.tab._checkin_rows)
                self.tab._checkin_rows = {}
                self.tab._checkins = {key: self.tab._checkins[key] for key in sorted(self.tab._checkins, key=lambda key: ordering.get(key, 1000000))}
                for callsign, record in self.tab._checkins.items():
                    self.tab._update_row(callsign, record)
            if self.protocol == Protocol.FLDIGI:
                self.tab._restoring_preparation = True
                try:
                    self.tab._apply_role_workspace(self.tab.role_combo.currentText())
                    self.tab._refresh_partner_controls()
                    for key, card in self.tab._custom_bucket_cards.items():
                        card.setVisible(key in state.get('_workspace_visible_bucket_ids', []))
                        card.setProperty('preparedBucketInitialized', True)
                    self.tab._workspace_bucket_defaults = state.get('_workspace_bucket_defaults', self.tab._workspace_bucket_defaults)
                    self.tab._refresh_roster_action_scope_styles()
                finally:
                    self.tab._restoring_preparation = False
            table = getattr(self.tab, 'checkin_table' if self.protocol == Protocol.JS8 else 'roster_table', None)
            if table is not None and 'table_position' in state:
                row, column, scroll = state['table_position']
                table.setCurrentCell(row, column)
                table.verticalScrollBar().setValue(scroll)
        self.prepared = draft.preparation
        self.manual = bool(draft.state().get('_manual_context', False))
        if self.prepared is not None and self.protocol == Protocol.JS8:
            # An explicit catalog reload belongs to the retained draft, not
            # the original preparation snapshot. Restore the current picker.
            forms = bool(self.tab._available_net_form_codes or self.tab.spotter_combo.currentData())
            self.tab.spotter_combo.setEnabled(forms)
            self.tab._on_spotter_selection_changed()
            self.tab._update_group_button_state()
            self.tab._update_preparation_summary()
        if self.protocol == Protocol.JS8:
            from freqinout.core.group_utils import normalize_group_name
            with QSignalBlocker(self.tab.group_edit):
                self.tab.group_edit.setText(normalize_group_name(self.tab.group_edit.text()))
        self.operator_edited = draft.operator_edited
        self.generation += 1
        self.allow_manual_edits()

    def restore(self, draft, *, retain_current=False):
        if getattr(self.tab, '_net_in_progress', False):
            return 'conflict'
        if draft is None:
            return 'unavailable'
        recovery = self.pre_start_recovery is draft
        retained = self.retained.get(draft.protocol, draft.radio_id) is draft
        if not recovery and not retained:
            return 'superseded'
        old, preserve = self.capture(), self.has_work()
        if preserve and not retain_current:
            self._set_status('Existing draft retained', 'Review current work before restoring another draft')
            return 'conflict'
        try:
            if preserve and retained:
                self.retained.check_exchange(draft, old)
            elif preserve and recovery:
                self.retained.retain(old)
        except DraftCapacity as exc:
            self._set_status('Existing draft retained', str(exc))
            return 'conflict'
        try:
            self._restore(draft)
        except Exception:
            self._restore(old)
            if recovery and self.retained.get(old.protocol, old.radio_id) is old:
                self.retained.remove(old)
            raise
        if preserve and retained:
            self.retained.exchange(draft, old)
        elif retained:
            self.retained.remove(draft)
        if recovery:
            self.pre_start_recovery = None
        self._set_status('Manual net context' if self.manual else ('Prepared' if self.prepared else 'Existing draft restored'), 'Review inputs before Start')
        return 'accepted'

    def prepare(self, prepared: PreparedNet, inputs, *, retain_current=False):
        if self.closed or prepared.scope.protocol != self.protocol or inputs.scope.identity != prepared.scope.identity:
            return 'unavailable'
        if getattr(self.tab, '_net_in_progress', False):
            same = bool(not self.manual and getattr(self.tab, '_ncs_session_id', '') and self.prepared and
                        self.prepared.association == prepared.association)
            self._set_status('Open active net' if same else 'Another net active',
                             'Existing session retained; use its separate End workflow')
            return 'accepted' if same else 'conflict'
        if not self.manual and self.prepared and self.prepared.association == prepared.association:
            return 'accepted'
        self.pending = (prepared, inputs)
        if self.has_work() and not retain_current:
            self._set_status('Existing draft retained', 'Keep current draft, review differences, or keep draft and prepare')
            return 'conflict'
        old, old_settings = self.capture(), self.tab.settings
        if self.has_work():
            try:
                self.retained.retain(old)
            except DraftCapacity as exc:
                self._set_status('Existing draft retained', str(exc))
                return 'conflict'
        try:
            with self.blocked():
                self._bind(prepared.scope)
                if self.protocol == Protocol.JS8:
                    self.tab._checkins, self.tab._checkin_rows = {}, {}
                    self.tab._checkins_saved, self.tab._status_mismatch = set(), {}
                    self.tab._clear_table()
                else:
                    self.tab._roster_rebuild_rows([])
                    for field in ('main_text', 'late_text', 'qru_text', 'reference_text', 'compare_results_text'):
                        getattr(self.tab, field).setPlainText('')
                    self.tab.review_card.set_text('')
                    for card in self.tab._custom_bucket_cards.values():
                        card.set_text('')
                        card.setProperty('preparedBucketInitialized', False)
                    self.tab.known_op_edit.clear()
                    self.tab._roster_dirty = False
                    self.tab._refresh_custom_bucket_cards()
                name = prepared.request.source.values.values().get('net_name')
                if self.protocol == Protocol.JS8:
                    self.tab.net_name_edit.setText(str(name or ''))
                    group = prepared.request.source.values.values().get('primary_js8call_group')
                    normalized = str(group or '').strip().upper().replace('@', '')
                    normalized = '@' + normalized if normalized else ''
                    self.tab.group_edit.setText(normalized.lstrip('@'))
                    self.tab._group_target = normalized
                else:
                    self.tab.net_name_combo.setEditText(str(name or ''))
                self.prepared = prepared
                self.manual = False
                self.tab._ncs_session_radio_id = prepared.scope.radio_id
                if self.protocol == Protocol.JS8:
                    self.tab._directed_path = Path(prepared.scope.directed_path) if prepared.scope.directed_path else None
                    self.tab._startup_directed_size = self.tab._last_directed_size = inputs.directed_size
                    self.tab._last_all_size = inputs.all_size
                    self.tab._refresh_ncs_offset_keys()
                    values = prepared.scope.settings()
                    self.tab._preparation_callsign = bool(str(values.get('operator_callsign') or values.get('callsign') or '').strip())
                    self.tab.refresh_spin.setValue(int(values.get('js8_refresh_sec') or 15))
                    self.tab.spotter_combo.clear()
                    forms = values.get('_preparation_forms', [])
                    self.tab._available_net_form_codes = {code for code, _ in forms}
                    for code, title in forms:
                        self.tab.spotter_combo.addItem(title, code)
                    if not forms:
                        self.tab.spotter_combo.addItem('No forms found')
                    else:
                        self.tab.spotter_combo.setCurrentIndex(self.tab.spotter_combo.findData(values.get('_preparation_preferred_form', '')))
                        self.tab.spotter_combo.setPlaceholderText('Select a form')
                    self.tab.spotter_combo.setEnabled(bool(forms))
                    self.tab._adopt_query_selection()
                    self.tab._update_group_button_state()
                    self.tab._update_preparation_summary()
                else:
                    selected = str(prepared.scope.settings().get('fldigi_selected_macro_profile') or '')
                    self.tab.macro_profile_edit.setText(selected)
                    self.tab.macro_profile_combo.clear()
                    self.tab.macro_profile_combo.addItem(selected or 'No macro profile selected', selected)
                self.tab._refresh_ncs_session_context()
        except Exception as exc:
            self.tab.settings = old_settings
            self._restore(old)
            if self.has_work() and self.retained.get(old.protocol, old.radio_id) is old:
                self.retained.remove(old)
            self._set_status('Needs setup', f'Preparation could not be applied; previous work retained: {exc}')
            return 'unavailable'
        self._detach_session_inputs()
        self.tab._ncs_rx_epoch = int(getattr(self.tab, '_ncs_rx_epoch', 0)) + 1
        self.pending = None
        self.allow_manual_edits()
        self.operator_edited = False
        self.generation += 1
        self.cancel_start()
        self._set_status('Needs setup' if inputs.missing else 'Prepared',
                         ', '.join(inputs.missing) if inputs.missing else 'Review inputs; Start Net is a separate action')
        return 'needs attention' if inputs.missing else 'accepted'

    def _bind(self, scope):
        previous_scope = getattr(self.tab, '_preparation_scope', None)
        workbench = getattr(self.tab, 'workbench', None)
        if workbench is not None and getattr(previous_scope, 'identity', None) != scope.identity:
            workbench.scope_changed()
        self.tab.settings = ScopeSettings(scope.settings(), self.reader.config_dir,
                                          writer=self.writer, scope=scope)
        # Preparation's own apply is signal blocked and cannot persist anything.
        self.tab.settings.frozen = True
        self.tab._preparation_scope = scope
        self.tab._preparation_shell = True
        if self.protocol == Protocol.JS8:
            values = scope.settings()
            self.tab._preparation_callsign = bool(str(values.get('operator_callsign') or values.get('callsign') or '').strip())

    def rebind(self, prepared, inputs):
        if self.prepared is None or self.tab._net_in_progress or inputs.scope.identity != prepared.scope.identity:
            return 'conflict'
        if (self.prepared.request.source.association != prepared.request.source.association or
                self.prepared.scope.radio_id != prepared.scope.radio_id or self.protocol != prepared.scope.protocol):
            return 'superseded'
        old = self.capture()
        try:
            with self.blocked():
                self._bind(prepared.scope)
                self.prepared = prepared
                self.tab._ncs_session_radio_id = prepared.scope.radio_id
                if self.protocol == Protocol.JS8:
                    selected_form = self.tab.spotter_combo.currentData()
                    self.tab._directed_path = Path(prepared.scope.directed_path) if prepared.scope.directed_path else None
                    self.tab._startup_directed_size = inputs.directed_size
                    self.tab._last_directed_size, self.tab._last_all_size = inputs.directed_size, inputs.all_size
                    self.tab._refresh_ncs_offset_keys()
                    forms = prepared.scope.settings().get('_preparation_forms', [])
                    self.tab._available_net_form_codes = {code for code, _ in forms}
                    self.tab.spotter_combo.clear()
                    for code, title in forms:
                        self.tab.spotter_combo.addItem(title, code)
                    if not forms:
                        self.tab.spotter_combo.addItem('No forms found')
                    elif self.tab.spotter_combo.findData(selected_form) >= 0:
                        self.tab.spotter_combo.setCurrentIndex(self.tab.spotter_combo.findData(selected_form))
                    else:
                        self.tab.spotter_combo.setCurrentIndex(-1)
                        self.tab.spotter_combo.setPlaceholderText('Select a form')
                    self.tab.spotter_combo.setEnabled(bool(forms))
                    self.tab._on_spotter_selection_changed()
                    self.tab._update_group_button_state()
                    self.tab._update_preparation_summary()
                self.tab._refresh_ncs_session_context()
        except Exception:
            self._restore(old)
            raise
        self.allow_manual_edits()
        self.generation += 1
        self.cancel_start()
        self._set_status('Needs setup' if inputs.missing else 'Prepared',
                         ', '.join(inputs.missing) if inputs.missing else 'Updated setup checked; entered work retained. Start remains explicit.')
        return 'needs attention' if inputs.missing else 'accepted'

    def allow_manual_edits(self):
        if isinstance(self.tab.settings, ScopeSettings):
            self.tab.settings.frozen = False

    def start(self):
        if self.prepared is None or self.closed or getattr(self.tab, '_is_shutting_down', False) or getattr(self.tab.window(), '_closing', False):
            return False
        if getattr(self.tab, '_net_in_progress', False):
            self._set_status('Another net active', 'Open the active net; End remains a separate action')
            return True
        self.cancel_start()
        intent = self.start_intents.begin(self.prepared, self.generation,
                                          lifecycle=self.lifecycle, navigation=self.navigation, now=self.clock())
        self._set_status('Checking selected net', 'Checking intended radio for this manual Start click' if self.manual else 'Checking saved source and intended radio for this Start click')
        self.deadline.start(10000)
        snapshot = self.capture()
        job = (intent, self.prepared, (snapshot.controls, snapshot.state_payload))
        if self.inflight:
            self.latest = job
        else:
            self._submit(job)
        return True

    def _submit(self, job):
        self.inflight = True
        intent, prepared, signature = job
        future = self.executor.submit(self.reader.load, prepared.request, for_start=True, expected=prepared.scope, for_manual=self.manual)
        def done(result):
            try:
                value, error = result.result(), None
            except Exception as exc:
                value, error = None, str(exc)
            if not self.closed:
                try:
                    self.checked.emit(job, (value, error))
                except RuntimeError:
                    pass
        future.add_done_callback(done)

    def _checked(self, job, result):
        self.inflight = False
        intent, prepared, signature = job
        value, error = result
        stopping = self.closed or getattr(self.tab, '_is_shutting_down', False) or getattr(self.tab.window(), '_closing', False)
        snapshot = self.capture() if not stopping and not getattr(self.tab, '_net_in_progress', False) else None
        same_draft = snapshot is not None and (snapshot.controls, snapshot.state_payload) == signature
        valid = (not self.closed and same_draft and self.prepared is not None and
                 self.start_intents.consume(intent, self.prepared, self.generation,
                                           lifecycle=self.lifecycle, navigation=self.navigation, now=self.clock()))
        if self.start_intents.current is intent and not same_draft:
            self.cancel_start('Inputs changed; work retained, review and click Start again' if not stopping else '')
        if valid:
            self.deadline.stop()
            if error:
                self._set_status('Source changed' if 'Source' in error or 'occurrence' in error else 'Needs setup', error + '; review and click Start again')
            else:
                self._dispatch_start(value.radios[0])
        latest, self.latest = self.latest, None
        if latest is not None and self.start_intents.current is latest[0] and not self.closed:
            self._submit(latest)

    def _dispatch_start(self, inputs):
        if self.has_work() or self.pre_start_recovery is None:
            if self.pre_start_recovery is not None and self.has_work():
                self._set_status('Existing draft retained', 'Restore/review the previous Start recovery draft before starting another net')
                return
            self.pre_start_recovery = self.capture()
        self.dispatching = True
        self.pending_session_token = uuid.uuid4().hex
        self.allow_manual_edits()
        try:
            self._attach_session_inputs()
            self.tab._start_net()
            if getattr(self.tab, '_net_in_progress', False):
                self._set_status('Active net', 'Scoped session; use End Net to finish')
            else:
                self._set_status('Prepared', 'Start was cancelled; draft retained')
        except Exception as exc:
            # File preservation is not a multi-file transaction. Restore UI while
            # keeping any verified pre-start archive available for recovery.
            self.tab._net_in_progress = False
            self.tab._ncs_session_id = ''
            self._restore(self.pre_start_recovery)
            self.tab._set_net_button_styles(active=False)
            if self.protocol == Protocol.JS8:
                self.tab._auto_query_paused_by_net = False
                self.tab.end_btn.setEnabled(False)
                self.tab.ack_btn.setEnabled(False)
                self.tab.ack_callsign_btn.setEnabled(False)
            self._set_status('Needs setup', f'Start failed; draft restored and preserved files retained: {exc}')
        finally:
            if not getattr(self.tab, '_net_in_progress', False):
                self._detach_session_inputs()
            self.pending_session_token = ''
            self.dispatching = False

    def _session_matches(self, token, association):
        return bool(not self.closed and getattr(self.tab, '_net_in_progress', False)
                    and getattr(self.tab, '_ncs_session_id', '') == token
                    and self.prepared and self.prepared.association == association)

    def _attach_session_inputs(self):
        token, association = self.pending_session_token, self.prepared.association
        if self.protocol == Protocol.JS8:
            from freqinout.radio_interface.js8_rx_hub import JS8RxHub
            self._detach_session_inputs()
            host, port = self.prepared.scope.endpoint
            hub = JS8RxHub.instance(host, port, settings=self.tab.settings)
            def receive(messages):
                if self._session_matches(token, association):
                    self.tab._on_js8_rx_messages(messages)
            self.tab._scoped_rx_callback = receive
            self.tab._js8_rx_hub = hub
            hub.register_listener(receive)
            self.tab._js8_rx_registered = True
            if not hub.start(host, port, native_only=True):
                self._detach_session_inputs()
                raise PreparationError('The intended JS8 receive endpoint is unavailable; review setup and click Start again')
            self.tab._poll_timer = QTimer(self.tab)
            self.tab._poll_timer.setInterval(self.tab.refresh_spin.value() * 1000)
            self.tab._poll_timer.timeout.connect(lambda: self.tab._poll_directed_file()
                                                if self._session_matches(token, association) else None)
        else:
            old = getattr(self.tab, '_log_assisted_timer', None)
            if old is not None:
                old.stop()
                old.deleteLater()
            self.tab._log_assisted_timer = QTimer(self.tab)
            self.tab._log_assisted_timer.setInterval(5000)
            self.tab._log_assisted_timer.timeout.connect(lambda: self.tab._poll_log_assisted_intake()
                                                        if self._session_matches(token, association) else None)

    def _detach_session_inputs(self):
        if self.protocol == Protocol.JS8:
            hub = getattr(self.tab, '_js8_rx_hub', None)
            if hub is not None and getattr(self.tab, '_js8_rx_registered', False):
                hub.unregister_listener(getattr(self.tab, '_scoped_rx_callback', self.tab._on_js8_rx_messages))
                self.tab._js8_rx_registered = False
            timer = getattr(self.tab, '_poll_timer', None)
            if timer is not None:
                timer.stop()
                timer.deleteLater()
                self.tab._poll_timer = None
        else:
            timer = getattr(self.tab, '_log_assisted_timer', None)
            if timer is not None:
                timer.stop()

    def cancel_start(self, detail=''):
        self.start_intents.cancel()
        self.latest = None
        self.deadline.stop()
        if detail:
            self._set_status('Manual net context' if self.manual else ('Prepared' if self.prepared else 'Existing draft retained'), detail)

    def deactivate(self):
        self.navigation += 1
        self.cancel_start()

    def close(self, *_):
        self._detach_session_inputs()
        self.closed = True
        self.lifecycle += 1
        self.cancel_start()
        self.executor.shutdown(wait=False, cancel_futures=True)
