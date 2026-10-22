from __future__ import annotations

import datetime
import re
import sqlite3
import time
import json
import queue
import uuid
from pathlib import Path
from typing import List, Dict, Set, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget,
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
)

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
from freqinout.core.config_paths import get_config_dir
from freqinout.gui.theme import (
    resolve_theme, button_style, fit_child_combo_boxes, fit_combo_box_to_contents,
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
    net_status_changed = Signal(str, bool)
    net_session_status_changed = Signal(str, bool, int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = SettingsManager()
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
        self._load_settings()
        self._setup_timer()
        self._setup_clock_timer()
        self._update_clock_labels()
        self._setup_js8_rx_timer()
        self._update_suspend_state()
        self._refresh_auto_query_flags()
        self._refresh_qsy_options()

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
            log.debug("JS8 NCS: failed to persist session snapshot: %s", exc)

    def _refresh_ncs_session_context(self) -> None:
        if not hasattr(self, "ncs_session_chip_layout") or not hasattr(self, "ncs_session_summary_label"):
            return
        profiles = self._ncs_radio_profiles()
        selected_id = self._ncs_session_radio_id or self._ncs_selected_radio_id(profiles)
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

    # ---------------- UI ---------------- #

    def _build_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
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

        session_group = QGroupBox("NCS Session")
        session_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        session_layout = QVBoxLayout()
        session_layout.setContentsMargins(12, 10, 12, 10)
        session_layout.setSpacing(6)
        chip_row = QHBoxLayout()
        chip_row.addWidget(QLabel("Radio:"))
        self.ncs_session_chip_layout = QHBoxLayout()
        self.ncs_session_chip_layout.setSpacing(8)
        chip_row.addLayout(self.ncs_session_chip_layout, 1)
        self.ncs_session_summary_label = QLabel("Session: Radio | JS8Call | NCS")
        self.ncs_session_summary_label.setWordWrap(True)
        session_layout.addLayout(chip_row)
        session_layout.addWidget(self.ncs_session_summary_label)
        session_group.setLayout(session_layout)
        layout.addWidget(session_group)

        setup_group = QGroupBox("Net Setup")
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
        self.group_edit = QLineEdit()
        self.group_edit.setPlaceholderText("@GROUP")
        controls_grid.addWidget(self.set_group_btn, 1, 1)
        controls_grid.addWidget(self.group_edit, 1, 2, 1, 2)

        self._js8_expect_label = QLabel("Expect:")
        controls_grid.addWidget(self._js8_expect_label, 1, 4)
        self.set_spotter_btn = QPushButton("Set Expect Query")
        self.spotter_combo = QComboBox()
        self.spotter_combo.setMinimumWidth(220)
        self.spotter_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        controls_grid.addWidget(self.set_spotter_btn, 1, 5)
        controls_grid.addWidget(self.spotter_combo, 1, 6, 1, 4)

        self._js8_qsy_label = QLabel("QSY:")
        controls_grid.addWidget(self._js8_qsy_label, 2, 0)
        self.qsy_combo = QComboBox()
        self.qsy_combo.currentIndexChanged.connect(self._update_qsy_button_enabled)
        controls_grid.addWidget(self.qsy_combo, 2, 1, 1, 3)
        self.hold_duration_combo = QComboBox()
        self.hold_duration_combo.setToolTip("Temporary schedule hold duration after QSY.")
        self.hold_duration_combo.currentIndexChanged.connect(self._on_hold_duration_changed)
        self._js8_hold_label = QLabel("Hold:")
        controls_grid.addWidget(self.hold_duration_combo, 2, 4)
        self.suspend_btn = QPushButton("QSY + Hold")
        controls_grid.addWidget(self.suspend_btn, 2, 5)
        self.ad_hoc_btn = QPushButton("Ad Hoc Net")
        controls_grid.addWidget(self.ad_hoc_btn, 2, 6)
        controls_grid.setColumnStretch(2, 1)
        controls_grid.setColumnStretch(3, 2)
        controls_grid.setColumnStretch(6, 2)
        controls_grid.setColumnStretch(7, 2)
        controls_grid.setColumnStretch(8, 2)
        controls_grid.setColumnStretch(9, 2)
        setup_group.setLayout(controls_grid)
        layout.addWidget(setup_group)

        # Check-ins table
        checkins_group = QGroupBox("Check-Ins")
        checkins_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        table_layout = QVBoxLayout()
        table_layout.setContentsMargins(12, 12, 12, 12)
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
        btn_row = QHBoxLayout()
        self._js8_action_row = btn_row
        btn_row.setSpacing(8)
        self.start_btn = QPushButton("Start Net")
        self.ack_btn = QPushButton("ACK GROUP")
        self.ack_callsign_btn = QPushButton("ACK CALLSIGN")
        self.group_spotter_btn = QPushButton("E? GROUP")
        self.single_spotter_btn = QPushButton("E? Callsign")
        self.save_btn = QPushButton("Save Checkins")
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
        btn_row.addWidget(self.end_btn)
        btn_row.addStretch()

        table_layout.addLayout(btn_row)
        checkins_group.setLayout(table_layout)
        layout.addWidget(checkins_group, 1)
        self._update_checkin_empty_state()

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
        self.group_edit.textChanged.connect(self._update_group_button_state)
        self.checkin_filter_combo.currentIndexChanged.connect(self._on_checkin_filter_changed)
        self.checkin_table.cellClicked.connect(self._on_checkin_table_clicked)
        self.refresh_spin.valueChanged.connect(self._update_timer_interval)
        self.suspend_btn.clicked.connect(self._on_suspend_clicked)
        self.ad_hoc_btn.clicked.connect(self._start_ad_hoc_net)
        self.role_combo.currentTextChanged.connect(lambda _text: self._on_ncs_session_context_changed())
        self.net_name_edit.textChanged.connect(lambda _text: self._on_ncs_session_context_changed())

        self._set_net_button_styles(active=False)
        self._refresh_ncs_session_context()
        QTimer.singleShot(0, self._reflow_ncs_layouts)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_ncs_layouts()

    def _reflow_ncs_layouts(self) -> None:
        """Reposition existing controls only; never reload NCS state on resize."""
        controls = (
            self._js8_role_label,
            self.role_combo,
            self._js8_net_name_label,
            self.net_name_edit,
            self._js8_refresh_label,
            self.refresh_spin,
            self._js8_group_label,
            self.group_edit,
            self.set_group_btn,
            self._js8_expect_label,
            self.spotter_combo,
            self.set_spotter_btn,
            self._js8_qsy_label,
            self.qsy_combo,
            self._js8_hold_label,
            self.hold_duration_combo,
            self.suspend_btn,
            self.ad_hoc_btn,
        )
        metrics = self.fontMetrics()
        widest_control = max(
            max(int(widget.minimumSizeHint().width()), int(widget.sizeHint().width()))
            for widget in controls
        )
        compact = self.width() < max(1000, (widest_control * 4) + (metrics.horizontalAdvance("M") * 8))
        grid = getattr(self, "_js8_setup_grid", None)
        if grid is not None:
            while grid.count():
                grid.takeAt(0)
            for column in range(10):
                grid.setColumnStretch(column, 0)
            if compact:
                rows = (
                    (self._js8_role_label, self.role_combo),
                    (self._js8_net_name_label, self.net_name_edit),
                    (self._js8_refresh_label, self.refresh_spin),
                    (self._js8_group_label, self.group_edit),
                    (self._js8_expect_label, self.spotter_combo),
                    (self._js8_qsy_label, self.qsy_combo),
                    (self._js8_hold_label, self.hold_duration_combo),
                )
                for row, (label, field) in enumerate(rows):
                    grid.addWidget(label, row, 0)
                    grid.addWidget(field, row, 1)
                grid.addWidget(self.set_group_btn, 7, 1)
                grid.addWidget(self.set_spotter_btn, 8, 1)
                grid.addWidget(self.suspend_btn, 9, 0)
                grid.addWidget(self.ad_hoc_btn, 9, 1)
                grid.setColumnStretch(1, 1)
            else:
                grid.addWidget(self._js8_role_label, 0, 0)
                grid.addWidget(self.role_combo, 0, 1)
                grid.addWidget(self._js8_net_name_label, 0, 2)
                grid.addWidget(self.net_name_edit, 0, 3, 1, 3)
                grid.addWidget(self._js8_refresh_label, 0, 6)
                grid.addWidget(self.refresh_spin, 0, 7)
                grid.addWidget(self._js8_group_label, 1, 0)
                grid.addWidget(self.group_edit, 1, 1, 1, 2)
                grid.addWidget(self.set_group_btn, 1, 3)
                grid.addWidget(self._js8_expect_label, 1, 4)
                grid.addWidget(self.spotter_combo, 1, 5, 1, 2)
                grid.addWidget(self.set_spotter_btn, 1, 7)
                grid.addWidget(self._js8_qsy_label, 2, 0)
                grid.addWidget(self.qsy_combo, 2, 1, 1, 2)
                grid.addWidget(self._js8_hold_label, 2, 3)
                grid.addWidget(self.hold_duration_combo, 2, 4)
                grid.addWidget(self.suspend_btn, 2, 5)
                grid.addWidget(self.ad_hoc_btn, 2, 6, 1, 2)
                for column in (3, 5):
                    grid.setColumnStretch(column, 1)
        action_row = getattr(self, "_js8_action_row", None)
        if action_row is not None:
            action_row.setDirection(QHBoxLayout.TopToBottom if compact else QHBoxLayout.LeftToRight)

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
        with perf_span("js8_ncs.on_tab_activated", settings=self.settings, min_ms=5.0):
            self._update_clock_labels()
            self._update_suspend_state()
            self._maybe_reload_operating_groups()
            self._refresh_group_completer()

    def set_tab_active(self, active: bool) -> None:
        self._active = bool(active)
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
                all_path = self._directed_path.parent / "ALL.TXT"
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
        self.spotter_combo.clear()
        forms = []
        for definition in discover_effective_spotter_forms(global_forms_path, legacy_forms_path):
            label = definition.form_code
            if definition.title:
                label = f"{definition.form_code} - {definition.title}"
            forms.append((definition.form_code, label))
        if forms:
            for code, label in forms:
                self.spotter_combo.addItem(label, code)
            self.spotter_combo.setEnabled(True)
            self.set_spotter_btn.setEnabled(True)
            self.group_spotter_btn.setEnabled(True)
            self.single_spotter_btn.setEnabled(True)
        else:
            self.spotter_combo.addItem("No forms found")
            self.spotter_combo.setEnabled(False)
            self.set_spotter_btn.setEnabled(False)
            self.group_spotter_btn.setEnabled(False)
            self.single_spotter_btn.setEnabled(False)
        fit_combo_box_to_contents(self.spotter_combo)
        self._refresh_ncs_session_context()

    def _refresh_ncs_offset_keys(self):
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
                    self._js8_rx_hub.unregister_listener(self._on_js8_rx_messages)
                except Exception:
                    pass
                self._js8_rx_registered = False
            self._js8_rx_hub = JS8RxHub.instance(host, port)
        if not self._js8_rx_registered:
            self._js8_rx_hub.register_listener(self._on_js8_rx_messages)
            self._js8_rx_registered = True
        try:
            self._js8_rx_hub.start(host, port)
        except RuntimeError:
            self._js8_rx_hub = JS8RxHub.instance(host, port)
            self._js8_rx_hub.register_listener(self._on_js8_rx_messages)
            self._js8_rx_registered = True
            self._js8_rx_hub.start(host, port)

    def _js8_live_source_context(self) -> Dict[str, str]:
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
            self.ack_btn.setStyleSheet(button_style("eligible_info", theme))
            self.ack_callsign_btn.setStyleSheet(button_style("eligible_info", theme))
            self.group_spotter_btn.setStyleSheet(button_style("eligible_info", theme))
            self.single_spotter_btn.setStyleSheet(button_style("eligible_info", theme))
            self.save_btn.setStyleSheet(button_style("eligible_success", theme))
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

    def _apply_theme(self) -> None:
        theme = resolve_theme(self.settings)
        self._start_btn_default_style = button_style("success", theme)
        self._end_btn_default_style = button_style("danger", theme)
        self._set_net_button_styles(self._net_in_progress)
        self.suspend_btn.setStyleSheet(button_style("warning", theme))
        self._update_group_button_state()
        self._update_spotter_button_state()
        self._refresh_ncs_session_context()

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
        win = self.window()
        return perform_qsy(win, meta)

    # ---------------- START / END NET ---------------- #

    def _validate_before_start(self) -> bool:
        if not self.net_name_edit.text().strip():
            # Try auto-prefill once if empty
            self._auto_prefill_net_name()
        if not self.net_name_edit.text().strip():
            QMessageBox.warning(self, "Missing Net Name", "Enter Net Name before starting the net.")
            return False

        cs = self._my_callsign()
        if not cs:
            QMessageBox.warning(self, "Missing Callsign", "Configure your callsign in the Settings tab.")
            return False
        if not self._directed_path:
            QMessageBox.warning(
                self,
                "DIRECTED.TXT Not Configured",
                "JS8Call DIRECTED.TXT path is not configured or does not exist.\n"
                "Set it in the Settings tab.",
            )
            return False
        if not self._group_target:
            QMessageBox.warning(
                self,
                "Group Required",
                "Set Call Group to Start Net.",
            )
            return False
        return True

    def _start_net(self):
        if self._net_in_progress:
            QMessageBox.information(self, "Net In Progress", "A net is already active. End it before starting a new one.")
            return
        if not self._validate_before_start():
            return

        self._net_in_progress = True
        profiles = self._ncs_radio_profiles()
        self._ncs_session_radio_id = self._ncs_selected_radio_id(profiles) or None
        self._ncs_session_id = uuid.uuid4().hex
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
        self._persist_ncs_session_snapshot(timing_state="active")
        self.net_status_changed.emit("JS8", True)
        self.net_session_status_changed.emit(
            "JS8", True, int(self._ncs_session_radio_id or 0), self._ncs_session_id
        )
        self._refresh_ncs_session_context()

          # Track file size so we only read new lines
        try:
            if self._directed_path:
                self._last_directed_size = self._directed_path.stat().st_size
                all_path = self._directed_path.parent / "ALL.TXT"
                self._last_all_size = all_path.stat().st_size if all_path.exists() else 0
        except Exception:
            self._last_directed_size = 0
            self._last_all_size = 0
        self._last_query_tx_ts = 0.0

        if self._poll_timer:
            self._poll_timer.start()

        log.info("JS8Call net started: %s (%s)", self.net_name_edit.text().strip(), self.role_combo.currentText())
        self._refresh_operator_history_views()

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
        if clicked == btn_no:
            return
        send_concluded = clicked == btn_yes
        session_radio_id = getattr(self, "_ncs_session_radio_id", None)
        session_id = getattr(self, "_ncs_session_id", "")

        if not self._net_in_progress:
            log.info("JS8Call End Net clicked but net_in_progress flag not set; writing log from current state.")

        if self._poll_timer:
            self._poll_timer.stop()

        self._net_end_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

        # Send net concluded to group if set
        if send_concluded:
            group = self._group_target or self.group_edit.text().strip().upper()
            if group:
                if not group.startswith("@"):
                    group = "@" + group
                mycall = self._my_callsign()
                if mycall:
                    self._send_js8_message(f"{mycall}: {group} NET CONCLUDED")

        # Record check-ins once more at end (no duplicate increments)
        self._save_checkins(show_message=False)

        # Write the net log file from the current panels
        self._write_net_log_file()

        self._net_in_progress = False
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
        QMessageBox.information(self, "Net Ended", "JS8Call net ended and log saved.")

    # ---------------- AUTO-PREFILL NET NAME ---------------- #

    def _auto_prefill_net_name(self):
        """
        Prefill net_name_edit from net_schedule if:
          - net is NOT in progress
          - net_name_edit is currently empty
          - there's a net on current UTC day whose (start_utc - early_checkin)
            is within the next 20 minutes.
        """
        if self._net_in_progress:
            return
        if self.net_name_edit.text().strip():
            return

        data = self.settings.all()
        net_sched = data.get("net_schedule", [])
        if not isinstance(net_sched, list):
            return

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        day_name = now_utc.strftime("%A")
        now_min = now_utc.hour * 60 + now_utc.minute

        best_row = None
        best_delta = 9999

        for row in net_sched:
            try:
                if row.get("day_utc") != day_name:
                    continue
                smin = self._parse_hhmm(row.get("start_utc", ""))
                if smin is None:
                    continue
                early = int(row.get("early_checkin", "0") or 0)
                s_eff = max(0, smin - early)
                delta = s_eff - now_min
                if 0 <= delta <= 20 and delta < best_delta:
                    best_row = row
                    best_delta = delta
            except Exception:
                continue

        if best_row:
            nn = (best_row.get("net_name") or "").strip()
            if nn and not self.net_name_edit.text().strip():
                self.net_name_edit.setText(nn)

    # ---------------- POLLING DIRECTED.TXT ---------------- #

    def _poll_directed_file(self):
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
                            if base_call and self._checkins.get(base_call, {}).get("offset") is not None:
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
        all_path = self._directed_path.parent / "ALL.TXT"
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
            record = broker.identity(call)
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
            if call in getattr(self, "_fcc_identity_pending", {}):
                grouped.setdefault(call, []).append(record)
        token = self._fcc_session_token()
        for call, records in grouped.items():
            if self._fcc_identity_pending.get(call) != token:
                self._drop_fcc_pending(call)
                continue
            if not self._active:
                continue
            broker = getattr(self, "_fcc_identity_broker", None)
            record = broker.identity(call) if broker is not None else None
            if record is None:
                continue
            self._apply_fcc_identity(call, (record,), token, update_widget=True)

    def _rebuild_checkin_table(self) -> None:
        self._checkin_rows = {}
        self._clear_table()
        for cs, data in self._checkins.items():
            self._update_row(cs, data)
        self._update_checkin_empty_state()

    def _update_checkin_empty_state(self) -> None:
        if not hasattr(self, "checkin_empty_label") or not hasattr(self, "checkin_table"):
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
        if self._checkin_filter_mode() != "All Callsigns":
            return ""
        return "Delete"

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
        resp = QMessageBox.question(
            self,
            "Delete Check-in",
            f"Remove {callsign} from this net?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        self._checkins.pop(callsign, None)
        self._drop_fcc_pending(callsign)
        self._status_mismatch.pop(callsign, None)
        self._checkins_saved.discard(callsign)
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
            db_path = _nets_db_path()
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
        base = cs.split("/", 1)[0]
        meta = self._lookup_operator_meta(base)
        if grid:
            meta["grid"] = grid
        data = self._checkins.get(base, {})
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
                self._drop_fcc_pending(base)
            else:
                self._queue_fcc_identity(base, token)
                try:
                    broker.request(base, interactive=False)
                except Exception:
                    pass
        elif name and state:
            self._drop_fcc_pending(base)
        current_status = (data.get("status") or "").upper()
        # Do not downgrade from ACKED/F!xxx to NEW
        if status:
            if current_status.startswith("F!") or current_status == "ACKED":
                status_to_use = data.get("status", status)
            else:
                status_to_use = status
        else:
            status_to_use = data.get("status", "")
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
        self._checkins[base] = data
        self._status_mismatch[base] = status_mismatch
        self._update_row(base, data)

    def _selected_callsign(self) -> Optional[str]:
        selected = self.checkin_table.selectedItems()
        if not selected:
            return None
        row = selected[0].row()
        item = self.checkin_table.item(row, 0)
        return item.text().strip().upper() if item else None

    def _set_group_target(self):
        txt = self.group_edit.text().strip().upper()
        if txt and not txt.startswith("@"):
            txt = "@" + txt.lstrip("@")
        if txt.count("@") > 1:
            txt = "@" + txt.replace("@", "")
        self._group_target = txt
        self.group_edit.setText(txt)
        self._update_group_button_state()

    def _update_group_button_state(self):
        theme = resolve_theme(self.settings)
        current = self.group_edit.text().strip().upper()
        if current and not current.startswith("@"):
            current = "@" + current
        needs_set = bool(current) and current != (self._group_target or "")
        if self._group_target and not needs_set:
            self.set_group_btn.setStyleSheet(button_style("muted", theme))
        elif needs_set:
            self.set_group_btn.setStyleSheet(button_style("eligible_info", theme))
        else:
            self.set_group_btn.setStyleSheet(button_style("muted", theme))

    def _set_spotter_form(self):
        if not self.spotter_combo.isEnabled():
            QMessageBox.warning(self, "Spotter", "No MCF forms found.")
            return
        self._spotter_form = self._current_spotter_code()
        self._expected_form = self._spotter_form
        self._update_spotter_button_state()

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

    def _on_spotter_selection_changed(self) -> None:
        theme = resolve_theme(self.settings)
        current = self._current_spotter_code()
        if current and current != (self._spotter_form or ""):
            self.set_spotter_btn.setStyleSheet(button_style("eligible_info", theme))
        else:
            self._update_spotter_button_state()

    def _refresh_group_completer(self):
        groups: Set[str] = set()
        mycall = self._base_callsign(self._my_callsign())
        db_path = _nets_db_path()
        if mycall and db_path.exists():
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    "SELECT group1, group2, group3, groups_json FROM operator_checkins WHERE callsign=?",
                    (mycall,),
                )
                row = cur.fetchone()
                conn.close()
                if row:
                    g1, g2, g3, groups_json = row
                    for g in (g1, g2, g3):
                        if g:
                            groups.add(str(g).strip().upper())
                    if groups_json:
                        try:
                            for g in json.loads(groups_json):
                                if g:
                                    groups.add(str(g).strip().upper())
                        except Exception:
                            pass
            except Exception as e:
                log.debug("JS8CallNetControl: group lookup failed: %s", e)
        for g in self.settings.get("primary_js8_groups", []) or []:
            if g:
                groups.add(str(g).strip().upper())
        normalized = []
        for g in groups:
            if not g:
                continue
            up = g if g.startswith("@") else f"@{g}"
            normalized.append(up)
            normalized.append(up.lstrip("@"))
        normalized = sorted(set(normalized))
        if normalized:
            completer = QCompleter(normalized, self)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchStartsWith)
            completer.setCompletionMode(QCompleter.InlineCompletion)
            self.group_edit.setCompleter(completer)

    def _ack_checkins(self):
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
        if not self._net_in_progress:
            QMessageBox.information(self, "Net", "Start the net first.")
            return
        group = self._group_target or self.group_edit.text().strip().upper()
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
        """
        Increment check-in counters for current check-ins (once per net).
        """
        try:
            for cs in self._checkins.keys():
                base = cs.split("/", 1)[0]
                if base in self._checkins_saved:
                    continue
                self._increment_checkin_counter(base)
                self._checkins_saved.add(base)
        except Exception as e:
            log.error("JS8CallNetControl: save checkins failed: %s", e)
        if show_message:
            QMessageBox.information(self, "Saved", "Check-ins recorded for this net.")
        self._refresh_operator_history_views()

    # ---------------- LOG FILE WRITING ---------------- #

    def _write_net_log_file(self):
        """
        Write net log into net_logs directory under DIRECTED.TXT's directory.

        Filename: netname-ROLE-YYYYMMDD.txt
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
            return

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
            log.error("JS8CallNetControl: unable to create net_logs directory: %s", e)
            return

        safe_net = "".join(c for c in net_name if c.isalnum() or c in ("_", "-", " "))
        safe_net = safe_net.replace(" ", "_") or "net"
        filename = f"{safe_net}-{role}-{date_str}.txt"
        path = net_logs_dir / filename

        # Collect all full callsigns from the table
        all_calls = list(self._checkins.keys())

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

        try:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info("JS8Call net log written to %s", path)
        except Exception as e:
            log.error("JS8CallNetControl: failed to write net log file %s: %s", path, e)

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
        db_path = _nets_db_path()
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
        if self._is_shutting_down or self._polling_rx:
            return
        self._polling_rx = True
        try:
            source_context = self._js8_live_source_context()
            try:
                if any("F!" in str((msg.get("params", {}) or {}).get("TEXT") or msg.get("value") or "").upper() for msg in messages if isinstance(msg, dict)):
                    MessageIngestor(self.settings).ingest_spotter_from_js8_events(
                        messages,
                        source_radio_id=source_context.get("source_radio_id", ""),
                        js8_instance_id=source_context.get("js8_instance_id", ""),
                        source_key=source_context.get("source_id", ""),
                    )
            except Exception as exc:
                log.debug("JS8CallNetControl: Spotter live ingest failed: %s", exc)
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
                        if self._net_in_progress:
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
                                base_frm,
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
                    # Spotter form response handling
                    if self._net_in_progress and self._expected_form:
                        forms_found = extract_form_codes(combined)
                        for form in forms_found:
                            if not self._is_checkin_form_code(form):
                                continue
                            if base_frm:
                                mismatch = form != self._expected_form
                                self._upsert_checkin(
                                    base_frm,
                                    status=form,
                                    status_mismatch=mismatch,
                                )
                except Exception:
                    continue
        finally:
            self._polling_rx = False

    def shutdown(self) -> None:
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
                self._js8_rx_hub.unregister_listener(self._on_js8_rx_messages)
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
        mode = self._checkin_filter_mode()
        if mode in {"Mapped Check-ins", "F!103 / F!104"}:
            return self._line_has_checkin_form(line)
        if mode == "Any Spotter":
            return self._line_has_any_spotter_form(line)
        if mode == "All Callsigns":
            return True
        return self._line_has_checkin_form(line)

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
        return _nets_db_path()

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
            db_path = _nets_db_path()
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
            db_path = get_config_dir() / "config" / "freqinout.db"
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
