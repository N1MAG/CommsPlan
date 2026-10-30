from __future__ import annotations

import datetime
import math
import re
import sqlite3
import time
import json
import queue
import uuid
from pathlib import Path
from typing import List, Dict, Set, Optional

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget,
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QMessageBox,
    QComboBox,
    QApplication,
    QSpinBox,
    QCompleter,
    QTableWidget,
    QTableWidgetItem,
    QSizePolicy,
    QScrollArea,
    QMenu,
    QToolButton,
)

from freqinout.core.net_schedule_occurrences import next_net_occurrence
from freqinout.core.schedule_targeting import schedule_row_matches_target_context
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.software_status_service import SoftwareStatusService
from freqinout.core.logger import log
from freqinout.core.perf_metrics import span as perf_span
from freqinout.core.checkins_db import ensure_operator_checkins_schema
from freqinout.core.js8_spotter_forms import (
    MAPPER_SETTINGS_KEY,
    PURPOSE_NET_CHECKIN,
    PURPOSE_NET_NOTIFICATION,
    discover_effective_spotter_forms,
    resolve_net_form_options,
    extract_form_codes,
    forms_enabled_for,
    legacy_default_forms_for,
)
from freqinout.core.message_ingest import MessageIngestor
from freqinout.core.ingest_runtime_status import active_runtime_ingest_inventory
from freqinout.core.js8_ncs_offsets import ncs_offset_keys_for_directed_path
from freqinout.core.js8_send_service import (
    js8_profile_allows_transmit,
    js8_speed_name,
    send_js8_message_guarded,
)
from freqinout.core.js8_source_context import resolve_js8_source_context
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.ncs_session_contract import NcsSessionSnapshot, write_ncs_session_snapshot
from freqinout.core.ncs_workbench_model import is_incidental_net_traffic, net_traffic_payload
from freqinout.utils.timezones import get_timezone
from freqinout.radio_interface.js8_api_client import JS8ApiClientRegistry, JS8ApiEndpoint
from freqinout.radio_interface.js8_rx_hub import JS8RxHub
from freqinout.gui.qsy_helper import (
    load_operating_groups as qsy_load_operating_groups,
    snapshot_operating_groups as qsy_snapshot_operating_groups,
    build_qsy_options,
    refresh_qsy_combo,
    refresh_hold_duration_combo,
    selected_qsy_meta,
    selected_hold_duration,
    set_hold_duration_default,
    notify_hold_duration_default_changed,
    current_scheduler_freq,
    perform_qsy,
    perform_qsy_with_hold,
    get_suspend_until,
    set_suspend_until,
    suspend_snapshot,
    suspend_active,
    scheduler_enabled,
    resume_schedule_hold,
    active_hold_button_role,
    active_hold_button_text,
    active_hold_status_text,
)
from freqinout.core.group_utils import normalize_group_name
from freqinout.core.config_paths import get_config_dir
from freqinout.gui.theme import (
    resolve_theme, active_app_theme, button_style, fit_child_combo_boxes, fit_combo_box_to_contents,
    font_derived_widget_height,
)


def _nets_db_path() -> Path:
    return get_config_dir() / "config" / "freqinout_nets.db"

# Vendored js8net (replacement for pyjs8call)
JS8NET_PATH = Path(__file__).resolve().parents[2] / "third_party" / "js8net" / "js8net-main"
if JS8NET_PATH.exists():
    import sys
    sys.path.insert(0, str(JS8NET_PATH))
try:
    import js8net  # type: ignore
except Exception:
    js8net = None

AUTO_GRID_QUIET_SECS = 90  # idle time required since last RX from a station before sending GRID?
CHECKIN_FORMS = {"F!103", "F!104"}
ANNOUNCE_FORM = "F!106"  # JS8Spotter net announcement


class JS8CallNetControlTab(QWidget):
    """
    JS8Call Net Control tab.

    Uses JS8Call's DIRECTED.TXT and net_schedule to manage JS8 nets:

    - Settings:
        * callsign, operator_name, operator_state
        * js8_directed_path: full path to JS8Call DIRECTED.TXT
        * js8_refresh_sec: poll interval (seconds)
    - Single Check-Ins table with per-call metadata (mode, SNR, offset, status).

    Buttons:
        Start Net, ACK Check-ins, Set Group, Set Spotter, Group Spotter,
        Single Spotter, Save Checkins, End Net, QSY/Suspend (shared across tabs)

    - End Net:
        * Stops polling
        * Writes log file in 'net_logs' under DIRECTED.TXT's directory
          filename: netname-ROLE-YYYYMMDD.txt
          header: net name, role, start/end UTC, band (if found)
          body: full callsigns, one per line

    - Auto-prefill Net Name:
        * Uses net_schedule and current UTC time
        * Looks for nets on current day whose (start_utc - early_checkin)
          is within next 20 minutes.
        * Prefills net name if the field is empty and no net is in progress.
    """
    form_tools_ready = Signal(object, object, object)
    roster_groups_ready = Signal(object, object, object)
    net_status_changed = Signal(str, bool)
    net_session_status_changed = Signal(str, bool, int, str)

    def __init__(self, parent=None, *, preparation_settings=None):
        super().__init__(parent)
        self._roster_groups_inflight = False
        self._roster_group_names = ()
        self._roster_groups_checked = 0.0
        self._roster_groups_cache_scope = None
        self._query_destination_session = None
        self.roster_groups_ready.connect(self._publish_roster_groups)
        self._form_tools_executor = None
        self._form_tools_inflight = False
        self._form_mappings_generation = 0
        self._form_mappings_dirty = False
        self.form_tools_ready.connect(self._publish_form_tools)
        self._preparation_shell = preparation_settings is not None
        self._preparation_scope = None
        self.settings = preparation_settings if preparation_settings is not None else SettingsManager()
        self._status_service = SoftwareStatusService(self.settings)

        self._net_in_progress = False
        self._net_start_utc: str | None = None
        self._net_end_utc: str | None = None
        self._ncs_session_radio_id: Optional[int] = None
        self._ncs_session_id: str = ""

        self._directed_path: Path | None = None
        self._last_directed_size: int = 0
        self._startup_directed_size: int = 0
        self._directed_offset_key: str = ""
        self._all_offset_key: str = ""

        self._all_calls_seen: Set[str] = set()
        self._queried_msg_ids: Set[str] = set()
        self._pending_queries: List[tuple[Optional[float], Optional[int], str, str]] = []
        self._waiting_for_completion: bool = False
        self._current_query: tuple[str, str] | None = None
        self._js8_client = None
        self._js8_net_started = False
        self.auto_query_msg_id = bool(self.settings.get("js8_auto_query_msg_id", False))
        self.auto_query_grids = bool(self.settings.get("js8_auto_query_grids", False))
        self._js8_rx_timer: QTimer | None = None
        self._js8_rx_hub: JS8RxHub | None = None
        self._js8_rx_registered = False
        self._js8_live_source_context_cache: Dict[str, object] = {
            "endpoint": "",
            "expires": 0.0,
            "context": {},
        }
        self._last_rx_ts: float = 0.0
        self._pending_grid_queries: List[tuple[Optional[float], str]] = []
        self._grid_waiting: bool = False
        self._grid_last_rx_ts: float = 0.0
        self._last_directed_size: int = 0
        self._last_all_size: int = 0
        self._last_query_tx_ts: float = 0.0
        self._app_start_ts: float = time.time()
        self._last_tx_ts: float = 0.0
        # Track inbound triggers to map replies to groups
        self._last_inbound_triggers: Dict[str, tuple[str, float]] = {}
        self._auto_inserted_callsigns: Set[str] = set()
        self._operator_schema_ready = False
        self._awaiting_ack_for: Optional[str] = None
        self._call_last_rx_ts: Dict[str, float] = {}
        self._last_traffic_to_me_ts: float = 0.0
        self._last_traffic_group_ts: float = 0.0
        self._last_traffic_by_call_ts: Dict[str, float] = {}
        self._msg_defer_last_log: Dict[str, float] = {}
        self._grid_defer_last_log: Dict[str, float] = {}
        self._grid_defer_start_ts: Dict[str, float] = {}
        self._auto_query_paused_by_net = False
        theme = resolve_theme(self.settings)
        self._start_btn_default_style = button_style("success", theme)
        self._end_btn_default_style = button_style("danger", theme)

        # Check-in table state
        self._checkins: Dict[str, Dict] = {}
        self._checkin_rows: Dict[str, int] = {}
        self._checkins_saved: Set[str] = set()
        self._group_target: str = ""
        self._spotter_form: Optional[str] = None
        self._available_net_form_codes = set()
        self._expected_form: Optional[str] = None
        self._status_mismatch: Dict[str, bool] = {}
        self._pending_announcements: Dict[str, float] = {}  # callsign -> ts waiting for completion
        self._recent_announcements: Dict[str, float] = {}  # callsign -> last popup ts
        self._backlog_loaded: bool = False
        self._awaiting_msg_responses: Dict[tuple[str, str], float] = {}  # (call, msg_id) -> expiry ts
        self._awaiting_grid_responses: Dict[str, float] = {}  # call -> expiry ts
        self._current_query_sent_ts: float = 0.0
        self._qsy_options: Dict[str, Dict] = {}
        self._opgroups_sig: str = ""
        self._call_last_speed: Dict[str, int] = {}
        self._is_shutting_down = False
        self._polling_directed = False
        self._polling_rx = False
        self._active = False

        # FCC enrichment is deliberately passive: it only fills blank
        # check-in name/state fields after the row already exists.  The
        # broker owns the worker lane and its memory cache; this tab owns only
        # session-scoped pending identity application.
        from freqinout.gui.fcc_identity_lookup import identity_broker
        self._fcc_identity_broker = identity_broker()
        self._fcc_identity_broker.identities_ready.connect(self._on_fcc_identities_ready)
        self._fcc_identity_pending: Dict[str, tuple[str, Optional[int], bool]] = {}

        self._poll_timer: QTimer | None = None
        self._clock_timer: QTimer | None = None
        self._js8_rx_timer: QTimer | None = None

        self._build_ui()
        self._apply_theme()
        if not self._preparation_shell:
            self._load_settings()
            self._setup_timer()
            self._setup_clock_timer()
            self._update_clock_labels()
            self._setup_js8_rx_timer()
            self._update_suspend_state()
            self._refresh_auto_query_flags()
            self._refresh_qsy_options()
        from freqinout.gui.net_prepared_receiver import PreparedReceiver
        self._prepared_receiver = PreparedReceiver(self, "JS8", writer=getattr(preparation_settings, "writer", None))
        from freqinout.gui.ncs_workbench_panel import NcsWorkbenchPanel
        from freqinout.gui.ncs_workbench_controller import NcsWorkbenchController
        self.workbench_panel = NcsWorkbenchPanel(self)
        self._workbench_layout.insertWidget(0, self.workbench_panel, 1)
        self._mount_workbench_rail()
        # Reuse the original buttons and guarded handlers at their target.
        self._js8_action_row.removeWidget(self.ack_btn)
        self._js8_action_row.removeWidget(self.ack_callsign_btn)

        self.query_group_selector = QComboBox()
        self.query_group_selector.setEditable(True)
        self.query_group_selector.setInsertPolicy(QComboBox.NoInsert)
        self.query_group_selector.setProperty('fio_no_auto_fit', True)
        self.query_group_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.query_group_selector.setMinimumContentsLength(12)
        self.query_group_selector.setAccessibleName('Expect query group destination')
        self.query_group_selector.setToolTip('HF roster group to query. Selection sends nothing and does not change net identity.')
        self.query_group_selector.lineEdit().textChanged.connect(self._update_query_controls)
        self.query_context_widget = QWidget()
        self._query_context_grid = QGridLayout(self.query_context_widget)
        self._query_context_grid.setContentsMargins(0, 0, 0, 0)
        self._query_group_label = QLabel('Query group')
        self._query_form_label = QLabel('Form')
        self.workbench_panel.query_picker_layout.addWidget(self.query_context_widget)
        self.spotter_combo.setPlaceholderText('Query form (optional)')
        self.spotter_combo.show()
        self.review_form_tools_btn.setText('Reload forms')
        self.review_form_tools_btn.setToolTip('Reload available optional query forms; selection sends nothing')
        self.review_form_tools_btn.hide()
        self._mount_live_query_actions()
        self.workbench = NcsWorkbenchController(self, self.workbench_panel)
        self.workbench_panel.state_presented.connect(self._update_workspace_presentation)
        self.workbench_panel.workspace_changed.connect(self._ncs_workspace_changed)
        self.workbench_panel.editor_reveal_requested.connect(self._reveal_saved_message_editor)
        self._ncs_workspace_scroll = {}
        self._ncs_workspace_mode = 'traffic'
        self._prepared_receiver.changed.connect(self._update_workspace_presentation)
        self._update_workspace_presentation()
        self._update_checkin_empty_state()


    def _reveal_saved_message_editor(self):
        def reveal():
            if self._is_shutting_down or not self.workbench_panel.manage_messages_toggle.isChecked():
                return
            editor = self.workbench_panel.saved_library.editor
            area = self.js8_ncs_scroll_area
            top = editor.mapTo(area.widget(), editor.rect().topLeft()).y()
            if editor.height() > area.viewport().height():
                area.verticalScrollBar().setValue(top)
            else:
                area.ensureWidgetVisible(editor, 0, 0)
        QTimer.singleShot(0, reveal)

    def _ncs_workspace_changed(self, mode):
        bar = self.js8_ncs_scroll_area.verticalScrollBar()
        old = getattr(self, '_ncs_workspace_mode', 'traffic')
        cache = getattr(self, '_ncs_workspace_scroll', {})
        cache[old] = bar.value()
        self._ncs_workspace_scroll = cache
        self._ncs_workspace_mode = mode
        if hasattr(self, 'workspace_menu_button'):
            self.workspace_menu_button.setText('Net workspace')
        def position():
            if self._is_shutting_down or self._ncs_workspace_mode != mode:
                return
            if mode == 'traffic' and mode in cache:
                bar.setValue(cache[mode])
            elif mode in {'compose', 'library'}:
                bar.setValue(0)
            else:
                bar.setValue(0)
        QTimer.singleShot(0, position)

    def _mount_workbench_rail(self):
        panel = self.workbench_panel
        panel._external_navigation = True
        panel._legacy_message_controls = False
        panel.navigation_widget.hide()
        self._rail_workspace = QWidget(self.net_action_rail)
        self._rail_workspace.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        rows = QVBoxLayout(self._rail_workspace)
        rows.setContentsMargins(0, 0, 0, 0)
        for button in (panel.manage_messages_toggle, panel.sent_activity_toggle):
            rows.addWidget(button)
            button.show()
        self.workspace_menu_button = QPushButton('Traffic')
        self.workspace_menu = QMenu(self.workspace_menu_button)
        for label, button in (('Traffic', panel.traffic_button),
                              ('Saved messages', panel.manage_messages_toggle), ('Sent activity', panel.sent_activity_toggle)):
            action = self.workspace_menu.addAction(label)
            action.triggered.connect(lambda checked=False, control=button:
                panel.show_traffic() if control is panel.traffic_button else control.setChecked(True))
        self.workspace_menu_button.setMenu(self.workspace_menu)
        self.workspace_menu_button.setAccessibleName('NCS workspace')
        self.workspace_menu_button.hide()
        rows.insertWidget(0, self.workspace_menu_button)
        rows.insertWidget(1, panel.traffic_button)
        self.query_menu_button = QPushButton('Query…')
        self.query_menu = QMenu(self.query_menu_button)
        self.query_menu.aboutToShow.connect(self._populate_net_query_menu)
        self.query_menu_button.setMenu(self.query_menu)
        self.query_menu_button.hide()
        self._secondary_actions_button = QPushButton('Tools…')
        tools = QMenu(self._secondary_actions_button)
        self._secondary_actions_button.setMenu(tools)
        tools.addAction('Reload forms').triggered.connect(self.review_form_tools_btn.click)
        self._topics_action = tools.addAction('Topics')
        self._topics_action.setCheckable(True)
        self._topics_action.triggered.connect(panel.topics_button.setChecked)
        panel.topics_button.toggled.connect(self._topics_action.setChecked)
        tools.addAction('Refresh traffic').triggered.connect(panel.refresh_button.click)
        self._records_menu = tools.addMenu('Open net record…')
        self._records_menu.aboutToShow.connect(self._populate_record_menu)
        rows.addWidget(self._secondary_actions_button)
        panel.topics_button.hide()
        panel.run_controls_widget.hide()
        self.net_actions_menu.addMenu(tools)
        # ACK controls join the direct operational action strip after construction.
        self._net_rail_layout.insertWidget(1, self._rail_workspace)
        # Lifecycle → activity → reusable configuration. Reuse command objects
        # and original guarded handlers; only their presentation owner changes.
        for control in (self.net_actions_more, self.net_action_buttons, self._rail_workspace, self.net_setup_toggle):
            self._net_rail_layout.removeWidget(control)
        self._net_rail_layout.insertWidget(0, self.net_actions_more)
        self._net_rail_layout.insertWidget(1, self.net_action_buttons)
        self._rail_separators = []
        for position in (2, 3):
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setFrameShadow(QFrame.Shadow.Sunken)
            self._net_rail_layout.insertWidget(position, line)
            self._rail_separators.append(line)
        self._net_rail_layout.insertWidget(3, self._rail_workspace)
        self._rail_configuration = QWidget()
        self._rail_configuration.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        configuration = QVBoxLayout(self._rail_configuration)
        configuration.setContentsMargins(0, 0, 0, 0)
        for control in (panel.manage_messages_toggle, self._secondary_actions_button, self.net_setup_toggle):
            rows.removeWidget(control)
            configuration.addWidget(control)
        self._net_rail_layout.insertWidget(5, self._rail_configuration)
        self._rail_workspace.setAccessibleName('Net activity')
        self._rail_configuration.setAccessibleName('Configuration and tools')
        panel.group_context_label.hide()
        panel.traffic_button.hide()
        # Hidden compatibility buttons keep the original guarded handlers/state.
        for button in (panel.query_group_button, panel.query_station_button, panel.undo_button):
            button.setParent(self.net_action_rail)
            button.hide()
        for button in (self.net_setup_toggle, panel.manage_messages_toggle, panel.sent_activity_toggle):
            action = self.net_actions_menu.addAction(button.text())
            action.triggered.connect(button.click)
            self._net_menu_actions.append((action, button))
        self._compact_ack_menu = self.net_actions_menu.addMenu('Acknowledge')
        self._compact_ack_menu.aboutToShow.connect(self._populate_compact_ack_menu)
        self._undo_net_action = self.net_actions_menu.addAction('Undo last exclusion')
        self._undo_net_action.triggered.connect(panel.undo_button.click)

    def _populate_record_menu(self):
        self._records_menu.clear()
        for index in range(self.workbench_panel.run_selector.count()):
            selector = self.workbench_panel.run_selector
            action = self._records_menu.addAction(selector.itemText(index))
            action.triggered.connect(lambda checked=False, ident=selector.itemData(index):
                self.workbench_panel.action_requested.emit('open_run', ident))
        if not self._records_menu.actions():
            self._records_menu.addAction('No retained nets').setEnabled(False)

    def _mount_live_query_actions(self):
        panel = self.workbench_panel
        self.live_query_widget = QWidget()
        self._live_query_layout = QGridLayout(self.live_query_widget)
        self._live_query_layout.setContentsMargins(0, 0, 0, 0)
        self.query_target_mode = QComboBox()
        self.query_target_mode.addItem('Group', 'group')
        self.query_target_mode.addItem('Selected station', 'station')
        self.query_target_mode.setAccessibleName('Query destination type')
        self.query_target_label = QLabel()
        self.query_target_label.setWordWrap(True)
        self.live_query_buttons = []
        for label, command in [('Expect query', 'expect'), ('SNR?', 'SNR?'), ('QUERY MSGS', 'QUERY MSGS')]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, cmd=command: self._send_live_query(cmd))
            self.live_query_buttons.append(button)
        panel.query_picker_layout.addWidget(self.live_query_widget)
        self.query_target_mode.currentIndexChanged.connect(self._update_live_query_target)
        self.query_group_selector.currentTextChanged.connect(self._update_live_query_target)
        panel.roster_table.itemSelectionChanged.connect(self._update_live_query_target)
        self._update_live_query_target()

    def _update_live_query_target(self):
        if not hasattr(self, 'query_target_mode'):
            return
        mode = self.query_target_mode.currentData()
        station = self.workbench_panel._selected_roster() or {}
        target = self._query_group_destination() if mode == 'group' else str(station.get('from_call') or station.get('callsign') or '')
        self.query_target_label.setText(('Group: ' if mode == 'group' else 'Station: ') + (normalize_group_name(target) if mode == 'group' else target or 'Select a station'))
        writable = bool(self._net_in_progress and self.workbench_panel._snapshot.get('writable'))
        for button in self.live_query_buttons:
            button.setEnabled(writable and bool(target))
            button.setToolTip('Send to ' + target if writable and target else 'Start an active net and select a destination')
        expect = self.live_query_buttons[0]
        available = self._spotter_form in self._available_net_form_codes
        expect.setEnabled(writable and bool(target) and available)
        if writable and target and not available:
            expect.setToolTip('Select an available form to send an Expect query')

    def _send_live_query(self, command):
        mode = self.query_target_mode.currentData()
        station = dict(self.workbench_panel._selected_roster() or {})
        target = self._query_group_destination() if mode == 'group' else str(station.get('from_call') or station.get('callsign') or '')
        self._dispatch_menu_query(command, mode, target, station, self._ncs_session_id, self.workbench.generation)

    def _populate_compact_ack_menu(self):
        self._compact_ack_menu.clear()
        run_id = self._ncs_session_id
        for target in self.workbench_panel._snapshot.get('query_groups', ()):
            action = self._compact_ack_menu.addAction('Group: ' + normalize_group_name(target))
            action.setEnabled(self.ack_btn.isEnabled())
            action.triggered.connect(lambda checked=False, destination=target, ident=run_id:
                self.workbench.acknowledge(destination=destination, expected_run=ident))
        action = self._compact_ack_menu.addAction('Selected station')
        action.setEnabled(self.ack_callsign_btn.isEnabled() and bool(self.workbench_panel._selected_roster()))
        action.triggered.connect(self.ack_callsign_btn.click)

    def _dispatch_menu_query(self, command, mode, target, station, session, generation):
        panel = self.workbench_panel
        selected = panel._selected_roster() or {}
        current = self._query_group_destination() if mode == 'group' else str(selected.get('from_call') or selected.get('callsign') or '')
        if (session != self._ncs_session_id or generation != self.workbench.generation or target != current):
            self.workbench._status('Destination changed. Open Query again to review the current target.')
            return
        if command == 'expect':
            (panel.query_group_button if mode == 'group' else panel.query_station_button).click()
        else:
            payload = dict(panel._draft(), target_mode=mode, target_station=dict(station), command=command)
            panel.action_requested.emit('utility_query', payload)

    def _reflow_query_context(self, compact):
        if not hasattr(self, '_query_context_grid') or compact == getattr(self, '_query_context_compact', None):
            return
        self._query_context_compact = compact
        grid = self._query_context_grid
        while grid.count(): grid.takeAt(0)
        for column in range(5): grid.setColumnStretch(column, 0)
        grid.addWidget(self._query_group_label, 0, 0)
        grid.addWidget(self.query_group_selector, 0, 1, 1, 2 if compact else 1)
        grid.addWidget(self._query_form_label, 1 if compact else 0, 0 if compact else 2)
        grid.addWidget(self.spotter_combo, 1 if compact else 0, 1 if compact else 3)

        grid.setColumnStretch(1, 1)
        if not compact: grid.setColumnStretch(3, 1)

    def _focus_query_choice(self, kind):
        def focus():
            if self._is_shutting_down:
                return
            field = self.query_group_selector if kind == 'group' else self.spotter_combo
            self.js8_ncs_scroll_area.ensureWidgetVisible(field)
            field.setFocus()
            field.showPopup()
        QTimer.singleShot(0, focus)

    def _populate_net_query_menu(self):
        panel = self.workbench_panel
        self.query_menu.clear()
        self.query_menu.addAction('Change query group…').triggered.connect(lambda: self._focus_query_choice('group'))
        self.query_menu.addAction('Choose query form…').triggered.connect(lambda: self._focus_query_choice('form'))
        self.query_menu.addSeparator()
        group = self._query_group_destination()
        station = panel._selected_roster() or {}
        call = str(station.get('from_call') or station.get('callsign') or '')
        writable = bool(self._net_in_progress and panel._snapshot.get('writable'))
        for label, mode, target in (('Group', 'group', group), ('Station', 'station', call)):
            heading = self.query_menu.addAction(f'{label}: {normalize_group_name(target) if mode == "group" else target or "Select a station"}')
            heading.setEnabled(False)
            for text, command in (('Expect query', 'expect'), ('SNR?', 'SNR?'), ('QUERY MSGS', 'QUERY MSGS')):
                action = self.query_menu.addAction(text)
                action.setEnabled(writable and bool(target) and
                    (command != 'expect' or self._spotter_form in self._available_net_form_codes))
                action.triggered.connect(lambda checked=False, cmd=command, target_mode=mode, destination=target,
                    row=dict(station), session=self._ncs_session_id, generation=self.workbench.generation:
                    self._dispatch_menu_query(cmd, target_mode, destination, row, session, generation))
            if mode == 'group': self.query_menu.addSeparator()

    def _ncs_nets_db_path(self):
        if getattr(self, '_preparation_scope', None) is not None or self._preparation_shell:
            return Path(self.settings.db_path).parent / 'freqinout_nets.db'
        return _nets_db_path()

    def _send_js8_message(self, text: str) -> bool:
        """
        Send through the shared endpoint-scoped JS8 API client with safety preflight.
        """
        host = (self.settings.get("js8_host", "") or "").strip() or "127.0.0.1"
        try:
            port = int(self.settings.get("js8_port", 2442) or 2442)
        except Exception:
            port = 2442
        try:
            endpoint = JS8ApiEndpoint(host, port)
            client = JS8ApiClientRegistry.get(endpoint, timeout_s=1.0, auto_reconnect=True)
            result = send_js8_message_guarded(
                client,
                text,
                timeout_s=0.6,
                allow_uncertain_target_state=True,
            )
            if result.sent:
                self._last_tx_ts = time.time()
                log.info("JS8CallNetControl: sent TX.SEND_MESSAGE to %s:%s text=%s", host, port, text)
                return True
            log.warning("JS8CallNetControl: JS8 send blocked for %s:%s: %s", host, port, result.detail)
        except Exception as e:
            log.error("JS8CallNetControl: failed TX.SEND_MESSAGE to %s:%s text=%s err=%s", host, port, text, e)
        return False

    def _ncs_radio_profiles(self) -> List[Dict]:
        scope = getattr(self, "_preparation_scope", None)
        if scope is not None:
            return [scope.settings().get('_preparation_profile', {'id': scope.radio_id, 'name': scope.radio_name})]
        if getattr(self, "_preparation_shell", False):
            return []
        try:
            profiles = MultiRadioStore().list_runtime_active_device_profiles()
        except Exception as exc:
            log.debug("JS8 NCS: failed to read runtime radio profiles: %s", exc)
            profiles = []
        profiles = [p for p in profiles if js8_profile_allows_transmit(p)]
        js8_profiles = [p for p in profiles if bool(p.get("use_js8call", False))]
        if js8_profiles:
            profiles = js8_profiles
        return sorted(
            [dict(p) for p in profiles if isinstance(p, dict)],
            key=lambda p: (int(p.get("display_order", 0) or 0), int(p.get("id", 0) or 0)),
        )

    @staticmethod
    def _ncs_profile_name(profile: Optional[Dict]) -> str:
        if not profile:
            return "Radio"
        return str(profile.get("name") or profile.get("label") or f"Radio {profile.get('id', '')}").strip()

    def _ncs_selected_radio_id(self, profiles: List[Dict]) -> int:
        scope = getattr(self, "_preparation_scope", None)
        if scope is not None:
            return scope.radio_id
        if getattr(self, "_preparation_shell", False):
            return 0
        ids = {int(p.get("id", 0) or 0) for p in profiles}
        try:
            selected = int(getattr(self.window(), "_station_command_selected_profile_id", 0) or 0)
            if selected in ids:
                return selected
        except Exception:
            pass
        try:
            primary = MultiRadioStore().get_runtime_primary_device_profile()
            primary_id = int((primary or {}).get("id", 0) or 0)
            if primary_id in ids:
                return primary_id
        except Exception:
            pass
        return next(iter(ids), 0)

    def _clear_ncs_session_chip_layout(self) -> None:
        layout = getattr(self, "ncs_session_chip_layout", None)
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _select_ncs_radio_session(self, profile_id: int) -> None:
        if getattr(self, "_preparation_scope", None) is not None:
            self._prepared_receiver._set_status('Prepared', 'Keep the intended radio; review or cancel preparation before choosing another')
            return
        if self._net_in_progress:
            return
        ident = int(profile_id or 0)
        if ident <= 0:
            return
        try:
            MultiRadioStore().set_runtime_primary_device_profile(ident)
        except Exception as exc:
            log.debug("JS8 NCS: failed to set runtime radio session %s: %s", ident, exc)
        try:
            win = self.window()
            if win is not None and hasattr(win, "_activate_station_command_radio"):
                win._activate_station_command_radio(ident)  # type: ignore[attr-defined]
        except Exception as exc:
            log.debug("JS8 NCS: failed to focus station command radio %s: %s", ident, exc)
        self._load_settings()
        self._refresh_qsy_options()
        self._persist_ncs_session_snapshot()
        self._refresh_ncs_session_context()

    def _current_ncs_session_snapshot(self, *, timing_state: Optional[str] = None) -> NcsSessionSnapshot:
        profiles = self._ncs_radio_profiles()
        selected_id = self._ncs_session_radio_id or self._ncs_selected_radio_id(profiles)
        selected_profile = next((p for p in profiles if int(p.get("id", 0) or 0) == selected_id), None)
        state = timing_state or ("active" if self._net_in_progress else "idle")
        return NcsSessionSnapshot(
            protocol="JS8Call",
            source_id=str(selected_id or "radio"),
            source_name=self._ncs_profile_name(selected_profile),
            role=self.role_combo.currentText().strip() if hasattr(self, "role_combo") else "NCS",
            net_name=self.net_name_edit.text().strip() if hasattr(self, "net_name_edit") else "",
            timing_state=state,
            started_utc=self._net_start_utc or "",
            ended_utc=self._net_end_utc or "",
            detail="JS8 NCS session scoped to one configured radio.",
        )

    def _persist_ncs_session_snapshot(self, *, timing_state: Optional[str] = None) -> None:
        try:
            write_ncs_session_snapshot(self.settings, self._current_ncs_session_snapshot(timing_state=timing_state))
        except Exception as exc:
            if getattr(self, '_preparation_scope', None) is not None:
                raise
            log.debug("JS8 NCS: failed to persist session snapshot: %s", exc)

    def _refresh_ncs_session_context(self) -> None:
        if not hasattr(self, "ncs_session_chip_layout") or not hasattr(self, "ncs_session_summary_label"):
            return
        profiles = self._ncs_radio_profiles()
        selected_id = self._ncs_session_radio_id or self._ncs_selected_radio_id(profiles)
        self._draft_radio_id = selected_id
        theme = resolve_theme(self.settings)
        self._clear_ncs_session_chip_layout()
        if not profiles:
            self.ncs_session_chip_layout.addWidget(QLabel("No active JS8 radio"))
        for profile in profiles:
            ident = int(profile.get("id", 0) or 0)
            name = self._ncs_profile_name(profile)
            chip = QPushButton(name)
            chip.setToolTip(f"Switch this NCS workspace to {name}.")
            chip.setStyleSheet(button_style("success" if ident == selected_id else "info", theme))
            chip.setEnabled(not self._net_in_progress)
            chip.clicked.connect(lambda _checked=False, profile_id=ident: self._select_ncs_radio_session(profile_id))
            self.ncs_session_chip_layout.addWidget(chip)
        self.ncs_session_chip_layout.addStretch()
        selected_profile = next((p for p in profiles if int(p.get("id", 0) or 0) == selected_id), None)
        self._preparation_radio_id = selected_id or None
        self._preparation_operating_id = (selected_profile or {}).get("operating_profile_id")
        radio_name = self._ncs_profile_name(selected_profile)
        role = self.role_combo.currentText().strip() if hasattr(self, "role_combo") else "NCS"
        net_name = self.net_name_edit.text().strip() if hasattr(self, "net_name_edit") else ""
        summary = f"Session: {radio_name} | JS8Call | {role}"
        if net_name:
            summary = f"{summary} | {net_name}"
        if self._net_in_progress:
            summary = f"{summary} | Active"
        self.ncs_session_summary_label.setText(summary)
        self.ncs_session_summary_label.setToolTip(summary)
        self._ncs_context_radio_name = radio_name
        self._update_workspace_presentation()

    # ---------------- UI ---------------- #

    def _build_ui(self):
        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        # Persistent task rail: net controls never scroll below the roster.
        self.net_action_rail = QWidget(self)
        rail_layout = QVBoxLayout(self.net_action_rail)
        self._net_rail_layout = rail_layout
        rail_layout.setContentsMargins(0, 0, 0, 0)
        self.net_action_rail.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        outer_layout.addWidget(self.net_action_rail)
        self.js8_ncs_scroll_area = QScrollArea(self)
        self.js8_ncs_scroll_area.setWidgetResizable(True)
        self.js8_ncs_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.js8_ncs_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        content = QWidget(self.js8_ncs_scroll_area)
        self.js8_ncs_scroll_area.setWidget(content)
        outer_layout.addWidget(self.js8_ncs_scroll_area)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Header with clocks
        header = QHBoxLayout()
        header.addWidget(QLabel("<h3>JS8Call Net Control</h3>"))
        header.addStretch()
        self.utc_label = QLabel()
        self.local_label = QLabel()
        self.utc_label.setVisible(False)
        self.local_label.setVisible(False)
        header.addWidget(self.utc_label)
        header.addWidget(self.local_label)
        layout.addLayout(header)

        self.net_context_label = QLabel()
        self.net_context_label.setWordWrap(True)
        self.net_context_label.setTextFormat(Qt.PlainText)
        self.net_context_label.setAccessibleName('Net radio, group and role')
        layout.addWidget(self.net_context_label)
        session_group = QGroupBox("Radio")
        self._js8_session_group = session_group
        session_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        session_layout = QVBoxLayout()
        session_layout.setContentsMargins(12, 10, 12, 10)
        session_layout.setSpacing(6)
        chip_row = QHBoxLayout()
        chip_row.addWidget(QLabel("Radio:"))
        self.ncs_session_chip_layout = QHBoxLayout()
        self.ncs_session_chip_layout.setSpacing(8)
        chip_row.addLayout(self.ncs_session_chip_layout, 1)
        self.ncs_session_summary_label = QLabel("Session: Radio | JS8Call | NCS", session_group)
        self.ncs_session_summary_label.setWordWrap(True)
        self.ncs_session_summary_label.setTextFormat(Qt.PlainText)
        session_layout.addLayout(chip_row)
        self.ncs_session_summary_label.hide()
        session_group.setLayout(session_layout)
        layout.addWidget(session_group)

        setup_group = QGroupBox("Net Setup")
        self._js8_setup_group = setup_group
        setup_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        controls_grid = QGridLayout()
        self._js8_setup_grid = controls_grid
        controls_grid.setContentsMargins(12, 12, 12, 12)
        controls_grid.setHorizontalSpacing(12)
        controls_grid.setVerticalSpacing(10)
        self._js8_role_label = QLabel("Role:")
        controls_grid.addWidget(self._js8_role_label, 0, 0)
        self.role_combo = QComboBox()
        self.role_combo.addItems(["NCS", "ANCS"])
        controls_grid.addWidget(self.role_combo, 0, 1)

        self._js8_net_name_label = QLabel("Net Name:")
        controls_grid.addWidget(self._js8_net_name_label, 0, 2)
        self.net_name_edit = QLineEdit()
        self.net_name_edit.setPlaceholderText("Type net name (auto-complete from schedule)...")
        self.net_name_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        controls_grid.addWidget(self.net_name_edit, 0, 3, 1, 5)

        self._js8_refresh_label = QLabel("Refresh (sec):")
        controls_grid.addWidget(self._js8_refresh_label, 0, 8)
        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(5, 300)
        self.refresh_spin.setValue(15)
        controls_grid.addWidget(self.refresh_spin, 0, 9)

        self._js8_group_label = QLabel("Group:")
        controls_grid.addWidget(self._js8_group_label, 1, 0)
        self.set_group_btn = QPushButton("Set Group")
        self.group_selector = QComboBox()
        self.group_selector.setEditable(True)
        self.group_selector.setInsertPolicy(QComboBox.NoInsert)
        self.group_selector.setProperty('fio_no_auto_fit', True)
        self.group_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.group_selector.setMinimumContentsLength(12)
        self.group_selector.setAccessibleName('Net group destination')
        self.group_edit = self.group_selector.lineEdit()
        self.group_edit.setPlaceholderText("Select operating group")
        controls_grid.addWidget(self.set_group_btn, 1, 1)
        controls_grid.addWidget(self.group_selector, 1, 2, 1, 2)

        self._js8_expect_label = QLabel("Query form:")
        controls_grid.addWidget(self._js8_expect_label, 1, 4)
        self.set_spotter_btn = QPushButton("Set Expect Query")
        self.review_form_tools_btn = QPushButton("Review form tools")
        self.review_form_tools_btn.setToolTip("Load current form-tool mappings for review; Set Expect Query adopts a selection.")
        self.review_form_tools_btn.clicked.connect(self._review_form_tools)
        self.spotter_combo = QComboBox()
        self.spotter_combo.setProperty("fio_no_auto_fit", True)
        self.spotter_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.spotter_combo.setMinimumContentsLength(12)
        controls_grid.addWidget(self.set_spotter_btn, 1, 5)
        controls_grid.addWidget(self.spotter_combo, 1, 6, 1, 4)

        self._js8_qsy_label = QLabel("QSY:")
        controls_grid.addWidget(self._js8_qsy_label, 2, 0)
        self.qsy_combo = QComboBox()
        self.qsy_combo.setProperty("fio_no_auto_fit", True)
        self.qsy_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.qsy_combo.setMinimumContentsLength(12)
        self.qsy_combo.currentIndexChanged.connect(self._update_qsy_button_enabled)
        controls_grid.addWidget(self.qsy_combo, 2, 1, 1, 3)
        self.hold_duration_combo = QComboBox()
        self.hold_duration_combo.setToolTip("Temporary schedule hold duration after QSY.")
        self.hold_duration_combo.currentIndexChanged.connect(self._on_hold_duration_changed)
        self._js8_hold_label = QLabel("Hold:")
        controls_grid.addWidget(self.hold_duration_combo, 2, 4)
        self.suspend_btn = QPushButton("QSY + Hold")
        controls_grid.addWidget(self.suspend_btn, 2, 5, alignment=Qt.AlignLeft)
        self.ad_hoc_btn = QPushButton("Ad Hoc Net")
        controls_grid.addWidget(self.ad_hoc_btn, 2, 6)
        controls_grid.setColumnStretch(2, 1)
        controls_grid.setColumnStretch(3, 2)
        controls_grid.setColumnStretch(6, 2)
        controls_grid.setColumnStretch(7, 2)
        controls_grid.setColumnStretch(8, 2)
        controls_grid.setColumnStretch(9, 2)
        setup_group.setLayout(controls_grid)
        self._configure_setup_sections()
        self.net_setup_toggle = QPushButton('Net setup')
        self.net_setup_toggle.setCheckable(True)
        self.net_setup_toggle.setChecked(True)
        self.net_setup_toggle.setAccessibleName('Show or hide net setup fields')
        self.net_setup_toggle.toggled.connect(self._update_workspace_presentation)
        rail_layout.addWidget(self.net_setup_toggle)
        layout.addWidget(setup_group)
        self.form_tools_status = QLabel()
        self.form_tools_status.hide()
        self.form_tools_status.setWordWrap(True)
        self.form_tools_status.setTextFormat(Qt.PlainText)
        self.form_tools_status.setAccessibleName("Form tools review status")
        layout.addWidget(self.form_tools_status)

        self.preparation_label = QLabel()
        self.preparation_label.setObjectName("js8NetPreparation")
        self.preparation_label.setWordWrap(True)
        self.preparation_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.preparation_label.setOpenExternalLinks(False)
        self.preparation_label.linkActivated.connect(self._open_preparation_link)
        layout.addWidget(self.preparation_label)

        # Check-ins table
        checkins_group = QWidget()
        checkins_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        table_layout = QVBoxLayout()
        self._workbench_layout = table_layout
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(8)
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Check-in Filter:"))
        self.checkin_filter_combo = QComboBox()
        self.checkin_filter_combo.addItems(["Mapped Check-ins", "Any Spotter", "All Callsigns"])
        fit_combo_box_to_contents(self.checkin_filter_combo)
        filter_row.addWidget(self.checkin_filter_combo)
        from freqinout.gui.call_finder_dialog import open_call_finder
        self.fcc_lookup_btn = QPushButton("FCC lookup…")
        self.fcc_lookup_btn.setToolTip("Review the selected callsign offline; does not alter check-ins or send RF.")
        self.fcc_lookup_btn.clicked.connect(lambda: open_call_finder(self, self._selected_callsign() or ""))
        filter_row.addWidget(self.fcc_lookup_btn)
        filter_row.addStretch()
        for index in range(filter_row.count()):
            widget = filter_row.itemAt(index).widget()
            if widget is not None:
                widget.hide()
        table_layout.addLayout(filter_row)
        self.checkin_table = QTableWidget(0, 10)
        self.checkin_table.setHorizontalHeaderLabels(
            ["CALLSIGN", "NAME", "ST", "GRID", "REGION", "MODE", "SNR", "OFFSET", "STATUS", ""]
        )
        self.checkin_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.checkin_table.setSelectionMode(QTableWidget.SingleSelection)
        self.checkin_table.horizontalHeader().setStretchLastSection(True)
        self.checkin_table.setMinimumHeight(
            font_derived_widget_height(self.checkin_table, vertical_padding=12, floor=120, include_size_hints=False)
        )
        self.checkin_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.checkin_empty_label = QLabel(
            "No JS8 check-ins yet. Start the net and accept mapped MCF forms as stations check in."
        )
        self.checkin_empty_label.setObjectName("js8NcsCheckinEmptyState")
        self.checkin_empty_label.setWordWrap(True)
        self.checkin_empty_label.setVisible(False)
        table_layout.addWidget(self.checkin_empty_label)
        table_layout.addWidget(self.checkin_table, 1)

        # Buttons row
        self.net_action_buttons = QWidget()
        self.net_action_buttons.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        btn_row = QVBoxLayout(self.net_action_buttons)
        btn_row.setContentsMargins(0, 0, 0, 0)
        self._js8_action_row = btn_row
        btn_row.setSpacing(8)
        self.start_btn = QPushButton("Start Net")
        self.ack_btn = QPushButton("ACK group…")
        self.ack_callsign_btn = QPushButton("ACK station…")
        self.group_spotter_btn = QPushButton("E? GROUP")
        self.single_spotter_btn = QPushButton("E? Callsign")
        self.save_btn = QPushButton("Save check-ins")
        self.end_btn = QPushButton("End Net")
        self.ack_btn.setEnabled(False)
        self.ack_callsign_btn.setEnabled(False)
        self.end_btn.setEnabled(False)
        self._set_net_button_styles(active=False)
        fit_child_combo_boxes(self)

        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.ack_btn)
        btn_row.addWidget(self.ack_callsign_btn)
        btn_row.addWidget(self.group_spotter_btn)
        btn_row.addWidget(self.single_spotter_btn)
        btn_row.addWidget(self.save_btn)
        btn_row.addSpacing(self.fontMetrics().lineSpacing())
        btn_row.addWidget(self.end_btn)
        btn_row.addStretch()

        self.net_actions_menu = QMenu(self)
        self._net_menu_actions = []
        for command in (self.start_btn, self.save_btn, self.end_btn):
            if command is self.end_btn:
                self.net_actions_menu.addSeparator()
            action = self.net_actions_menu.addAction(command.text())
            action.triggered.connect(lambda checked=False, button=command: button.click())
            self._net_menu_actions.append((action, command))
        self.net_actions_menu.aboutToShow.connect(self._sync_net_action_menu)
        self.net_actions_more = QPushButton('Net actions')
        self.net_actions_more.setMenu(self.net_actions_menu)
        self.net_actions_more.setAccessibleName('Net actions: Start, save or end')
        rail_layout.addWidget(self.net_actions_more)
        rail_layout.addWidget(self.net_action_buttons)
        rail_layout.addStretch()
        checkins_group.setLayout(table_layout)
        layout.addWidget(checkins_group, 1)
        self._update_checkin_empty_state()
        self.group_spotter_btn.hide()
        self.single_spotter_btn.hide()

        # Signals
        self.start_btn.clicked.connect(self._start_net)
        self.ack_btn.clicked.connect(self._ack_checkins)
        self.ack_callsign_btn.clicked.connect(self._ack_callsign)
        self.group_spotter_btn.clicked.connect(self._group_spotter)
        self.single_spotter_btn.clicked.connect(self._single_spotter)
        self.save_btn.clicked.connect(self._save_checkins)
        self.end_btn.clicked.connect(self._end_net)
        self.set_group_btn.clicked.connect(self._set_group_target)
        self.set_spotter_btn.clicked.connect(self._set_spotter_form)
        self.spotter_combo.currentIndexChanged.connect(self._on_spotter_selection_changed)
        self.spotter_combo.activated.connect(self._adopt_query_selection)
        self.group_edit.textChanged.connect(self._adopt_group_text)
        self.group_edit.editingFinished.connect(self._on_group_choice_finished)
        self.group_selector.activated.connect(self._on_group_choice_finished)
        self.group_edit.textChanged.connect(self._update_group_button_state)
        self.group_edit.textChanged.connect(self._update_preparation_summary)
        self.checkin_filter_combo.currentIndexChanged.connect(self._on_checkin_filter_changed)
        self.checkin_table.cellClicked.connect(self._on_checkin_table_clicked)
        self.refresh_spin.valueChanged.connect(self._update_timer_interval)
        self.suspend_btn.clicked.connect(self._on_suspend_clicked)
        self.ad_hoc_btn.clicked.connect(self._start_ad_hoc_net)
        self.role_combo.currentTextChanged.connect(lambda _text: self._on_ncs_session_context_changed())
        self.role_combo.currentTextChanged.connect(self._update_workspace_presentation)
        self.net_name_edit.editingFinished.connect(self._on_ncs_session_context_changed)
        self.net_name_edit.textChanged.connect(self._update_preparation_summary)

        self._set_net_button_styles(active=False)
        self._refresh_ncs_session_context()
        QTimer.singleShot(0, self._reflow_ncs_layouts)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_ncs_layouts()

    def _configure_setup_sections(self):
        """Build task groups once; reflow moves cached controls only."""
        root = self._js8_setup_grid
        while root.count():
            root.takeAt(0)
        for column in range(10):
            root.setColumnStretch(column, 0)
        self._identity_grid = QGridLayout()
        self._query_grid = QGridLayout()
        self._radio_grid = QGridLayout()
        root.addLayout(self._identity_grid, 0, 0)
        for control in (self._js8_expect_label, self.spotter_combo, self.set_spotter_btn,
                        self.review_form_tools_btn, self._js8_qsy_label, self.qsy_combo,
                        self._js8_hold_label, self.hold_duration_combo, self.suspend_btn,
                        self._js8_refresh_label, self.refresh_spin, self.set_group_btn):
            control.hide()
        # Retained only for old draft/test adapters; deliberately absent from UI.
        self.radio_change_toggle = QToolButton(self)
        self.radio_change_toggle.hide()
        self.radio_change_controls = QWidget(self)
        self.radio_change_controls.setLayout(self._radio_grid)
        self.radio_change_controls.hide()
        self.net_options_toggle = QToolButton(self)
        self.net_options_toggle.hide()
        self.net_options_controls = QWidget(self)
        self.net_options_controls.hide()
        self._js8_setup_mode = None

    def _update_workspace_presentation(self, *_args):
        if not hasattr(self, 'net_setup_toggle'):
            return
        shown = self.net_setup_toggle.isChecked()
        self._js8_setup_group.setVisible(shown)
        self._js8_session_group.setVisible(shown and not self._net_in_progress)
        self.net_setup_toggle.setText('Edit setup' if self._net_in_progress else 'Net setup')
        role = self.role_combo.currentText().strip()
        panel = getattr(self, 'workbench_panel', None)
        snapshot = getattr(panel, '_snapshot', {}) if panel is not None else {}
        run = snapshot.get('run') or {}
        group = normalize_group_name(run.get('group_name') or self._group_target) or 'Group not set'
        name = run.get('label') or self.net_name_edit.text().strip() or 'Net not named'
        radio = getattr(self, '_ncs_context_radio_name', '') or 'Radio not selected'
        self.net_context_label.setText(f'{radio} · {name} · {group} · {role}' if not shown else f'{radio} · {group} · {role}')
        self.net_context_label.setToolTip(self.ncs_session_summary_label.text())
        if panel is not None:
            panel.group_context_label.setText(f'Group: {group}')
            writable = bool(self._net_in_progress and snapshot.get('writable'))
            self.save_btn.setEnabled(writable)
            self.save_btn.setToolTip('Save participating stations for this active net' if writable
                                     else 'Start an active net to save check-ins')
            self._present_ack_group_menu(snapshot, writable)
            self.ack_callsign_btn.setEnabled(writable)
            self._update_query_controls()
            self._update_live_query_target()
        self._update_preparation_summary()

    def _reflow_ncs_layouts(self) -> None:
        """Reposition cached controls; labels and data never choose breakpoints."""
        metrics = self.fontMetrics()
        self.spotter_combo.setMaximumWidth(metrics.horizontalAdvance("M") * 46)
        panel = getattr(self, 'workbench_panel', None)
        commands = (self.net_setup_toggle, self.start_btn, self.save_btn, self.end_btn)
        if panel is not None:
            commands += (panel.manage_messages_toggle, panel.sent_activity_toggle, self._secondary_actions_button)
        rail_floor = sum(max(button.sizeHint().height(), button.minimumHeight()) for button in commands)
        rail_floor += self._js8_action_row.spacing() * (len(commands) + 2)
        rail_floor += metrics.lineSpacing() * 3
        short = self.height() < rail_floor
        self.net_actions_more.setVisible(short)
        self.net_action_buttons.setVisible(not short)
        if panel is not None:
            self.ack_btn.show()
            self.ack_callsign_btn.show()
            self.net_setup_toggle.setVisible(not short)
            for button in (panel.manage_messages_toggle, panel.sent_activity_toggle):
                button.setVisible(not short)
            self.workspace_menu_button.setVisible(short)
        available = self.js8_ncs_scroll_area.viewport().width()
        panel = getattr(self, 'workbench_panel', None)
        if panel is not None:
            panel.set_workspace_width(max(0, available - metrics.horizontalAdvance('M') * 4))
        # A short setup form stays grouped at a readable width on large screens.
        self._js8_setup_group.setMaximumWidth(metrics.horizontalAdvance('M') * 92)
        compact = available < metrics.horizontalAdvance('M') * 76
        self._js8_setup_compact = compact
        self._reflow_query_context(compact)
        if hasattr(self, '_live_query_layout') and compact != getattr(self, '_live_query_compact', None):
            self._live_query_compact = compact
            grid = self._live_query_layout
            while grid.count(): grid.takeAt(0)
            controls = (self.query_target_mode, self.query_target_label, *self.live_query_buttons, self.ack_btn, self.ack_callsign_btn, panel.send_toggle)
            for index, widget in enumerate(controls):
                grid.addWidget(widget, index // (3 if compact else 8), index % (3 if compact else 8))
                widget.show()
            grid.setColumnStretch(1, 0)
            grid.setColumnStretch(8, 1)
            grid.invalidate()
            self.live_query_widget.updateGeometry()
            panel.query_picker_layout.invalidate()
            panel.group_action_widget.layout().invalidate()
            panel.group_action_widget.updateGeometry()
            panel.layout().invalidate()
            panel.layout().activate()
            panel.updateGeometry()
        if self._js8_setup_mode == compact:
            return
        self._js8_setup_mode = compact
        identity, query, radio = self._identity_grid, self._query_grid, self._radio_grid
        for grid in (identity, query, radio):
            while grid.count():
                grid.takeAt(0)
            for column in range(6):
                grid.setColumnStretch(column, 0)
        identity.addWidget(self._js8_net_name_label, 0, 0)
        identity.addWidget(self.net_name_edit, 0, 1, 1, 3 if not compact else 1)
        identity.addWidget(self.ad_hoc_btn, 1 if compact else 0, 1 if compact else 4, alignment=Qt.AlignLeft)
        identity.addWidget(self._js8_group_label, 2 if compact else 1, 0)
        identity.addWidget(self.group_selector, 2 if compact else 1, 1)
        identity.addWidget(self._js8_role_label, 3 if compact else 1, 0 if compact else 3)
        identity.addWidget(self.role_combo, 3 if compact else 1, 1 if compact else 4)
        identity.setColumnStretch(1, 1)
        self._js8_setup_group.updateGeometry()

    def event(self, event):
        result = super().event(event)
        if (event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.FontChange, QEvent.Type.StyleChange)
                and hasattr(self, 'net_actions_more') and not getattr(self, '_ncs_reflow_pending', False)):
            self._ncs_reflow_pending = True
            QTimer.singleShot(0, self._publish_ncs_reflow)
        return result

    def _publish_ncs_reflow(self):
        self._ncs_reflow_pending = False
        if self._is_shutting_down:
            return
        self._reflow_ncs_layouts()

    def _sync_net_action_menu(self):
        if hasattr(self, '_undo_net_action'):
            self._undo_net_action.setEnabled(self.workbench_panel.undo_button.isEnabled())
        for action, button in self._net_menu_actions:
            action.setText(button.text())
            action.setEnabled(button.isEnabled())

    def _on_ncs_session_context_changed(self) -> None:
        self._persist_ncs_session_snapshot()
        self._refresh_ncs_session_context()

    # ---------------- SETTINGS & TIMER ---------------- #

    def _refresh_auto_query_flags(self):
        try:
            self.settings.reload()
        except Exception:
            pass
        self._load_settings()

    def on_settings_saved(self):
        if getattr(self, "_preparation_shell", False):
            return
        """
        Slot invoked when Settings tab emits settings_saved.
        """
        self._refresh_auto_query_flags()
        self._maybe_reload_operating_groups()
        self._apply_theme()
        self._refresh_group_completer()
        self._setup_js8_rx_timer()

    def show_loading_toast(self) -> None:
        # NCS tabs do not use a loading banner/toast.
        return

    def on_tab_activated(self) -> None:
        if getattr(self, "_preparation_shell", False):
            return
        with perf_span("js8_ncs.on_tab_activated", settings=self.settings, min_ms=5.0):
            self._update_clock_labels()
            self._update_suspend_state()
            self._maybe_reload_operating_groups()
            self._refresh_group_completer()

    def set_tab_active(self, active: bool) -> None:
        receiver = getattr(self, '_prepared_receiver', None)
        if not active and receiver is not None:
            receiver.deactivate()
        self._active = bool(active)
        if active:
            self._refresh_group_completer()
        if active and self._form_mappings_dirty and not self._net_in_progress:
            self._review_form_tools()
        if active and receiver is not None:
            receiver.changed.emit()
        if getattr(self, '_preparation_shell', False):
            if self._active:
                self._apply_pending_fcc_identities()
            return
        if self._active:
            self._apply_pending_fcc_identities()
            if self._clock_timer and not self._clock_timer.isActive():
                self._clock_timer.start(1000)
            if self._poll_timer and not self._poll_timer.isActive():
                self._poll_timer.start()
            QTimer.singleShot(0, self.on_tab_activated)
            return
        if self._clock_timer and self._clock_timer.isActive() and not self._js8_net_started:
            self._clock_timer.stop()
        if self._poll_timer and self._poll_timer.isActive() and not self._js8_net_started:
            self._poll_timer.stop()

    def _load_settings(self):
        data = self.settings.all()
        self.auto_query_msg_id = bool(data.get("js8_auto_query_msg_id", False))
        self.auto_query_grids = bool(data.get("js8_auto_query_grids", False))
        self._refresh_group_completer()

        # Net name autocomplete from net_schedule
        net_sched = data.get("net_schedule", [])
        self._preparation_schedule = list(net_sched) if isinstance(net_sched, list) else []
        self._preparation_callsign = bool(str(data.get("operator_callsign") or data.get("callsign") or "").strip())
        net_names = sorted(
            {row.get("net_name", "") for row in net_sched if isinstance(row, dict) and row.get("net_name")}
        )
        if net_names:
            completer = QCompleter(net_names, self)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            self.net_name_edit.setCompleter(completer)

        # DIRECTED.TXT path
        directed_path = data.get("js8_directed_path", "")
        if directed_path:
            p = Path(directed_path)
            if p.exists() and p.is_file():
                self._directed_path = p
                self._refresh_ncs_offset_keys()
                try:
                    size_now = p.stat().st_size
                    self._startup_directed_size = size_now
                    saved_off = self._settings_int(
                        self._directed_offset_key,
                        self._settings_int("js8_directed_offset", 0),
                    )
                    if saved_off <= 0:
                        self._last_directed_size = size_now
                    else:
                        self._last_directed_size = min(saved_off, size_now)
                except Exception:
                    self._startup_directed_size = 0
            else:
                self._directed_path = None
                self._directed_offset_key = ""
                self._all_offset_key = ""
                log.warning("JS8CallNetControl: js8_directed_path not found: %s", directed_path)
        else:
            self._directed_path = None
            self._directed_offset_key = ""
            self._all_offset_key = ""
        if self._directed_path:
            try:
                all_path = self._scoped_all_path()
                size_now = all_path.stat().st_size if all_path.exists() else 0
                saved_all = self._settings_int(
                    self._all_offset_key,
                    self._settings_int("js8_all_offset", 0),
                )
                if saved_all <= 0:
                    self._last_all_size = size_now
                else:
                    self._last_all_size = min(saved_all, size_now)
            except Exception:
                self._last_all_size = 0

        # Refresh interval
        refresh = int(data.get("js8_refresh_sec", 15) or 15)
        self.refresh_spin.setValue(refresh)
        if self._poll_timer:
            self._poll_timer.setInterval(refresh * 1000)

        # Spotter forms dropdown
        global_forms_path = str(self.settings.get("js8_forms_path", "") or "").strip()
        legacy_forms_path = str(data.get("js8_forms_path", "") or "").strip() if not global_forms_path else ""
        # Active nets retain their picker until an explicit review.
        if not getattr(self, "_net_in_progress", False):
            self._refresh_form_tool_choices(data, global_forms_path, legacy_forms_path)
        fit_combo_box_to_contents(self.spotter_combo)
        self._refresh_ncs_session_context()
        self._update_preparation_summary()

    def _refresh_form_tool_choices(self, data, global_forms_path, legacy_forms_path):
        selected = self.spotter_combo.currentData()
        definitions = discover_effective_spotter_forms(global_forms_path, legacy_forms_path)
        forms = resolve_net_form_options(definitions, data.get(MAPPER_SETTINGS_KEY, []),
                                         group_key=self._query_operating_group_key()).options
        self._publish_form_tool_choices(forms)
        self._net_form_tools_group_key = self._query_operating_group_key()

    def _publish_form_tool_choices(self, forms):
        selected = self.spotter_combo.currentData() or self._spotter_form
        self._available_net_form_codes = {code for code, _ in forms}
        self.spotter_combo.blockSignals(True)
        self.spotter_combo.clear()
        for code, label in forms:
            self.spotter_combo.addItem(label, code)
        # Preserve a draft or committed choice even if the current catalog removed it.
        for code in dict.fromkeys(filter(None, (selected, self._spotter_form))):
            if self.spotter_combo.findData(code) < 0:
                self.spotter_combo.addItem(f"{code} — unavailable (review)", code)
        if self.spotter_combo.count():
            self.spotter_combo.setCurrentIndex(self.spotter_combo.findData(selected) if selected else -1)
            self.spotter_combo.setPlaceholderText("Select a form")
        else:
            self.spotter_combo.addItem("No forms found")
        self.spotter_combo.blockSignals(False)
        has_choices = bool(forms or selected or self._spotter_form)
        self.spotter_combo.setEnabled(has_choices)
        self._on_spotter_selection_changed()
        self.spotter_combo.setToolTip("Optional query form. Selecting changes the next query and sends nothing.")

    def on_form_mappings_saved(self):
        self._form_mappings_generation += 1
        self._form_mappings_dirty = True
        if self._net_in_progress:
            self.form_tools_status.setText('Form mappings changed. Reload forms to review the new choices; current query and net are retained.')
            self.form_tools_status.show()
        elif self._active:
            self._review_form_tools()

    def _query_operating_group_key(self):
        prepared = getattr(getattr(self, '_prepared_receiver', None), 'prepared', None)
        values = prepared.request.source.values.values() if prepared is not None else {}
        if values.get('operating_group_key'):
            return str(values['operating_group_key'])
        from freqinout.core.operating_group_identity import operating_group_options
        name = normalize_group_name(self._group_target if self._net_in_progress else self.group_edit.text())
        matches = [group.operating_group_key for group in operating_group_options(
            self.settings.get('operating_groups', []) or [])
            if normalize_group_name(group.group_name) == name]
        return matches[0] if len(matches) == 1 else ''

    def _form_tools_scope(self):
        receiver = getattr(self, '_prepared_receiver', None)
        return (str(self.settings.db_path), id(getattr(self, '_preparation_scope', None)),
                getattr(receiver, 'generation', 0), self._ncs_session_id,
                self.spotter_combo.currentData(), self._spotter_form,
                self._form_mappings_generation, self._query_operating_group_key())

    def _review_form_tools(self):
        # The settings getters here are cached and remain on their owner thread.
        if self._form_tools_inflight or self._is_shutting_down:
            return
        from freqinout.core.ncs_form_tools import read_net_form_tools
        from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor
        if self._form_tools_executor is None:
            self._form_tools_executor = DaemonSerialExecutor(thread_name_prefix='fio-ncs-form-tools')
        token = self._form_tools_scope()
        path = self.settings.db_path
        fallback = str(self.settings.get('js8_forms_path', '') or '')
        group_key = self._query_operating_group_key()
        self._form_tools_inflight = True
        self.review_form_tools_btn.setEnabled(False)
        self.form_tools_status.show()
        self.form_tools_status.setText('Reviewing current form tools…')
        future = self._form_tools_executor.submit(read_net_form_tools, path,
                                                  fallback_path=fallback, group_key=group_key)
        def completed(result):
            try:
                value, error = result.result(), None
            except Exception as exc:
                value, error = None, str(exc)
            if self._is_shutting_down:
                return
            try:
                self.form_tools_ready.emit(token, value, error)
            except RuntimeError:
                pass
        future.add_done_callback(completed)

    def _publish_form_tools(self, token, snapshot, error):
        self._form_tools_inflight = False
        if self._is_shutting_down:
            return
        self.review_form_tools_btn.setEnabled(True)
        if token != self._form_tools_scope():
            self.form_tools_status.setText('Form tools review superseded; current work retained. Review again when ready.')
            if self._form_mappings_dirty and self._active and not self._net_in_progress:
                QTimer.singleShot(0, self._review_form_tools)
            return
        if error:
            self.form_tools_status.setText('Could not review form tools; current choices retained. ' + str(error))
            return
        self._form_mappings_dirty = False
        self._net_form_tools_group_key = self._query_operating_group_key()
        self._publish_form_tool_choices(snapshot.choices.options)
        self._net_form_tools_revision = snapshot.revision
        if self._spotter_form and self._spotter_form not in self._available_net_form_codes:
            self.form_tools_status.setText('The selected query form is unavailable. Choose another form to send a query; starting and receiving remain available.')
        elif snapshot.choices.missing_preferred or snapshot.choices.ambiguous:
            self.form_tools_status.setText('Choose any available query form when needed. Queries are optional and can change during the net.')
        else:
            self.form_tools_status.setText('Query forms updated.')
            QTimer.singleShot(4000, lambda: self.form_tools_status.hide() if self.form_tools_status.text() == 'Query forms updated.' else None)
        fit_combo_box_to_contents(self.spotter_combo)

    def _scoped_all_path(self):
        scope = getattr(self, '_preparation_scope', None)
        return Path(scope.all_path) if scope is not None and scope.all_path else self._directed_path.parent / 'ALL.TXT'

    def _refresh_ncs_offset_keys(self):
        scope = getattr(self, '_preparation_scope', None)
        if scope is not None:
            from freqinout.core.ingest_source_model import stable_source_id
            self._directed_offset_key = 'js8_ncs_directed_offset_' + stable_source_id('js8call', scope.instance_id, 'directed', scope.directed_path, prefix='source')
            self._all_offset_key = 'js8_ncs_all_offset_' + stable_source_id('js8call', scope.instance_id, 'all', scope.all_path, prefix='source')
            return
        if not self._directed_path:
            self._directed_offset_key = ""
            self._all_offset_key = ""
            return
        try:
            inventory = active_runtime_ingest_inventory()
        except Exception:
            inventory = None
        keys = ncs_offset_keys_for_directed_path(self._directed_path, inventory=inventory)
        self._directed_offset_key = keys.directed_offset_key
        self._all_offset_key = keys.all_offset_key

    def _settings_int(self, key: object, default: int = 0) -> int:
        key_txt = str(key or "").strip()
        if not key_txt:
            return int(default or 0)
        try:
            return int(self.settings.get(key_txt, default) or default)
        except Exception:
            return int(default or 0)

    def _save_ncs_offset(self, key: object, value: int):
        key_txt = str(key or "").strip()
        if not key_txt:
            return
        try:
            self.settings.set(key_txt, int(value))
        except Exception:
            pass

    def _save_refresh_setting(self):
        try:
            self.settings.set("js8_refresh_sec", int(self.refresh_spin.value()))
            if hasattr(self.settings, "save"):
                self.settings.save()
            elif hasattr(self.settings, "write"):
                self.settings.write()
        except AttributeError:
            # Fallback if set()/save() not implemented
            data = self.settings.all()
            data["js8_refresh_sec"] = int(self.refresh_spin.value())
            if hasattr(self.settings, "_data"):
                self.settings._data = data  # type: ignore[attr-defined]
            if hasattr(self.settings, "write"):
                self.settings.write()

    def _setup_timer(self):
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_directed_file)
        self._update_timer_interval()

    def _setup_clock_timer(self):
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)

    def _tick_clock(self):
        self._update_clock_labels()

    def _setup_js8_rx_timer(self):
        host = (self.settings.get("js8_host", "") or "").strip() or "127.0.0.1"
        try:
            port = int(self.settings.get("js8_port", 2442) or 2442)
        except Exception:
            port = 2442
        hub_valid = False
        if self._js8_rx_hub is not None:
            try:
                hub_valid = bool(self._js8_rx_hub.is_valid())
            except Exception:
                hub_valid = False
        if not hub_valid:
            self._js8_rx_hub = None
            self._js8_rx_registered = False
        if self._js8_rx_hub is None or self._js8_rx_hub.endpoint() != (host, port):
            if self._js8_rx_hub is not None and self._js8_rx_registered:
                try:
                    self._js8_rx_hub.unregister_listener(getattr(self, '_scoped_rx_callback', self._on_js8_rx_messages))
                except Exception:
                    pass
                self._js8_rx_registered = False
            self._js8_rx_hub = JS8RxHub.instance(host, port)
        if not self._js8_rx_registered:
            epoch = int(getattr(self, '_ncs_rx_epoch', 0))
            def receive(messages):
                if epoch == int(getattr(self, '_ncs_rx_epoch', 0)):
                    self._on_js8_rx_messages(messages)
            self._scoped_rx_callback = receive
            self._js8_rx_hub.register_listener(self._scoped_rx_callback)
            self._js8_rx_registered = True
        try:
            self._js8_rx_hub.start(host, port)
        except RuntimeError:
            self._js8_rx_hub = JS8RxHub.instance(host, port)
            self._js8_rx_hub.register_listener(getattr(self, '_scoped_rx_callback', self._on_js8_rx_messages))
            self._js8_rx_registered = True
            self._js8_rx_hub.start(host, port)

    def _js8_live_source_context(self) -> Dict[str, str]:
        scope = getattr(self, '_preparation_scope', None)
        if scope is not None:
            return {'source_radio_id': str(scope.radio_id), 'js8_instance_id': scope.instance_id.split(':')[-1],
                    'source_id': scope.instance_id}
        host = (self.settings.get("js8_host", "") or "").strip() or "127.0.0.1"
        try:
            port = int(self.settings.get("js8_port", 2442) or 2442)
        except Exception:
            port = 2442
        endpoint = f"{host}:{port}".strip().lower()
        cache = getattr(self, "_js8_live_source_context_cache", {}) or {}
        if (
            str(cache.get("endpoint", "") or "") == endpoint
            and float(cache.get("expires", 0.0) or 0.0) > time.time()
            and isinstance(cache.get("context"), dict)
        ):
            return dict(cache.get("context") or {})
        context = resolve_js8_source_context(self.settings, host=host, port=port)
        self._js8_live_source_context_cache = {
            "endpoint": endpoint,
            "expires": time.time() + 30.0,
            "context": context,
        }
        return context

    def _update_timer_interval(self):
        if self._poll_timer:
            self._poll_timer.setInterval(self.refresh_spin.value() * 1000)
        self._save_refresh_setting()

    # ---------------- CLOCK LABELS ---------------- #

    def _ui_tz_abbr(self, tz_name: str, fallback: str) -> str:
        mapping = {
            "UTC": "UTC",
            "America/New_York": "ET",
            "America/Chicago": "CT",
            "America/Denver": "MT",
            "America/Los_Angeles": "PT",
        }
        return mapping.get(tz_name, fallback)

    def _update_clock_labels(self):
        """
        UTC from system clock; local time derived via Settings timezone + get_timezone(),
        with a UI label like ET / CT / MT / PT / UTC.
        """
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        utc_day = now_utc.strftime("%a")
        self.utc_label.setText(now_utc.strftime(f"<b>UTC ({utc_day}):</b> %y%m%d %H:%M:%S Z"))

        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        now_local = now_utc.astimezone(tz)
        fallback = now_local.tzname() or tz_name
        abbr = self._ui_tz_abbr(tz_name, fallback)

        local_day = now_local.strftime("%a")
        self.local_label.setText(
            now_local.strftime(f"<b>Local ({local_day}):</b> %y%m%d %H:%M:%S {abbr}")
        )

    # --------- Suspend (shared across tabs) --------- #

    def _get_suspend_until(self) -> Optional[datetime.datetime]:
        try:
            if hasattr(self.settings, "reload"):
                self.settings.reload()
        except Exception:
            pass
        return get_suspend_until(self.settings)

    def _set_suspend_until(self, dt: Optional[datetime.datetime]) -> None:
        set_suspend_until(self.settings, dt)

    def _suspend_active(self) -> bool:
        return suspend_active(self.settings)

    def _scheduler_enabled(self) -> bool:
        return scheduler_enabled(self.settings)

    def _set_suspend_button(self, active: bool, remaining_sec: Optional[float] = None):
        theme = resolve_theme(self.settings)
        if active:
            self.suspend_btn.setText(active_hold_button_text(remaining_sec))
            self.suspend_btn.setToolTip(active_hold_status_text(remaining_sec))
            self.suspend_btn.setStyleSheet(button_style(active_hold_button_role(remaining_sec), theme))
        else:
            mins = self._selected_hold_minutes()
            self.suspend_btn.setText("QSY + Hold")
            self.suspend_btn.setToolTip(f"QSY now and pause schedule control for {mins} minutes.")
            self.suspend_btn.setStyleSheet(button_style("warning", theme))
        self._update_qsy_button_enabled()

    def _update_suspend_state(self, snapshot: Optional[Dict[str, object]] = None):
        try:
            if not self.hold_duration_combo.view().isVisible() and not self.hold_duration_combo.hasFocus():
                refresh_hold_duration_combo(self.hold_duration_combo, self.settings)
        except Exception:
            pass
        enabled = self._scheduler_enabled()
        self.suspend_btn.setEnabled(enabled)
        if not enabled:
            self._set_suspend_button(False)
            self._update_qsy_button_enabled()
            return

        if not isinstance(snapshot, dict):
            snapshot = suspend_snapshot(self.settings)
        if snapshot.get("active"):
            self._set_suspend_button(True, remaining_sec=snapshot.get("remaining_sec"))
        else:
            if snapshot.get("until"):
                resume_schedule_hold(self.window(), self.settings)
            self._set_suspend_button(False)
        self._update_qsy_button_enabled()

    def on_hold_state_changed(self, snapshot: Optional[Dict[str, object]] = None) -> None:
        self._update_suspend_state(snapshot=snapshot)

    def _on_suspend_clicked(self):
        scope = getattr(self, '_preparation_scope', None)
        if scope is not None:
            self._prepared_receiver._set_status('Prepared', f'Use {scope.radio_name} in the Station Control Bar to QSY or set a hold')
            return
        if self._suspend_active():
            resume_schedule_hold(self.window(), self.settings)
            self._set_suspend_button(False)
            QMessageBox.information(self, "Scheduling", "Scheduling resumed.")
        else:
            meta = self._selected_qsy_meta()
            if not meta:
                QMessageBox.warning(self, "QSY", "Select a frequency to QSY to.")
                return
            mins = perform_qsy_with_hold(self.window(), self.settings, meta, self._selected_hold_minutes())
            if mins <= 0:
                return
            snapshot = suspend_snapshot(self.settings)
            self._set_suspend_button(True, remaining_sec=snapshot.get("remaining_sec"))
            QMessageBox.information(
                self,
                "QSY Applied",
                f"Frequency changed and scheduling paused for {mins} minutes.",
            )

    def _refresh_operator_history_views(self) -> None:
        try:
            win = self.window()
            if win and hasattr(win, "refresh_operator_history_views"):
                win.refresh_operator_history_views()
        except Exception:
            pass

    def _format_freq(self, val) -> str:
        try:
            return f"{float(val):.3f}"
        except Exception:
            return str(val) if val is not None else ""

    def _set_net_button_styles(self, active: bool):
        """
        Mirror FLDigi styling: green Start when idle, gray when active; End stays red.
        """
        theme = resolve_theme(self.settings)
        if active:
            self.start_btn.setStyleSheet(button_style("muted", theme))
            self.ack_btn.setStyleSheet("")
            self.ack_callsign_btn.setStyleSheet("")
            self.group_spotter_btn.setStyleSheet(button_style("eligible_info", theme))
            self.single_spotter_btn.setStyleSheet(button_style("eligible_info", theme))
            self.save_btn.setStyleSheet("")
            self.end_btn.setStyleSheet(button_style("eligible_danger", theme))
            self.ad_hoc_btn.setStyleSheet(button_style("muted", theme))
            self.start_btn.setEnabled(False)
            self.ad_hoc_btn.setEnabled(False)
        else:
            self.start_btn.setStyleSheet(button_style("eligible_success", theme))
            self.ack_btn.setStyleSheet(button_style("muted", theme))
            self.ack_callsign_btn.setStyleSheet(button_style("muted", theme))
            self.group_spotter_btn.setStyleSheet(button_style("muted", theme))
            self.single_spotter_btn.setStyleSheet(button_style("muted", theme))
            self.save_btn.setStyleSheet(button_style("muted", theme))
            self.end_btn.setStyleSheet(button_style("muted", theme))
            self.ad_hoc_btn.setStyleSheet(button_style("eligible_info", theme))
            self.start_btn.setEnabled(True)
            self.ad_hoc_btn.setEnabled(True)
        self._update_group_button_state()
        self._update_spotter_button_state()
        self._update_workspace_presentation()

    def _apply_theme(self) -> None:
        theme = resolve_theme(self.settings)
        self._start_btn_default_style = button_style("success", theme)
        self._end_btn_default_style = button_style("danger", theme)
        self._set_net_button_styles(self._net_in_progress)
        self.suspend_btn.setStyleSheet(button_style("warning", theme))
        self._update_group_button_state()
        self._update_spotter_button_state()
        self._refresh_ncs_session_context()
        self._update_preparation_summary()

    def apply_theme(self) -> None:
        self._apply_theme()

    def _maybe_reload_operating_groups(self):
        try:
            if hasattr(self.settings, "reload"):
                self.settings.reload()
        except Exception:
            pass
        og = self._load_operating_groups()
        sig = self._snapshot_operating_groups(og)
        if sig == self._opgroups_sig:
            return
        self._opgroups_sig = sig
        self._refresh_qsy_options(og)

    def _load_operating_groups(self) -> List[Dict]:
        return qsy_load_operating_groups(self.settings)

    def _snapshot_operating_groups(self, og_list: List[Dict]) -> str:
        return qsy_snapshot_operating_groups(og_list)

    def _refresh_qsy_options(self, og_list: Optional[List[Dict]] = None):
        ops = og_list if og_list is not None else self._load_operating_groups()
        self._opgroups_sig = self._snapshot_operating_groups(ops)
        self._qsy_options = build_qsy_options(ops)
        refresh_qsy_combo(self.qsy_combo, self._qsy_options)
        refresh_hold_duration_combo(self.hold_duration_combo, self.settings)
        fit_combo_box_to_contents(self.qsy_combo)
        fit_combo_box_to_contents(self.hold_duration_combo)
        self._update_qsy_button_enabled()

    def _selected_qsy_meta(self) -> Optional[Dict]:
        return selected_qsy_meta(self.qsy_combo)

    def _selected_hold_minutes(self) -> int:
        return selected_hold_duration(self.hold_duration_combo, self.settings)

    def _on_hold_duration_changed(self) -> None:
        mins = self._selected_hold_minutes()
        set_hold_duration_default(self.settings, mins)
        notify_hold_duration_default_changed(self.window())
        self._update_suspend_state()

    def _current_scheduler_freq(self) -> Optional[float]:
        return current_scheduler_freq(self.window())

    def _update_qsy_button_enabled(self):
        if self._suspend_active():
            self.suspend_btn.setEnabled(True)
            return
        enabled = self._scheduler_enabled()
        meta = self._selected_qsy_meta()
        if not enabled or not meta:
            self.suspend_btn.setEnabled(False)
            return
        cur = self._current_scheduler_freq()
        if cur is not None and abs(cur - meta.get("freq", -1)) < 0.001:
            self.suspend_btn.setEnabled(False)
        else:
            self.suspend_btn.setEnabled(True)

    def _perform_qsy(self, meta: Dict) -> bool:
        if getattr(self, '_preparation_scope', None) is not None:
            self._prepared_receiver._set_status('Prepared', 'Use the intended radio in the Station Control Bar to QSY')
            return False
        win = self.window()
        return perform_qsy(win, meta)

    # ---------------- START / END NET ---------------- #

    def _validate_before_start(self) -> bool:
        self._finish_group_selection()
        if not self.net_name_edit.text().strip():
            self._auto_prefill_net_name()
        self._preparation_callsign = bool(self._my_callsign())
        self._update_preparation_summary()
        missing_name = not self.net_name_edit.text().strip()
        missing_group = not self._group_target
        if missing_name or missing_group:
            self._open_preparation_link('name' if missing_name else 'group')
            return False
        return bool(self._preparation_callsign and self._directed_path)

    def _start_net(self):
        receiver = getattr(self, '_prepared_receiver', None)
        if receiver is not None and receiver.prepared is not None and not receiver.dispatching:
            receiver.start()
            return
        if self._net_in_progress:
            QMessageBox.information(self, "Net In Progress", "A net is already active. End it before starting a new one.")
            return
        workbench = getattr(self, 'workbench', None)
        if workbench is not None and workbench.run is not None and (workbench.busy or workbench.dirty_drafts or workbench.pending_label):
            QMessageBox.information(self, 'Pending net work', 'Wait for the current net record and working drafts to finish saving before starting another net.')
            return
        if not self._validate_before_start():
            return

        self._ncs_workbench_started_ts = time.time()
        self._net_in_progress = True
        profiles = self._ncs_radio_profiles()
        self._ncs_session_radio_id = self._ncs_selected_radio_id(profiles) or None
        self._ncs_session_id = getattr(getattr(self, '_prepared_receiver', None), 'pending_session_token', '') or uuid.uuid4().hex
        self._net_start_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        self._net_end_utc = None
        self._all_calls_seen.clear()
        self._queried_msg_ids.clear()
        self._pending_queries.clear()
        self._waiting_for_completion = False
        self._current_query = None
        self._pending_grid_queries.clear()
        getattr(self, "_fcc_identity_pending", {}).clear()
        self._grid_waiting = False
        self._awaiting_ack_for = None
        self._call_last_rx_ts.clear()
        self._checkins.clear()
        self._checkin_rows.clear()
        self._checkins_saved.clear()
        self._clear_table()
        self._auto_query_paused_by_net = True
        self._set_net_button_styles(active=True)
        self.end_btn.setEnabled(True)
        self.ack_btn.setEnabled(True)
        self.ack_callsign_btn.setEnabled(True)
        self._refresh_ncs_session_context()

          # Track file size so we only read new lines
        try:
            if self._directed_path:
                self._last_directed_size = self._directed_path.stat().st_size
                all_path = self._scoped_all_path()
                self._last_all_size = all_path.stat().st_size if all_path.exists() else 0
        except Exception:
            self._last_directed_size = 0
            self._last_all_size = 0
        self._last_query_tx_ts = 0.0

        if self._poll_timer:
            self._poll_timer.start()

        log.info("JS8Call net started: %s (%s)", self.net_name_edit.text().strip(), self.role_combo.currentText())
        self._refresh_operator_history_views()
        self._persist_ncs_session_snapshot(timing_state="active")
        self.net_status_changed.emit("JS8", True)
        self.net_session_status_changed.emit(
            "JS8", True, int(self._ncs_session_radio_id or 0), self._ncs_session_id
        )
        if getattr(self, 'workbench', None) is not None:
            self.workbench.on_started()
            self.net_setup_toggle.setChecked(False)


    def _start_ad_hoc_net(self):
        """
        Generate and start an ad hoc JS8 net with a UTC-stamped name.
        """
        if self._net_in_progress:
            QMessageBox.information(self, "Net In Progress", "End the current net before starting an ad hoc net.")
            return
        current = self.net_name_edit.text().strip()
        if current:
            resp = QMessageBox.question(
                self,
                "Replace Net Name",
                "Replace the current net name with an ad hoc name?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d %H:%M")
        ad_hoc_name = f"JS8 Net - Ad Hoc - {ts} UTC"
        self.net_name_edit.setText(ad_hoc_name)
        self._start_net()

    def _end_net(self):
        workbench = getattr(self, 'workbench', None)
        if workbench is not None and workbench.run is not None:
            if workbench._end_retry:
                workbench.request_end(lambda: None)
                return
            if not self._net_in_progress:
                return
            session_id, generation = self._ncs_session_id, workbench.generation
            choice = JS8CallNetControlTab._confirm_end_net(self)
            if self._ncs_session_id != session_id or workbench.generation != generation or not self._net_in_progress:
                workbench._status('Net context changed during confirmation; no End or concluding message was requested.')
                return
            if choice is not None:
                workbench.request_end(lambda: self._end_net_impl(_send_concluded=choice))
            return
        JS8CallNetControlTab._end_net_impl(self)

    def _confirm_end_net(self):
        prompt = QMessageBox(self)
        prompt.setIcon(QMessageBox.Question)
        prompt.setWindowTitle("End Net")
        prompt.setText("TX NET CONCLUDED MSG?")
        btn_yes = prompt.addButton("YES", QMessageBox.YesRole)
        btn_no = prompt.addButton("NO", QMessageBox.NoRole)
        btn_end_no_tx = prompt.addButton("END w/o TX", QMessageBox.DestructiveRole)
        prompt.setDefaultButton(btn_yes)
        prompt.exec()
        clicked = prompt.clickedButton()
        if clicked not in (btn_yes, btn_end_no_tx):
            return None
        return clicked == btn_yes

    def _end_net_impl(self, *, _send_concluded=None):
        if not self._net_in_progress:
            return
        session_radio_id = getattr(self, "_ncs_session_radio_id", None)
        session_id = getattr(self, "_ncs_session_id", "")
        send_concluded = _send_concluded
        if send_concluded is None:
            send_concluded = JS8CallNetControlTab._confirm_end_net(self)
            if (send_concluded is None or not self._net_in_progress
                    or getattr(self, '_ncs_session_id', '') != session_id
                    or getattr(self, '_ncs_session_radio_id', None) != session_radio_id):
                return

        previous_end = self._net_end_utc
        self._net_end_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        try:
            if not self._save_checkins(show_message=False):
                raise RuntimeError("Check-in counters could not be saved")
            log_path = self._write_net_log_file()
            if not log_path:
                raise RuntimeError("Net log could not be saved")
        except Exception as exc:
            self._net_end_utc = previous_end
            log.error("JS8CallNetControl: unable to end net: %s", exc)
            QMessageBox.critical(self, "Net Not Ended",
                                 f"Unable to save the net record: {exc}\n"
                                 "Check the log location and database, then retry End. "
                                 "The active session and check-ins have been retained.")
            return

        if getattr(self, '_preparation_scope', None) is not None:
            try:
                self._persist_ncs_session_snapshot(timing_state='ended')
            except Exception as exc:
                self._net_end_utc = previous_end
                self._prepared_receiver._set_status('Another net active', f'End record could not be saved: {exc}; work retained, retry End')
                return
        if self._poll_timer:
            self._poll_timer.stop()
        tx_confirmed = not send_concluded
        if send_concluded:
            try:
                group = self._group_target or self.group_edit.text().strip().upper()
                if group:
                    if not group.startswith("@"):
                        group = "@" + group
                    mycall = self._my_callsign()
                    if mycall:
                        tx_confirmed = self._send_js8_message(f"{mycall}: {group} NET CONCLUDED") is True
            except Exception as exc:
                log.error("JS8CallNetControl: concluding message outcome not confirmed: %s", exc)

        self._net_in_progress = False
        if getattr(self, '_preparation_scope', None) is None:
            self._persist_ncs_session_snapshot(timing_state="ended")
        self.net_status_changed.emit("JS8", False)
        self.net_session_status_changed.emit(
            "JS8", False, int(session_radio_id or 0), session_id
        )
        self._ncs_session_radio_id = None
        self._ncs_session_id = ""
        self._refresh_ncs_session_context()
        self._auto_query_paused_by_net = False
        self.end_btn.setEnabled(False)
        self._set_net_button_styles(active=False)
        self.ack_btn.setEnabled(False)
        self.ack_callsign_btn.setEnabled(False)
        self._checkins.clear()
        self._checkin_rows.clear()
        self._checkins_saved.clear()
        getattr(self, "_fcc_identity_pending", {}).clear()
        self._clear_table()
        if tx_confirmed:
            QMessageBox.information(self, "Net Ended", f"JS8Call net ended and log saved:\n{log_path}")
        else:
            QMessageBox.warning(self, "Net Ended; Message Not Confirmed",
                                f"JS8Call net ended and log saved:\n{log_path}\n\n"
                                "The concluding message was not confirmed; inspect JS8Call before retrying.")

    # ---------------- AUTO-PREFILL NET NAME ---------------- #

    def _auto_prefill_net_name(self):
        if self._net_in_progress or self.net_name_edit.text().strip():
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        group = (self._group_target or self.group_edit.text()).strip().upper().lstrip("@")
        candidates = set()
        for row in getattr(self, "_preparation_schedule", []):
            if not isinstance(row, dict):
                continue
            mode = str(row.get("mode") or "").strip().upper().replace(" ", "")
            if mode not in {"JS8", "JS8CALL"}:
                continue
            if not schedule_row_matches_target_context(
                row, device_profile_id=getattr(self, "_preparation_radio_id", None),
                operating_profile_id=getattr(self, "_preparation_operating_id", None),
            ):
                continue
            row_group = str(row.get("primary_js8call_group") or "").strip().upper().lstrip("@")
            if group and row_group and group != row_group:
                continue
            occurrence = next_net_occurrence(row, now)
            if occurrence and (occurrence["active"] or datetime.timedelta(0) <= occurrence["window_start"] - now <= datetime.timedelta(minutes=20)):
                name = str(row.get("net_name") or "").strip()
                if name:
                    candidates.add(name)
        if len(candidates) == 1:
            self.net_name_edit.setText(next(iter(candidates)))
        elif len(candidates) > 1:
            self.net_name_edit.setPlaceholderText("Multiple scheduled nets; choose or enter a name")

    def _update_preparation_summary(self, *_args):
        if not hasattr(self, "preparation_label"):
            return
        missing = []
        for field, absent, description in (
            (self.net_name_edit, not self.net_name_edit.text().strip(), 'Required: enter a net name'),
            (self.group_selector, not self._group_target, 'Required: select or enter a group')):
            absent = bool(absent and not self._net_in_progress)
            if field.property('fioRequiredMissing') != absent:
                field.setProperty('fioRequiredMissing', absent)
                field.style().unpolish(field)
                field.style().polish(field)
                field.update()
            field.setAccessibleDescription(description if absent else '')
        if not self.net_name_edit.text().strip():
            missing.append('<a href="name">net name</a>')
        if not getattr(self, "_preparation_callsign", False):
            missing.append('<a href="callsign">callsign</a>')
        if not getattr(self, "_directed_path", None):
            missing.append('<a href="js8">DIRECTED.TXT</a>')
        if not self._group_target:
            missing.append('<a href="group">group destination</a>')
        accent = active_app_theme()["accent"]
        links = ", ".join(missing).replace("<a href=", f'<a style="color:{accent}" href=')
        if self._net_in_progress:
            self.preparation_label.setText('')
            self.preparation_label.hide()
        else:
            self.preparation_label.setText("Before Start: " + links if missing else "Setup complete. Start Net checks the selected source before listening.")
            self.preparation_label.show()
            # Remain clickable for the existing validation/automatic-name route;
            # neutral styling never presents incomplete local setup as ready.
            style = button_style('muted' if missing else 'eligible_success', resolve_theme(self.settings))
            if self.start_btn.styleSheet() != style:
                self.start_btn.setStyleSheet(style)
            self.start_btn.setToolTip('Complete: ' + ', '.join(text for key, text in
                [('name', 'net name'), ('callsign', 'callsign'), ('js8', 'DIRECTED.TXT'), ('group', 'group destination')]
                if any(f'href="{key}"' in item for item in missing)) if missing
                else 'Start this net after the existing source and session checks.')


    def _open_preparation_link(self, link):
        if link in {"name", "group"}:
            self.net_setup_toggle.setChecked(True)
            field = self.net_name_edit if link == "name" else self.group_edit
            field.setFocus()
            self.js8_ncs_scroll_area.ensureWidgetVisible(field)
        elif link in {"callsign", "js8"}:
            host = self.window()
            if hasattr(host, "open_settings_section"):
                host.open_settings_section("operator_info" if link == "callsign" else "js8call",
                                           radio_id=getattr(self, "_preparation_radio_id", None))

    # ---------------- POLLING DIRECTED.TXT ---------------- #

    def _poll_directed_file(self):
        if getattr(self, '_preparation_scope', None) is not None and not self._net_in_progress:
            return
        if self._is_shutting_down or self._polling_directed:
            return
        self._polling_directed = True
        start_ts = time.time()
        try:
            if not self._directed_path:
                return
            monitor_announcements = True
            if not self._net_in_progress and not monitor_announcements:
                return

            log.debug("JS8CallNetControl: polling DIRECTED/ALL (net_in_progress=%s)", self._net_in_progress)

            try:
                size_now = self._directed_path.stat().st_size
            except Exception as e:
                log.error("JS8CallNetControl: stat DIRECTED.TXT failed: %s", e)
                return

            if size_now < self._last_directed_size:
                # File truncated or rotated; re-read from start
                self._last_directed_size = 0

            # Drop stale pending announcements
            now_ts = time.time()
            for k, ts in list(self._pending_announcements.items()):
                if now_ts - ts > 60:
                    self._pending_announcements.pop(k, None)
            # Expire pending query waits
            self._expire_pending_responses()

            try:
                with self._directed_path.open("r", encoding="utf-8", errors="ignore") as f:
                    if self._last_directed_size > 0:
                        f.seek(self._last_directed_size)
                    max_lines = 500
                    line_count = 0
                    last_pos = f.tell()
                    while True:
                        if line_count >= max_lines:
                            break
                        line = f.readline()
                        if not line:
                            break
                        last_pos = f.tell()
                        line_count += 1
                        line = line.strip()
                        if not line:
                            continue
                        if not self._line_ts_after_start(line):
                            log.debug("JS8 NCS: skipping DIRECTED line before app start: %s", line)
                            continue
                        calls = self._extract_callsigns_from_line(line)
                        msg_text = self._message_text_from_line(line)
                        call_primary = calls[0] if calls else ""
                        if call_primary and msg_text:
                            self._maybe_capture_geo_tokens(
                                call_primary,
                                msg_text,
                                group_name=self._active_group_name(),
                            )
                        self._update_recent_traffic(line, calls)
                        # Net announcement detection (only when net not in progress)
                        if self._line_has_announce_form(line):
                            # If message completion marker present, notify immediately; else mark pending
                            if self._is_message_complete_line(line):
                                log.debug(
                                    "JS8 NCS: F!106 complete line detected (net_in_progress=%s): %s",
                                    self._net_in_progress,
                                    line,
                                )
                                self._maybe_notify_announcement(call_primary, line)
                            else:
                                log.debug("JS8 NCS: F!106 partial line, waiting for completion: %s", line)
                                self._pending_announcements[(call_primary or "UNKNOWN")] = time.time()
                        # If a completion marker arrives, see if we had a pending announcement for this call
                        if self._is_message_complete_line(line) and self._pending_announcements:
                            call_primary = calls[0] if calls else "UNKNOWN"
                            pending_ts = self._pending_announcements.pop(call_primary, None)
                            if pending_ts:
                                log.debug(
                                    "JS8 NCS: F!106 completion arrived for pending call %s (net_in_progress=%s): %s",
                                    call_primary,
                                    self._net_in_progress,
                                    line,
                                )
                                self._maybe_notify_announcement(call_primary, line)
                        self._maybe_capture_grid_report(line)
                        self._maybe_record_inbound_trigger(line, calls)
                        msg_ids = self._extract_message_ids(line)
                        # If multiple stations reported YES MSG <id>, query each (only when addressed to us)
                        mycall = self._my_callsign()
                        if msg_ids and calls:
                            dest_cs = ""
                            try:
                                msg_field = line.split("\t", 4)[4]
                                if ":" in msg_field:
                                    dest_cs = msg_field.split(":", 1)[1].strip().split()[0].strip().upper()
                            except Exception:
                                dest_cs = ""
                            if not mycall:
                                log.info("JS8CallNetControl: YES MSG line but no mycall set; skipping: %s", line.strip())
                            elif dest_cs != mycall:
                                log.info(
                                    "JS8CallNetControl: YES MSG line addressed to %s (not %s); skipping",
                                    dest_cs or "(unknown)",
                                    mycall,
                                )
                            elif not self._is_message_complete_line(line):
                                log.debug(
                                    "JS8CallNetControl: YES MSG line incomplete; waiting for completion: %s",
                                    line.strip(),
                                )
                            else:
                                for c in calls:
                                    for mid in msg_ids:
                                        self._backlog_upsert(c, mid, "MSG", status="PENDING")
                        if not call_primary:
                            continue

                        # During an active net, record/update the check-in row
                        if self._net_in_progress:
                            if not self._receiver_traffic_belongs_to_net('', line.split('\t', 4)[-1]):
                                continue
                            if (
                                not self._should_accept_checkin_line(line)
                                and call_primary not in self._checkins
                            ):
                                continue
                            snr_line, offset_line = self._parse_directed_metrics(line)
                            speed_guess = self._call_last_speed.get(self._base_callsign(call_primary))
                            mode_name = ""
                            if speed_guess is not None:
                                mode_name = js8_speed_name(speed_guess)
                            base_call = self._base_callsign(call_primary) if call_primary else ""
                            if base_call and self._checkins.get(call_primary, {}).get("offset") is not None:
                                offset_line = None
                            self._upsert_checkin(
                                call_primary,
                                status="NEW",
                                mode=mode_name or None,
                                snr=snr_line,
                                offset=offset_line,
                            )

                    self._last_directed_size = int(last_pos)
                    self._save_ncs_offset(self._directed_offset_key, self._last_directed_size)
            except Exception as e:
                log.error("JS8CallNetControl: failed reading DIRECTED.TXT: %s", e)
                return

            # If no net in progress, skip UI updates (auto-query can still run)
            if not self._net_in_progress:
                return
        finally:
            self._polling_directed = False
            elapsed = time.time() - start_ts
            if elapsed > 0.5:
                log.debug("JS8CallNetControl: directed poll took %.2fs", elapsed)

    def _poll_all_for_query_tx(self):
        """
        Scan ALL.TXT for outgoing QUERY MSG(S) transmissions to enable auto-query from DIRECTED.
        """
        if self._auto_query_paused_by_net:
            return
        if not self._directed_path:
            return
        all_path = self._scoped_all_path()
        if not all_path.exists():
            return
        try:
            size_now = all_path.stat().st_size
        except Exception as e:
            log.error("JS8CallNetControl: stat ALL.TXT failed: %s", e)
            return
        if size_now < self._last_all_size:
            self._last_all_size = 0
        try:
            with all_path.open("r", encoding="utf-8", errors="ignore") as f:
                if self._last_all_size > 0:
                    f.seek(self._last_all_size)
                last_pos = f.tell()
                while True:
                    line = f.readline()
                    if not line:
                        break
                    last_pos = f.tell()
                    if "Transmitting" not in line:
                        continue
                    if not self._line_ts_after_start(line):
                        continue
                    up = line.upper()
                    mycall = self._my_callsign()
                    if mycall and f"{mycall}:" in up:
                        self._last_tx_ts = time.time()
                    if "QUERY MSG" in up:
                        self._last_query_tx_ts = time.time()
                        log.info("JS8CallNetControl: detected outgoing QUERY MSG in ALL.TXT: %s", line.strip())
                    # Track outbound direct transmissions to add untrusted operators
                    self._maybe_register_outgoing_call(line)
                self._last_all_size = int(last_pos)
                self._save_ncs_offset(self._all_offset_key, self._last_all_size)
        except Exception as e:
            log.error("JS8CallNetControl: failed reading ALL.TXT: %s", e)
            return

    def _saw_recent_query_tx(self, window_sec: int = 600) -> bool:
        """
        Return True if a QUERY MSG(S) transmit was seen in ALL.TXT within the last window.
        """
        if self._last_query_tx_ts <= 0:
            return False
        return (time.time() - self._last_query_tx_ts) <= window_sec

    def _line_ts_after_start(self, line: str) -> bool:
        """
        Return True if the line begins with a timestamp after app start.
        """
        try:
            ts_str = line[:19]
            ts = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
            log.debug("JS8CallNetControl: parsed line ts=%s (app_start=%s)", ts, self._app_start_ts)
            return ts > self._app_start_ts
        except Exception:
            return False

    # ---------------- CHECK-IN TABLE HELPERS ---------------- #

    def _clear_table(self) -> None:
        self.checkin_table.setRowCount(0)
        self._update_checkin_empty_state()

    def _fcc_session_token(self) -> tuple[str, Optional[int], bool]:
        return (
            str(getattr(self, "_ncs_session_id", "") or ""),
            getattr(self, "_ncs_session_radio_id", None),
            bool(getattr(self, "_net_in_progress", False)),
        )

    def _drop_fcc_pending(self, callsign: str) -> None:
        call = str(callsign or "").strip().upper()
        if call:
            self._fcc_identity_pending.pop(call, None)

    def _queue_fcc_identity(self, callsign: str, token: tuple[str, Optional[int], bool]) -> None:
        call = str(callsign or "").strip().upper()
        if not call:
            return
        pending = getattr(self, "_fcc_identity_pending", None)
        if pending is None:
            return
        if call not in pending and len(pending) >= 512:
            oldest = next(iter(pending), "")
            if oldest:
                pending.pop(oldest, None)
        pending[call] = token

    @staticmethod
    def _fcc_record_fields(record) -> tuple[str, str]:
        kind = str(getattr(record, "kind", "") or "").strip()
        first = str(getattr(record, "first_name", "") or "").strip()
        last = str(getattr(record, "last_name", "") or "").strip()
        name = " ".join(part for part in (first, last) if part).strip()
        if kind != "Individual" or not name:
            name = str(getattr(record, "name", "") or "").strip()
        return name, str(getattr(record, "state", "") or "").strip().upper()

    def _store_fcc_metadata(self, data: Dict, record) -> None:
        name, state = self._fcc_record_fields(record)
        data.update(
            {
                "fcc_name": name,
                "fcc_state": state,
                "fcc_service": str(getattr(record, "service", "") or "").strip(),
                "fcc_source_date": str(getattr(record, "source_date", "") or "").strip(),
            }
        )

    def _apply_fcc_identity(
        self,
        callsign: str,
        records: tuple,
        token: tuple[str, Optional[int], bool],
        *,
        update_widget: bool,
    ) -> bool:
        call = str(callsign or "").strip().upper()
        if token != self._fcc_session_token() or not token[2]:
            self._drop_fcc_pending(call)
            return False
        if call not in self._checkins or len(records) != 1:
            self._drop_fcc_pending(call)
            return False
        data = self._checkins[call]
        record = records[0]
        fcc_name, fcc_state = self._fcc_record_fields(record)
        previous_name = str(data.get("name") or "").strip()
        previous_state = str(data.get("state") or "").strip().upper()
        self._store_fcc_metadata(data, record)
        if not previous_name and fcc_name:
            data["name"] = fcc_name
            data["_fcc_name_fallback"] = True
        if not previous_state and fcc_state:
            data["state"] = fcc_state
            data["_fcc_state_fallback"] = True
        self._drop_fcc_pending(call)
        changed = bool((not previous_name and fcc_name) or (not previous_state and fcc_state))
        if changed and update_widget and self._active:
            self._update_row(call, data)
        return changed

    def _apply_pending_fcc_identities(self) -> None:
        if not self._active or not self._net_in_progress:
            return
        token = self._fcc_session_token()
        pending = getattr(self, "_fcc_identity_pending", {})
        broker = getattr(self, "_fcc_identity_broker", None)
        if broker is None:
            return
        for call in tuple(pending):
            if pending.get(call) != token:
                self._drop_fcc_pending(call)
                continue
            record = broker.identity(JS8CallNetControlTab._base_callsign(call))
            if record is None:
                # The worker may still be in flight. Keep the session token
                # so its eventual result can fill this row.
                continue
            self._apply_fcc_identity(call, (record,), token, update_widget=True)

    def _on_fcc_identities_ready(self, rows) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        grouped: Dict[str, list] = {}
        for record in tuple(rows or ()):
            call = str(getattr(record, "callsign", "") or "").strip().upper()
            for pending_call in getattr(self, "_fcc_identity_pending", {}):
                if JS8CallNetControlTab._base_callsign(pending_call) == call:
                    grouped.setdefault(pending_call, []).append(record)
        token = self._fcc_session_token()
        for call, records in grouped.items():
            if self._fcc_identity_pending.get(call) != token:
                self._drop_fcc_pending(call)
                continue
            if not self._active:
                continue
            broker = getattr(self, "_fcc_identity_broker", None)
            record = broker.identity(JS8CallNetControlTab._base_callsign(call)) if broker is not None else None
            if record is None:
                continue
            self._apply_fcc_identity(call, (record,), token, update_widget=True)

    def _rebuild_checkin_table(self) -> None:
        self._checkin_rows = {}
        self._clear_table()
        for cs, data in self._checkins.items():
            if cs not in getattr(self, '_excluded_checkins', set()):
                self._update_row(cs, data)
        self._update_checkin_empty_state()

    def _update_checkin_empty_state(self) -> None:
        if not hasattr(self, "checkin_empty_label") or not hasattr(self, "checkin_table"):
            return
        if hasattr(self, 'workbench_panel'):
            self.checkin_empty_label.hide()
            self.checkin_table.hide()
            return
        has_rows = self.checkin_table.rowCount() > 0
        self.checkin_empty_label.setVisible(not has_rows)
        self.checkin_table.setVisible(has_rows)

    def _checkin_filter_mode(self) -> str:
        if hasattr(self, "checkin_filter_combo"):
            return self.checkin_filter_combo.currentText().strip()
        return "Mapped Check-ins"

    def _mapped_checkin_forms(self) -> set[str]:
        forms = forms_enabled_for(self.settings, purpose=PURPOSE_NET_CHECKIN, flag="net")
        if self._has_custom_spotter_mapper():
            return forms
        return forms or set(CHECKIN_FORMS) or legacy_default_forms_for(purpose=PURPOSE_NET_CHECKIN, flag="net")

    def _mapped_announcement_forms(self) -> set[str]:
        forms = forms_enabled_for(self.settings, purpose=PURPOSE_NET_NOTIFICATION, flag="alert")
        if self._has_custom_spotter_mapper():
            return forms
        return forms or {ANNOUNCE_FORM} or legacy_default_forms_for(purpose=PURPOSE_NET_NOTIFICATION, flag="alert")

    def _has_custom_spotter_mapper(self) -> bool:
        try:
            raw = self.settings.get(MAPPER_SETTINGS_KEY, [])
        except Exception:
            raw = []
        return isinstance(raw, list) and bool(raw)

    def _is_checkin_form_code(self, form_code: object) -> bool:
        return str(form_code or "").strip().upper() in self._mapped_checkin_forms()

    def _delete_action_text(self) -> str:
        if not self._net_in_progress:
            return ""
        return "Exclude"

    def _on_checkin_filter_changed(self) -> None:
        self._rebuild_checkin_table()

    def _on_checkin_table_clicked(self, row: int, col: int) -> None:
        delete_col = self.checkin_table.columnCount() - 1
        if col != delete_col:
            return
        if not self._delete_action_text():
            return
        item = self.checkin_table.item(row, 0)
        callsign = item.text().strip().upper() if item else ""
        if not callsign:
            return
        workbench = getattr(self, 'workbench', None)
        if workbench is not None:
            row = next((row for row in workbench.snapshot.get('roster', ()) if row.get('from_call') == callsign), None)
            if row:
                workbench.annotate('station', row, {'excluded': True})
            return
        # Compatibility fixture path: retain data and count markers independently.
        self._excluded_checkins = getattr(self, '_excluded_checkins', set()) | {callsign}
        self._rebuild_checkin_table()

    def _region_for_state(self, st: str) -> str:
        st = (st or "").strip().upper()
        fema = {
            "CT": "R01",
            "ME": "R01",
            "MA": "R01",
            "NH": "R01",
            "RI": "R01",
            "VT": "R01",
            "NJ": "R02",
            "NY": "R02",
            "PR": "R02",
            "VI": "R02",
            "DC": "R03",
            "DE": "R03",
            "MD": "R03",
            "PA": "R03",
            "VA": "R03",
            "WV": "R03",
            "AL": "R04",
            "FL": "R04",
            "GA": "R04",
            "KY": "R04",
            "MS": "R04",
            "NC": "R04",
            "SC": "R04",
            "TN": "R04",
            "IL": "R05",
            "IN": "R05",
            "MI": "R05",
            "MN": "R05",
            "OH": "R05",
            "WI": "R05",
            "AR": "R06",
            "LA": "R06",
            "NM": "R06",
            "OK": "R06",
            "TX": "R06",
            "IA": "R07",
            "KS": "R07",
            "MO": "R07",
            "NE": "R07",
            "CO": "R08",
            "MT": "R08",
            "ND": "R08",
            "SD": "R08",
            "UT": "R08",
            "WY": "R08",
            "AZ": "R09",
            "CA": "R09",
            "HI": "R09",
            "NV": "R09",
            "GU": "R09",
            "AS": "R09",
            "MP": "R09",
            "AK": "R10",
            "ID": "R10",
            "OR": "R10",
            "WA": "R10",
        }
        return fema.get(st, "")

    def _lookup_operator_meta(self, callsign: str) -> Dict[str, str]:
        meta = {"name": "", "state": "", "grid": "", "region": ""}
        cs = self._base_callsign(callsign)
        if not cs:
            return meta
        try:
            root = Path(__file__).resolve().parents[2]
            db_path = self._ncs_nets_db_path()
            if not db_path.exists():
                return meta
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT name, state, grid FROM operator_checkins WHERE callsign=?",
                (cs,),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                meta["name"] = row[0] or ""
                meta["state"] = (row[1] or "").upper()
                meta["grid"] = (row[2] or "").upper()
                if meta["state"]:
                    meta["region"] = self._region_for_state(meta["state"])
        except Exception:
            pass
        return meta

    def _ensure_row(self, callsign: str) -> int:
        if callsign in self._checkin_rows:
            return self._checkin_rows[callsign]
        row = self.checkin_table.rowCount()
        self.checkin_table.insertRow(row)
        self._checkin_rows[callsign] = row
        for col in range(self.checkin_table.columnCount()):
            item = QTableWidgetItem("")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.checkin_table.setItem(row, col, item)
        return row

    def _update_row(self, callsign: str, data: Dict) -> None:
        row = self._ensure_row(callsign)
        cols = ["CALLSIGN", "NAME", "ST", "GRID", "REGION", "MODE", "SNR", "OFFSET", "STATUS", "DELETE"]
        values = [
            callsign,
            data.get("name", ""),
            data.get("state", ""),
            data.get("grid", ""),
            data.get("region", ""),
            data.get("mode", ""),
            "" if data.get("snr") is None else str(data.get("snr")),
            "" if data.get("offset") is None else str(data.get("offset")),
            data.get("status", ""),
            self._delete_action_text(),
        ]
        for idx, val in enumerate(values):
            item = self.checkin_table.item(row, idx)
            if item is None:
                item = QTableWidgetItem()
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.checkin_table.setItem(row, idx, item)
            item.setText(val)
            if cols[idx] == "NAME" and data.get("fcc_name"):
                item.setToolTip(
                    "FCC licensee: " + str(data["fcc_name"])
                    + " (may differ from the operator identity)."
                )
            elif cols[idx] == "ST" and data.get("fcc_state"):
                item.setToolTip(
                    "FCC mailing state: " + str(data["fcc_state"])
                    + " (not a live operating location)."
                )
            # Status coloring
            if cols[idx] == "STATUS":
                theme = resolve_theme(self.settings)
                status_upper = val.upper()
                mismatch = self._status_mismatch.get(callsign, False)
                if status_upper == "ACKED":
                    item.setBackground(Qt.green)
                elif status_upper.startswith("F!"):
                    item.setBackground(Qt.red if mismatch else Qt.cyan)
                elif status_upper == "NEW":
                    bg = QColor(theme["warning"])
                    bg.setAlpha(60)
                    item.setBackground(bg)
                else:
                    item.setBackground(QColor(theme["surface_alt"]))
        self._update_checkin_empty_state()

    def _receiver_traffic_belongs_to_net(self, recipient: str, text: str = '') -> bool:
        """Raw receiver evidence may establish only its explicitly addressed net.

        Canonical reports use pinned source, reception time and destination for
        association, retaining relay provenance without a blanket relay exclusion.
        Existing accepted stations remain independent of this test.
        """
        if not self._ncs_session_id:
            return True  # Existing unscoped compatibility callers have no run.
        destination = str(recipient or '').strip().upper()
        if not destination and ':' in text:
            destination = text.split(':', 1)[1].strip().split(' ', 1)[0].upper()
        if '>' in destination:
            return False
        group = str(self._group_target or '').strip().upper()
        if group and not group.startswith('@'):
            group = '@' + group
        # Additional query groups enter through canonical, timestamp-qualified evidence.
        # Raw buffered lines have no reliable query-time fence here.
        return bool(destination and destination in {group, self._my_callsign().upper()})

    def _upsert_checkin(
        self,
        callsign: str,
        *,
        status: str = "NEW",
        mode: Optional[str] = None,
        snr: Optional[float] = None,
        offset: Optional[int] = None,
        grid: str = "",
        status_mismatch: bool = False,
    ) -> None:
        cs = (callsign or "").strip().upper()
        if not cs:
            return
        workbench = getattr(self, 'workbench', None)
        if workbench is not None and cs in workbench.excluded:
            return
        base = cs.split("/", 1)[0]
        identity = cs if workbench is not None else base
        meta = self._lookup_operator_meta(base)
        if grid:
            meta["grid"] = grid
        data = self._checkins.get(identity, {})
        current_name = str(data.get("name") or "").strip()
        current_state = str(data.get("state") or "").strip().upper()
        local_name = str(meta.get("name") or "").strip()
        local_state = str(meta.get("state") or "").strip().upper()
        if data.get("_fcc_name_fallback"):
            if current_name and data.get("fcc_name") and current_name != data.get("fcc_name"):
                data["_fcc_name_fallback"] = False
            elif local_name and local_name != current_name:
                current_name = local_name
                data["_fcc_name_fallback"] = False
        if data.get("_fcc_state_fallback"):
            if current_state and data.get("fcc_state") and current_state != data.get("fcc_state"):
                data["_fcc_state_fallback"] = False
            elif local_state and local_state != current_state:
                current_state = local_state
                data["_fcc_state_fallback"] = False
        name = current_name or local_name
        state = current_state or local_state
        fcc_record = None
        broker = getattr(self, "_fcc_identity_broker", None)
        token = self._fcc_session_token()
        if self._net_in_progress and broker is not None and (not name or not state):
            try:
                fcc_record = broker.identity(base)
            except Exception:
                fcc_record = None
            if fcc_record is not None:
                fcc_name, fcc_state = self._fcc_record_fields(fcc_record)
                self._store_fcc_metadata(data, fcc_record)
                if not name and fcc_name:
                    name = fcc_name
                    data["_fcc_name_fallback"] = True
                if not state and fcc_state:
                    state = fcc_state
                    data["_fcc_state_fallback"] = True
                self._drop_fcc_pending(identity)
            else:
                self._queue_fcc_identity(identity, token)
                try:
                    broker.request(base, interactive=False)
                except Exception:
                    pass
        elif name and state:
            self._drop_fcc_pending(identity)
        current_status = (data.get("status") or "").upper()
        # Do not downgrade from ACKED/F!xxx to NEW
        if status:
            if current_status.startswith("F!") or current_status == "ACKED":
                status_to_use = data.get("status", status)
            else:
                status_to_use = status
        else:
            status_to_use = data.get("status", "")
        try:
            snr = float(snr) if snr not in (None, "") else None
            if snr is not None and not math.isfinite(snr):
                snr = None
        except (TypeError, ValueError, OverflowError):
            snr = None
        if snr is not None:
            data["snr_received_ts"] = time.time()
        data.update(
            {
                "name": name,
                "state": state,
                "grid": grid or data.get("grid") or meta.get("grid", ""),
                "region": meta.get("region") or data.get("region", ""),
                "mode": mode or data.get("mode", ""),
                "snr": snr if snr is not None else data.get("snr"),
                "offset": offset if offset is not None else data.get("offset"),
                "status": status_to_use,
            }
        )
        self._checkins[identity] = data
        self._status_mismatch[identity] = status_mismatch
        self._update_row(identity, data)

    def _selected_callsign(self) -> Optional[str]:
        panel = getattr(self, 'workbench_panel', None)
        if panel is not None:
            selected_row = panel._selected_roster() or {}
            if selected_row.get('from_call'):
                return str(selected_row['from_call']).strip().upper()
        selected = self.checkin_table.selectedItems()
        if not selected:
            return None
        row = selected[0].row()
        item = self.checkin_table.item(row, 0)
        return item.text().strip().upper() if item else None

    def _adopt_group_text(self, *_args):
        """Adopt an idle CP group immediately; preserve active-session ownership."""
        if self._net_in_progress:
            return
        name = normalize_group_name(self.group_edit.text())
        self._group_target = '@' + name if name else ''
        self._update_workspace_presentation()

    def _on_group_choice_finished(self, *_args):
        self._finish_group_selection()
        key = self._query_operating_group_key()
        if not self._net_in_progress and key != getattr(self, '_net_form_tools_group_key', ''):
            self._form_mappings_dirty = True
            self._review_form_tools()

    def _finish_group_selection(self, *_args):
        name = normalize_group_name(self.group_edit.text())
        if self._net_in_progress and name != normalize_group_name(self._group_target):
            self.form_tools_status.setText('The active net retains its operating group. End it before changing groups; your draft is retained.')
            self.form_tools_status.show()
            return
        self.group_edit.setText(name)
        self._adopt_group_text()

    def _set_group_target(self):
        # Compatibility for old callers/drafts. There is no user-facing Set step.
        self._finish_group_selection()

    def _update_group_button_state(self):
        self.set_group_btn.hide()

    def _adopt_query_selection(self, *_args):
        code = self._current_spotter_code()
        self._spotter_form = code if code in self._available_net_form_codes else None
        self._expected_form = self._spotter_form
        self._on_spotter_selection_changed()

    def _set_spotter_form(self):
        if not self.spotter_combo.isEnabled():
            QMessageBox.warning(self, "Spotter", "No MCF forms found.")
            return
        if self._current_spotter_code() not in self._available_net_form_codes:
            QMessageBox.warning(self, "Spotter", "Select an available form first.")
            return
        self._spotter_form = self._current_spotter_code()
        self._expected_form = self._spotter_form
        self._on_spotter_selection_changed()

    def _current_spotter_code(self) -> str:
        data = self.spotter_combo.currentData()
        if data:
            return str(data).strip().upper()
        text = self.spotter_combo.currentText().strip().upper()
        if "|" in text:
            text = text.split("|")[-1].strip()
        if " - " in text:
            text = text.split(" - ", 1)[0].strip()
        return text

    def _update_spotter_button_state(self):
        theme = resolve_theme(self.settings)
        if self._spotter_form:
            self.set_spotter_btn.setStyleSheet(button_style("muted", theme))
        else:
            self.set_spotter_btn.setStyleSheet(button_style("muted", theme))

    def _update_query_controls(self):
        panel = getattr(self, 'workbench_panel', None)
        if panel is None:
            return
        writable = bool(self._net_in_progress and panel._snapshot.get('writable'))
        available = self._spotter_form in self._available_net_form_codes
        for button in (panel.query_group_button, panel.query_station_button):
            has_target = button is panel.query_station_button or bool(self._query_group_destination())
            button.setEnabled(writable and available and has_target)
            button.setToolTip('Send the selected form query' if writable and available else
                              'Choose an optional query form to send a query' if writable else
                              'Start an active net to send a query; queries are not required to start')
        self._update_live_query_target()

    def _on_spotter_selection_changed(self) -> None:
        theme = resolve_theme(self.settings)
        current = self._current_spotter_code()
        available = getattr(self, "_available_net_form_codes", set())
        self.set_spotter_btn.setEnabled(current in available)
        self.group_spotter_btn.setEnabled(self._spotter_form in available)
        self.single_spotter_btn.setEnabled(self._spotter_form in available)
        if current and current != (self._spotter_form or ""):
            self.set_spotter_btn.setStyleSheet(button_style("eligible_info", theme))
        else:
            self._update_spotter_button_state()
        self._update_query_controls()

    def _roster_groups_scope(self):
        return (str(self._ncs_nets_db_path()), id(self.settings), id(self._preparation_scope))

    def _refresh_group_completer(self):
        scope = self._roster_groups_scope()
        if scope != self._roster_groups_cache_scope:
            self._roster_groups_cache_scope = scope
            self._roster_group_names = ()
            self._roster_groups_checked = 0.0
        self._present_roster_group_choices()
        if self._roster_groups_inflight or self._is_shutting_down:
            return
        if time.monotonic() - self._roster_groups_checked < 2:
            return
        from freqinout.core.operator_groups import load_hf_roster_group_names
        from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor
        if self._form_tools_executor is None:
            self._form_tools_executor = DaemonSerialExecutor(thread_name_prefix='fio-ncs-form-tools')
        token = self._roster_groups_scope()
        self._roster_groups_inflight = True
        future = self._form_tools_executor.submit(load_hf_roster_group_names, token[0])
        def completed(result):
            try:
                value, error = result.result(), None
            except Exception as exc:
                value, error = (), str(exc)
            if not self._is_shutting_down:
                try:
                    self.roster_groups_ready.emit(token, value, error)
                except RuntimeError:
                    pass
        future.add_done_callback(completed)

    def _publish_roster_groups(self, token, groups, error):
        self._roster_groups_inflight = False
        if self._is_shutting_down:
            return
        if token != self._roster_groups_scope():
            QTimer.singleShot(0, self._refresh_group_completer)
            return
        self._roster_groups_checked = time.monotonic()
        if error:
            self.group_selector.setToolTip('Roster groups could not be loaded; your entered group is retained. Reopen NCS to retry.')
            return
        self._roster_group_names = tuple(groups)
        self._present_roster_group_choices()

    def _present_roster_group_choices(self):
        for combo in (self.group_selector, getattr(self, 'query_group_selector', None)):
            if combo is None:
                continue
            current = combo.currentText()
            blocked = combo.blockSignals(True)
            edit_blocked = combo.lineEdit().blockSignals(True)
            combo.clear()
            combo.addItems(list(self._roster_group_names))
            combo.setCurrentIndex(-1)
            combo.setEditText(current)
            combo.lineEdit().blockSignals(edit_blocked)
            combo.blockSignals(blocked)
            completer = QCompleter(list(self._roster_group_names), combo)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            combo.setCompleter(completer)
        self.group_edit.setPlaceholderText('Select or enter a roster group')

    def _query_group_destination(self):
        name = normalize_group_name(self.query_group_selector.currentText())
        return '@' + name if name else ''

    def _present_ack_group_menu(self, snapshot, writable):
        session = self._ncs_session_id
        if self._net_in_progress and session != self._query_destination_session:
            self._query_destination_session = session
            self.query_group_selector.setEditText(normalize_group_name(self._group_target))
        targets = tuple(snapshot.get('query_groups') or ())
        key = ((snapshot.get('run') or {}).get('run_id'), targets)
        if key != getattr(self, '_ack_menu_key', None):
            old = self.ack_btn.menu()
            menu = QMenu(self.ack_btn) if targets else None
            if menu is not None:
                for target in targets:
                    action = menu.addAction(normalize_group_name(target))
                    action.triggered.connect(lambda checked=False, dest=target, run_id=key[0]:
                        self.workbench.acknowledge(destination=dest, expected_run=run_id))
            self.ack_btn.setMenu(menu)
            if old is not None:
                old.deleteLater()
            self._ack_menu_key = key
        self.ack_btn.setEnabled(bool(writable and targets))
        self.ack_btn.setToolTip('Acknowledge to a group queried in this net' if targets else 'Submit an Expect group query before acknowledging a group')

    def _ack_checkins(self):
        workbench = getattr(self, 'workbench', None)
        if workbench is not None and workbench.run is not None:
            if self.ack_btn.menu() is not None:
                return
            workbench.acknowledge()
            return
        if not self._net_in_progress:
            QMessageBox.information(self, "Net", "Start the net before ACK.")
            return
        new_calls = [c for c, d in self._checkins.items() if (d.get("status") or "").upper() == "NEW"]
        if not new_calls:
            QMessageBox.information(self, "ACK", "No NEW check-ins to ACK.")
            return
        short_codes = self._build_short_code_summary(new_calls)
        if not short_codes:
            QMessageBox.information(self, "ACK", "No callsigns to ACK.")
            return
        text = f"ACK {short_codes}"
        resp = QMessageBox.question(
            self,
            "ACK",
            f"SEND ACK?\n\n{text}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        if self._send_js8_message(text):
            for cs in new_calls:
                self._upsert_checkin(cs, status="ACKED")

    def _ack_callsign(self):
        workbench = getattr(self, 'workbench', None)
        if workbench is not None and workbench.run is not None:
            selected = self._selected_callsign()
            if not selected:
                workbench._status('Select a station before acknowledging it.')
                return
            workbench.acknowledge(selected)
            return
        if not self._net_in_progress:
            QMessageBox.information(self, "Net", "Start the net before ACK.")
            return
        cs = self._selected_callsign()
        if not cs:
            QMessageBox.information(self, "ACK", "Select one check-in row.")
            return
        text = f"ACK {cs}"
        resp = QMessageBox.question(
            self,
            "ACK",
            f"SEND ACK?\n\n{text}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        if self._send_js8_message(text):
            self._upsert_checkin(cs, status="ACKED")

    def _group_spotter(self):
        if getattr(self, 'workbench', None) is not None and self.workbench.run:
            self.workbench.query(self._query_group_destination())
            return
        if self._spotter_form not in getattr(self, '_available_net_form_codes', set()):
            QMessageBox.warning(self, 'Form unavailable', 'Review form tools and select an available form before sending. Your choice is retained.')
            return
        if not self._net_in_progress:
            QMessageBox.information(self, "Net", "Start the net first.")
            return
        group = self._query_group_destination()
        if group and not group.startswith("@"):
            group = "@" + group
        if not group:
            QMessageBox.warning(self, "Group", "Set a group first.")
            return
        if self._spotter_form is None:
            QMessageBox.warning(self, "Spotter", "Select a spotter form first.")
            return
        mycall = self._my_callsign()
        if not mycall:
            QMessageBox.warning(self, "Callsign", "Configure your callsign in Configuration.")
            return
        text = f"{mycall}: {group} E? {self._spotter_form}"
        resp = QMessageBox.question(
            self,
            "Send Spotter",
            f"Send this JS8Call message?\n\n{text}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        self._expected_form = self._spotter_form
        self._send_js8_message(text)

    def _single_spotter(self):
        if getattr(self, 'workbench', None) is not None and self.workbench.run:
            row = self.workbench_panel._selected_roster() or {}
            self.workbench.query(str(row.get('from_call') or ''))
            return
        if self._spotter_form not in getattr(self, '_available_net_form_codes', set()):
            QMessageBox.warning(self, 'Form unavailable', 'Review form tools and select an available form before sending. Your choice is retained.')
            return
        if not self._net_in_progress:
            QMessageBox.information(self, "Net", "Start the net first.")
            return
        if self._spotter_form is None:
            QMessageBox.warning(self, "Spotter", "Select a spotter form first.")
            return
        cs = self._selected_callsign()
        if not cs:
            QMessageBox.information(self, "Spotter", "Select one check-in row.")
            return
        mycall = self._my_callsign()
        if not mycall:
            QMessageBox.warning(self, "Callsign", "Configure your callsign in Configuration.")
            return
        text = f"{mycall}: {cs} E? {self._spotter_form}"
        resp = QMessageBox.question(
            self,
            "Send Spotter",
            f"Send this JS8Call message?\n\n{text}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        self._expected_form = self._spotter_form
        self._send_js8_message(text)

    def _save_checkins(self, *, show_message: bool = True):
        workbench = getattr(self, 'workbench', None)
        if getattr(self, '_end_participant_calls', None) is not None:
            # The workbench worker already committed every eligible counter and
            # once-per-base marker. End must not enter a second increment path.
            self._refresh_operator_history_views()
            return True
        if show_message and workbench is not None and workbench.run is not None:
            workbench.save_participants()
            return False  # Explicit UI action is asynchronous; End uses its own gate.
        """
        Increment check-in counters for current check-ins (once per net).
        """
        try:
            calls = getattr(self, '_end_participant_calls', None)
            for cs in (self._checkins.keys() if calls is None else calls):
                if calls is None and cs in getattr(getattr(self, 'workbench', None), 'excluded', set()):
                    continue
                base = cs.split("/", 1)[0]
                if base in self._checkins_saved:
                    continue
                self._increment_checkin_counter(base)
                self._checkins_saved.add(base)
        except Exception as e:
            log.error("JS8CallNetControl: save checkins failed: %s", e)
            if show_message:
                QMessageBox.critical(self, "Save Failed", f"Unable to record all check-ins: {e}\nRetry Save.")
            return False
        if show_message:
            QMessageBox.information(self, "Saved", "Check-ins recorded for this net.")
        self._refresh_operator_history_views()
        return True

    # ---------------- LOG FILE WRITING ---------------- #

    def _write_net_log_file(self):
        """
        Write net log into net_logs directory under DIRECTED.TXT's directory.

        Filename: netname-ROLE-YYYYMMDD-occurrence.txt
        Header:
          # Net: ...
          # Role: ...
          # Start UTC: ...
          # End UTC: ...
          # Band: ... (if found)
        Body:
          one full callsign per line.
        """
        if not self._directed_path:
            raise RuntimeError("DIRECTED.TXT location is not configured")

        net_name = self.net_name_edit.text().strip()
        role = self.role_combo.currentText().strip().upper()
        if not net_name:
            net_name = "UNKNOWN_NET"

        date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")

        base_dir = self._directed_path.parent
        net_logs_dir = base_dir / "net_logs"
        try:
            net_logs_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise OSError(f"Unable to create net log directory {net_logs_dir}: {e}") from e

        safe_net = "".join(c for c in net_name if c.isalnum() or c in ("_", "-", " "))
        safe_net = safe_net.replace(" ", "_") or "net"
        filename = f"{safe_net}-{role}-{date_str}-{uuid.uuid4().hex}.txt"
        path = net_logs_dir / filename

        # Collect all full callsigns from the table
        excluded = getattr(getattr(self, 'workbench', None), 'excluded', set())
        complete = getattr(self, '_end_participant_calls', None)
        all_calls = list(complete) if complete is not None else [call for call in self._checkins if call not in excluded]

        # Try to find band from net_schedule
        band = self._lookup_band_for_net(net_name)

        lines: List[str] = []
        lines.append(f"# Net: {net_name}")
        lines.append(f"# Role: {role}")
        if self._net_start_utc:
            lines.append(f"# Start UTC: {self._net_start_utc}")
        if self._net_end_utc:
            lines.append(f"# End UTC:   {self._net_end_utc}")
        if band:
            lines.append(f"# Band: {band}")
        lines.append("#")
        for cs in all_calls:
            lines.append(cs)

        created = False
        try:
            with path.open("x", encoding="utf-8") as output:
                created = True
                output.write("\n".join(lines) + "\n")
            log.info("JS8Call net log written to %s", path)
            return path
        except Exception as e:
            if created:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.exception("Unable to remove incomplete net log %s", path)
            raise OSError(f"Unable to write net log {path}: {e}") from e

    def _lookup_band_for_net(self, net_name: str) -> str:
        """
        Attempts to find a band in net_schedule matching this net_name and current UTC day/time.
        """
        if not net_name:
            return ""

        data = self.settings.all()
        net_sched = data.get("net_schedule", [])
        if not isinstance(net_sched, list):
            return ""

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        current_day_name = now_utc.strftime("%A")
        now_min = now_utc.hour * 60 + now_utc.minute

        for row in net_sched:
            try:
                if (row.get("net_name", "") or "").strip().lower() != net_name.strip().lower():
                    continue
                if row.get("day_utc") != current_day_name:
                    continue
                smin = self._parse_hhmm(row.get("start_utc", ""))
                emin = self._parse_hhmm(row.get("end_utc", ""))
                if smin is None or emin is None:
                    continue
                if smin <= now_min <= emin:
                    return (row.get("band") or "").strip()
            except Exception:
                continue

        return ""

    # ---------------- PARSING & UTILS ---------------- #

    def _parse_hhmm(self, text: str) -> int | None:
        text = (text or "").strip()
        if not text:
            return None
        try:
            h, m = text.split(":")
            h = int(h)
            m = int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h * 60 + m
        except Exception:
            return None
        return None

    def _my_callsign(self) -> str:
        return (
            (self.settings.get("operator_callsign", "") or self.settings.get("callsign", "") or "")
            .strip()
            .upper()
        )

    @staticmethod
    def _base_callsign(cs: str) -> str:
        import re

        cs_norm = (cs or "").strip().upper()
        if not cs_norm:
            return ""
        return re.sub(r"/(P|M|MM|QRP|SOTA|ROVER|[A-Z0-9]{1,4})$", "", cs_norm)

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

    def _open_operator_db(self) -> sqlite3.Connection:
        db_path = self._ncs_nets_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            conn.execute("PRAGMA busy_timeout=2000")
        except Exception:
            pass
        if not self._operator_schema_ready:
            ensure_operator_checkins_schema(conn)
            self._operator_schema_ready = True
        return conn

    def _extract_callsigns_from_line(self, line: str) -> List[str]:
        """
        JS8Check-in line examples:

          XY1245: ... F!103 ...
          N1MAG: some text
          A1BC:  something

        Rules:
          - If line contains 'F!103', treat first token up to ':' as callsign.
          - Else, if any token ends with ':', treat that token (without ':')
            as the remote callsign, as long as it's not our own callsign.
        """
        line = line.strip()
        if not line:
            return []

        mycall = self._my_callsign()
        msg = line
        if "\t" in line:
            parts = line.split("\t", 4)
            if len(parts) >= 5:
                msg = parts[4].strip()

        # Try F!103 pattern first
        if "F!103" in msg:
            first = msg.split()[0]
            if ":" in first:
                first = first.split(":", 1)[0]
            return [first.upper()]

        # Otherwise, look for token ending with ':'
        hits: List[str] = []
        parts = msg.split()
        for tok in parts:
            if tok.endswith(":"):
                cs = tok[:-1].upper()
                if cs and cs != mycall:
                    hits.append(cs)
        return hits

    def _extract_message_ids(self, line: str) -> List[str]:
        """
        Look for all patterns like 'YES MSG 123' in a JS8Call line and
        return numeric message IDs as strings.
        """
        return re.findall(r"\bYES\s+MSG(?:\s+ID)?\s+(\d+)", line, flags=re.IGNORECASE)

    def _parse_directed_metrics(self, line: str) -> tuple[Optional[float], Optional[int]]:
        """
        Attempt to parse SNR / Offset from a DIRECTED.TXT line.
        Common format: date time freq offset snr CALL: DEST ...
        Returns (snr, offset_hz)
        """
        parts = line.split()
        snr_val: Optional[float] = None
        offset_val: Optional[int] = None
        if len(parts) >= 5:
            try:
                offset_val = int(parts[3])
            except Exception:
                offset_val = None
            try:
                snr_val = float(parts[4])
            except Exception:
                snr_val = None
        return snr_val, offset_val

    def _get_js8_client(self):
        scope = getattr(self, '_preparation_scope', None)
        if scope is not None:
            return JS8ApiClientRegistry.get(JS8ApiEndpoint(*scope.endpoint), timeout_s=1.0, auto_reconnect=True)
        if self._js8_client:
            return self._js8_client
        if js8net is None:
            log.warning("JS8CallNetControl: js8net not available")
            return None
        # Do not spawn JS8Call; only attach if it is already running
        try:
            running = bool(self._status_service.program_is_running("JS8Call"))
            if not running:
                log.info("JS8CallNetControl: JS8Call not running; skipping js8net attach.")
                return None
        except Exception:
            return None
        try:
            port = int(self.settings.get("js8_port", 2442) or 2442)
        except Exception:
            port = 2442
        host = (self.settings.get("js8_host", "") or "").strip() or "127.0.0.1"
        try:
            hub = self._js8_rx_hub or JS8RxHub.instance(host, port)
            if not hub.start(host, port):
                log.error("JS8CallNetControl: shared js8net connection could not start.")
                return None
            self._js8_rx_hub = hub
            self._js8_client = js8net
            self._js8_net_started = True
            return js8net
        except BaseException as e:
            log.error("JS8CallNetControl: failed to start js8net: %s", e)
            return None

    def _maybe_record_inbound_trigger(self, line: str, calls: List[str]) -> None:
        """
        Track the group that caused our potential autoreply so we can tag outbound inserts.
        If message was to our callsign, store group = our callsign.
        If message was to a configured @GROUP, store that group.
        """
        if not calls:
            return
        mycall = self._my_callsign()
        upper = line.upper()
        to_me = mycall and mycall in upper
        groups_cfg = [g.strip().upper() for g in (self.settings.get("primary_js8_groups", []) or []) if g]
        hit_group = None
        for g in groups_cfg:
            if f"@{g}" in upper:
                hit_group = g
                break
        group_val = None
        if hit_group:
            group_val = hit_group
        elif to_me:
            group_val = mycall
        if not group_val:
            return
        origin = calls[0]
        self._last_inbound_triggers[origin] = (group_val, time.time())
        # prune stale entries (older than 15 min)
        now = time.time()
        stale = [k for k, (_, ts) in self._last_inbound_triggers.items() if now - ts > 900]
        for k in stale:
            self._last_inbound_triggers.pop(k, None)

    def _maybe_register_outgoing_call(self, line: str) -> None:
        """
        For any outgoing transmission to a callsign, add to operator_checkins as untrusted
        if not already present. Use group from the triggering inbound if available.
        """
        # Parse timestamp
        ts = None
        try:
            ts = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        except Exception:
            ts = datetime.datetime.now(datetime.timezone.utc)
        # Extract message after "JS8:"
        if "JS8:" not in line:
            return
        try:
            msg_part = line.split("JS8:", 1)[1].strip()
        except Exception:
            return
        tokens = msg_part.split()
        if not tokens:
            return
        # Require first token to be exactly our callsign + colon, then dest callsign token
        mycall = self._my_callsign()
        first = tokens[0].strip()
        if not mycall or first.upper() != (mycall + ":"):
            return
        if len(tokens) < 2:
            return
        dest_call = tokens[1].strip().strip(":").upper()
        if not dest_call:
            return
        # Only proceed if dest looks like a normal callsign (must contain a letter; avoid pure digits/macros)
        if not re.match(r"^(?=.*[A-Z])[A-Z0-9]{3,}$", dest_call):
            return
        # Determine group from last trigger if recent
        group_val = ""
        now = time.time()
        trig = self._last_inbound_triggers.get(dest_call)
        if trig and now - trig[1] <= 900:
            group_val = trig[0]
        elif self._my_callsign():
            group_val = self._my_callsign()
        # Prevent multiple inserts for the same dest during this run
        if dest_call in self._auto_inserted_callsigns:
            return
        self._auto_inserted_callsigns.add(dest_call)
        self._maybe_insert_untrusted(dest_call, ts, group_val)

    def _maybe_insert_untrusted(self, callsign: str, last_seen: datetime.datetime, group_val: str) -> None:
        cs = self._base_callsign(callsign)
        if not cs:
            return
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_operator_db()
            cur = conn.cursor()
            # check existing
            cur.execute("SELECT trusted FROM operator_checkins WHERE callsign=?", (cs,))
            row = cur.fetchone()
            ts_str = last_seen.astimezone(datetime.timezone.utc).isoformat()
            group_norm = (group_val or "").strip().upper()
            groups_json = json.dumps([group_norm]) if group_norm else None
            if row is None:
                cur.execute(
                    """
                    INSERT INTO operator_checkins (
                        callsign, name, state, grid, group1, group2, group3, group_role,
                        first_seen_utc, last_seen_utc, last_net, last_role, checkin_count, groups_json, trusted
                    ) VALUES (?, '', '', '', ?, '', '', '', ?, ?, '', '', 0, ?, 0)
                    """,
                    (cs, group_norm, ts_str, ts_str, groups_json),
                )
            else:
                trusted = int(row[0] or 0)
                if trusted == 0:
                    cur.execute(
                        """
                        UPDATE operator_checkins
                        SET last_seen_utc=?, group1=COALESCE(NULLIF(group1,''), ?), groups_json=COALESCE(groups_json, ?)
                        WHERE callsign=?
                        """,
                        (ts_str, group_norm, groups_json, cs),
                    )
            conn.commit()
        except Exception as e:
            log.debug("JS8CallNetControl: failed to upsert untrusted operator %s: %s", callsign, e)
            self._operator_schema_ready = False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _increment_checkin_counter(self, callsign: str) -> None:
        cs = self._base_callsign(callsign)
        if not cs:
            return
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_operator_db()
            cur = conn.cursor()
            now_iso = self._utc_now_iso()
            cur.execute(
                """
                INSERT INTO operator_checkins (
                    callsign, name, state, grid, group1, group2, group3, group_role,
                    first_seen_utc, last_seen_utc, last_net, last_role, checkin_count, groups_json, trusted
                )
                VALUES (?, '', '', '', '', '', '', '', ?, ?, '', '', 1, NULL, 0)
                ON CONFLICT(callsign) DO UPDATE SET
                    checkin_count=COALESCE(operator_checkins.checkin_count, 0) + 1,
                    last_seen_utc=excluded.last_seen_utc
                """,
                (cs, now_iso, now_iso),
            )
            conn.commit()
        except Exception as e:
            self._operator_schema_ready = False
            log.error("JS8CallNetControl: failed to increment checkin count for %s: %s", cs, e)
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
    def _maybe_capture_grid_report(self, line: str) -> None:
        """
        Capture GRID reports in DIRECTED.TXT lines (ignore GRID? queries).
        """
        if "..." in line:
            return
        parts = line.split("\t")
        if len(parts) < 5:
            return
        if "GRID?" in line.upper():
            return
        msg = parts[4]
        if "GRID" not in msg.upper():
            return
        try:
            ts = datetime.datetime.strptime(parts[0][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        except Exception:
            ts = datetime.datetime.now(datetime.timezone.utc)
        freq_hz = None
        try:
            freq_hz = float(parts[1]) * 1_000_000.0
        except Exception:
            freq_hz = None
        # Parse origin and tokens
        if ":" not in msg:
            return
        origin, rest = msg.split(":", 1)
        origin = origin.strip().upper()
        tokens = rest.strip().replace(",", " ").split()
        if not tokens:
            return
        # Look for GRID token
        try:
            idx = [t.upper() for t in tokens].index("GRID")
        except ValueError:
            return
        if idx + 1 >= len(tokens):
            return
        grid = tokens[idx + 1].strip().upper()
        if not grid or "?" in grid or not self._valid_grid(grid):
            return
        # Choose longest grid compared to existing later
        grp = ""
        # explicit @GROUP if present
        for t in tokens:
            if t.startswith("@"):
                grp = t.lstrip("@").upper()
                break
        groups = []
        if grp and self._is_allowed_group(grp):
            groups.append(grp)
        op_group = self._lookup_operating_group(freq_hz)
        if op_group:
            groups.append(op_group)
        # Require at least one group (explicit or via frequency)
        if not groups:
            return
        self._upsert_operator_info(origin, grid, groups, ts)

    def _lookup_operating_group(self, freq_hz: Optional[float]) -> str:
        try:
            ops = self.settings.get("operating_groups", []) or []
        except Exception:
            return ""
        if not freq_hz:
            return ""
        mhz = round(freq_hz / 1_000_000.0, 3)
        for row in ops:
            try:
                ftxt = str(row.get("frequency", "")).strip()
                if not ftxt:
                    continue
                if abs(float(ftxt) - mhz) < 0.0005:
                    grp = str(row.get("group", "")).strip()
                    if grp:
                        return grp.upper()
            except Exception:
                continue
        return ""

    def _is_allowed_group(self, grp: str) -> bool:
        g = (grp or "").strip().upper()
        if not g:
            return False
        try:
            prim = [x.strip().upper() for x in (self.settings.get("primary_js8_groups", []) or []) if x]
        except Exception:
            prim = []
        try:
            ops = [str(row.get("group", "")).strip().upper() for row in (self.settings.get("operating_groups", []) or []) if row]
        except Exception:
            ops = []
        return g in prim or g in ops

    def _valid_grid(self, grid: str) -> bool:
        import re
        # Maidenhead: 4-char (LLDD) or 6-char (LLDDLL)
        return bool(re.match(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$", grid.upper()))

    def _parse_commstat_grid(self, text: str) -> str:
        m = re.search(r",\s*([A-R]{2}[0-9]{2}(?:[A-X]{2})?)\s*,", text or "")
        return m.group(1) if m else ""

    def _parse_commstat_state(self, text: str) -> str:
        m = re.search(
            r",\s*[A-R]{2}[0-9]{2}(?:[A-X]{2})?\s*,[^,]*,[^,]*,[^,]*,\s*([A-Z]{2})\b",
            text or "",
        )
        return m.group(1) if m else ""

    def _maybe_capture_geo_tokens(self, callsign: str, text: str, group_name: str = "") -> None:
        cs = self._base_callsign(callsign)
        msg = (text or "").strip()
        if not cs or not msg:
            return
        upper = msg.upper()
        if "*DE*" in upper:
            return
        if any(code in upper for code in ("F!107", "F!305", "F!307", "F!308", "F!701")):
            return
        if "GR[" not in upper and "ST[" not in upper and "," not in msg:
            return
        state = ""
        grid = ""
        match = re.search(r"GR\[([A-R]{2}[0-9]{2}(?:[A-X]{2})?)\]", upper)
        if match:
            grid = match.group(1)
        match = re.search(r"ST\[([A-Z]{2})\]", upper)
        if match:
            state = match.group(1)
        if "," in msg:
            if not grid:
                grid = self._parse_commstat_grid(upper)
            if not state:
                state = self._parse_commstat_state(upper)
        if grid and not self._valid_grid(grid):
            grid = ""
        if state and not re.match(r"^[A-Z]{2}$", state):
            state = ""
        if not grid and not state:
            return
        self._update_operator_geo(cs, state, grid, group_name=group_name)

    def _update_operator_geo(self, callsign: str, state: str, grid: str, group_name: str = "") -> None:
        cs = self._base_callsign(callsign)
        state = (state or "").strip().upper()
        grid = (grid or "").strip().upper()
        if not cs or (not state and not grid):
            return
        if state and not re.match(r"^[A-Z]{2}$", state):
            state = ""
        if grid and not self._valid_grid(grid):
            grid = ""
        if not state and not grid:
            return
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_operator_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT state, grid, group1, group2, group3, groups_json, trusted FROM operator_checkins WHERE callsign=?",
                (cs,),
            )
            row = cur.fetchone()
            now_iso = self._utc_now_iso()
            group_name = (group_name or "").strip().upper()
            if row is None:
                groups = [g for g in [group_name] if g]
                groups_json = json.dumps(groups) if groups else None
                cur.execute(
                    """
                    INSERT INTO operator_checkins
                    (
                        callsign, name, state, grid, group1, group2, group3, group_role,
                        first_seen_utc, last_seen_utc, last_net, last_role, checkin_count, groups_json, trusted
                    )
                    VALUES (?, '', ?, ?, ?, ?, ?, NULL, ?, ?, '', '', 0, ?, 0)
                    """,
                    (
                        cs,
                        state or None,
                        grid or None,
                        group_name or None,
                        None,
                        None,
                        now_iso,
                        now_iso,
                        groups_json,
                    ),
                )
            else:
                old_state, old_grid, g1, g2, g3, groups_json, trusted = row
                new_state = (old_state or "").strip().upper()
                old_grid_norm = (old_grid or "").strip().upper()
                has_group = bool((g1 or "").strip() or (g2 or "").strip() or (g3 or "").strip())
                if not has_group and groups_json:
                    try:
                        parsed = json.loads(groups_json)
                        if isinstance(parsed, list) and any(str(x).strip() for x in parsed):
                            has_group = True
                    except Exception:
                        pass
                if not (has_group and (new_state or old_grid_norm)) and state and state != new_state:
                    new_state = state
                new_grid = old_grid_norm
                if grid:
                    if not new_grid:
                        new_grid = grid
                    elif len(new_grid) == 4 and len(grid) == 6 and new_grid == grid[:4]:
                        new_grid = grid
                    elif len(new_grid) == 6 and len(grid) == 6 and grid != new_grid:
                        new_grid = grid
                g_list = [g1 or "", g2 or "", g3 or ""]
                if group_name and group_name not in g_list:
                    for idx, val in enumerate(g_list):
                        if not val:
                            g_list[idx] = group_name
                            break
                try:
                    current_groups = json.loads(groups_json) if groups_json else []
                    if group_name and group_name not in current_groups:
                        current_groups.append(group_name)
                    groups_json_out = json.dumps(current_groups) if current_groups else None
                except Exception:
                    groups_json_out = groups_json
                cur.execute(
                    """
                    UPDATE operator_checkins
                    SET state=?, grid=?, group1=?, group2=?, group3=?, last_seen_utc=?, groups_json=?, trusted=COALESCE(trusted, ?)
                    WHERE callsign=?
                    """,
                    (
                        new_state or None,
                        new_grid or None,
                        g_list[0] or None,
                        g_list[1] or None,
                        g_list[2] or None,
                        now_iso,
                        groups_json_out,
                        trusted if trusted is not None else 0,
                        cs,
                    ),
                )
            conn.commit()
        except Exception as e:
            self._operator_schema_ready = False
            log.debug("JS8CallNetControl: failed to update operator geo for %s: %s", callsign, e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _upsert_operator_info(self, callsign: str, grid: str, groups: List[str], ts: datetime.datetime) -> None:
        cs = self._base_callsign(callsign)
        if not cs:
            return
        ts_str = ts.astimezone(datetime.timezone.utc).isoformat()
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_operator_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT grid, group1, group2, group3, groups_json, trusted FROM operator_checkins WHERE callsign=?",
                (cs,),
            )
            row = cur.fetchone()
            groups = [g.strip().upper() for g in groups if g]
            groups = [g for g in groups if g]
            groups_json = json.dumps(groups) if groups else None
            if row is None:
                cur.execute(
                    """
                    INSERT INTO operator_checkins (
                        callsign, name, state, grid, group1, group2, group3, group_role,
                        first_seen_utc, last_seen_utc, last_net, last_role, checkin_count, groups_json, trusted
                    ) VALUES (?, '', '', ?, ?, ?, ?, '', ?, ?, '', '', 0, ?, 0)
                    """,
                    (
                        cs,
                        grid,
                        groups[0] if len(groups) > 0 else "",
                        groups[1] if len(groups) > 1 else "",
                        groups[2] if len(groups) > 2 else "",
                        ts_str,
                        ts_str,
                        groups_json,
                    ),
                )
            else:
                existing_grid, g1, g2, g3, gj, trusted = row
                # Keep existing grid if already set; do not replace with new reports
                final_grid = existing_grid.strip().upper() if existing_grid else grid
                # merge groups into slots then json
                slots = [g1 or "", g2 or "", g3 or ""]
                slot_set = {s.strip().upper() for s in slots if s}
                merged = slot_set.copy()
                merged.update(groups)
                # fill slots first
                slots_filled = []
                for s in slots:
                    val = s.strip().upper()
                    if val:
                        slots_filled.append(val)
                for g in groups:
                    if len(slots_filled) < 3 and g not in slots_filled:
                        slots_filled.append(g)
                while len(slots_filled) < 3:
                    slots_filled.append("")
                extra = merged - set(slots_filled) if merged else set()
                extra_json = []
                if gj:
                    try:
                        prev = json.loads(gj)
                        if isinstance(prev, list):
                            extra_json.extend([str(x).upper() for x in prev])
                    except Exception:
                        pass
                for g in extra:
                    if g and g not in extra_json:
                        extra_json.append(g)
                cur.execute(
                    """
                    UPDATE operator_checkins
                    SET
                        grid=?,
                        group1=?,
                        group2=?,
                        group3=?,
                        groups_json=?,
                        last_seen_utc=?
                    WHERE callsign=?
                    """,
                    (
                        final_grid,
                        slots_filled[0],
                        slots_filled[1],
                        slots_filled[2],
                        json.dumps(extra_json) if extra_json else gj,
                        ts_str,
                        cs,
                    ),
                )
            conn.commit()
        except Exception as e:
            self._operator_schema_ready = False
            log.debug("JS8CallNetControl: failed to upsert operator info %s: %s", callsign, e)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    def _queue_auto_query(self, call: str, msg_id: str, snr: float | None = None, speed: int | None = None) -> None:
        """
        Queue a query for MSG ID, and process one at a time when enabled.
        """
        self._backlog_upsert(call, msg_id, "MSG", status="PENDING")
        return

    def _maybe_process_next_query(self) -> None:
        return
        if self._waiting_for_completion:
            if self._current_query_sent_ts and (time.time() - self._current_query_sent_ts) > 15:
                log.debug("JS8CallNetControl: completion timeout; clearing wait and advancing queue")
                self._waiting_for_completion = False
                self._awaiting_ack_for = None
                self._current_query = None
                self._current_query_sent_ts = 0.0
            else:
                log.debug("JS8CallNetControl: waiting_for_completion; skipping process_next_query")
                return
        if self._auto_query_paused_by_net:
            return
        pending = self._backlog_fetch_next_pending()
        if not pending:
            log.debug("JS8CallNetControl: no pending queries to process")
            return
        call, msg_id, created_ts = pending
        now_ts = time.time()
        last_relevant = max(
            self._last_traffic_to_me_ts,
            self._last_traffic_group_ts,
            self._last_traffic_by_call_ts.get(self._base_callsign(call), 0.0),
        )
        idle_ok = (now_ts - last_relevant) >= 5.0
        if not idle_ok:
            log.debug(
                "JS8CallNetControl: deferring auto-query call=%s id=%s (idle_gap=%.1fs wait=%.1fs)",
                call,
                msg_id,
                max(0.0, now_ts - last_relevant),
                max(0.0, now_ts - created_ts),
            )
            key = f"{call}:{msg_id}"
            last_log = self._msg_defer_last_log.get(key, 0.0)
            if (now_ts - created_ts) >= 60.0 and (now_ts - last_log) >= 60.0:
                log.info(
                    "JS8CallNetControl: auto-query still blocked by traffic for %s id=%s (wait=%.1fs)",
                    call,
                    msg_id,
                    max(0.0, now_ts - created_ts),
                )
                self._msg_defer_last_log[key] = now_ts
            return
        log.debug(
            "JS8CallNetControl: processing auto-query call=%s id=%s (idle_ok=%s)",
            call,
            msg_id,
            idle_ok,
        )
        key = f"{call}:{msg_id}"
        if key in self._queried_msg_ids:
            # skip duplicates
            self._maybe_process_next_query()
            return
        mycall = self._my_callsign() or ""
        query_text = f"{mycall}: {call} QUERY MSG {msg_id}".strip()
        log.info("JS8CallNetControl: attempting auto-query TX to %s msg_id=%s text=\"%s\"", call, msg_id, query_text)
        sent = self._send_js8_message(query_text)
        if sent:
            self._backlog_touch_attempt(call, msg_id, "MSG")
            self._queried_msg_ids.add(key)
            self._waiting_for_completion = True
            self._current_query = (call, msg_id)
            self._current_query_sent_ts = time.time()
            # Expect a MSG reply; extend timeout for slow mode
            is_slow = False
            base_timeout = 120
            if is_slow:
                base_timeout = 180
            expiry = time.time() + base_timeout
            self._awaiting_msg_responses[(call, msg_id)] = expiry
            log.info("JS8CallNetControl: auto-queried MSG ID %s from %s via TX.SEND_MESSAGE", msg_id, call)
        else:
            log.error("JS8CallNetControl: auto query send failed for %s/%s", call, msg_id)
            self._current_query = None
            self._waiting_for_completion = False
            self._maybe_process_next_query()

    def _on_js8_rx_messages(self, messages: List[dict]) -> None:
        if getattr(self, '_preparation_scope', None) is not None and not self._net_in_progress:
            return
        if self._is_shutting_down or self._polling_rx:
            return
        self._polling_rx = True
        try:
            source_context = self._js8_live_source_context()
            workbench = getattr(self, 'workbench', None)
            if workbench is not None:
                workbench.enqueue_events(messages, source_context)
            for msg in messages:
                now_ts = time.time()
                self._last_rx_ts = now_ts
                self._grid_last_rx_ts = now_ts
                try:
                    p = msg.get("params", {}) if isinstance(msg, dict) else {}
                    txt = str(p.get("TEXT") or "").upper()
                    cmd_txt = str(p.get("CMD") or "").upper()
                    extra_txt = str(p.get("EXTRA") or "").upper()
                    combined = " ".join([txt, cmd_txt, extra_txt]).strip()
                    frm = (p.get("FROM") or "").strip().upper()
                    base_frm = self._base_callsign(frm) if frm else ""
                    if base_frm:
                        self._call_last_rx_ts[base_frm] = now_ts
                        # If awaiting MSG response for this call, mark retrieved on any MSG token
                        for (c, mid), exp in list(self._awaiting_msg_responses.items()):
                            if c == base_frm and "MSG" in combined:
                                self._mark_backlog_retrieved(c, mid, "MSG")
                                self._awaiting_msg_responses.pop((c, mid), None)
                        # If awaiting GRID response for this call and GRID present, mark retrieved
                        if base_frm in self._awaiting_grid_responses and "GRID" in combined:
                            self._mark_backlog_retrieved(base_frm, "", "GRID")
                            self._awaiting_grid_responses.pop(base_frm, None)
                        if self._net_in_progress and self._receiver_traffic_belongs_to_net(str(p.get('TO') or ''), txt) and net_traffic_payload(
                            txt or ' '.join((cmd_txt, extra_txt)), recipient=str(p.get('TO') or '')) and not is_incidental_net_traffic(
                            txt or ' '.join((cmd_txt, extra_txt)), recipient=str(p.get('TO') or '')):
                            # Extract metrics from API payload when available
                            try:
                                snr_val = float(p.get("SNR")) if p.get("SNR") not in (None, "") else None
                            except Exception:
                                snr_val = None
                            speed_val = p.get("SPEED")
                            mode_name = ""
                            sval: int | None = None
                            if speed_val is not None:
                                try:
                                    sval = int(speed_val)
                                    mode_name = js8_speed_name(sval)
                                except Exception:
                                    mode_name = str(speed_val)
                                if sval is not None:
                                    # Remember last seen speed per base callsign
                                    self._call_last_speed[base_frm] = sval
                            try:
                                offset_val = int(p.get("OFFSET")) if p.get("OFFSET") not in (None, "") else None
                            except Exception:
                                offset_val = None
                            self._upsert_checkin(
                                frm,
                                status="NEW",
                                mode=mode_name,
                                snr=snr_val,
                                offset=offset_val,
                                grid=(p.get("GRID") or "").strip().upper(),
                            )
                        if combined:
                            self._maybe_capture_geo_tokens(
                                base_frm,
                                combined,
                                group_name=self._active_group_name(),
                            )
                    snr_val = None
                    try:
                        snr_val = float(p.get("SNR")) if p.get("SNR") not in (None, "") else None
                    except Exception:
                        snr_val = None
                    if "YES MSG" in combined:
                        ids = re.findall(r"\b(\d+)\b", combined)
                        for mid in ids:
                            if frm:
                                dest = ""
                                try:
                                    first = txt.split()[0].strip().upper()
                                    dest = first
                                except Exception:
                                    dest = ""
                                mycall = self._my_callsign()
                                if mycall and dest == mycall and self._is_message_complete_line(txt):
                                    log.info(
                                        "JS8CallNetControl: detected YES MSG %s from %s (snr=%s)",
                                        mid,
                                        frm,
                                        snr_val,
                                    )
                                    self._backlog_upsert(frm, mid, "MSG", status="PENDING")
                    # Passive grid capture
                    grid_val = (p.get("GRID") or "").strip()
                    base_frm = self._base_callsign(frm) if frm else ""
                    if grid_val and base_frm:
                        self._update_operator_grid(base_frm, grid_val, self._active_group_name())
                    else:
                        for token in txt.split():
                            if 4 <= len(token) <= 6 and token[:2].isalpha() and token[2:4].isdigit():
                                self._update_operator_grid(base_frm or frm, token, self._active_group_name())
                                break
                    # Form identity describes received content; it is not reply
                    # matching or a check-in purpose/selected-query restriction.
                    if self._net_in_progress and base_frm and self._receiver_traffic_belongs_to_net(str(p.get('TO') or ''), txt) and not is_incidental_net_traffic(
                        txt or ' '.join((cmd_txt, extra_txt)), recipient=str(p.get('TO') or '')):
                        forms_found = extract_form_codes(combined)
                        if forms_found:
                            self._upsert_checkin(frm, status=forms_found[0], status_mismatch=False)
                except Exception:
                    continue
        finally:
            self._polling_rx = False

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        if self._is_shutting_down:
            return
        self._is_shutting_down = True
        if getattr(self, 'workbench', None) is not None:
            self.workbench.close()
        executor = getattr(self, '_form_tools_executor', None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        receiver = getattr(self, '_prepared_receiver', None)
        if receiver is not None:
            receiver.close()
        self._is_shutting_down = True
        try:
            if self._poll_timer:
                self._poll_timer.stop()
        except Exception:
            pass
        try:
            if self._clock_timer:
                self._clock_timer.stop()
        except Exception:
            pass
        try:
            if self._js8_rx_hub and self._js8_rx_registered:
                self._js8_rx_hub.unregister_listener(getattr(self, '_scoped_rx_callback', self._on_js8_rx_messages))
                self._js8_rx_registered = False
        except Exception:
            pass
        try:
            broker = getattr(self, "_fcc_identity_broker", None)
            if broker is not None:
                broker.identities_ready.disconnect(self._on_fcc_identities_ready)
        except Exception:
            pass
        getattr(self, "_fcc_identity_pending", {}).clear()

    def _line_has_checkin_form(self, line: str) -> bool:
        """
        Returns True if the line contains a JS8Spotter form mapped as a net check-in.
        """
        codes = set(extract_form_codes(line))
        return bool(codes.intersection(self._mapped_checkin_forms()))

    def _line_has_any_spotter_form(self, line: str) -> bool:
        msg_text = self._message_text_from_line(line).upper()
        return bool(extract_form_codes(msg_text))

    def _should_accept_checkin_line(self, line: str) -> bool:
        # Mapping/query choices are tools, never an incoming roster whitelist.
        text = self._message_text_from_line(line)
        return bool(net_traffic_payload(text)) and not is_incidental_net_traffic(text)

    def _message_text_from_line(self, line: str) -> str:
        if "\t" in line:
            parts = line.split("\t", 4)
            if len(parts) >= 5:
                return parts[4].strip()
        return line.strip()

    def _extract_dest_callsign(self, msg_text: str) -> str:
        txt = (msg_text or "").strip()
        if not txt:
            return ""
        if ":" in txt:
            rest = txt.split(":", 1)[1]
        else:
            rest = txt
        if not rest:
            return ""
        return rest.strip().split()[0].strip().upper()

    def _update_recent_traffic(self, line: str, calls: List[str]) -> None:
        msg_text = self._message_text_from_line(line)
        now_ts = time.time()

        if calls:
            sender = self._base_callsign(calls[0])
            if sender:
                self._last_traffic_by_call_ts[sender] = now_ts

        dest = self._base_callsign(self._extract_dest_callsign(msg_text))
        if dest:
            self._last_traffic_by_call_ts[dest] = now_ts

        mycall = self._my_callsign()
        if mycall and dest == mycall:
            self._last_traffic_to_me_ts = now_ts

        if "@" in msg_text:
            tokens = re.findall(r"@([A-Z0-9]{1,15})", msg_text.upper())
            if tokens:
                self._last_traffic_group_ts = now_ts

    def _line_has_announce_form(self, line: str) -> bool:
        codes = set(extract_form_codes(line))
        return bool(codes.intersection(self._mapped_announcement_forms()))

    def _maybe_notify_announcement(self, callsign: str, line: str) -> None:
        """
        Show a popup when a net announcement (F!106) is fully received and no net is in progress.
        Debounced per callsign.
        """
        # Debug guards for missed popups
        if self._net_in_progress:
            log.debug("JS8 NCS: F!106 suppressed (net in progress): %s", line)
            return
        call = (callsign or "").strip().upper()
        if not call:
            log.debug("JS8 NCS: F!106 suppressed (no callsign parsed): %s", line)
            return
        now = time.time()
        last = self._recent_announcements.get(call, 0)
        if now - last < 300:
            log.debug("JS8 NCS: F!106 suppressed (debounce <5min) from %s: %s", call, line)
            return
        self._recent_announcements[call] = now
        msg_text = line
        try:
            QMessageBox.information(
                self,
                "Net Announcement Received",
                f"Net announcement (F!106) received from {call}.\n\n{msg_text}",
            )
        except Exception as e:
            log.debug("JS8CallNetControl: failed to show announcement popup: %s", e)

    # ---------------- Auto-query backlog ---------------- #

    def _backlog_db_path(self) -> Path:
        return self._ncs_nets_db_path()

    def _backlog_upsert(self, callsign: str, msg_id: str, kind: str, status: str = "PENDING") -> None:
        try:
            callsign = (callsign or "").strip().upper()
            conn = sqlite3.connect(self._backlog_db_path())
            cur = conn.cursor()
            now_ts = time.time()
            cur.execute(
                """
                SELECT attempts FROM autoquery_backlog
                WHERE callsign=? AND COALESCE(msg_id,'')=COALESCE(?, '') AND kind=?
                """,
                (callsign, msg_id or "", kind),
            )
            row = cur.fetchone()
            if row is not None:
                attempts = row[0] or 0
                cur.execute(
                    """
                    UPDATE autoquery_backlog
                    SET status=?, attempts=?, last_attempt_ts=?
                    WHERE callsign=? AND COALESCE(msg_id,'')=COALESCE(?, '') AND kind=?
                    """,
                    (status, int(attempts) + 1, now_ts, callsign, msg_id or "", kind),
                )
                conn.commit()
                conn.close()
                return
            cur.execute(
                """
                INSERT INTO autoquery_backlog (callsign, msg_id, kind, status, attempts, last_attempt_ts, created_ts)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (callsign, msg_id, kind, status, now_ts, now_ts),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.debug("JS8 autoquery backlog upsert failed: %s", e)

    def _backlog_mark(self, callsign: str, msg_id: str, kind: str, status: str) -> None:
        try:
            conn = sqlite3.connect(self._backlog_db_path())
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE autoquery_backlog
                SET status=?, last_attempt_ts=?
                WHERE callsign=? AND COALESCE(msg_id,'')=COALESCE(?, '') AND kind=?
                """,
                (status, time.time(), callsign, msg_id or "", kind),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.debug("JS8 autoquery backlog mark failed: %s", e)

    def _backlog_fetch_pending(self, callsigns: List[str]) -> List[tuple[str, str, str]]:
        if not callsigns:
            return []
        try:
            conn = sqlite3.connect(self._backlog_db_path())
            cur = conn.cursor()
            qs = ",".join("?" for _ in callsigns)
            cur.execute(
                f"""
                SELECT callsign, msg_id, kind
                FROM autoquery_backlog
                WHERE COALESCE(attempts,0)=0 AND callsign IN ({qs})
                """,
                [c.upper() for c in callsigns],
            )
            rows = cur.fetchall()
            conn.close()
            return [(r[0] or "", r[1] or "", r[2] or "MSG") for r in rows]
        except Exception as e:
            log.debug("JS8 autoquery backlog fetch failed: %s", e)
            return []

    def _backlog_has_pending_msg(self, callsign: str) -> bool:
        cs = (callsign or "").strip().upper()
        if not cs:
            return False
        try:
            conn = sqlite3.connect(self._backlog_db_path())
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1 FROM autoquery_backlog
                WHERE kind='MSG' AND COALESCE(attempts,0)=0 AND callsign=?
                LIMIT 1
                """,
                (cs,),
            )
            row = cur.fetchone()
            conn.close()
            return row is not None
        except Exception as e:
            log.debug("JS8 autoquery backlog pending check failed: %s", e)
            return False

    def _backlog_has_any_pending_msg(self) -> bool:
        try:
            conn = sqlite3.connect(self._backlog_db_path())
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1 FROM autoquery_backlog
                WHERE kind='MSG' AND COALESCE(attempts,0)=0
                LIMIT 1
                """
            )
            row = cur.fetchone()
            conn.close()
            return row is not None
        except Exception as e:
            log.debug("JS8 autoquery backlog pending-any check failed: %s", e)
            return False

    def _backlog_fetch_next_pending(self) -> Optional[tuple[str, str, float]]:
        try:
            conn = sqlite3.connect(self._backlog_db_path())
            cur = conn.cursor()
            cur.execute(
                """
                SELECT callsign, msg_id, created_ts
                FROM autoquery_backlog
                WHERE kind='MSG' AND COALESCE(attempts,0)=0 AND COALESCE(msg_id,'')<>''
                ORDER BY COALESCE(created_ts, 0) ASC, id ASC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            conn.close()
            if not row:
                return None
            created_ts = float(row[2] or 0)
            if created_ts <= 0:
                created_ts = time.time()
            return (row[0] or "", row[1] or "", created_ts)
        except Exception as e:
            log.debug("JS8 autoquery backlog fetch next failed: %s", e)
            return None

    def _backlog_touch_attempt(self, callsign: str, msg_id: str, kind: str) -> None:
        try:
            conn = sqlite3.connect(self._backlog_db_path())
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE autoquery_backlog
                SET attempts=attempts+1, last_attempt_ts=?
                WHERE callsign=? AND COALESCE(msg_id,'')=COALESCE(?, '') AND kind=?
                """,
                (time.time(), callsign, msg_id or "", kind),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.debug("JS8 autoquery backlog touch failed: %s", e)

    def _mark_backlog_retrieved(self, callsign: str, msg_id: str, kind: str) -> None:
        if kind == "MSG":
            self._backlog_delete(callsign, msg_id, kind)
        else:
            self._backlog_mark(callsign, msg_id, kind, "RETRIEVED")

    def _mark_backlog_failed(self, callsign: str, msg_id: str, kind: str) -> None:
        if kind == "MSG":
            return
        self._backlog_mark(callsign, msg_id, kind, "FAILED")

    def _backlog_delete(self, callsign: str, msg_id: str, kind: str) -> None:
        try:
            conn = sqlite3.connect(self._backlog_db_path())
            cur = conn.cursor()
            cur.execute(
                """
                DELETE FROM autoquery_backlog
                WHERE callsign=? AND COALESCE(msg_id,'')=COALESCE(?, '') AND kind=?
                """,
                (callsign, msg_id or "", kind),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.debug("JS8 autoquery backlog delete failed: %s", e)

    def _expire_pending_responses(self) -> None:
        now = time.time()
        for key, exp in list(self._awaiting_msg_responses.items()):
            if now > exp:
                self._awaiting_msg_responses.pop(key, None)
        for call, exp in list(self._awaiting_grid_responses.items()):
            if now > exp:
                self._mark_backlog_failed(call, "", "GRID")
                self._awaiting_grid_responses.pop(call, None)

    def _process_message_completion(self, line: str) -> None:
        """
        Detect end-of-message markers before issuing next queued query.
        """
        return

    def _send_query_msgs(self, call: str) -> None:
        """
        Send QUERY MSGS to a specific station to discover additional messages.
        """
        mycall = self._my_callsign() or ""
        text = f"{mycall}: {call} QUERY MSGS".strip()
        sent = self._send_js8_message(text)
        if sent:
            log.info("JS8CallNetControl: queried additional messages from %s", call)
        else:
            log.error("JS8CallNetControl: failed sending QUERY MSGS to %s", call)

    # ---------------- Grid helpers ---------------- #

    def _operator_missing_grid(self, callsign: str) -> bool:
        cs = self._base_callsign(callsign)
        if not cs:
            return False
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_operator_db()
            cur = conn.cursor()
            cur.execute("SELECT grid FROM operator_checkins WHERE callsign=?", (cs,))
            row = cur.fetchone()
            if row is None:
                return True
            grid = row[0] or ""
            return grid.strip() == ""
        except Exception:
            self._operator_schema_ready = False
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _update_operator_grid(self, callsign: str, grid: str, group_name: str = "") -> None:
        cs = self._base_callsign(callsign)
        grid = (grid or "").strip().upper()
        if not cs or not grid:
            return
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_operator_db()
            cur = conn.cursor()
            cur.execute("SELECT grid, group1, group2, group3, groups_json, trusted FROM operator_checkins WHERE callsign=?", (cs,))
            row = cur.fetchone()
            now_iso = self._utc_now_iso()
            group_norm = (group_name or "").strip().upper()
            if row is None:
                groups = [g for g in [group_norm] if g]
                groups_json = json.dumps(groups) if groups else None
                cur.execute(
                    """
                    INSERT INTO operator_checkins
                    (
                        callsign, name, state, grid, group1, group2, group3, group_role,
                        first_seen_utc, last_seen_utc, last_net, last_role, checkin_count, groups_json, trusted
                    )
                    VALUES (?, '', '', ?, ?, ?, ?, NULL, ?, ?, '', '', 0, ?, 0)
                    """,
                    (cs, grid, group_norm or None, None, None, now_iso, now_iso, groups_json),
                )
            else:
                old_grid, g1, g2, g3, groups_json, trusted = row
                new_grid = old_grid or grid
                g_list = [g1 or "", g2 or "", g3 or ""]
                if group_norm and group_norm not in g_list:
                    for idx, val in enumerate(g_list):
                        if not val:
                            g_list[idx] = group_norm
                            break
                try:
                    current_groups = json.loads(groups_json) if groups_json else []
                    if group_norm and group_norm not in current_groups:
                        current_groups.append(group_norm)
                    groups_json_out = json.dumps(current_groups) if current_groups else None
                except Exception:
                    groups_json_out = groups_json
                cur.execute(
                    """
                    UPDATE operator_checkins
                    SET grid=?, group1=?, group2=?, group3=?, last_seen_utc=?, groups_json=?, trusted=COALESCE(trusted, ?)
                    WHERE callsign=?
                    """,
                    (new_grid, g_list[0] or None, g_list[1] or None, g_list[2] or None, now_iso, groups_json_out, trusted if trusted is not None else 0, cs),
                )
            conn.commit()
        except Exception as e:
            self._operator_schema_ready = False
            log.debug("JS8CallNetControl: failed to update operator grid for %s: %s", callsign, e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _active_group_name(self) -> str:
        entry = self._active_schedule()
        if not entry:
            return ""
        return (entry.get("group_name") or "").strip()

    def _maybe_queue_grid_query(self, callsign: str, snr: Optional[float], msg_params: Dict, text: str) -> None:
        # Disabled until auto-grid workflow is re-enabled.
        return
        call = (callsign or "").strip().upper()
        if not call:
            return
        if self._auto_query_paused_by_net:
            return
        # Only query when traffic is directed to us or a group (skip third-party directed traffic)
        mycall = (self._my_callsign() or "").strip().upper()
        dest = (msg_params.get("TO") or "").strip().upper()
        if dest and not dest.startswith("@") and mycall and dest != mycall:
            return
        # Require active schedule and group match: a AND (b OR c)
        active = self._active_schedule()
        if not active:
            return
        sched_group = (active.get("group_name") or "").strip().upper()
        configured_groups = [g.strip().upper() for g in (self.settings.get("primary_js8_groups", []) or []) if g]
        incoming_group = ""
        for tok in text.split():
            if tok.startswith("@") and len(tok) > 1:
                incoming_group = tok[1:].upper()
                break
        if not configured_groups:
            group_ok = True
        else:
            group_ok = sched_group in configured_groups or incoming_group in configured_groups
        if not group_ok:
            return
        # Enqueue if not already queued
        for _, queued_call in self._pending_grid_queries:
            if queued_call == call:
                return
        self._pending_grid_queries.append((snr, call))

    def _maybe_process_next_grid(self) -> None:
        # Disabled until auto-grid workflow is re-enabled.
        return
        if not self._pending_grid_queries:
            return
        if self._auto_query_paused_by_net:
            return
        if self._waiting_for_completion or self._backlog_has_any_pending_msg():
            return
        if time.time() - self._grid_last_rx_ts < 2.0:
            return
        if self._net_lockout_active():
            return
        # Defer while our own transmission recently occurred (e.g., auto-reply in progress)
        if time.time() - self._last_tx_ts < 5.0:
            return
        now_ts = time.time()
        # Weakest SNR first
        self._pending_grid_queries.sort(key=lambda t: (999 if t[0] is None else t[0]))
        # Respect per-callsign quiet window
        processed = 0
        max_attempts = len(self._pending_grid_queries)
        while self._pending_grid_queries and processed < max_attempts:
            snr_val, call = self._pending_grid_queries.pop(0)
            if self._backlog_has_pending_msg(self._base_callsign(call)):
                # Finish MSG auto-queries for this callsign before GRID?
                self._pending_grid_queries.append((snr_val, call))
                processed += 1
                continue
            now_ts = time.time()
            last_relevant = max(
                self._last_traffic_to_me_ts,
                self._last_traffic_group_ts,
                self._last_traffic_by_call_ts.get(self._base_callsign(call), 0.0),
            )
            idle_ok = (now_ts - last_relevant) >= 5.0
            if not idle_ok:
                key = self._base_callsign(call) or call
                if key not in self._grid_defer_start_ts:
                    self._grid_defer_start_ts[key] = now_ts
                last_log = self._grid_defer_last_log.get(key, 0.0)
                wait = now_ts - self._grid_defer_start_ts.get(key, now_ts)
                if wait >= 60.0 and (now_ts - last_log) >= 60.0:
                    log.info(
                        "JS8CallNetControl: auto grid query still blocked by traffic for %s",
                        call,
                    )
                    self._grid_defer_last_log[key] = now_ts
                self._pending_grid_queries.append((snr_val, call))
                processed += 1
                continue
            key = self._base_callsign(call) or call
            self._grid_defer_start_ts.pop(key, None)
            last_rx = self._call_last_rx_ts.get(self._base_callsign(call), 0.0)
            if last_rx and (now_ts - last_rx) < AUTO_GRID_QUIET_SECS:
                # Too recent; push to back and try later
                self._pending_grid_queries.append((snr_val, call))
                processed += 1
                continue
            break
        else:
            return
        mycall = self._my_callsign() or ""
        query_text = f"{mycall}: {call} GRID?".strip()
        log.info("JS8CallNetControl: attempting auto grid query to %s text=\"%s\"", call, query_text)
        if self._send_js8_message(query_text):
            log.info("JS8CallNetControl: auto grid query to %s", call)
            self._grid_waiting = True
            self._awaiting_grid_responses[call] = time.time() + 120
            self._backlog_upsert(call, "", "GRID", status="PENDING")
        else:
            log.error("JS8CallNetControl: failed GRID? to %s", call)
            self._backlog_upsert(call, "", "GRID", status="PENDING")

    def _is_message_complete_line(self, line: str) -> bool:
        """
        Heuristic: treat lines containing the JS8Call end-of-message marker
        (diamond U+2662) as completion markers.
        """
        txt = line.strip()
        if not txt:
            return False
        return "\u2662" in txt

    # ---------------- Schedule helpers ---------------- #

    def _parse_hhmm_to_minutes(self, hhmm: str) -> Optional[int]:
        txt = (hhmm or "").strip()
        if not txt:
            return None
        try:
            hh, mm = txt.split(":")
            hh_i = int(hh)
            mm_i = int(mm)
            if 0 <= hh_i <= 23 and 0 <= mm_i <= 59:
                return hh_i * 60 + mm_i
        except Exception:
            return None
        return None

    def _load_net_rows(self) -> List[Dict]:
        data = []
        try:
            db_path = self._ncs_nets_db_path()
            if db_path.exists():
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    "SELECT day_utc, frequency, start_utc, end_utc, early_checkin, group_name FROM net_schedule_tab"
                )
                for row in cur.fetchall():
                    data.append(
                        {
                            "day_utc": row[0] or "",
                            "frequency": row[1] or "",
                            "start_utc": row[2] or "",
                            "end_utc": row[3] or "",
                            "early_checkin": int(row[4] or 0),
                            "group_name": row[5] or "",
                        }
                    )
                conn.close()
        except Exception:
            pass
        if not data:
            try:
                raw = self.settings.get("net_schedule", [])
                if isinstance(raw, list):
                    data = [
                        {
                            "day_utc": r.get("day_utc", ""),
                            "frequency": r.get("frequency", ""),
                            "start_utc": r.get("start_utc", ""),
                            "end_utc": r.get("end_utc", ""),
                            "early_checkin": int(r.get("early_checkin", 0) or 0),
                            "group_name": r.get("group_name", ""),
                        }
                        for r in raw
                        if isinstance(r, dict)
                    ]
            except Exception:
                data = []
        return data

    def _load_daily_rows(self) -> List[Dict]:
        data = []
        try:
            db_path = Path(self.settings.db_path) if getattr(self, '_preparation_scope', None) is not None else get_config_dir() / 'config' / 'freqinout.db'
            if db_path.exists():
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute("SELECT day_utc, frequency, start_utc, end_utc, group_name FROM daily_schedule_tab")
                for row in cur.fetchall():
                    data.append(
                        {
                            "day_utc": row[0] or "ALL",
                            "frequency": row[1] or "",
                            "start_utc": row[2] or "",
                            "end_utc": row[3] or "",
                            "group_name": row[4] or "",
                        }
                    )
                conn.close()
        except Exception:
            pass
        if not data:
            try:
                raw = self.settings.get("daily_schedule", [])
                if isinstance(raw, list):
                    data = [
                        {
                            "day_utc": r.get("day_utc", "ALL"),
                            "frequency": r.get("frequency", ""),
                            "start_utc": r.get("start_utc", ""),
                            "end_utc": r.get("end_utc", ""),
                            "group_name": r.get("group_name", ""),
                        }
                        for r in raw
                        if isinstance(r, dict)
                    ]
            except Exception:
                data = []
        return data

    def _day_matches(self, entry_day: str, now_day: str) -> bool:
        d = (entry_day or "ALL").strip().upper()
        if d == "ALL":
            return True
        return d.upper() == now_day.upper()

    def _is_in_window(self, entry, now: datetime.datetime, allow_early: bool = False) -> bool:
        day = entry.get("day_utc", "ALL")
        start_txt = entry.get("start_utc", "")
        end_txt = entry.get("end_utc", "")
        early = int(entry.get("early_checkin", 0) or 0) if allow_early else 0
        start_m = self._parse_hhmm_to_minutes(start_txt)
        end_m = self._parse_hhmm_to_minutes(end_txt)
        if start_m is None or end_m is None:
            return False
        start_m = max(0, start_m - early)
        now_m = now.hour * 60 + now.minute
        # Overnight handling
        if start_m <= end_m:
            return self._day_matches(day, now.strftime("%A")) and start_m <= now_m <= end_m
        else:
            # window crosses midnight
            today_match = self._day_matches(day, now.strftime("%A")) and now_m >= start_m
            prev_day = (now - datetime.timedelta(days=1)).strftime("%A")
            overnight_match = self._day_matches(day, prev_day) and now_m <= end_m
            return today_match or overnight_match

    def _active_schedule(self) -> Optional[Dict]:
        now = datetime.datetime.now(datetime.timezone.utc)
        # Prefer net schedule windows (respect early)
        for row in self._load_net_rows():
            if self._is_in_window(row, now, allow_early=True):
                return row
        for row in self._load_daily_rows():
            if self._is_in_window(row, now, allow_early=False):
                return row
        return None

    def _next_net_lockout(self) -> Optional[datetime.datetime]:
        """
        Return the UTC datetime when the next net window starts (start - early).
        """
        rows = self._load_net_rows()
        if not rows:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        now_day = now.strftime("%A")
        candidates: List[datetime.datetime] = []
        for row in rows:
            start_m = self._parse_hhmm_to_minutes(row.get("start_utc", ""))
            end_m = self._parse_hhmm_to_minutes(row.get("end_utc", ""))
            if start_m is None or end_m is None:
                continue
            early = int(row.get("early_checkin", 0) or 0)
            window_start = max(0, start_m - early)
            for day_offset in (0, 1):
                dt = now + datetime.timedelta(days=day_offset)
                if not self._day_matches(row.get("day_utc", ""), dt.strftime("%A")):
                    continue
                cand = dt.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(
                    minutes=window_start
                )
                if cand >= now:
                    candidates.append(cand)
        if not candidates:
            return None
        return min(candidates)

    def _net_lockout_active(self) -> bool:
        """
        True if within 5 minutes of a net window start (including early check-in)
        or currently inside a net window.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        for row in self._load_net_rows():
            if self._is_in_window(row, now, allow_early=True):
                return True
        nxt = self._next_net_lockout()
        if nxt is None:
            return False
        delta = (nxt - now).total_seconds() / 60.0
        return 0 <= delta <= 5

    def _dedupe_calls_from_text(self, text: str) -> List[str]:
        calls = []
        seen = set()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # If line looks like full JS8 text, re-parse; otherwise treat as bare callsign
            if ":" in line or " " in line:
                call = self._extract_callsign_from_line(line)
            else:
                call = line.upper()
            if not call:
                continue
            if call not in seen:
                seen.add(call)
                calls.append(call)
        return calls

    def _build_short_code_summary(self, calls: List[str]) -> str:
        """
        Build minimal unique short codes from callsigns.

        Rules:
          - If a call has a suffix (e.g. K7ABC/P), strip everything after "/" and
            derive the short code from the base call.
          - Start with the last 3 characters of the base call (or fewer if shorter).
          - If duplicates collide, incrementally extend to 4, 5, ... characters
            (up to the full base) until each code is unique.
          - Preserve input order; return space-delimited codes.
        """
        bases: List[str] = []
        for cs in calls:
            base = (cs or "").strip().upper()
            if not base:
                continue
            if "/" in base:
                base = base.split("/", 1)[0]
            bases.append(base)

        # Track how many chars to use from the end for each base call
        lengths = [min(3, len(b)) if len(b) < 3 else 3 for b in bases]

        # Gradually extend colliding codes until unique or max length reached
        while True:
            codes = [b[-lengths[i]:] for i, b in enumerate(bases)]
            counts = {}
            for c in codes:
                counts[c] = counts.get(c, 0) + 1
            duplicates = {idx for idx, c in enumerate(codes) if counts[c] > 1}
            if not duplicates:
                break
            progressed = False
            for idx in duplicates:
                if lengths[idx] < len(bases[idx]):
                    lengths[idx] += 1
                    progressed = True
            if not progressed:
                # Cannot disambiguate further (very short/identical bases); exit
                break

        return " ".join(bases[i][-lengths[i]:] for i in range(len(bases)))

    # ---------------- Qt events ---------------- #

    def showEvent(self, event):
        """
        When the tab is shown:
         - Reload settings (in case DIRECTED.TXT path or net schedule changed)
         - Try auto-prefill net name (if not in progress and empty)
         - Update clocks
        """
        super().showEvent(event)
        try:
            self._load_settings()
            self._auto_prefill_net_name()
            self._update_clock_labels()
        except Exception as e:
            log.error("JS8CallNetControl: showEvent failed: %s", e)
