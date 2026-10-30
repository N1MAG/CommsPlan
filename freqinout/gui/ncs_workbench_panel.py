"""Cached, presentation-only JS8 NCS workspace.

The host supplies bounded snapshots and handles all persistence, readers and RF
operations. Actions carry detached row/entry dictionaries or a draft dictionary;
browsing never dispatches; explicit message activation loads a recoverable draft.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from freqinout.core.ncs_activity_presentation import (
    activity_action, activity_content, activity_destination, activity_kind,
    submission_outcome_label,
)

from PySide6.QtCore import Qt, QTimer, Signal, QEvent, QModelIndex
from PySide6.QtGui import QStandardItem, QStandardItemModel
from freqinout.gui.theme import active_app_theme, control_icon, button_style

from PySide6.QtWidgets import (QAbstractItemView, QBoxLayout, QComboBox, QCompleter, QTableView, QFormLayout, QGridLayout, QHBoxLayout,
    QHeaderView, QMenu, QSizePolicy, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QSplitter, QStyle, QTableWidget, QTableWidgetItem, QTextEdit,
    QToolButton, QVBoxLayout, QWidget)


class SavedMessagePicker(QComboBox):
    """Cache-only two-column picker; the controller supplies searched rows."""
    search_requested = Signal(str)
    message_activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setProperty('fio_no_auto_fit', True)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)
        self.setAccessibleName('Saved outgoing message')
        self.lineEdit().setPlaceholderText('Search message name or text…')
        self._choices = None
        self._pending_search = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(['Message name', 'Full message'])
        self.setModel(self._model)
        self.setView(self._table())
        complete = QCompleter(self._model, self)
        complete.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        complete.setCompletionColumn(0)
        complete.setPopup(self._table())
        self.setCompleter(complete)
        complete.activated[QModelIndex].connect(lambda index: self._activate_message(
            str(index.data(Qt.ItemDataRole.UserRole) or '')))
        self.activated.connect(lambda _: self._activate_message(str(self.currentData() or '')))
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(lambda: self.search_requested.emit(self.lineEdit().text()))
        self.lineEdit().textEdited.connect(self._search_edited)

    def _activate_message(self, ident):
        if ident and not self._pending_search:
            self.message_activated.emit(ident)

    def _search_edited(self, _text):
        self._pending_search = True
        self.view().setEnabled(False)
        self.completer().popup().setEnabled(False)
        self._search_timer.start()

    def _table(self):
        view = QTableView()
        view.setModel(self._model)
        view.setSelectionBehavior(QAbstractItemView.SelectRows)
        view.setSelectionMode(QAbstractItemView.SingleSelection)
        view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        view.setWordWrap(True)
        view.setTextElideMode(Qt.TextElideMode.ElideNone)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.verticalHeader().hide()
        view.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        view.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        return view

    def _fit_popup(self, view):
        # Fit within the screen and wrap full wording rather than clipping it.
        screen = self.screen().availableGeometry()
        width = min(screen.width(), max(self.width(), self.fontMetrics().averageCharWidth() * 72))
        view.setMinimumWidth(width)
        view.setColumnWidth(0, width // 3)
        view.resizeRowsToContents()

    def showPopup(self):
        self._fit_popup(self.view())
        super().showPopup()

    @staticmethod
    def wording(row):
        definition = row.get('definition') or {}
        return str(definition.get('text') or ('E? ' + str(definition.get('form_code') or '')
            if row.get('kind') == 'query' else 'Locally saved form · ' + str(definition.get('expect_entry_id') or '')))

    def publish_choices(self, rows, *, search='', more=False):
        choices = [(r['entry_id'], str(r.get('name') or 'Untitled'), self.wording(r)) for r in rows]
        self.setToolTip('First 200 matches — type to narrow the search.' if more else
                        'Search name or full message; selecting loads a draft and sends nothing.')
        key = (choices, search, more)
        if self._pending_search and search != self.lineEdit().text().strip()[:512]:
            return
        self._pending_search = False
        self.view().setEnabled(True)
        self.completer().popup().setEnabled(True)
        if key == self._choices:
            return
        text, ident = self.lineEdit().text(), self.currentData()
        self.blockSignals(True)
        self._model.removeRows(0, self._model.rowCount())
        blank = QStandardItem('Choose a saved message')
        self._model.appendRow([blank, QStandardItem('')])
        for entry_id, name, body in choices:
            items = [QStandardItem(name), QStandardItem(body)]
            for item in items:
                item.setData(entry_id, Qt.ItemDataRole.UserRole)
                item.setToolTip(body)
            self._model.appendRow(items)
        self.setCurrentIndex(max(-1, self.findData(ident)))
        self.lineEdit().setText(text)
        self.blockSignals(False)
        self._choices = key
        if search and self.lineEdit().hasFocus():
            self._fit_popup(self.completer().popup())
            self.completer().complete()


class NcsWorkbenchPanel(QWidget):
    action_requested = Signal(str, object)
    refresh_requested = Signal()
    view_changed = Signal(str)
    draft_changed = Signal(object)
    state_presented = Signal()
    roster_filters_changed = Signal(object)
    message_search_requested = Signal(str)
    workspace_changed = Signal(str)
    editor_reveal_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._snapshot = {}
        self._roster = []
        self._reports = []
        self._entries = []
        self._runs = []
        self._editing = None
        self._replaced_drafts = []
        self._note_drafts = {}
        self._note_owner = None
        self._note_owner_row = {}
        self._note_baseline = ''
        self._copied_note_key = None
        self._editor_generation = 0
        self._run_id = None
        self._detail_open = False
        self._detail_requested = False
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(300)
        self._draft_timer.timeout.connect(lambda: self.draft_changed.emit(self._draft()))
        self._build()

    def _button(self, label, action, payload=None):
        button = QPushButton(label)
        button.clicked.connect(lambda: self.action_requested.emit(action, payload() if callable(payload) else payload))
        return button

    def _build(self):
        outer = QVBoxLayout(self)
        controls = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._controls_layout = controls
        self.status_label = QLabel('No net selected')
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        controls.addWidget(self.status_label, 1)
        self.run_selector = QComboBox()
        self.run_selector.setProperty("fio_no_auto_fit", True)
        self.run_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.run_selector.setMinimumContentsLength(24)
        self.run_selector.setAccessibleName('Net name')
        self.run_selector.activated.connect(self._open_run)
        self.run_selector.currentTextChanged.connect(self.run_selector.setToolTip)
        run_controls = QHBoxLayout()
        run_controls.addWidget(QLabel('Net name'))
        run_controls.addWidget(self.run_selector, 1)
        self.refresh_button = QPushButton('Refresh')
        self.refresh_button.clicked.connect(self.refresh_requested)
        run_controls.addWidget(self.refresh_button)
        self.run_controls_widget = QWidget()
        self.run_controls_widget.setLayout(run_controls)
        controls.addWidget(self.run_controls_widget)
        outer.addLayout(controls)
        self.group_action_widget = QWidget()
        group_rows = QVBoxLayout(self.group_action_widget)
        group_rows.setContentsMargins(0, 0, 0, 0)
        self.group_action_layout = QHBoxLayout()
        group_rows.addLayout(self.group_action_layout)
        self.query_picker_layout = QVBoxLayout()
        group_rows.addLayout(self.query_picker_layout)
        self.group_context_label = QLabel('Group')
        self.group_context_label.setWordWrap(True)
        self.group_action_layout.addWidget(self.group_context_label)
        self.query_group_button = self._button('E? Group', 'query_group')
        self.query_station_button = self._button('E? Station', 'query_station', self._selected_roster)
        self.group_action_layout.addWidget(self.query_group_button)
        self.undo_button = self._button('Undo', 'undo')
        self.group_action_layout.addWidget(self.undo_button)
        self.group_action_layout.addStretch()
        self.newer_label = QLabel('New traffic · Refresh')
        self.newer_label.setWordWrap(True)
        self.newer_label.setVisible(False)
        self.group_action_layout.addWidget(self.newer_label)
        self.summary_widget = QWidget()
        summary_row = QHBoxLayout(self.summary_widget)
        summary_row.setContentsMargins(0, 0, 0, 0)
        self.summary_label = QLabel()
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        summary_row.addWidget(self.summary_label, 1)
        self.topics_button = QPushButton('Topics')
        self.topics_button.setCheckable(True)
        summary_row.addWidget(self.topics_button)
        controls.addWidget(self.summary_widget, 2)
        outer.addWidget(self.group_action_widget)
        self.topic_summary_widget = QWidget()
        self.topic_summary_grid = QGridLayout(self.topic_summary_widget)
        self.topic_summary_grid.setContentsMargins(0, 0, 0, 0)
        self._topic_widgets = []
        self._summary_topics = None
        self.topic_summary_widget.hide()
        self.topics_button.toggled.connect(self.topic_summary_widget.setVisible)
        outer.addWidget(self.topic_summary_widget)
        self.navigation_widget = QWidget()
        disclosures = QGridLayout(self.navigation_widget)
        disclosures.setContentsMargins(0, 0, 0, 0)
        self._disclosures_layout = disclosures
        self.traffic_button = QPushButton('Traffic')
        self.traffic_button.setCheckable(True)
        self.traffic_button.clicked.connect(self.show_traffic)
        self.send_toggle = QPushButton('Compose')
        self.manage_messages_toggle = QPushButton('Saved messages')
        self.sent_activity_toggle = QPushButton('Sent activity')
        for toggle in (self.send_toggle, self.manage_messages_toggle, self.sent_activity_toggle):
            toggle.setCheckable(True)
        disclosures.addWidget(self.send_toggle, 0, 0)
        disclosures.addWidget(self.manage_messages_toggle, 0, 1)
        disclosures.addWidget(self.sent_activity_toggle, 0, 2)
        disclosures.setColumnStretch(3, 1)
        outer.addWidget(self.navigation_widget)
        workspace = QWidget()
        self.receive_workspace = workspace
        outer.addWidget(workspace, 1)
        layout = QVBoxLayout(workspace)
        self.view_selector = QComboBox()
        for text, value in [('Net', 'net'), ('Other', 'other'), ('Show excluded', 'excluded')]:
            self.view_selector.addItem(text, value)
        self.view_selector.setAccessibleName('Report view')
        self.view_selector.currentIndexChanged.connect(lambda: self.view_changed.emit(self.view_selector.currentData()))
        self.previous_button = self._button('Previous', 'previous_page')
        self.next_button = self._button('Next', 'next_page', lambda: self._snapshot.get('next_cursor'))
        self.previous_button.setAccessibleName('Previous reports page')
        self.next_button.setAccessibleName('Next reports page')
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(self.splitter, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self._roster_layout = left_layout
        roster_header = QHBoxLayout()
        roster_header.addWidget(QLabel('Station intelligence'))
        roster_header.addStretch()
        self.roster_previous_button = self._button('Previous', 'roster_previous_page')
        self.roster_next_button = self._button('Next', 'roster_next_page', lambda: self._snapshot.get('roster_next_cursor'))
        self.roster_previous_button.setAccessibleName('Previous stations page')
        self.roster_next_button.setAccessibleName('Next stations page')
        roster_header.addWidget(self.roster_previous_button)
        roster_header.addWidget(self.roster_next_button)
        left_layout.addLayout(roster_header)
        self.roster_search = QLineEdit()
        self.roster_search.setPlaceholderText('Find callsign')
        self.roster_search.setClearButtonEnabled(True)
        self.roster_search.setAccessibleName('Filter net roster by callsign')
        self.roster_group_filter = QComboBox()
        self.roster_group_filter.setProperty('fio_no_auto_fit', True)
        self.roster_group_filter.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.roster_group_filter.setMinimumContentsLength(10)
        self.roster_group_filter.addItem('All groups', '')
        self.roster_group_filter.setAccessibleName('Filter net roster by group')
        self.roster_state_filter = QComboBox()
        self.roster_state_filter.setProperty('fio_no_auto_fit', True)
        self.roster_state_filter.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.roster_state_filter.setMinimumContentsLength(6)
        self.roster_state_filter.addItem('All states', '')
        self.roster_state_filter.setAccessibleName('Filter net roster by state')
        self.roster_region_filter = QComboBox()
        self.roster_region_filter.addItem('All regions', '')
        self.roster_region_filter.setAccessibleName('Filter station intelligence by FEMA region')
        self.roster_region_filter.setToolTip('FEMA region derived from roster state or Call Finder mailing state; not a confirmed operating location.')
        self.roster_state_filter.setToolTip('Roster state, with Call Finder mailing-state fallback when missing; not a confirmed live location.')
        filter_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._roster_filter_layout = filter_row
        filter_row.addWidget(self.roster_search, 2)
        filter_row.addWidget(self.roster_group_filter, 1)
        filter_row.addWidget(self.roster_state_filter, 1)
        filter_row.addWidget(self.roster_region_filter, 1)
        filter_row.addWidget(self.view_selector, 1)
        left_layout.addLayout(filter_row)
        self._roster_filter_timer = QTimer(self)
        self._roster_filter_timer.setSingleShot(True)
        self._roster_filter_timer.setInterval(200)
        self._roster_filter_timer.timeout.connect(lambda: self.roster_filters_changed.emit(dict(
            callsign=self.roster_search.text().strip(), group=self.roster_group_filter.currentData() or '',
            state=self.roster_state_filter.currentData() or '', region=self.roster_region_filter.currentData() or '')))
        self.roster_search.textChanged.connect(lambda: self._roster_filter_timer.start())
        self.roster_group_filter.activated.connect(lambda: self._roster_filter_timer.start())
        self.roster_state_filter.activated.connect(lambda: self._roster_filter_timer.start())
        self.roster_region_filter.activated.connect(lambda: self._roster_filter_timer.start())
        self.roster_table = QTableWidget(0, 12)
        self.roster_table.setHorizontalHeaderLabels(['Callsign', 'State', 'Region', 'Groups', 'Latest traffic', 'Summary', 'Topics', 'Attention', 'Items', 'View', '', 'RX SNR'])
        self.roster_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.roster_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.roster_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.roster_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.roster_table.horizontalHeader().setSectionResizeMode(10, QHeaderView.ResizeMode.ResizeToContents)
        self.roster_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.roster_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for column in (3, 4, 6, 7):
            self.roster_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.roster_table.setColumnWidth(column, self.fontMetrics().horizontalAdvance('M') * 10)
        self.roster_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.roster_table.horizontalHeader().setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)
        self.roster_table.horizontalHeader().setSectionResizeMode(9, QHeaderView.ResizeMode.ResizeToContents)
        self.roster_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.roster_table.setColumnWidth(5, self.fontMetrics().horizontalAdvance('M') * 32)
        # Keep existing logical action columns stable; signal quality scans next
        # to Callsign without shifting retained selection/action identities.
        self.roster_table.horizontalHeader().moveSection(11, 1)
        self.roster_table.horizontalHeader().setSectionResizeMode(11, QHeaderView.ResizeMode.Interactive)
        self.roster_table.setColumnWidth(11, self.fontMetrics().horizontalAdvance('M') * 12)
        self.roster_table.horizontalHeaderItem(11).setToolTip('Latest locally received JS8 signal-to-noise ratio in dB; relayed reports identify the measured relay link.')
        self.roster_table.setAccessibleName('Station intelligence')
        self.roster_table.cellDoubleClicked.connect(lambda row, column: self._view_station(self._roster[row]) if 0 <= row < len(self._roster) else None)
        self.roster_table.itemSelectionChanged.connect(self._roster_selected)
        left_layout.addWidget(self.roster_table, 1)
        self.empty_roster_label = QLabel()
        self.empty_roster_label.setWordWrap(True)
        left_layout.addWidget(self.empty_roster_label)
        self.station_action_widget = QWidget()
        self.station_action_layout = QHBoxLayout(self.station_action_widget)
        self.station_action_layout.setContentsMargins(0, 0, 0, 0)
        self.station_action_layout.addWidget(self.query_station_button)
        self.earlier_work_button = self._button('Earlier work', 'prior_work', self._selected_roster)
        self.station_action_layout.addWidget(self.earlier_work_button)
        self.station_action_layout.addStretch()
        left_layout.insertWidget(1, self.station_action_widget)
        self._roster_spacer_index = left_layout.count()
        left_layout.addStretch(1)
        self.splitter.addWidget(left)
        right = QWidget()
        self.traffic_detail = right
        right_layout = QVBoxLayout(right)
        self._report_layout = right_layout
        report_header = QVBoxLayout()
        self.report_header_label = QLabel('Traffic')
        self.report_header_label.setWordWrap(True)
        report_header.addWidget(self.report_header_label)
        report_controls = QHBoxLayout()
        self.all_reports_button = QPushButton('Close detail')
        self.all_reports_button.setToolTip('Return to the full-width station intelligence table')
        self.all_reports_button.clicked.connect(self._close_detail)
        report_controls.addWidget(self.all_reports_button)
        # View selector belongs to the complete intelligence table.
        report_controls.addWidget(self.previous_button)
        report_controls.addWidget(self.next_button)
        report_header.addLayout(report_controls)
        right_layout.addLayout(report_header)
        self.report_list = QListWidget()
        self.report_list.setAccessibleName('Received traffic')
        self.report_list.currentRowChanged.connect(self._report_selected)
        right_layout.addWidget(self.report_list)
        self.empty_reports_label = QLabel('No reports in this view')
        self.empty_reports_label.setWordWrap(True)
        right_layout.addWidget(self.empty_reports_label)
        self.inspector = QTextEdit()
        self.inspector.setReadOnly(True)
        self.inspector.setAccessibleName('Selected report details')
        right_layout.addWidget(self.inspector, 1)
        self.report_actions_widget = QWidget()
        actions = QGridLayout(self.report_actions_widget)
        actions.setContentsMargins(0, 0, 0, 0)
        self.report_exclude_button = QToolButton()
        self.report_exclude_button.setText('Exclude')
        self.report_exclude_button.setIcon(control_icon('list-x', active_app_theme(), max(12, self.fontMetrics().height())))
        self.report_exclude_button.setAccessibleName('Exclude selected report')
        self.report_exclude_button.setToolTip('Exclude selected report')
        self.report_exclude_button.clicked.connect(lambda: self.action_requested.emit('report_exclude_restore', self._selected_report()))
        actions.addWidget(self.report_exclude_button, 0, 0)
        self.include_button = self._button('Add to net', 'report_include', self._selected_report)
        self.include_button.setEnabled(False)
        actions.addWidget(self.include_button, 0, 1)
        self.report_buttons = {}
        for number, (label, action) in enumerate( [('Reviewed', 'review'), ('Follow-up', 'follow_up'), ('Open full message', 'open_report')]):
            button = self._button(label, action, self._selected_report)
            self.report_buttons[action] = button
            actions.addWidget(button, (number + 2) // 3, (number + 2) % 3)
        right_layout.addWidget(self.report_actions_widget)
        self.report_notes_widget = QWidget()
        notes = QHBoxLayout(self.report_notes_widget)
        notes.setContentsMargins(0, 0, 0, 0)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText('Note')
        self.note_edit.setAccessibleName('Report note')
        self.note_edit.textEdited.connect(self._queue_draft)
        notes.addWidget(self.note_edit, 1)
        self.save_note_button = self._button('Save note', 'save_note', lambda: {'report': self._selected_report(), 'note': self.note_edit.text()})
        notes.addWidget(self.save_note_button)
        right_layout.addWidget(self.report_notes_widget)
        self.pending_note_row = QWidget()
        pending_layout = QHBoxLayout(self.pending_note_row)
        pending_layout.setContentsMargins(0, 0, 0, 0)
        self.pending_note_combo = QComboBox()
        self.pending_note_combo.setAccessibleName('Retained pending report notes')
        pending_layout.addWidget(self.pending_note_combo, 1)
        self.use_pending_note_button = QPushButton('Use pending note')
        self.use_pending_note_button.setToolTip('Copy this pending note to the selected report for review before Save note.')
        self.use_pending_note_button.clicked.connect(self._use_pending_note)
        pending_layout.addWidget(self.use_pending_note_button)
        self.pending_note_row.hide()
        right_layout.addWidget(self.pending_note_row)
        self._report_spacer_index = right_layout.count()
        right_layout.addStretch(1)
        self.splitter.addWidget(right)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([600, 400])
        self._build_messages(outer)
        self._report_selected(-1)
        self._fit_report_list()
        self.publish({})

    def _build_messages(self, outer):
        self.send_panel = QWidget()
        box = QVBoxLayout(self.send_panel)
        self.editor_mode_label = QLabel('Outgoing message')
        box.addWidget(self.editor_mode_label)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.saved_message_combo = SavedMessagePicker()
        self.saved_message_combo.search_requested.connect(self.message_search_requested)
        self.saved_message_combo.message_activated.connect(self._use_saved_message)
        # Compatibility entry point; explicit picker activation now loads directly.
        self.use_saved_message_button = QPushButton('Use message')
        self.use_saved_message_button.clicked.connect(lambda: self._use_saved_message())
        self.use_saved_message_button.hide()
        form.addRow('Saved message', self.saved_message_combo)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText('Message name')
        self.kind_combo = QComboBox()
        for name, key in [('Custom message', 'custom'), ('Query', 'query'), ('Saved form', 'saved_expect')]:
            self.kind_combo.addItem(name, key)
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText('Choose group or selected station')
        self._target_mode = 'manual'
        self._target_station = None
        self.target_row = QWidget()
        targets = QGridLayout(self.target_row)
        targets.setContentsMargins(0, 0, 0, 0)
        targets.addWidget(self.target_edit, 0, 0, 1, 2)
        self.target_group_button = self._button('Group', 'use_group_target')
        self.target_station_button = QPushButton('Selected station')
        self.target_station_button.clicked.connect(self._use_station_target)
        targets.addWidget(self.target_group_button, 1, 0)
        targets.addWidget(self.target_station_button, 1, 1)
        self.target_edit.textEdited.connect(self._manual_target)

        self.form_code_edit = QLineEdit()
        self.saved_expect_combo = QComboBox()
        self.saved_form_row = QWidget()
        saved_row = QHBoxLayout(self.saved_form_row)
        saved_row.setContentsMargins(0, 0, 0, 0)
        saved_row.addWidget(self.saved_expect_combo, 1)
        self.expect_previous_button = self._button('Previous', 'expect_previous_page')
        self.expect_next_button = self._button('Next', 'expect_next_page')
        self.expect_previous_button.setAccessibleName('Previous saved forms page')
        self.expect_next_button.setAccessibleName('Next saved forms page')
        saved_row.addWidget(self.expect_previous_button)
        saved_row.addWidget(self.expect_next_button)
        self.message_edit = QTextEdit()
        self.message_edit.setAccessibleName('Message text')
        self.message_edit.setPlaceholderText('Freeform text; {TARGET GROUP} inserts the selected group.')
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName('Message preview')
        self.preview.textChanged.connect(self._update_send_fields)
        for label, widget in [('Kind', self.kind_combo), ('To', self.target_row),
                              ('Form code', self.form_code_edit), ('Saved form', self.saved_form_row),
                              ('Text', self.message_edit), ('Preview', self.preview)]:
            form.addRow(label, widget)
        self._send_form = form
        form.setRowVisible(self.kind_combo, False)
        box.addLayout(form)
        utilities = QHBoxLayout()
        self.utility_controls = QWidget()
        self.utility_controls.setLayout(utilities)
        self.utility_controls.hide()
        self.snr_button = self._button('SNR?', 'utility_query', lambda: dict(self._draft(), command='SNR?'))
        self.query_msgs_button = self._button('QUERY MSGS', 'utility_query', lambda: dict(self._draft(), command='QUERY MSGS'))
        utilities.addWidget(self.snr_button)
        utilities.addWidget(self.query_msgs_button)
        utilities.addStretch()
        buttons = QHBoxLayout()
        for label, handler in [('Preview', self._preview_entry), ('Send', self._send_entry),
                               ('Clear draft', lambda: self._clear_draft())]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
            setattr(self, label.lower().replace(' ', '_') + '_button', button)
        self.restore_previous_draft_button = QPushButton('Restore previous draft')
        self.restore_previous_draft_button.clicked.connect(self._restore_previous_draft)
        self.restore_previous_draft_button.hide()
        buttons.addWidget(self.restore_previous_draft_button)
        buttons.addStretch()
        box.addLayout(buttons)
        box.addStretch(1)
        self.send_panel.hide()
        self.send_toggle.toggled.connect(self._send_disclosed)

        self.manage_messages_panel = QWidget()
        admin = QVBoxLayout(self.manage_messages_panel)
        self.entry_list = QListWidget()
        self.entry_list.setAccessibleName('Saved messages')
        admin.addWidget(self.entry_list)
        entry_pages = QHBoxLayout()
        self.entries_previous_button = self._button('Previous', 'entries_previous_page')
        self.entries_next_button = self._button('Next', 'entries_next_page', lambda: self._snapshot.get('entries_next_cursor'))
        entry_pages.addWidget(self.entries_previous_button)
        entry_pages.addWidget(self.entries_next_button)
        admin.addLayout(entry_pages)
        name_row = QFormLayout()
        name_row.addRow('Name', self.name_edit)
        admin.addLayout(name_row)
        self.form_drafts_combo = QComboBox()
        self.form_drafts_combo.setAccessibleName('Retained form working copies')
        self.clear_form_draft_button = QPushButton('Clear form draft')
        self.clear_form_draft_button.setToolTip('Clear the selected working copy; the saved form and other drafts are retained.')
        self.clear_form_draft_button.clicked.connect(lambda: self.action_requested.emit(
            'clear_form_draft', {'draft_key': self.form_drafts_combo.currentData()}))
        draft_row = QHBoxLayout()
        draft_row.addWidget(self.form_drafts_combo, 1)
        draft_row.addWidget(self.clear_form_draft_button)
        admin.addLayout(draft_row)
        admin_buttons = QHBoxLayout()
        for label, handler in [('New', self._new_entry), ('Edit', self._edit_entry),
                               ('Save', self._save_entry), ('Delete', self._delete_entry)]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            admin_buttons.addWidget(button)
            setattr(self, label.lower() + '_button', button)
        admin.addLayout(admin_buttons)
        self.manage_messages_panel.hide()
        self.manage_messages_panel.setParent(self)
        self.manage_messages_panel.hide()
        self.manage_messages_toggle.toggled.connect(self._manage_disclosed)

        self.sent_activity_panel = QWidget()
        history = QVBoxLayout(self.sent_activity_panel)
        self.history_list = QListWidget()
        self.history_list.setAccessibleName('Sent activity')
        history.addWidget(QLabel('Sent activity'))
        self.empty_history_label = QLabel('No sent activity for this net yet.')
        self.empty_history_label.setWordWrap(True)
        history.addWidget(self.empty_history_label)
        self.history_list.setParent(self.sent_activity_panel)
        self.history_list.hide()  # Compatibility model; operator surface is the table.
        self.activity_table = QTableWidget(0, 6)
        self.activity_table.setHorizontalHeaderLabels(['Time · UTC', 'Action', 'To', 'Message / form', 'Result', ''])
        self.activity_table.setAccessibleName('Outgoing net activity')
        self.activity_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.activity_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.activity_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.activity_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.activity_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.activity_table.itemSelectionChanged.connect(self._activity_selected)
        self.activity_table.cellDoubleClicked.connect(lambda row, col: self._view_activity(row))
        history.addWidget(self.activity_table, 1)
        self._activity_rows = []
        self._activity_run = None
        self.activity_details = QWidget()
        detail_layout = QVBoxLayout(self.activity_details)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_heading = QHBoxLayout()
        detail_heading.addWidget(QLabel('Selected activity'), 1)
        self.close_activity_button = QPushButton('Close detail')
        self.close_activity_button.clicked.connect(self.activity_details.hide)
        detail_heading.addWidget(self.close_activity_button)
        detail_layout.addLayout(detail_heading)
        self.activity_detail_text = QTextEdit()
        self.activity_detail_text.setReadOnly(True)
        self.activity_detail_text.setAccessibleName('Outgoing activity details')
        self.activity_detail_text.setMaximumHeight(self.fontMetrics().lineSpacing() * 12)
        detail_layout.addWidget(self.activity_detail_text)
        history.addWidget(self.activity_details)
        self.activity_details.hide()
        footer = QLabel('Submitted to JS8Call does not confirm over-air transmission or delivery. Additional confirmation is the operator’s choice.')
        footer.setWordWrap(True)
        history.addWidget(footer)
        history_pages = QHBoxLayout()
        self.history_previous_button = self._button('Previous', 'history_previous_page')
        self.history_next_button = self._button('Next', 'history_next_page', lambda: self._snapshot.get('history_next_cursor'))
        history_pages.addWidget(self.history_previous_button)
        history_pages.addWidget(self.history_next_button)
        history.addLayout(history_pages)
        self.sent_activity_panel.hide()
        outer.addWidget(self.sent_activity_panel)
        self.sent_activity_toggle.toggled.connect(self._sent_disclosed)
        from freqinout.gui.ncs_saved_messages import NcsSavedMessages
        self.saved_library = NcsSavedMessages()
        self.saved_messages_table = self.saved_library.table
        self.saved_library.search_requested.connect(self.message_search_requested)
        self.saved_library.use_requested.connect(self._use_library_message)
        self.saved_library.editing_started.connect(self._library_edit_started)
        self.saved_library.save_requested.connect(lambda row: self.action_requested.emit('save_library_entry', row))
        self.saved_library.delete_requested.connect(lambda row: self.action_requested.emit('delete_entry', row))
        self.saved_library.draft_changed.connect(self._queue_library_draft)
        admin.removeItem(draft_row)
        self.form_working_copies = QWidget()
        self.form_working_copies.setLayout(draft_row)
        draft_row.insertWidget(0, QLabel('Form working copies'))
        self.saved_library.layout().addWidget(self.form_working_copies)
        self.form_working_copies.hide()
        self.transmit_workspace = QSplitter(Qt.Horizontal)
        outer.removeWidget(self.receive_workspace)
        self.transmit_workspace.addWidget(self.receive_workspace)
        self.transmit_workspace.addWidget(self.send_panel)
        outer.insertWidget(outer.indexOf(self.sent_activity_panel), self.saved_library, 1)
        self.saved_library.hide()
        self.transmit_workspace.setChildrenCollapsible(False)
        self.transmit_workspace.setStretchFactor(0, 3)
        self.transmit_workspace.setStretchFactor(1, 2)
        outer.insertWidget(outer.indexOf(self.sent_activity_panel), self.transmit_workspace, 1)
        self.transmit_workspace.hide()
        self.content_options_button = QPushButton('Content…')
        content_menu = QMenu(self.content_options_button)
        for label, kind in [('Text message', 'custom'), ('Form query', 'query'), ('Saved form', 'saved_expect')]:
            content_menu.addAction(label).triggered.connect(lambda checked=False, value=kind:
                self.kind_combo.setCurrentIndex(self.kind_combo.findData(value)))
        self.content_options_button.setMenu(content_menu)
        buttons.addWidget(self.content_options_button)
        for widget in (self.name_edit, self.target_edit, self.form_code_edit):
            widget.textChanged.connect(self._queue_draft)
        self.message_edit.textChanged.connect(self._queue_draft)
        self.kind_combo.currentIndexChanged.connect(self._queue_draft)
        self.kind_combo.currentIndexChanged.connect(self._update_send_fields)
        self.saved_expect_combo.currentIndexChanged.connect(self._queue_draft)
        self._set_editable(False)
        self._update_send_fields()
        self._fit_message_widgets()

    def _library_edit_started(self):
        self.manage_messages_toggle.setChecked(True)
        self.editor_reveal_requested.emit()

    def _use_library_message(self, entry):
        self.manage_messages_toggle.setChecked(False)
        self._load_entry(dict(entry))
        self.send_toggle.setChecked(True)
        self.message_edit.setFocus()

    def _manage_disclosed(self, shown):
        if shown:
            self.sent_activity_toggle.setChecked(False)
            self.send_toggle.blockSignals(True)
            self.send_toggle.setChecked(False)
            self.send_toggle.blockSignals(False)
        self._arrange_workspaces()

    def _sent_disclosed(self, shown):
        if shown:
            for button in (self.send_toggle, self.manage_messages_toggle):
                button.blockSignals(True); button.setChecked(False); button.blockSignals(False)
        self._arrange_workspaces()

    def show_traffic(self, checked=False, *, clear_detail=True):
        if clear_detail:
            self._close_detail()
        self._detail_open = False
        for button in (self.send_toggle, self.manage_messages_toggle, self.sent_activity_toggle):
            button.setChecked(False)
        self._arrange_workspaces()

    def _arrange_workspaces(self):
        if not hasattr(self, 'transmit_workspace'):
            return
        compose = self.send_toggle.isChecked()
        library = self.manage_messages_toggle.isChecked()
        self.roster_table.setMaximumHeight(self.fontMetrics().lineSpacing() * 8 if compose and getattr(self, '_compact_workspace', False) else 16777215)
        self.transmit_workspace.setVisible(not library and not self.sent_activity_toggle.isChecked())
        self.saved_library.setVisible(library)
        self.send_panel.setVisible(compose)
        history = self.sent_activity_toggle.isChecked()
        self.receive_workspace.setVisible(not library and not history)
        self.traffic_detail.setVisible(self._detail_open and not compose)
        self.send_toggle.setText('Back to traffic' if compose else 'Compose')
        self.group_action_widget.setVisible(not library and not history)
        self.status_label.setVisible(not library and not history or self.status_label.text() not in {
            'Listening for net traffic', 'No net selected',
            'Retained net record · no listening or automatic transmission.'})
        self.summary_widget.setVisible(not library and not history and bool(self._snapshot.get('run')))
        self.sent_activity_panel.setVisible(history)
        if getattr(self, '_external_navigation', False):
            self.traffic_button.setVisible(library or history)
            if library or history:
                self.traffic_button.setText('Back to net')
        mode = 'compose' if compose else 'library' if library else 'history' if history else 'traffic'
        if mode != getattr(self, '_workspace_mode', None):
            self._workspace_mode = mode
            if mode == 'compose':
                width = max(self.width(), 1)
                self.transmit_workspace.setSizes([width * 3 // 5, width * 2 // 5])
            if mode in {'compose', 'library'}:
                if mode == 'compose':
                    self.saved_message_combo._search_timer.stop()
                    query = self.saved_message_combo.lineEdit().text().strip()[:512]
                    pending = query != str(self._snapshot.get('message_search', ''))
                    self.saved_message_combo._pending_search = pending
                    self.saved_message_combo.view().setEnabled(not pending)
                else:
                    self.saved_library.search_timer.stop()
                    query = self.saved_library.search.text().strip()[:512]
                    self.saved_library.table.setEnabled(query == str(self._snapshot.get('message_search', '')))
                self.message_search_requested.emit(query)
            self.workspace_changed.emit(mode)
        self.traffic_button.setToolTip('Return to received traffic; outgoing and saved edits are retained')
        self.saved_library.use_button.hide()
        self.saved_library.editor.setVisible(library and self.saved_library.editing is not None)
        self.saved_library.restore_button.setVisible(library and bool(self.saved_library._cancelled))
        self.saved_library.editor_heading.setText('Edit saved message' if (self.saved_library.editing or {}).get('entry_id') else 'New saved message')
        self._style_workspace_navigation()

    def _style_workspace_navigation(self):
        theme = active_app_theme()
        mode = getattr(self, '_workspace_mode', 'traffic')
        key = (mode, tuple(sorted(theme.items())))
        if key == getattr(self, '_nav_theme_key', None):
            return
        for name, button in (('traffic', self.traffic_button), ('compose', self.send_toggle),
                             ('library', self.manage_messages_toggle), ('history', self.sent_activity_toggle)):
            button.blockSignals(True)
            button.setChecked(name == mode)
            button.blockSignals(False)
            button.setStyleSheet(button_style('primary' if name == mode else 'secondary', theme))
        self._nav_theme_key = key

    def _send_disclosed(self, shown):
        if shown:
            self._close_detail()
            self.sent_activity_toggle.setChecked(False)
            self.manage_messages_toggle.blockSignals(True)
            self.manage_messages_toggle.setChecked(False)
            self.manage_messages_toggle.blockSignals(False)
        if shown and self.name_edit.isReadOnly():
            self._set_editable(True)
        if shown and not self.target_edit.text():
            self.action_requested.emit('use_group_target', None)
        self._arrange_workspaces()
        self._report_selected(self.report_list.currentRow())

    def _use_saved_message(self, entry_id=None):
        entry_id = entry_id or self.saved_message_combo.currentData()
        entry = next((dict(row) for row in getattr(self, '_quick_entries', self._entries)
                      if row.get('entry_id') == entry_id), None)
        if entry:
            self._load_entry(entry)
            if not self.target_edit.text():
                self.action_requested.emit('use_group_target', None)

    def set_group_target(self, destination):
        self._target_mode, self._target_station = 'group', None
        self.target_edit.setText(str(destination or '').lstrip('@'))
        self._queue_draft()

    def _manual_target(self, _text):
        self._target_mode, self._target_station = 'manual', None

    def _use_station_target(self):
        station = self._selected_roster()
        if station:
            self._target_mode, self._target_station = 'station', dict(station)
            self.target_edit.setText(str(station.get('from_call') or station.get('callsign') or ''))
            self._queue_draft()

    def _update_send_fields(self, *_args):
        kind = self.kind_combo.currentData()
        self._send_form.setRowVisible(self.form_code_edit, kind == 'query')
        self._send_form.setRowVisible(self.saved_form_row, kind == 'saved_expect')
        self._send_form.setRowVisible(self.message_edit, kind == 'custom')
        self.send_button.setText('Open form…' if kind == 'saved_expect' else 'Send')
        self.preview_button.setVisible(kind != 'saved_expect')
        self._send_form.setRowVisible(self.preview, kind != 'saved_expect' and bool(self.preview.toPlainText()))

    def _fit_message_widgets(self):
        for widget in (self.message_edit, self.preview, self.entry_list, self.history_list):
            widget.setMaximumHeight(widget.fontMetrics().lineSpacing() * 5 + 2 * widget.frameWidth())

    def _selected_roster(self):
        row = self.roster_table.currentRow()
        return dict(self._roster[row]) if 0 <= row < len(self._roster) else None

    def _selected_report(self):
        row = self.report_list.currentRow()
        return dict(self._reports[row]) if 0 <= row < len(self._reports) else None

    def _selected_entry(self):
        row = self.entry_list.currentRow()
        return dict(self._entries[row]) if 0 <= row < len(self._entries) else None

    def _roster_selected(self):
        station = self._selected_roster()
        if self._detail_open and self._snapshot.get('detail_station') and self._snapshot['detail_station'] != (station or {}).get('from_call'):
            self._close_detail()
        self._filter_reports(station)
        self.target_station_button.setEnabled(bool(station))
        if self._target_mode == 'station' and station:
            chosen = str((self._target_station or {}).get('from_call') or (self._target_station or {}).get('callsign') or '')
            current = str(station.get('from_call') or station.get('callsign') or '')
            self.target_station_button.setToolTip('Choose this selected station explicitly; the outgoing target is retained' if current != chosen else 'Use the selected station as the outgoing destination')
        self.action_requested.emit('select_station', station)

    def _view_station(self, station):
        call = str(station.get('from_call') or station.get('callsign') or '')
        index = next((i for i, row in enumerate(self._roster) if str(row.get('from_call') or row.get('callsign') or '') == call), -1)
        if index < 0:
            return
        self.show_traffic(clear_detail=False)
        self.roster_table.selectRow(index)
        self._detail_open = True
        self._detail_requested = True
        self._arrange_workspaces()
        self.empty_reports_label.setText('Loading matching traffic…' if station.get('traffic_count') else 'No indexed traffic; receiver metadata only.')
        self.action_requested.emit('view_station', dict(station))

    def _close_detail(self):
        opened = self._detail_open or getattr(self, '_detail_requested', False)
        self._retain_note()
        self._detail_open = False
        self._detail_requested = False
        self._arrange_workspaces()
        if opened:
            self.action_requested.emit('close_detail', None)

    def _all_reports(self):
        self.roster_table.clearSelection()
        self.roster_table.setCurrentCell(-1, -1)
        self._filter_reports(None)

    def _filter_reports(self, station):
        call = str((station or {}).get('from_call') or (station or {}).get('callsign') or '').upper()
        for index, report in enumerate(self._reports):
            self.report_list.item(index).setHidden(bool(call and str(report.get('from_call') or '').upper() != call))
        self.station_action_widget.setVisible(bool(station))
        self.earlier_work_button.setToolTip(f'Earlier work for {call}' if call else 'Select a station to review earlier work')
        visible = any(not self.report_list.item(i).isHidden() for i in range(self.report_list.count()))
        self.report_list.setVisible(visible)
        if station and not visible and station.get('traffic_count') == 0:
            self.empty_reports_label.setText('Receiver metadata only; no indexed report is available for this station.')
        elif station and not visible and self._reports:
            self.empty_reports_label.setText('No reports for this station in the current view.')
        else:
            self.empty_reports_label.setText(self._empty_copy('reports'))
        self.empty_reports_label.setVisible(not visible and bool(self._roster))
        selected = self.report_list.currentItem()
        if selected is not None and selected.isHidden():
            self.report_list.setCurrentRow(-1)
        self._update_report_header()
        self._report_layout.setStretch(self._report_spacer_index, 0 if self._selected_report() else 1)

    def _empty_copy(self, kind):
        kind = 'traffic' if kind == 'reports' else kind
        if not self._snapshot.get('run'):
            return 'Start a net or select a retained run to see ' + kind + '.'
        if self._snapshot.get('writable'):
            return 'No ' + kind + ' yet. Listening for net traffic.'
        if (self._snapshot.get('run') or {}).get('state') == 'ended':
            return 'No ' + kind + ' in this ended net. Select another retained run to review its traffic.'
        return 'No ' + kind + ' in this retained view. Sending is unavailable for this run.'

    def _update_report_header(self):
        report = self._selected_report()
        station = self._selected_roster()
        if report:
            call = report.get('from_call') or 'Unknown station'
            kind = report.get('kind_label') or report.get('source_family') or 'Report'
            self.report_header_label.setText(f'Traffic · {call} · {kind}')
        elif station:
            self.report_header_label.setText(f"Traffic · {station.get('from_call') or station.get('callsign') or 'Station'}")
        else:
            self.report_header_label.setText('Traffic')

    def _report_selected(self, _row):
        self._retain_note()
        report = self._selected_report()
        if report:
            key = self._note_key(report)
            note = str(report.get('note') or '')
            if (self._note_drafts.get(key) or {}).get('note') == note:
                self._note_drafts.pop(key, None)
            self._note_owner = key
            self._note_owner_row = dict(report)
            self._note_baseline = note
            self.note_edit.setText(str((self._note_drafts.get(key) or {}).get('note', note)))
            self.note_edit.setReadOnly(key not in self._note_drafts and len(self._note_drafts) >= 200)
        else:
            self._note_owner = None
            self._note_owner_row = {}
            self._note_baseline = ''
            self.note_edit.clear()
        self._publish_pending_notes()
        self._report_layout.setStretch(self._report_spacer_index, 0 if report else 1)
        details = bool(report)
        self.inspector.setVisible(details)
        self.report_actions_widget.setVisible(details)
        self.report_notes_widget.setVisible(details)
        self._update_report_header()
        self.report_exclude_button.setEnabled(bool(report))
        self.save_note_button.setEnabled(bool(report))
        self.note_edit.setEnabled(bool(report))
        for action, button in self.report_buttons.items():
            button.setEnabled(bool(report and (action != 'open_report' or report.get('message_id'))))
        if not report:
            self.include_button.setEnabled(False)
            self.inspector.clear()
            return
        self.include_button.setEnabled(bool(report.get('from_call')) and not bool(report.get('belongs_to_net')))
        self._refresh_action_icons()
        for action, button in self.report_buttons.items():
            if action == 'review':
                button.setText('Mark unreviewed' if report.get('reviewed') else 'Mark reviewed')
                detail = f"{'Clear review of' if report.get('reviewed') else 'Mark reviewed'} report from {report.get('from_call') or 'unknown station'}; local review sends no acknowledgment."
            elif action == 'follow_up':
                detail = f"{'Clear' if report.get('follow_up') else 'Flag'} follow-up for report from {report.get('from_call') or 'unknown station'}"
            else:
                detail = f"Open full report from {report.get('from_call') or 'unknown station'}; return to this net is available."
            button.setToolTip(detail)
            button.setAccessibleName(detail)

        verb = 'Restore' if report.get('report_excluded') else 'Exclude'
        target = str(report.get('from_call') or 'selected')
        self.report_exclude_button.setText(verb)
        self.report_exclude_button.setToolTip(f'{verb} report from {target}')
        self.report_exclude_button.setAccessibleName(f'{verb} report from {target}')
        self.report_exclude_button.setIcon(control_icon('resume' if verb == 'Restore' else 'list-x', active_app_theme(), max(12, self.fontMetrics().height())))
        fields = [('From', report.get('from_call')),
                  ('Relay via', (report.get('entities') or {}).get('relay_via')),
                  ('To', report.get('to_call')),
                  ('Type', report.get('kind_label')), ('Source', report.get('source_state') or report.get('source_family')),
                  ('Received', self._utc_stamp(report.get('stamp'))), ('Review', report.get('review_state')),
                  ('Follow-up', 'Needed' if report.get('follow_up') else 'None'),
                  ('Topics', self._topics(report.get('topics'))), ('Note', report.get('note')),
                  ('Summary', report.get('traffic_summary') or (report.get('intelligence') or {}).get('summary') or report.get('summary') or report.get('body_preview')),
                  ('Reported', (report.get('intelligence') or {}).get('date_summary')),
                  ('Reported state', (report.get('intelligence') or {}).get('state')),
                  ('Reported grid', (report.get('intelligence') or {}).get('grid')),
                  ('Message', report.get('body_preview'))]
        self.inspector.setPlainText('\n'.join(f'{key}: {value}' for key, value in fields if value not in (None, '')))
        self.action_requested.emit('select_report', report)

    @staticmethod
    def _note_key(report):
        return str(report.get('canonical_key') or '') + '|' + str(report.get('content_hash') or '')

    def _retain_note(self):
        if self._note_owner and self.note_edit.text() != self._note_baseline:
            if self._note_owner in self._note_drafts or len(self._note_drafts) < 200:
                row = {key: self._note_owner_row.get(key) for key in
                       ('canonical_key', 'content_hash', 'from_call', 'annotation_version')}
                self._note_drafts[self._note_owner] = {'row': row, 'note': self.note_edit.text()}

    def _publish_pending_notes(self):
        selected = self.pending_note_combo.currentData()
        self.pending_note_combo.clear()
        for key, draft in self._note_drafts.items():
            self.pending_note_combo.addItem(str((draft.get('row') or {}).get('from_call') or 'Report') + ' · Pending note', key)
        index = self.pending_note_combo.findData(selected)
        if index >= 0:
            self.pending_note_combo.setCurrentIndex(index)
        self.pending_note_row.setVisible(bool(self._note_drafts))
        self.use_pending_note_button.setEnabled(bool(self._selected_report()))

    def _use_pending_note(self):
        key = self.pending_note_combo.currentData()
        draft = self._note_drafts.get(key)
        if draft and self._selected_report():
            self._copied_note_key = key
            self.note_edit.setText(str(draft['note']))
            self._retain_note()
            self._queue_draft()

    def mark_note_saved(self, report, note):
        key = self._note_key(report)
        for candidate in (key, self._copied_note_key):
            if (self._note_drafts.get(candidate) or {}).get('note') == note:
                self._note_drafts.pop(candidate, None)
        if key == self._note_owner and self.note_edit.text() == note:
            self._note_baseline = note
        self._copied_note_key = None
        self._publish_pending_notes()
        self._queue_draft()

    @staticmethod
    def _topics(value):
        if isinstance(value, dict):
            return ', '.join(f'{k}: {v}' for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return ', '.join(str(x.get('label') or x.get('name') or x) if isinstance(x, dict) else str(x) for x in value)
        return str(value or '')

    def _open_run(self, index):
        value = self.run_selector.itemData(index)
        if value is not None:
            self.action_requested.emit('open_run', value)

    def _draft(self):
        self._retain_note()
        definition = {'text': self.message_edit.toPlainText(), 'form_code': self.form_code_edit.text(),
                      'expect_entry_id': self.saved_expect_combo.currentData()}
        return {'entry_id': (self._editing or {}).get('entry_id'), 'version': (self._editing or {}).get('version', 0),
                'name': self.name_edit.text(), 'kind': self.kind_combo.currentData(),
                'target': self.target_edit.text(), 'target_mode': self._target_mode,
                'target_station': self._target_station,
                'target_group': self._snapshot.get('outgoing_group', ''), 'definition': definition,
                'editing': not self.name_edit.isReadOnly(), 'pending_notes': dict(self._note_drafts),
                'replaced_drafts': list(self._replaced_drafts),
                'library_draft': self.saved_library.draft(), 'library_recovery': list(self.saved_library._cancelled)}

    @staticmethod
    def preview_identity(draft):
        return {key: draft.get(key) for key in ('kind', 'target', 'definition', 'target_mode', 'target_station', 'target_group')}

    def _queue_library_draft(self):
        self._draft_timer.start()

    def _queue_draft(self, *_args):
        self.preview.clear()
        self._draft_timer.start()

    def _retain_replaced_draft(self):
        draft = self._draft()
        draft.pop('replaced_drafts', None)
        draft.pop('library_draft', None)
        draft.pop('library_recovery', None)
        if draft.get('name') or draft.get('target') or any(draft['definition'].values()):
            self._replaced_drafts.append(draft)
        self.restore_previous_draft_button.setVisible(bool(self._replaced_drafts))

    def _restore_previous_draft(self):
        if self._replaced_drafts:
            draft = self._replaced_drafts.pop()
            draft['replaced_drafts'] = list(self._replaced_drafts)
            self._retain_note()
            draft['pending_notes'] = dict(self._note_drafts)
            self.restore_message_draft(draft, restore_library=False)
            self._queue_draft()

    def restore_message_draft(self, draft, *, restore_library=True):
        """Restore an explicitly supplied cached per-run editor draft."""
        self._editor_generation += 1
        self._draft_timer.stop()
        draft = dict(draft or {})
        if restore_library:
            self.saved_library.restore(draft.get('library_draft'), replace=True, recovery=draft.get('library_recovery') or [])
        self._replaced_drafts = list(draft.get('replaced_drafts') or [])
        self.restore_previous_draft_button.setVisible(bool(self._replaced_drafts))
        self.preview.clear()
        self._note_drafts = dict(draft.get('pending_notes') or {})
        self._note_owner = None
        self.note_edit.clear()
        self._publish_pending_notes()
        definition = draft.get('definition') or {}
        widgets = (self.name_edit, self.target_edit, self.form_code_edit, self.message_edit, self.kind_combo, self.saved_expect_combo)
        for widget in widgets: widget.blockSignals(True)
        self.name_edit.setText(str(draft.get('name') or ''))
        self._target_mode = draft.get('target_mode', 'manual')
        self._target_station = draft.get('target_station')
        self.target_edit.setText(str(draft.get('target') or ''))
        self.form_code_edit.setText(str(definition.get('form_code') or ''))
        self.message_edit.setPlainText(str(definition.get('text') or ''))
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(draft.get('kind'))))
        saved_id = definition.get('expect_entry_id')
        saved_index = self.saved_expect_combo.findData(saved_id)
        if saved_index < 0 and saved_id is not None:
            self.saved_expect_combo.addItem('Saved Expect unavailable', saved_id)
            saved_index = self.saved_expect_combo.count() - 1
        self.saved_expect_combo.setCurrentIndex(max(0, saved_index))
        for widget in widgets: widget.blockSignals(False)
        self._update_send_fields()
        self._editing = {'entry_id': draft.get('entry_id'), 'version': draft.get('version', 0)} if draft.get('entry_id') else None
        self._set_editable(bool(draft.get('editing')))
        self._report_selected(self.report_list.currentRow())
        self._arrange_workspaces()

    def _set_editable(self, enabled):
        self.editor_mode_label.setText('Outgoing message')
        for widget in (self.name_edit, self.form_code_edit, self.message_edit):
            widget.setReadOnly(not enabled)
        self.kind_combo.setEnabled(enabled)
        self.saved_expect_combo.setEnabled(enabled)
        self.save_button.setEnabled(enabled)

    def _clear_draft(self, emit=True):
        self._editor_generation += 1
        self._editing = None
        self._target_mode, self._target_station = 'manual', None
        self.name_edit.clear(); self.target_edit.clear(); self.form_code_edit.clear(); self.message_edit.clear(); self.preview.clear()
        self.kind_combo.setCurrentIndex(0)
        self._draft_timer.stop()
        self._set_editable(self.send_toggle.isChecked())
        if emit: self.action_requested.emit('clear_draft', {'draft_key': '__net_message_editor__'})

    def _new_entry(self):
        self._retain_replaced_draft()
        self.send_toggle.setChecked(True)
        self._clear_draft(False)
        self._set_editable(True)
        self.send_toggle.setChecked(True)
        self.name_edit.setFocus()

    def _edit_entry(self):
        entry = self._selected_entry()
        if not entry:
            return
        self._load_entry(entry)

    def _load_entry(self, entry):
        self._retain_replaced_draft()
        self.send_toggle.setChecked(True)
        self._editor_generation += 1
        self._editing = entry
        self._set_editable(True)
        self.send_toggle.setChecked(True)
        definition = entry.get('definition') or {}
        self.name_edit.setText(str(entry.get('name') or ''))
        index = self.kind_combo.findData(entry.get('kind'))
        if index >= 0: self.kind_combo.setCurrentIndex(index)
        self.form_code_edit.setText(str(definition.get('form_code') or ''))
        self.message_edit.setPlainText(str(definition.get('text') or ''))
        saved_id = definition.get('expect_entry_id')
        index = self.saved_expect_combo.findData(saved_id)
        if index < 0 and saved_id is not None:
            self.saved_expect_combo.addItem('Saved form unavailable', saved_id)
            index = self.saved_expect_combo.count() - 1
        self.saved_expect_combo.setCurrentIndex(max(0, index))
        self.action_requested.emit('edit_entry', entry)

    def _save_entry(self): self.action_requested.emit('save_entry', self._draft())
    def _delete_entry(self): self.action_requested.emit('delete_entry', self._selected_entry())
    def _action_draft(self):
        if not self.name_edit.isReadOnly():
            return self._draft()
        entry = self._selected_entry()
        if not entry:
            return self._draft()
        draft = self._draft()
        return dict(entry, **{key: draft[key] for key in ('target', 'target_mode', 'target_station', 'target_group')})

    def _preview_entry(self): self.action_requested.emit('preview_entry', self._action_draft())
    def _send_entry(self): self.action_requested.emit('send_entry', self._action_draft())

    def publish(self, snapshot: dict):
        """Apply a worker-produced snapshot without reading external state."""
        self._snapshot = dict(snapshot)
        if getattr(self, '_detail_run', None) != (snapshot.get('run') or {}).get('run_id'):
            self._detail_open = False
            self._detail_run = (snapshot.get('run') or {}).get('run_id')
        if 'detail_station' in snapshot and not snapshot['detail_station']:
            self._detail_open = False
        status = str(snapshot.get('status') or 'No net selected')
        if status.endswith(' · Listening · NCS decides what to keep.'):
            status = 'Listening for net traffic'
        self.status_label.setText(status)
        self.newer_label.setVisible(bool(snapshot.get('newer_available')))
        summary = snapshot.get('summary') or {}
        def count_caption(label, values, total):
            loaded = len(values or [])
            total = loaded if total is None else total
            return f'{label} {loaded}/{total} loaded' if loaded != total else f'{label} {total}'
        stations = count_caption('Stations', snapshot.get('roster'), snapshot.get('roster_total'))
        if any((snapshot.get('roster_filters') or {}).values()):
            stations += f" · filtered from {snapshot.get('roster_unfiltered_total', snapshot.get('roster_total', 0))}"
        items = 'Traffic ' + str(snapshot['table_traffic_total']) if 'table_traffic_total' in snapshot else count_caption('Traffic', snapshot.get('reports'), snapshot.get('total'))
        self.summary_label.setText(f"{stations} · {items} · Unreviewed {summary.get('unreviewed', 0)} · Follow-up {summary.get('follow_up_stations', 0)}")
        self.summary_label.setToolTip('Loaded pages / complete current-view totals. Unreviewed and follow-up counts cover this view, including older retained decisions. Items include messages and receipts; they are not query replies.')
        topics = summary.get('topics') or []
        self.topics_button.setText(f"Topics ({len(topics) + int(summary.get('other_topics') or 0)})")
        self._publish_topics(topics, int(summary.get('other_topics') or 0))

        self.next_button.setEnabled(snapshot.get('next_cursor') is not None)
        self.previous_button.setEnabled(bool(snapshot.get('has_previous', False)))
        self.roster_next_button.setEnabled(snapshot.get('roster_next_cursor') is not None)
        self.roster_previous_button.setEnabled(bool(snapshot.get('roster_has_previous')))
        report_pages = self.next_button.isEnabled() or self.previous_button.isEnabled()
        roster_pages = self.roster_next_button.isEnabled() or self.roster_previous_button.isEnabled()
        for button in (self.next_button, self.previous_button): button.setVisible(report_pages)
        for button in (self.roster_next_button, self.roster_previous_button): button.setVisible(roster_pages)
        for prefix in ('expect', 'entries', 'history'):
            next_button = getattr(self, prefix + '_next_button')
            previous_button = getattr(self, prefix + '_previous_button')
            next_button.setEnabled(snapshot.get(prefix + '_next_cursor') is not None)
            previous_button.setEnabled(bool(snapshot.get(prefix + '_has_previous')))
            for button in (next_button, previous_button):
                button.setVisible(next_button.isEnabled() or previous_button.isEnabled())
        writable = bool(snapshot.get('writable'))
        self.group_action_widget.setVisible(writable or bool(snapshot.get('can_undo')))
        self.summary_widget.setVisible(bool(snapshot.get('run') or snapshot.get('roster') or snapshot.get('reports')))
        self.send_button.setEnabled(writable)
        self.undo_button.setEnabled(bool(snapshot.get('can_undo')))
        for button in (self.query_group_button, self.query_station_button):
            button.setEnabled(writable)
            button.setToolTip('Send a specific form query' if writable else 'Start an active net to send a query')
        for combo, values, empty in ((self.roster_group_filter, snapshot.get('roster_groups', []), 'All groups'),
                                     (self.roster_state_filter, snapshot.get('roster_states', []), 'All states'),
                                     (self.roster_region_filter, snapshot.get('roster_regions', []), 'All regions')):
            selected = combo.currentData() or ''
            options = [(str(value) if value else 'Unknown', str(value) if value else '__UNKNOWN__') for value in values]
            if selected and selected not in {value for label, value in options}:
                options.append((str(selected), selected))
            current = [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]
            if current != [(empty, ''), *options]:
                combo.blockSignals(True); combo.clear(); combo.addItem(empty, '')
                for label, value in options: combo.addItem(label, value)
                combo.setCurrentIndex(max(0, combo.findData(selected))); combo.blockSignals(False)
        unreviewed = int((snapshot.get('summary') or {}).get('unreviewed') or 0)
        self.traffic_button.setText(f'Traffic ({unreviewed})' if unreviewed else 'Traffic')
        self.traffic_button.setAccessibleName(f'Traffic: {unreviewed} unreviewed items')
        self._publish_roster(list(snapshot.get('roster') or []), writable)
        self._publish_list(self.report_list, '_reports', list(snapshot.get('reports') or []),
                           lambda r: f"{self._utc_stamp(r.get('stamp'))} · {r.get('kind_label') or r.get('source_family') or 'Report'} · {str(r.get('traffic_summary') or r.get('summary') or r.get('body_preview') or '')[:120]}")
        self._filter_reports(self._selected_roster())
        if self._detail_open and self.report_list.currentRow() < 0 and self.report_list.count():
            self.report_list.setCurrentRow(0)
        if getattr(self, '_legacy_message_controls', True):
            self._publish_list(self.entry_list, '_entries', list(snapshot.get('entries') or []), lambda r: str(r.get('name') or 'Untitled'))
        else:
            self._entries = list(snapshot.get('entries') or [])
        self._quick_entries = list(snapshot.get('quick_entries', self._entries))
        self.saved_library.publish(self._quick_entries, search=snapshot.get('message_search', ''),
            more=bool(snapshot.get('message_search_more')), saved_forms=snapshot.get('saved_expect', ()))
        if getattr(self, '_workspace_mode', 'traffic') != 'library':
            self.saved_message_combo.publish_choices(self._quick_entries,
                search=snapshot.get('message_search', ''), more=bool(snapshot.get('message_search_more')))
        self.use_saved_message_button.setEnabled(bool(self._quick_entries))
        self.snr_button.setEnabled(writable)
        self.query_msgs_button.setEnabled(writable)
        self.target_station_button.setEnabled(bool(self._selected_roster()))
        self._publish_list(self.history_list, '_history', list(snapshot.get('history') or []), self._history_label)
        self._publish_activity(list(snapshot.get('history') or []), (snapshot.get('run') or {}).get('run_id'))
        self.empty_history_label.setVisible(not bool(snapshot.get('history')))
        self._roster_layout.setStretch(self._roster_spacer_index, 0)
        self.roster_table.setVisible(True)
        self.empty_roster_label.setText(self._empty_copy('stations'))
        self.empty_roster_label.setVisible(not self._roster)
        runs = list(snapshot.get('runs') or [])
        if runs != self._runs:
            current = (snapshot.get('run') or {}).get('run_id') or self.run_selector.currentData()
            self.run_selector.blockSignals(True); self.run_selector.clear()
            for run in runs:
                label = str(run.get('label') or run.get('name') or run.get('run_id') or 'Unnamed net')
                state = str(run.get('status') or run.get('state') or '')
                stamp = self._utc_stamp(run.get('started_ts')) if run.get('started_ts') else ''
                self.run_selector.addItem(' · '.join(part for part in (label, stamp, state) if part), run.get('run_id'))
            index = self.run_selector.findData(current)
            if index >= 0: self.run_selector.setCurrentIndex(index)
            self.run_selector.blockSignals(False)
            self._runs = runs
        run_id = (snapshot.get('run') or {}).get('run_id')
        run_index = self.run_selector.findData(run_id)
        if run_index >= 0:
            self.run_selector.blockSignals(True)
            self.run_selector.setCurrentIndex(run_index)
            self.run_selector.blockSignals(False)
        self.run_selector.setToolTip(self.run_selector.currentText())
        self._arrange_workspaces()
        saved = list(snapshot.get('saved_expect') or [])
        if saved != getattr(self, '_saved_expect', None):
            current = self.saved_expect_combo.currentData()
            self.saved_expect_combo.blockSignals(True)
            self.saved_expect_combo.clear()
            self.saved_expect_combo.addItem('Select saved Expect', None)
            for entry in saved: self.saved_expect_combo.addItem(str(entry.get('name') or entry.get('expect_key') or entry.get('id')), entry.get('id'))
            index = self.saved_expect_combo.findData(current)
            if index < 0 and current is not None:
                self.saved_expect_combo.addItem('Saved Expect unavailable', current)
                index = self.saved_expect_combo.count() - 1
            if index >= 0: self.saved_expect_combo.setCurrentIndex(index)
            self.saved_expect_combo.blockSignals(False)
            self._saved_expect = saved
        retained = {key: value for key, value in (snapshot.get('drafts') or {}).items()
                    if key != '__net_message_editor__' and isinstance(value, dict) and value.get('intent')}
        if retained != getattr(self, '_retained_form_drafts', None):
            selected = self.form_drafts_combo.currentData()
            self.form_drafts_combo.clear()
            for key, draft in retained.items():
                intent = draft.get('intent') or {}
                self.form_drafts_combo.addItem(' · '.join(str(x) for x in (
                    intent.get('expect_key'), intent.get('ncs_target') or intent.get('recipient_callsign')) if x), key)
            index = self.form_drafts_combo.findData(selected)
            if index >= 0:
                self.form_drafts_combo.setCurrentIndex(index)
            self._retained_form_drafts = retained
        self.clear_form_draft_button.setEnabled(bool(retained))
        self.form_working_copies.setVisible(bool(retained))
        run_id = (snapshot.get('run') or {}).get('run_id')
        if run_id != self._run_id and 'message_draft' in snapshot:
            self.restore_message_draft(snapshot.get('message_draft'))
        self._run_id = run_id
        if 'preview' in snapshot:
            matches = ('preview_identity' not in snapshot or
                       snapshot['preview_identity'] == self.preview_identity(self._action_draft()))
            self.preview.setPlainText(str(snapshot.get('preview') or '') if matches else '')
        self.state_presented.emit()

    @staticmethod
    def _utc_stamp(value):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).strftime('%Y-%m-%d %H:%MZ') if float(value) > 0 else 'Not reported'
        except (TypeError, ValueError, OSError, OverflowError):
            return 'Not reported'

    @staticmethod
    def _history_label(row):
        action = row.get('action') or {}
        if not isinstance(action, dict): action = {}
        return ' · '.join(str(value) for value in (NcsWorkbenchPanel._utc_stamp(row.get('created_ts')), activity_kind(row), activity_destination(row), submission_outcome_label(row)) if value)

    @staticmethod
    def _activity_stamp(value):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ') if float(value) > 0 else 'Not reported'
        except (TypeError, ValueError, OverflowError, OSError):
            return 'Not reported'

    @staticmethod
    def _activity_identity(row, index):
        return row.get('sequence') or row.get('intent_id') or ('loaded-row', index)

    def _publish_activity(self, rows, run_id):
        if rows == self._activity_rows and run_id == self._activity_run:
            return
        old_index = self.activity_table.currentRow()
        selected = self._activity_identity(self._activity_rows[old_index], old_index) if 0 <= old_index < len(self._activity_rows) and run_id == self._activity_run else None
        scroll = self.activity_table.verticalScrollBar().value()
        self.activity_table.blockSignals(True)
        self.activity_table.clearSelection()
        self.activity_table.setCurrentCell(-1, -1)
        self.activity_table.setRowCount(len(rows))
        self._activity_rows = list(rows)
        same_run = self._activity_run == run_id
        self._activity_run = run_id
        chosen = -1
        for index, row in enumerate(rows):
            content = activity_content(row)
            values = (self._activity_stamp(row.get('created_ts')), activity_kind(row),
                      activity_destination(row), ' '.join(content.split()), submission_outcome_label(row))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(content if column == 3 else str(value))
                self.activity_table.setItem(index, column, item)
            view = QPushButton('View')
            view.setAccessibleName('View outgoing activity ' + str(row.get('sequence') or index + 1))
            view.clicked.connect(lambda checked=False, position=index: self._view_activity(position))
            self.activity_table.setCellWidget(index, 5, view)
            if selected == self._activity_identity(row, index):
                chosen = index
        if chosen >= 0:
            self.activity_table.selectRow(chosen)
        self.activity_table.verticalScrollBar().setValue(scroll if same_run else 0)
        self.activity_table.blockSignals(False)
        if chosen < 0:
            self.activity_details.hide()
            self.activity_detail_text.clear()
        elif not self.activity_details.isHidden():
            self._activity_selected()

    def _view_activity(self, row):
        if not 0 <= row < len(self._activity_rows):
            return
        self.activity_table.selectRow(row)
        self.activity_details.show()
        self._activity_selected()

    def _activity_selected(self):
        index = self.activity_table.currentRow()
        if self.activity_details.isHidden() or not 0 <= index < len(self._activity_rows):
            return
        row = self._activity_rows[index]
        action = activity_action(row)
        fields = [('Time · UTC', self._activity_stamp(row.get('created_ts'))),
                  ('Action', activity_kind(row)), ('To', activity_destination(row)),
                  ('Result', submission_outcome_label(row)),
                  ('Recorded outcome time · UTC', self._activity_stamp(row.get('completed_ts')) if row.get('completed_ts') else ''),
                  ('Content', activity_content(row)),
                  ('Diagnostic', '' if row.get('outcome') == 'accepted' else str(row.get('detail') or 'No additional diagnostic recorded.'))]
        if action.get('ack_calls') and isinstance(action['ack_calls'], (list, tuple)):
            fields.append(('Acknowledged stations', ', '.join(str(call) for call in action['ack_calls'])))
        self.activity_detail_text.setPlainText('\n\n'.join(f'{label}: {value}' for label, value in fields if value))

    def _publish_list(self, widget, attr, rows, label):
        old = getattr(self, attr, [])
        if old == rows: return
        old_row = widget.currentRow()
        key = None
        if 0 <= old_row < len(old):
            key = old[old_row].get('canonical_key') or old[old_row].get('entry_id') or old[old_row].get('message_id')
        scroll = widget.verticalScrollBar().value()
        widget.blockSignals(True); widget.clear()
        for row in rows:
            item = QListWidgetItem(label(row))
            if widget is self.report_list:
                item.setIcon(control_icon('flag-filled' if row.get('follow_up') else 'flag', active_app_theme(), max(12, self.fontMetrics().height())))
                item.setToolTip(f"{row.get('from_call') or 'Unknown station'} · {'Follow-up needed' if row.get('follow_up') else 'No follow-up flag'} · {row.get('older_follow_up', 0)} older follow-ups")
            if widget is self.history_list:
                action = row.get('action') or {}
                item.setToolTip(str(action.get('content') or row.get('content') or label(row)) if isinstance(action, dict) else label(row))
            widget.addItem(item)
        setattr(self, attr, rows)
        index = next((i for i, r in enumerate(rows) if key and key in (r.get('canonical_key'), r.get('entry_id'), r.get('message_id'))), -1)
        if index >= 0: widget.setCurrentRow(index)
        widget.verticalScrollBar().setValue(scroll); widget.blockSignals(False)
        if widget is self.report_list: self._report_selected(index)

    @staticmethod
    def _rx_snr_caption(row):
        try:
            snr = float(row.get('rx_snr'))
            if not math.isfinite(snr):
                raise ValueError('Nonfinite SNR')
        except (TypeError, ValueError, OverflowError):
            return '—', 'Local RX SNR not reported for this link.'
        sender = str(row.get('rx_snr_sender') or row.get('from_call') or '').strip().upper()
        relayed = sender != str(row.get('from_call') or '').strip().upper()
        text = f'{snr:+g} dB' + (f' via {sender}' if relayed else '')
        detail = f'Latest local reception from {sender}: {snr:+g} dB.'
        if relayed:
            detail += ' Relay-to-local-receiver link; not the original author’s direct signal.'
        detail += ' Observed: ' + NcsWorkbenchPanel._activity_stamp(row.get('rx_snr_received_ts')) + '.'
        return text, detail

    def _publish_roster(self, rows, writable):
        if rows == self._roster and writable == getattr(self, '_roster_writable', None): return
        selected = self._selected_roster() or {}
        call = selected.get('from_call') or selected.get('callsign')
        scroll = self.roster_table.verticalScrollBar().value()
        self.roster_table.blockSignals(True); self.roster_table.clearSelection(); self.roster_table.setCurrentCell(-1, -1); self.roster_table.setRowCount(len(rows)); self._roster = rows
        for i, row in enumerate(rows):
            station = str(row.get('from_call') or row.get('callsign') or 'Unknown')
            attention = []
            if row.get('follow_up'): attention.append('Follow-up')
            if row.get('older_follow_up'): attention.append(f"{row['older_follow_up']} older follow-up")
            if not row.get('reviewed'): attention.append('New / updated')
            summary = row.get('decoded_summary') or row.get('traffic_summary') or (row.get('intelligence') or {}).get('summary') or row.get('summary') or row.get('body_preview') or 'No indexed traffic; receiver metadata only'
            latest = ' · '.join(str(v) for v in (row.get('kind_label'), self._utc_stamp(row.get('stamp')) if row.get('stamp') else '') if v)
            values = (station, row.get('roster_state') or '—', row.get('roster_region') or '—',
                      ', '.join(row.get('roster_groups') or []) or '—', latest, summary,
                      self._topics(row.get('topics')), ', '.join(attention), row.get('traffic_count', 0))
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.roster_table.setItem(i, col, item)
            snr_text, snr_detail = self._rx_snr_caption(row)
            snr_item = QTableWidgetItem(snr_text)
            snr_item.setToolTip(snr_detail)
            self.roster_table.setItem(i, 11, snr_item)
            view = QPushButton('View')
            view.setAccessibleName('View traffic from ' + station)
            view.clicked.connect(lambda checked=False, item=dict(row): self._view_station(item))
            self.roster_table.setCellWidget(i, 9, view)
            excluded = bool(row.get('station_excluded') or row.get('excluded'))
            verb = 'Restore' if excluded else 'Exclude'
            button = QToolButton()
            button.setIcon(control_icon('resume' if excluded else 'list-x', active_app_theme(), max(12, self.fontMetrics().height())))
            button.setToolTip(f'{verb} {station}'); button.setAccessibleName(f'{verb} {station}')
            button.clicked.connect(lambda checked=False, item=dict(row), action=verb.lower(): self.action_requested.emit(action + '_station', item))
            self.roster_table.setCellWidget(i, 10, button)
            if row.get('decoded_summary'):
                self.roster_table.item(i, 5).setToolTip('Latest decoded Spotter/CommStat report · ' + self._utc_stamp(row.get('decoded_stamp')) + '\n' + str(row['decoded_summary']))
            self.roster_table.item(i, 0).setToolTip(str(row.get('roster_name') or station))
            self.roster_table.item(i, 1).setToolTip(str(row.get('roster_state_source') or 'HF Callsigns roster; unknown when not recorded'))
            self.roster_table.item(i, 2).setToolTip('FEMA region derived from displayed state. ' + str(row.get('roster_state_source') or 'State unknown'))
            self.roster_table.item(i, 3).setToolTip(', '.join(row.get('roster_groups') or []) or 'No roster groups recorded')
            if station == call: self.roster_table.selectRow(i)
        self.roster_table.verticalScrollBar().setValue(scroll); self.roster_table.blockSignals(False)
        self._roster_writable = writable
        self._refresh_action_icons()

    @staticmethod
    def _topic_icon(topic):
        text = str(topic).casefold()
        for token, name in (('power', 'power'), ('water', 'water'), ('medical', 'medical'),
                            ('communication', 'communications'), ('comms', 'communications')):
            if token in text:
                return 'topic-' + name
        return 'topic-unknown'

    def _publish_topics(self, topics, other):
        value = (topics, other)
        if value == self._summary_topics:
            return
        self._summary_topics = value
        while self.topic_summary_grid.count():
            widget = self.topic_summary_grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self._topic_widgets = []
        for index, row in enumerate(topics):
            cell = QWidget()
            layout = QHBoxLayout(cell)
            layout.setContentsMargins(0, 0, 0, 0)
            icon, caption = QLabel(), QLabel(f"{row['topic']}: {row['items']} items")
            caption.setTextFormat(Qt.TextFormat.PlainText)
            caption.setWordWrap(True)
            layout.addWidget(icon)
            layout.addWidget(caption, 1)
            cell.setToolTip('Shared topic classification and evidence; distinct traffic items, not stations or confirmed incidents.')
            self.topic_summary_grid.addWidget(cell, index // 2, index % 2)
            self._topic_widgets.append((icon, self._topic_icon(row['topic'])))
        if not topics or other:
            label = QLabel(f'{other} more topics' if other else 'No topics reported')
            self.topic_summary_grid.addWidget(label, (len(topics)+1)//2, 0, 1, 2)
        self._refresh_action_icons()

    def _refresh_action_icons(self):
        if not hasattr(self, 'report_buttons'):
            return
        self._style_workspace_navigation()
        theme, size = active_app_theme(), max(12, self.fontMetrics().height())
        key = tuple(sorted(theme.items()))
        if hasattr(self, 'send_button') and key != getattr(self, '_primary_button_theme', None):
            self.send_button.setStyleSheet(button_style('primary', theme))
            self.saved_library.save_button.setStyleSheet(button_style('primary', theme))
            self._primary_button_theme = key
        for label, name in self._topic_widgets:
            label.setPixmap(control_icon(name, theme, size).pixmap(size, size))
        selected = self._selected_report() or {}
        self.report_exclude_button.setIcon(control_icon('resume' if selected.get('report_excluded') else 'list-x', theme, size))
        for action, name in (('review', 'review-check'), ('open_report', 'document-open'),
                             ('follow_up', 'flag-filled' if selected.get('follow_up') else 'flag')):
            self.report_buttons[action].setIcon(control_icon(name, theme, size))
        self.undo_button.setIcon(control_icon('resume', theme, size))
        for button in (self.query_group_button, self.query_station_button):
            button.setIcon(control_icon('JS8', theme, size))
        for index, row in enumerate(self._roster):
            button = self.roster_table.cellWidget(index, 10)
            if button is not None:
                button.setIcon(control_icon('resume' if row.get('station_excluded') or row.get('excluded') else 'list-x', theme, size))
            item = self.roster_table.item(index, 7)
            if item is not None:
                item.setIcon(control_icon('flag-filled' if row.get('follow_up') or row.get('older_follow_up') else 'flag', theme, size))
        for index, row in enumerate(self._reports):
            item = self.report_list.item(index)
            if item is not None:
                item.setIcon(control_icon('flag-filled' if row.get('follow_up') else 'flag', theme, size))

    def _fit_report_list(self):
        # This is a report selector, not a second full-height reading pane.
        self.report_list.setMaximumHeight(self.report_list.fontMetrics().lineSpacing() * 5
                                         + 2 * self.report_list.frameWidth())

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._refresh_action_icons()
            self._fit_report_list()
            self._fit_message_widgets()

    def set_workspace_width(self, width):
        self._workspace_width = width
        # The host viewport is authoritative. A horizontal splitter's aggregate
        # child minimum must not prevent the resize needed to stack its panes.
        orientation = (Qt.Orientation.Horizontal if width >= self.fontMetrics().horizontalAdvance('M') * 66
                       else Qt.Orientation.Vertical)
        compact = width < self.fontMetrics().horizontalAdvance('M') * 66
        direction = QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
        for row in (self._controls_layout, self._roster_filter_layout):
            if row.direction() != direction:
                row.setDirection(direction)
        mode = bool(compact)
        if not getattr(self, '_external_navigation', False) and getattr(self, '_disclosures_compact', None) != mode:
            self._disclosures_compact = mode
            self._disclosures_layout.addWidget(self.send_toggle, 0, 0)
            self._disclosures_layout.addWidget(self.manage_messages_toggle, 0, 1)
            self._disclosures_layout.addWidget(self.sent_activity_toggle, 1 if compact else 0, 0 if compact else 2)
        self._compact_workspace = compact
        self._tx_focus_mode = False
        self._compose_wide = width >= self.fontMetrics().horizontalAdvance('M') * 108
        self._arrange_workspaces()
        tx_orientation = Qt.Vertical if compact else Qt.Horizontal
        if self.transmit_workspace.orientation() != tx_orientation:
            self.transmit_workspace.setOrientation(tx_orientation)
        for splitter in (self.splitter,):
            if splitter.orientation() != orientation:
                splitter.setOrientation(orientation)
                self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.set_workspace_width(min(self.width(), getattr(self, "_workspace_width", self.width())))
