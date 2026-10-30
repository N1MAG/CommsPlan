from __future__ import annotations

import datetime
import json
import sqlite3
import uuid
from pathlib import Path
from typing import List, Dict, Optional
import re

from PySide6.QtCore import Qt, QTimer, QEvent, Signal
from PySide6.QtWidgets import (
    QFrame,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QBoxLayout,
    QLabel,
    QCheckBox,
    QLineEdit,
    QTextEdit,
    QPushButton,
    QFileDialog,
    QMessageBox,
    QComboBox,
    QApplication,
    QButtonGroup,
    QCompleter,
    QDialog,
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QScrollArea,
    QSplitter,
)
from PySide6.QtGui import QFontMetrics, QColor

from freqinout.core.settings_manager import SettingsManager
from freqinout.core.logger import log
from freqinout.core.perf_metrics import span as perf_span
from freqinout.core.checkins_db import lookup_operator_identity, upsert_checkins
from freqinout.core.config_paths import get_fldigi_checkin_dir
from freqinout.core.fldigi_log_checkin_parser import FldigiLogCheckinCandidate, scan_fldigi_log_file
from freqinout.core.fldigi_macro_parser import scan_macro_profile, count_detected_file_references
from freqinout.core.fldigi_macro_profile import macro_mapping_path_leaf
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.ncs_session_contract import NcsSessionSnapshot, write_ncs_session_snapshot
from freqinout.core.fldigi_role_workspace import (
    default_role_workspace_prefs,
    get_role_workspace_preset,
    load_role_workspace_prefs,
    normalize_role,
    role_compare_defaults,
)
from freqinout.gui.fldigi_macro_mapping_dialog import FldigiMacroMappingDialog
from freqinout.gui.fldigi_workspace_cards import WorkspaceBucketCard
from freqinout.gui.net_tuning_actions import show_net_tuning_dialog
from freqinout.gui.help_registry import resolve_help_host
from freqinout.utils.timezones import get_timezone
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
from freqinout.gui.theme import (
    button_height_for_font,
    contrast_text_for_background,
    resolve_theme,
    button_style,
    style_splitter_handles,
)

from freqinout.core.ncs_record_files import CURRENT_CHECKIN_FILE_NAMES, ROLE_CHECKIN_FILE_NAMES, generated_checkin_paths

ACTION_SCOPES = ("NCS", "ANCS", "SHARED", "ALL")


class NumericTableItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            left = self.data(Qt.UserRole)
            right = other.data(Qt.UserRole) if other is not None else None
            if left is not None and right is not None:
                return int(left) < int(right)
        except Exception:
            pass
        return super().__lt__(other)


class FldigiNetControlTab(QWidget):
    """
    FLDigi Net Control tab.

    - Uses Settings (callsign, name, state)
    - Uses net_schedule entries to help auto-complete Net Name
    - Uses operator_checkins SQLite DB to auto-suggest known operators
    - Manages two files:
        * Net Check-in Macro File (main log)
        * Late Check-in Macro File (feed for late/new check-ins)
    """
    net_status_changed = Signal(str, bool)
    net_session_status_changed = Signal(str, bool, int, str)
    LOG_ASSISTED_INTAKE_VISIBLE = False
    FLDIGI_MACRO_PROFILES_KEY = "fldigi_macro_profiles_v1"
    FLDIGI_SELECTED_MACRO_PROFILE_KEY = "fldigi_selected_macro_profile"
    COL_SEQ = 0
    COL_HEARD = 1
    COL_ACKED = 2
    COL_CALLSIGN = 3
    COL_NAME = 4
    COL_STATE = 5
    COL_KEYWORD = 6
    COL_TRAFFIC = 7
    COL_TFC_STATUS = 8
    COL_CATEGORY = 9
    COL_NOTES = 10
    COL_ROLE = 11

    def __init__(self, parent=None, *, preparation_settings=None):
        super().__init__(parent)
        self._preparation_shell = preparation_settings is not None
        self._preparation_scope = None
        self.settings = preparation_settings if preparation_settings is not None else SettingsManager()

        self._net_in_progress = False
        self._net_start_utc: Optional[str] = None
        self._net_end_utc: Optional[str] = None
        self._active = False
        self._ncs_session_radio_id: Optional[int] = None
        self._ncs_session_id: str = ""

        self._clock_timer: Optional[QTimer] = None
        self._log_assisted_timer: Optional[QTimer] = None

        # Next frequency change tracking
        self._next_change_utc: Optional[datetime.datetime] = None
        self._auto_end_done: bool = False
        self._qsy_options: Dict[str, Dict] = {}
        self._opgroups_sig: str = ""

        self._start_btn_default_style: str = ""
        self._save_btn_default_style: str = ""
        self._normalizing_main = False
        self._normalizing_late = False
        self._known_operator_rows: List[Dict[str, str]] = []
        self._known_operator_by_callsign: Dict[str, Dict[str, str]] = {}
        self._known_op_autofilled_prefix: str = ""
        self._known_op_autofill_consumed: bool = False
        self._known_op_tab_stage: int = 0
        self._known_op_pending_focus: Optional[str] = None
        self._macro_profile_loading = False
        self._workspace_role_loading = False
        self._role_workspace_prefs = default_role_workspace_prefs()
        self._workspace_bucket_cards: Dict[str, WorkspaceBucketCard] = {}
        self._workspace_bucket_defaults: Dict[str, Dict[str, str]] = {}
        self._workspace_visible_bucket_ids: set[str] = set()
        self._custom_bucket_cards: Dict[str, WorkspaceBucketCard] = {}
        self._custom_bucket_sources: Dict[str, str] = {}
        self._roster_syncing = False
        self._roster_loading = False
        self._macro_profile_combo_loading = False
        self._setup_details_expanded = False
        self._compare_workspace_expanded = False
        self._roster_dirty = False
        self._activation_secondary_refresh_pending: bool = False
        self._activation_secondary_refresh_inflight: bool = False
        self._ncs_partner_call: str = ""
        self._ancs_partner_call: str = ""
        self._next_tfc_last_served: Dict[str, str] = {"NCS": "", "ANCS": ""}
        self._next_tfc_called_by_role: Dict[str, set[str]] = {"NCS": set(), "ANCS": set()}
        self._roster_action_scope: str = "NCS"
        self._roster_action_scope_user_selected: bool = False
        self._next_roster_seq: int = 1
        self._log_assisted_session_path: str = ""
        self._log_assisted_session_offset: int = 0
        self._log_assisted_session_tx_context: str = ""
        self._log_assisted_seen_normalized: set[str] = set()
        self._log_assisted_candidates_by_callsign: Dict[str, FldigiLogCheckinCandidate] = {}

        self._responsive_geometry_timer = QTimer(self)
        self._responsive_geometry_timer.setSingleShot(True)
        self._responsive_geometry_timer.timeout.connect(self._refresh_responsive_geometry)

        self._build_ui()
        self._apply_theme()
        if not self._preparation_shell:
            self._load_settings()
            self._load_known_operators()
            self._setup_timers()
            self._refresh_qsy_options()
        self._set_net_button_styles(active=False)
        self._sync_roster_action_scope_to_role(force=True)
        from freqinout.gui.net_prepared_receiver import PreparedReceiver
        self._prepared_receiver = PreparedReceiver(self, "FLDIGI", writer=getattr(preparation_settings, "writer", None))

    # ---------------- UI BUILD ---------------- #

    def _ncs_radio_profiles(self) -> List[Dict]:
        scope = getattr(self, "_preparation_scope", None)
        if scope is not None:
            return [scope.settings().get('_preparation_profile', {'id': scope.radio_id, 'name': scope.radio_name})]
        if getattr(self, "_preparation_shell", False):
            return []
        try:
            profiles = MultiRadioStore().list_runtime_active_device_profiles()
        except Exception as exc:
            log.debug("FLDigi NCS: failed to read runtime radio profiles: %s", exc)
            profiles = []
        fldigi_profiles = [p for p in profiles if bool(p.get("use_fldigi", False))]
        if fldigi_profiles:
            profiles = fldigi_profiles
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
            log.debug("FLDigi NCS: failed to set runtime radio session %s: %s", ident, exc)
        try:
            win = self.window()
            if win is not None and hasattr(win, "_activate_station_command_radio"):
                win._activate_station_command_radio(ident)  # type: ignore[attr-defined]
        except Exception as exc:
            log.debug("FLDigi NCS: failed to focus station command radio %s: %s", ident, exc)
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
            protocol="FLDigi/SSB",
            source_id=str(selected_id or "radio"),
            source_name=self._ncs_profile_name(selected_profile),
            role=self.role_combo.currentText().strip() if hasattr(self, "role_combo") else "NCS",
            net_name=self.net_name_combo.currentText().strip() if hasattr(self, "net_name_combo") else "",
            timing_state=state,
            started_utc=self._net_start_utc or "",
            ended_utc=self._net_end_utc or "",
            detail="FLDigi/SSB NCS session scoped to one configured radio.",
        )

    def _persist_ncs_session_snapshot(self, *, timing_state: Optional[str] = None) -> None:
        try:
            write_ncs_session_snapshot(self.settings, self._current_ncs_session_snapshot(timing_state=timing_state))
        except Exception as exc:
            if getattr(self, '_preparation_scope', None) is not None:
                raise
            log.debug("FLDigi NCS: failed to persist session snapshot: %s", exc)

    def _refresh_ncs_session_context(self) -> None:
        if not hasattr(self, "ncs_session_chip_layout") or not hasattr(self, "ncs_session_summary_label"):
            return
        profiles = self._ncs_radio_profiles()
        selected_id = self._ncs_session_radio_id or self._ncs_selected_radio_id(profiles)
        self._draft_radio_id = selected_id
        theme = resolve_theme(self.settings)
        self._clear_ncs_session_chip_layout()
        if not profiles:
            self.ncs_session_chip_layout.addWidget(QLabel("No active FLDigi radio"))
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
        net_name = self.net_name_combo.currentText().strip() if hasattr(self, "net_name_combo") else ""
        summary = f"Session: {radio_name} | FLDigi/SSB | {role}"
        if net_name:
            summary = f"{summary} | {net_name}"
        if self._net_in_progress:
            summary = f"{summary} | Active"
        self.ncs_session_summary_label.setText(summary)
        self.ncs_session_summary_label.setToolTip(summary)
        self._refresh_net_tuning_action_state()
        if hasattr(self, "_responsive_geometry_timer"):
            self._responsive_geometry_timer.start(0)

    def _selected_net_tuning_radio_id(self) -> int:
        """Return the already-published/session-scoped radio identity."""
        try:
            active = int(getattr(self, "_ncs_session_radio_id", 0) or 0)
            if active > 0:
                return active
        except (TypeError, ValueError):
            pass
        try:
            window = self.window()
            selected = int(getattr(window, "_station_command_selected_profile_id", 0) or 0)
            if selected > 0:
                return selected
        except (TypeError, ValueError, AttributeError):
            pass
        return 0

    def _refresh_net_tuning_action_state(self) -> None:
        button = getattr(self, "net_tuning_btn", None)
        if button is None:
            return
        radio_id = self._selected_net_tuning_radio_id()
        button.setEnabled(radio_id > 0)
        button.setToolTip(
            "Apply the configured FLDigi Center + Narrow tuning to this radio."
            if radio_id > 0
            else "Select a configured radio before applying NET FLDigi tuning."
        )

    def _open_net_tuning_dialog(self) -> None:
        radio_id = self._selected_net_tuning_radio_id()
        if radio_id <= 0:
            return
        window = self.window()
        scheduler = getattr(window, "scheduler", None)
        if scheduler is None:
            return
        radio_name = ""
        cache = getattr(window, "_station_command_profile_cache", ())
        for profile in cache if isinstance(cache, (list, tuple)) else ():
            try:
                if int(profile.get("id", 0) or 0) == radio_id:
                    radio_name = str(profile.get("name") or profile.get("label") or "").strip()
                    break
            except (AttributeError, TypeError, ValueError):
                continue
        if not radio_name:
            radio_name = f"Radio {radio_id}"
        show_net_tuning_dialog(
            self,
            scheduler,
            radio_id,
            radio_name=radio_name,
            net_name=self.net_name_combo.currentText().strip() if hasattr(self, "net_name_combo") else "",
        )

    def _build_session_bar(self, layout: QVBoxLayout) -> None:
        session_frame = QFrame()
        session_frame.setFrameShape(QFrame.StyledPanel)
        session_frame.setFrameShadow(QFrame.Raised)
        session_layout = QVBoxLayout(session_frame)
        session_layout.setContentsMargins(10, 8, 10, 8)
        session_layout.setSpacing(6)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("<h3>FLDigi / SSB Net Control</h3>"))
        title_row.addStretch()
        self.utc_label = QLabel()
        self.local_label = QLabel()
        self.utc_label.setVisible(False)
        self.local_label.setVisible(False)
        title_row.addWidget(self.utc_label)
        title_row.addWidget(self.local_label)
        self.total_checkins_label = QLabel("Total Check-ins: 0")
        self.total_checkins_label.setStyleSheet(self._count_chip_style(resolve_theme(self.settings)))
        self.total_checkins_label.setVisible(False)
        session_layout.addLayout(title_row)

        session_context_row = QHBoxLayout()
        self._session_context_row = session_context_row
        session_context_row.setSpacing(8)
        session_context_row.addWidget(QLabel("Radio:"))
        self.ncs_session_chip_layout = QHBoxLayout()
        self.ncs_session_chip_layout.setSpacing(8)
        session_context_row.addLayout(self.ncs_session_chip_layout, 1)
        self.start_btn = QPushButton("Start Net")
        self.end_btn = QPushButton("End Net")
        self.ad_hoc_btn = QPushButton("Ad Hoc Net")
        self.ad_hoc_btn.clicked.connect(self._start_ad_hoc_net)
        for btn in (self.start_btn, self.end_btn, self.ad_hoc_btn):
            btn.setMinimumWidth(max(110, button_height_for_font(btn) * 3))
            session_context_row.addWidget(btn)
        self.ncs_session_summary_label = QLabel("Session: Radio | FLDigi/SSB | NCS")
        self.ncs_session_summary_label.setWordWrap(True)
        self.ncs_session_summary_label.setTextFormat(Qt.PlainText)
        session_layout.addLayout(session_context_row)
        session_layout.addWidget(self.ncs_session_summary_label)

        context_row = QHBoxLayout()
        self._session_context_details_row = context_row
        context_row.addWidget(QLabel("Role:"))
        self.role_combo = QComboBox()
        self.role_combo.addItems(["NCS", "ANCS", "Joiner"])
        self.role_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.role_combo.setMinimumWidth(max(QFontMetrics(self.role_combo.font()).horizontalAdvance("Joiner") + 48, 120))
        context_row.addWidget(self.role_combo)

        context_row.addSpacing(16)
        context_row.addWidget(QLabel("Net Name:"))
        self.net_name_combo = QComboBox()
        self.net_name_combo.setEditable(True)
        self.net_name_combo.setInsertPolicy(QComboBox.NoInsert)
        self.net_name_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.net_name_combo.setMinimumWidth(220)
        self.net_name_combo.setMaximumWidth(440)
        self.net_name_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        context_row.addWidget(self.net_name_combo)

        context_row.addSpacing(16)
        self.next_change_label = QLabel("Next Scheduled Net: (unknown)")
        self.next_change_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.next_change_label.setWordWrap(False)
        self.next_change_label.setMinimumWidth(360)
        self.next_change_label.setMaximumWidth(620)
        self.next_change_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.next_change_label.setStyleSheet("QLabel { padding: 2px 4px; }")
        context_row.addWidget(self.next_change_label)
        context_row.addStretch(1)
        session_layout.addLayout(context_row)

        partner_row = QHBoxLayout()
        self._session_partner_row = partner_row
        self.partner_primary_label = QLabel("ANCS Callsign:")
        self.partner_primary_edit = QLineEdit()
        self.partner_primary_edit.setMaximumWidth(150)
        self.partner_primary_btn = QPushButton("Set ANCS")
        self.joiner_ncs_label = QLabel("NCS:")
        self.joiner_ncs_edit = QLineEdit()
        self.joiner_ncs_edit.setMaximumWidth(130)
        self.joiner_ancs_label = QLabel("ANCS:")
        self.joiner_ancs_edit = QLineEdit()
        self.joiner_ancs_edit.setMaximumWidth(130)
        self.joiner_add_btn = QPushButton("Add to Roster")
        self.partner_status_label = QLabel("")
        self.log_assisted_enable_chk = QCheckBox("Log-assisted intake")
        self.log_assisted_enable_chk.setToolTip("Watch the configured FLDigi log for new RX check-ins during a net.")
        self.log_assisted_enable_chk.setVisible(bool(self.LOG_ASSISTED_INTAKE_VISIBLE))
        self.log_assisted_enable_chk.toggled.connect(self._on_log_assisted_toggled)
        self.session_tools_layout = QHBoxLayout()
        self.session_tools_layout.setSpacing(6)
        self.net_tuning_btn = QPushButton("Center + Narrow…")
        self.net_tuning_btn.setObjectName("fldigiNetTuningAction")
        self.net_tuning_btn.setAccessibleName("Apply FLDigi Center and Narrow tuning")
        self.net_tuning_btn.clicked.connect(self._open_net_tuning_dialog)
        self.session_tools_layout.addWidget(self.net_tuning_btn)
        self.help_btn = QPushButton("Help")
        self.help_btn.setToolTip("Open FLDigi / SSB Net Control help.")
        self.help_btn.clicked.connect(lambda: self._open_context_help("tab.ncs-fldigi"))
        partner_row.addWidget(self.partner_primary_label)
        partner_row.addWidget(self.partner_primary_edit)
        partner_row.addWidget(self.partner_primary_btn)
        partner_row.addSpacing(10)
        partner_row.addWidget(self.joiner_ncs_label)
        partner_row.addWidget(self.joiner_ncs_edit)
        partner_row.addWidget(self.joiner_ancs_label)
        partner_row.addWidget(self.joiner_ancs_edit)
        partner_row.addWidget(self.joiner_add_btn)
        partner_row.addWidget(self.log_assisted_enable_chk)
        partner_row.addWidget(self.partner_status_label, stretch=1)
        partner_row.addLayout(self.session_tools_layout)
        partner_row.addWidget(self.help_btn, 0, Qt.AlignRight)

        # The Station Command Bar owns QSY and temporary hold control. Keep
        # these widgets alive for legacy helpers/signals, but do not duplicate
        # schedule-control UI inside the NCS cockpit.
        self.qsy_combo = QComboBox(session_frame)
        self.qsy_combo.currentIndexChanged.connect(self._update_qsy_button_enabled)
        self.hold_duration_combo = QComboBox(session_frame)
        self.hold_duration_combo.setToolTip("Temporary schedule hold duration after QSY.")
        self.hold_duration_combo.currentIndexChanged.connect(self._on_hold_duration_changed)
        self.suspend_btn = QPushButton("QSY + Hold", session_frame)
        self.suspend_btn.clicked.connect(self._on_suspend_clicked)
        for widget in (self.qsy_combo, self.hold_duration_combo, self.suspend_btn):
            widget.setVisible(False)
        session_layout.addLayout(partner_row)

        layout.addWidget(session_frame)

    def _build_setup_strip(self, layout: QVBoxLayout) -> None:
        setup_frame = QFrame()
        setup_frame.setFrameShape(QFrame.NoFrame)
        setup_frame.setFrameShadow(QFrame.Plain)
        self.setup_frame = setup_frame
        setup_layout = QVBoxLayout(setup_frame)
        setup_layout.setContentsMargins(10, 4, 10, 4)
        setup_layout.setSpacing(4)

        self.macro_profile_details_btn = QToolButton()
        self.macro_profile_details_btn.setCheckable(True)
        self.macro_profile_details_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.macro_profile_details_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.macro_profile_details_btn.setMaximumWidth(220)
        self.macro_profile_details_btn.setMinimumHeight(button_height_for_font(self.macro_profile_details_btn))
        if hasattr(self, "session_tools_layout"):
            self.session_tools_layout.addWidget(self.macro_profile_details_btn)
        else:
            setup_layout.addWidget(self.macro_profile_details_btn, 0, Qt.AlignLeft)

        self.setup_details_frame = QFrame()
        self.setup_details_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        details_layout = QVBoxLayout(self.setup_details_frame)
        details_layout.setContentsMargins(0, 4, 0, 0)
        details_layout.setSpacing(4)

        self.macro_setup_controls = QWidget()
        macro_setup_controls_layout = QVBoxLayout(self.macro_setup_controls)
        macro_setup_controls_layout.setContentsMargins(0, 0, 0, 0)
        macro_setup_controls_layout.setSpacing(4)

        summary_row = QHBoxLayout()
        self._macro_summary_row = summary_row
        summary_row.addWidget(QLabel("Macro Set:"))
        self.macro_profile_combo = QComboBox()
        self.macro_profile_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.macro_profile_combo.setMinimumWidth(220)
        self.macro_profile_combo.setMaximumWidth(420)
        self.macro_profile_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        summary_row.addWidget(self.macro_profile_combo)
        self.macro_profile_refresh_btn = QPushButton("Refresh")
        self.macro_profile_map_btn = QPushButton("Mappings...")
        summary_row.addWidget(self.macro_profile_refresh_btn)
        summary_row.addWidget(self.macro_profile_map_btn)
        macro_setup_controls_layout.addLayout(summary_row)

        path_row = QHBoxLayout()
        self._macro_path_row = path_row
        path_row.addWidget(QLabel("Macro File:"))
        self.macro_profile_edit = QLineEdit()
        self.macro_profile_edit.setPlaceholderText("Select an FLDigi macro profile (.mdf)")
        self.macro_profile_edit.setMaximumWidth(520)
        self.macro_profile_edit.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        path_row.addWidget(self.macro_profile_edit)
        self.macro_profile_browse_btn = QPushButton("Browse...")
        self.macro_profile_clear_btn = QPushButton("Clear")
        path_row.addWidget(self.macro_profile_browse_btn)
        path_row.addWidget(self.macro_profile_clear_btn)
        macro_setup_controls_layout.addLayout(path_row)
        details_layout.addWidget(self.macro_setup_controls)

        self.macro_profile_status = QLabel("Legacy mode: no macro profile selected.")
        self.macro_profile_status.setWordWrap(True)
        details_layout.addWidget(self.macro_profile_status)

        mapping_action_row = QHBoxLayout()
        self.macro_profile_edit_mappings_btn = QPushButton("Edit Mappings...")
        self.macro_profile_edit_mappings_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.macro_profile_edit_mappings_btn.clicked.connect(self._open_macro_mapping_dialog)
        mapping_action_row.addWidget(self.macro_profile_edit_mappings_btn)
        mapping_action_row.addStretch()
        details_layout.addLayout(mapping_action_row)

        self.macro_mapping_locations_label = QLabel()
        self.macro_mapping_locations_label.setWordWrap(True)
        self.macro_mapping_locations_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        details_layout.addWidget(self.macro_mapping_locations_label)

        setup_layout.addWidget(self.setup_details_frame)

        layout.addWidget(setup_frame)
        self._set_setup_details_expanded(False)

    @staticmethod
    def _count_chip_style(theme: Dict[str, str]) -> str:
        return (
            "QLabel {"
            f" border: 1px solid {theme['border']};"
            f" color: {theme['text']};"
            f" background-color: {theme['surface_alt']};"
            " padding: 2px 6px; border-radius: 3px;"
            " }"
        )

    def _make_status_chip(self, text: str) -> QLabel:
        chip = QLabel(text)
        chip.setStyleSheet(self._status_chip_style(resolve_theme(self.settings), "neutral"))
        return chip

    @staticmethod
    def _theme_chip_fill(color_hex: str, alpha: float) -> str:
        color = QColor(color_hex)
        color.setAlphaF(max(0.0, min(alpha, 1.0)))
        return color.name(QColor.HexArgb)

    def _status_chip_style(self, theme: Dict[str, str], state: str) -> str:
        state_key = str(state or "neutral").strip().lower()
        palette = {
            "neutral": (
                theme.get("surface_alt", theme.get("surface", "#f5f5f5")),
                theme.get("border", "#888888"),
                theme.get("text_muted", theme.get("text", "#444444")),
            ),
            "active": (
                self._theme_chip_fill(theme.get("accent", "#1565C0"), 0.18),
                theme.get("accent", "#1565C0"),
                theme.get("accent", "#1565C0"),
            ),
            "ready": (
                self._theme_chip_fill(theme.get("success", "#2E7D32"), 0.16),
                theme.get("success", "#2E7D32"),
                theme.get("success", "#2E7D32"),
            ),
            "warning": (
                self._theme_chip_fill(theme.get("warning", "#EF6C00"), 0.16),
                theme.get("warning", "#EF6C00"),
                theme.get("warning", "#EF6C00"),
            ),
        }
        background, border, text_color = palette.get(state_key, palette["neutral"])
        return (
            "QLabel {"
            f" border: 1px solid {border};"
            " padding: 2px 8px;"
            " border-radius: 9px;"
            f" background-color: {background};"
            f" color: {text_color};"
            " font-weight: 600;"
            " }"
        )

    def _macro_header_style(self) -> str:
        theme = resolve_theme(self.settings)
        return (
            "QToolButton {"
            f" background-color: {theme.get('surface_alt', '#DDE1E6')};"
            f" color: {theme.get('text', '#1C1F21')};"
            f" border: 1px solid {theme.get('border', '#D3D7DD')};"
            " border-radius: 5px;"
            " padding: 4px 8px;"
            " font-weight: 700;"
            " text-align: left;"
            " }"
            "QToolButton:hover {"
            f" background-color: {theme.get('surface', '#F0F2F4')};"
            " }"
        )

    def _set_status_chip(self, chip: QLabel, text: str, state: str) -> None:
        chip.setText(text)
        chip.setStyleSheet(self._status_chip_style(resolve_theme(self.settings), state))

    def _set_setup_details_expanded(self, expanded: bool) -> None:
        self._setup_details_expanded = bool(expanded)
        self.setup_details_frame.setVisible(self._setup_details_expanded)
        if hasattr(self, "setup_frame"):
            self.setup_frame.setVisible(self._setup_details_expanded)
        self.macro_profile_details_btn.setChecked(self._setup_details_expanded)
        self.macro_profile_details_btn.setArrowType(Qt.DownArrow if self._setup_details_expanded else Qt.RightArrow)
        self.macro_profile_details_btn.setStyleSheet(self._macro_header_style())
        self._refresh_setup_summary()

    def _build_operator_action_band(self, layout: QVBoxLayout) -> None:
        known_row = QHBoxLayout()
        self._known_operator_layout = known_row
        known_row.addWidget(QLabel("Operator Lookup/Add:"))
        self.known_op_edit = QLineEdit()
        self.known_op_edit.setPlaceholderText("Enter Callsign Name State...")
        self.known_op_edit.setMinimumWidth(240)
        self.known_op_edit.setMaximumWidth(520)
        self.known_op_edit.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        known_row.addWidget(self.known_op_edit)
        from freqinout.gui.fcc_identity_lookup import NcsIdentityPrefetch
        self._fcc_identity = NcsIdentityPrefetch(
            self, self.known_op_edit, self._split_checkin_with_extra,
            known=lambda: self._known_operator_by_callsign,
        )
        from freqinout.gui.call_finder_dialog import open_ncs_draft_picker
        self.fcc_lookup_btn = QPushButton("FCC lookup…")
        self.fcc_lookup_btn.setToolTip("Review offline FCC licensee details for this draft; does not add or transmit a check-in.")
        self.fcc_lookup_btn.clicked.connect(lambda: open_ncs_draft_picker(
            self, self.known_op_edit, self._split_checkin_with_extra, self._format_entry,
            lambda: self._known_operator_by_callsign,
        ))
        known_row.addWidget(self.fcc_lookup_btn)
        known_row.addStretch(1)
        layout.addLayout(known_row)

        known_btn_row = QHBoxLayout()
        self._known_buttons_layout = known_btn_row
        self.add_known_tfc_btn = QPushButton("Add to TFC")
        self.add_known_qru_btn = QPushButton("Add to QRU")
        self.add_known_late_btn = QPushButton("Add to LATE")
        self.add_known_seen_locally_btn = QPushButton("Add to Seen Locally")
        for btn in (
            self.add_known_tfc_btn,
            self.add_known_qru_btn,
            self.add_known_late_btn,
            self.add_known_seen_locally_btn,
        ):
            btn.setFocusPolicy(Qt.StrongFocus)
            known_btn_row.addWidget(btn)
        self._known_add_buttons = {
            "tfc": self.add_known_tfc_btn,
            "qru": self.add_known_qru_btn,
            "late": self.add_known_late_btn,
            "seen_locally": self.add_known_seen_locally_btn,
        }
        self._known_add_button_targets = {button: target for target, button in self._known_add_buttons.items()}
        self.roster_total_label = QLabel("Total Check-ins: 0")
        self.roster_total_label.setStyleSheet(self._count_chip_style(resolve_theme(self.settings)))
        self.roster_tfc_label = QLabel("TFC: 0")
        self.roster_qru_label = QLabel("QRU: 0")
        self.roster_late_label = QLabel("LATE: 0")
        for label in (self.roster_total_label, self.roster_tfc_label, self.roster_qru_label, self.roster_late_label):
            label.setStyleSheet(self._count_chip_style(resolve_theme(self.settings)))
            known_btn_row.addWidget(label)
        known_btn_row.addStretch(1)
        layout.addLayout(known_btn_row)

    def _build_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self._ncs_scroll_area = QScrollArea()
        self._ncs_scroll_area.setWidgetResizable(True)
        self._ncs_scroll_area.setFrameShape(QFrame.NoFrame)
        self._ncs_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._ncs_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer_layout.addWidget(self._ncs_scroll_area, 1)

        self._ncs_scroll_content = QWidget()
        self._ncs_scroll_content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(self._ncs_scroll_content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        self._build_session_bar(layout)
        self._build_setup_strip(layout)
        self._build_operator_action_band(layout)

        layout.addSpacing(6)

        # Bucket workspace panels
        self._left_bucket_col = QVBoxLayout()
        self._left_bucket_col.setSpacing(10)
        layout.addLayout(self._left_bucket_col, stretch=1)

        self.roster_compare_splitter = QSplitter(Qt.Vertical)
        style_splitter_handles(self.roster_compare_splitter, resolve_theme(self.settings))
        self.roster_compare_splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._left_bucket_col.addWidget(self.roster_compare_splitter, stretch=1)

        self.roster_frame = QFrame()
        self.roster_frame.setFrameShape(QFrame.StyledPanel)
        self.roster_frame.setFrameShadow(QFrame.Raised)
        self.roster_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        roster_layout = QVBoxLayout(self.roster_frame)
        roster_layout.setContentsMargins(10, 8, 10, 8)
        roster_layout.setSpacing(6)

        roster_header = QHBoxLayout()
        self._roster_header_layout = roster_header
        roster_header.addWidget(QLabel("<h3>Net Roster</h3>"))
        roster_header.addStretch()
        self.roster_compare_status_btn = QPushButton("Compare Rosters")
        self.roster_compare_status_btn.setToolTip("Show Roster Compare for NCS/ANCS roster gaps.")
        self.roster_compare_status_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        roster_header.addWidget(self.roster_compare_status_btn)
        self.copy_roster_gap_btn = QPushButton("Copy Gap")
        self.copy_roster_gap_btn.setToolTip("Copy the current NCS/ANCS roster gap.")
        self.copy_roster_gap_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        roster_header.addWidget(self.copy_roster_gap_btn)
        self.default_sort_btn = QPushButton("Default Sort")
        roster_header.addWidget(self.default_sort_btn)
        self.save_btn = QPushButton("Save Check-ins")
        roster_header.addWidget(self.save_btn)
        roster_layout.addLayout(roster_header)

        roster_scope_row = QHBoxLayout()
        self._roster_scope_layout = roster_scope_row
        roster_scope_row.addWidget(QLabel("Managed By:"))
        self.roster_scope_group = QButtonGroup(self)
        self.roster_scope_group.setExclusive(True)
        self.roster_scope_buttons: Dict[str, QToolButton] = {}
        for scope, label in (("NCS", "NCS"), ("ANCS", "ANCS"), ("SHARED", "Shared"), ("ALL", "All")):
            scope_btn = QToolButton()
            scope_btn.setText(label)
            scope_btn.setCheckable(True)
            scope_btn.setAutoRaise(False)
            scope_btn.setProperty("roster_action_scope", scope)
            scope_btn.setToolTip(f"Manage roster changes as {label}.")
            scope_btn.clicked.connect(lambda _checked=False, s=scope: self._set_roster_action_scope(s, user_selected=True))
            self.roster_scope_group.addButton(scope_btn)
            self.roster_scope_buttons[scope] = scope_btn
            roster_scope_row.addWidget(scope_btn)
        roster_scope_row.addStretch()
        roster_layout.addLayout(roster_scope_row)

        roster_actions = QVBoxLayout()
        roster_actions.setSpacing(6)
        roster_primary_actions = QHBoxLayout()
        self._roster_primary_actions_layout = roster_primary_actions
        roster_primary_actions.addWidget(QLabel("Actions:"))
        self.next_tfc_btn = QPushButton("Next TFC")
        self.copy_tfc_btn = QPushButton("TFC")
        self.copy_qru_btn = QPushButton("QRU")
        self.copy_late_btn = QPushButton("LATE")
        self.copy_seen_locally_btn = QPushButton("Copy Seen Locally")
        self.copy_roster_summary_btn = QPushButton("All Check-ins")
        self.copy_state_summary_btn = QPushButton("Copy Summary by State")
        self.relay_compare_btn = QPushButton("Find Relay Gaps")
        self.copy_relays_btn = QPushButton("Copy Relay List")
        self.copy_needs_sync_btn = QPushButton("ACK Needed")
        for primary_btn in (self.copy_needs_sync_btn, self.next_tfc_btn):
            primary_btn.setMinimumWidth(118)
            font = primary_btn.font()
            font.setBold(True)
            primary_btn.setFont(font)
        for btn in (
            self.copy_needs_sync_btn,
            self.next_tfc_btn,
        ):
            roster_primary_actions.addWidget(btn)
        roster_primary_actions.addSpacing(12)
        for btn in (
            self.copy_tfc_btn,
            self.copy_qru_btn,
            self.copy_late_btn,
            self.copy_roster_summary_btn,
        ):
            roster_primary_actions.addWidget(btn)
        roster_primary_actions.addStretch(1)
        self.relay_compare_btn.setVisible(False)
        self.copy_relays_btn.setVisible(False)
        roster_secondary_actions = QHBoxLayout()
        self._roster_secondary_actions_layout = roster_secondary_actions
        self.roster_action_status = QLabel("")
        self.roster_action_status.setWordWrap(True)
        self.roster_action_status.setMaximumWidth(520)
        self.roster_action_status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        roster_secondary_actions.addWidget(self.roster_action_status)
        roster_secondary_actions.addStretch(1)
        roster_actions.addLayout(roster_primary_actions)
        roster_actions.addLayout(roster_secondary_actions)
        roster_post_net_actions = QHBoxLayout()
        self._roster_post_net_actions_layout = roster_post_net_actions
        self.post_net_actions_label = QLabel("Post-net:")
        roster_post_net_actions.addWidget(self.post_net_actions_label)
        roster_post_net_actions.addWidget(self.copy_state_summary_btn)
        roster_post_net_actions.addStretch(1)
        roster_actions.addLayout(roster_post_net_actions)
        roster_layout.addLayout(roster_actions)

        self.roster_table = QTableWidget(0, 12)
        self.roster_table.setObjectName("fldigiRosterTable")
        self.roster_table.setHorizontalHeaderLabels(["#", "Directed By", "Acked By", "Callsign", "Name", "State", "Keyword", "Traffic", "TFC Status", "Category", "Notes", "Role"])
        self.roster_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.roster_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.roster_table.setAlternatingRowColors(True)
        self.roster_table.setWordWrap(False)
        self.roster_table.setSortingEnabled(False)
        self.roster_table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed | QTableWidget.AnyKeyPressed
        )
        roster_header_view = self.roster_table.horizontalHeader()
        roster_header_view.setStretchLastSection(False)
        roster_header_view.setSectionResizeMode(self.COL_SEQ, QHeaderView.ResizeToContents)
        roster_header_view.setSectionResizeMode(self.COL_HEARD, QHeaderView.ResizeToContents)
        roster_header_view.setSectionResizeMode(self.COL_ACKED, QHeaderView.ResizeToContents)
        roster_header_view.setSectionResizeMode(self.COL_CALLSIGN, QHeaderView.ResizeToContents)
        roster_header_view.setSectionResizeMode(self.COL_NAME, QHeaderView.Interactive)
        roster_header_view.setSectionResizeMode(self.COL_STATE, QHeaderView.ResizeToContents)
        roster_header_view.setSectionResizeMode(self.COL_KEYWORD, QHeaderView.Interactive)
        roster_header_view.setSectionResizeMode(self.COL_TRAFFIC, QHeaderView.Interactive)
        roster_header_view.setSectionResizeMode(self.COL_TFC_STATUS, QHeaderView.ResizeToContents)
        roster_header_view.setSectionResizeMode(self.COL_CATEGORY, QHeaderView.ResizeToContents)
        roster_header_view.setSectionResizeMode(self.COL_NOTES, QHeaderView.Stretch)
        roster_header_view.setSectionResizeMode(self.COL_ROLE, QHeaderView.ResizeToContents)
        roster_header_view.sectionClicked.connect(self._sort_roster_table_by_column)
        self.roster_table.setColumnWidth(self.COL_NAME, 150)
        self.roster_table.setColumnWidth(self.COL_KEYWORD, 140)
        self.roster_table.setColumnWidth(self.COL_TRAFFIC, 110)
        self.roster_table.setColumnWidth(self.COL_TFC_STATUS, 90)
        self.roster_table.setColumnWidth(self.COL_NOTES, 320)
        self.roster_table.setColumnHidden(self.COL_ROLE, True)
        self.roster_table.setStyleSheet(self._roster_table_style(resolve_theme(self.settings)))
        self.roster_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.roster_table.setMinimumHeight(0)
        self.roster_empty_label = QLabel(
            "No roster entries yet. Start a net, add an operator, or paste/import check-ins when traffic begins."
        )
        self.roster_empty_label.setObjectName("fldigiRosterEmptyState")
        self.roster_empty_label.setWordWrap(True)
        self.roster_empty_label.setVisible(False)
        roster_layout.addWidget(self.roster_empty_label)
        roster_layout.addWidget(self.roster_table)
        self._update_roster_empty_state()

        self.roster_compare_splitter.addWidget(self.roster_frame)

        # These cards remain as hidden compatibility buffers for derived file
        # output and legacy save/end-net code. The roster table is the sole
        # operator-facing local working surface.
        self.tfc_card = WorkspaceBucketCard(self.settings, title="TFC")
        self.qru_card = WorkspaceBucketCard(self.settings, title="QRU")
        self.late_card = WorkspaceBucketCard(self.settings, title="LATE")
        self.reference_card = WorkspaceBucketCard(
            self.settings,
            title="NCS List",
            allow_paste=True,
            copy_label="Compare",
            on_copy=self._run_inline_compare,
            secondary_label="Add Missing",
            on_secondary=self._import_reference_missing,
            on_paste=self._paste_into_reference_card,
        )
        self.compare_results_card = WorkspaceBucketCard(
            self.settings,
            title="Compare Results",
            read_only=True,
            copy_label="Copy Missing",
            on_copy=self._copy_compare_results,
            secondary_label="Merge Missing",
            on_secondary=self._merge_compare_missing,
        )
        self.review_card = WorkspaceBucketCard(
            self.settings,
            title="Review",
            allow_paste=True,
            on_paste=self._paste_into_review_card,
            secondary_label="Merge Reviewed",
            on_secondary=self._merge_review_candidates,
        )

        self._workspace_bucket_cards = {
            "tfc": self.tfc_card,
            "qru": self.qru_card,
            "late": self.late_card,
            "reference": self.reference_card,
            "compare_results": self.compare_results_card,
            "review": self.review_card,
        }

        self.tfc_card.setVisible(False)
        self.qru_card.setVisible(False)
        self.late_card.setVisible(False)

        self.compare_workspace_frame = QFrame()
        self.compare_workspace_frame.setFrameShape(QFrame.StyledPanel)
        self.compare_workspace_frame.setFrameShadow(QFrame.Raised)
        self.compare_workspace_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        compare_workspace_layout = QVBoxLayout(self.compare_workspace_frame)
        compare_workspace_layout.setContentsMargins(10, 8, 10, 8)
        compare_workspace_layout.setSpacing(6)

        compare_header = QHBoxLayout()
        compare_header.addWidget(QLabel("<b>Roster Compare</b>"))
        compare_header.addStretch()
        self.compare_workspace_toggle_btn = QToolButton()
        self.compare_workspace_toggle_btn.setText("Show")
        self.compare_workspace_toggle_btn.setCheckable(True)
        self.compare_workspace_toggle_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        compare_header.addWidget(self.compare_workspace_toggle_btn)
        compare_workspace_layout.addLayout(compare_header)

        self.compare_workspace_body = QWidget()
        self.compare_workspace_body.setMinimumHeight(0)
        compare_workspace_body_layout = QVBoxLayout(self.compare_workspace_body)
        compare_workspace_body_layout.setContentsMargins(0, 0, 0, 0)
        compare_workspace_body_layout.setSpacing(6)
        self.compare_workspace_tabs = QTabWidget()
        self.compare_workspace_tabs.setDocumentMode(True)
        self.compare_workspace_tabs.setMinimumHeight(0)
        self.compare_workspace_tabs.addTab(self.reference_card, "Reference")
        self.compare_workspace_tabs.addTab(self.compare_results_card, "Compare Results")
        self.compare_workspace_tabs.addTab(self.review_card, "Review")
        compare_workspace_body_layout.addWidget(self.compare_workspace_tabs)
        compare_workspace_layout.addWidget(self.compare_workspace_body)
        self.roster_compare_splitter.addWidget(self.compare_workspace_frame)
        self.roster_compare_splitter.setStretchFactor(0, 3)
        self.roster_compare_splitter.setStretchFactor(1, 2)
        self._set_compare_workspace_expanded(False)

        # Legacy aliases kept for existing save/start/merge behavior.
        self.main_text = self.tfc_card.text_edit
        self.late_text = self.late_card.text_edit
        self.qru_text = self.qru_card.text_edit
        self.reference_text = self.reference_card.text_edit
        self.compare_results_text = self.compare_results_card.text_edit

        # Button colors
        theme = resolve_theme(self.settings)
        self.start_btn.setStyleSheet(button_style("success", theme))
        self.end_btn.setStyleSheet(button_style("danger", theme))
        self._start_btn_default_style = self.start_btn.styleSheet()
        self._save_btn_default_style = button_style("success", theme)
        self._refresh_save_button_style()

        # Signals
        self.main_text.textChanged.connect(self._on_main_text_changed)
        self.late_text.textChanged.connect(self._on_late_text_changed)
        self.roster_table.itemChanged.connect(self._on_roster_item_changed)
        self.roster_table.model().dataChanged.connect(self._on_roster_model_data_changed)
        self.roster_table.model().rowsInserted.connect(self._on_roster_model_rows_changed)
        self.roster_table.model().rowsRemoved.connect(self._on_roster_model_rows_changed)
        self.known_op_edit.textChanged.connect(self._on_known_op_text_changed)
        self.known_op_edit.returnPressed.connect(self._on_known_op_return)
        self.known_op_edit.installEventFilter(self)
        self.copy_tfc_btn.clicked.connect(lambda: self._copy_roster_category("TFC"))
        self.copy_qru_btn.clicked.connect(lambda: self._copy_roster_category("QRU"))
        self.copy_late_btn.clicked.connect(lambda: self._copy_roster_category("LATE"))
        self.next_tfc_btn.clicked.connect(self._copy_next_tfc)
        self.copy_seen_locally_btn.clicked.connect(self._copy_roster_seen_locally)
        self.copy_roster_summary_btn.clicked.connect(self._copy_roster_summary)
        self.copy_state_summary_btn.clicked.connect(self._copy_state_summary)
        self.default_sort_btn.clicked.connect(self._restore_default_roster_sort)
        self.relay_compare_btn.clicked.connect(self._run_relay_compare)
        self.copy_relays_btn.clicked.connect(self._copy_selected_relays)
        self.roster_compare_status_btn.clicked.connect(self._show_roster_compare)
        self.copy_roster_gap_btn.clicked.connect(self._copy_roster_gap)
        self.copy_needs_sync_btn.clicked.connect(self._copy_needs_sync)
        self.setTabOrder(self.known_op_edit, self.add_known_tfc_btn)

        self.start_btn.clicked.connect(self._start_net)
        self.save_btn.clicked.connect(self._save_checkins)
        self.end_btn.clicked.connect(self._end_net)

        for button in self._known_add_buttons.values():
            button.setDefault(False)
            button.installEventFilter(self)
        self.add_known_tfc_btn.clicked.connect(lambda: self._insert_known_into_bucket("tfc"))
        self.add_known_qru_btn.clicked.connect(lambda: self._insert_known_into_bucket("qru"))
        self.add_known_late_btn.clicked.connect(lambda: self._insert_known_into_bucket("late"))
        self.add_known_seen_locally_btn.clicked.connect(lambda: self._insert_known_into_bucket("seen_locally"))
        self.macro_profile_combo.currentIndexChanged.connect(self._on_macro_profile_combo_changed)
        self.macro_profile_refresh_btn.clicked.connect(self._refresh_macro_profile_choices)
        self.macro_profile_edit.editingFinished.connect(self._on_macro_profile_editing_finished)
        self.macro_profile_browse_btn.clicked.connect(self._choose_macro_profile)
        self.macro_profile_map_btn.clicked.connect(self._open_macro_mapping_dialog)
        self.macro_profile_clear_btn.clicked.connect(self._clear_macro_profile)
        self.macro_profile_details_btn.clicked.connect(self._toggle_setup_details)
        self.compare_workspace_toggle_btn.clicked.connect(self._toggle_compare_workspace)
        self.role_combo.currentTextChanged.connect(self._on_role_changed)
        self.role_combo.currentTextChanged.connect(lambda _text: self._on_ncs_session_context_changed())
        self.net_name_combo.currentTextChanged.connect(lambda _text: self._on_ncs_session_context_changed())
        net_name_line_edit = self.net_name_combo.lineEdit()
        if net_name_line_edit is not None:
            net_name_line_edit.textChanged.connect(lambda _text: self._on_ncs_session_context_changed())
        self.partner_primary_btn.clicked.connect(self._set_partner_from_primary_controls)
        self.partner_primary_edit.returnPressed.connect(self._set_partner_from_primary_controls)
        self.joiner_add_btn.clicked.connect(self._add_joiner_net_control_rows)
        self.joiner_ncs_edit.returnPressed.connect(self._add_joiner_net_control_rows)
        self.joiner_ancs_edit.returnPressed.connect(self._add_joiner_net_control_rows)
        self.main_text.textChanged.connect(self._on_workspace_text_changed)
        self.late_text.textChanged.connect(self._on_workspace_text_changed)
        self.qru_text.textChanged.connect(self._on_workspace_text_changed)
        self.reference_text.textChanged.connect(self._on_workspace_text_changed)
        self.reference_text.textChanged.connect(self._update_roster_compare_status)
        self.compare_results_text.textChanged.connect(self._on_workspace_text_changed)
        self.review_card.text_edit.textChanged.connect(self._on_workspace_text_changed)

        self._apply_role_workspace(self.role_combo.currentText())
        self._refresh_partner_controls()
        self._refresh_ncs_session_context()
        self._ncs_scroll_area.setWidget(self._ncs_scroll_content)
        self._refresh_responsive_geometry()

    @staticmethod
    def _set_responsive_layout_direction(layout, compact: bool) -> None:
        if isinstance(layout, QBoxLayout):
            layout.setDirection(
                QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight
            )

    @staticmethod
    def _set_responsive_min_width(widget: QWidget, compact: bool) -> None:
        if widget.property("fldigiOriginalMinimumWidth") is None:
            widget.setProperty("fldigiOriginalMinimumWidth", int(widget.minimumWidth()))
        if compact:
            widget.setMinimumWidth(0)
        else:
            original = widget.property("fldigiOriginalMinimumWidth")
            widget.setMinimumWidth(max(0, int(original or 0)))

    def _refresh_responsive_geometry(self) -> None:
        """Reflow each control row independently of optional setup content."""
        if not hasattr(self, "_ncs_scroll_content"):
            return
        try:
            width = int(self._ncs_scroll_area.viewport().width() or self.width() or 0)
        except Exception:
            width = int(self.width() or 0)
        # Size hints can exceed an editor's explicit maximum (long net/macro
        # names). Count bounded widths and nested radio/tool rows, not the
        # currently stacked layout's geometry.
        def row_width(row) -> int:
            if row is None:
                return 0
            widths = []
            for index in range(row.count()):
                item = row.itemAt(index)
                widget = item.widget()
                if widget is not None:
                    if widget.isHidden():
                        continue
                    widths.append(min(widget.maximumWidth(), max(0, widget.sizeHint().width(), widget.minimumSizeHint().width())))
                elif item.layout() is not None:
                    widths.append(row_width(item.layout()))
                elif item.spacerItem() is not None:
                    widths.append(max(0, item.spacerItem().sizeHint().width()))
            margins = row.contentsMargins()
            return sum(widths) + max(0, row.spacing()) * max(0, len(widths) - 1) + margins.left() + margins.right()

        available = max(0, width - 32)
        for name in (
            "_session_context_row",
            "_session_context_details_row",
            "_session_partner_row",
            "_macro_summary_row",
            "_macro_path_row",
            "_known_operator_layout",
            "_known_buttons_layout",
            "_roster_header_layout",
            "_roster_scope_layout",
            "_roster_primary_actions_layout",
            "_roster_secondary_actions_layout",
            "_roster_post_net_actions_layout",
            "ncs_session_chip_layout",
        ):
            row = getattr(self, name, None)
            self._set_responsive_layout_direction(row, available < row_width(row))
        for child in self._ncs_scroll_content.findChildren(QWidget):
            self._set_responsive_min_width(child, width < 900)
        self._ncs_scroll_content.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_responsive_geometry_timer"):
            self._responsive_geometry_timer.start(0)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange):
            if hasattr(self, "_responsive_geometry_timer"):
                self._responsive_geometry_timer.start(0)

    def _on_ncs_session_context_changed(self) -> None:
        self._persist_ncs_session_snapshot()
        self._refresh_ncs_session_context()

    def _toggle_setup_details(self, *_args) -> None:
        self._set_setup_details_expanded(not self._setup_details_expanded)

    def _set_compare_workspace_expanded(self, expanded: bool) -> None:
        self._compare_workspace_expanded = bool(expanded)
        self.compare_workspace_body.setVisible(self._compare_workspace_expanded)
        self.compare_workspace_toggle_btn.setChecked(self._compare_workspace_expanded)
        self.compare_workspace_toggle_btn.setText("Hide" if self._compare_workspace_expanded else "Show")
        self.compare_workspace_toggle_btn.setArrowType(Qt.DownArrow if self._compare_workspace_expanded else Qt.RightArrow)
        if hasattr(self, "roster_compare_splitter"):
            sizes = [520, 360] if self._compare_workspace_expanded else [780, 60]
            QTimer.singleShot(0, lambda: self.roster_compare_splitter.setSizes(sizes))

    def _scroll_to_roster_compare(self) -> None:
        if not hasattr(self, "_ncs_scroll_area") or not hasattr(self, "compare_workspace_frame"):
            return

        def scroll() -> None:
            try:
                self._ncs_scroll_area.ensureWidgetVisible(self.compare_workspace_frame, 0, 16)
            except Exception as exc:
                log.debug("FLDigi NCS: failed to scroll to roster compare: %s", exc)

        QTimer.singleShot(0, scroll)
        QTimer.singleShot(75, scroll)

    def _toggle_compare_workspace(self, *_args) -> None:
        self._set_compare_workspace_expanded(not self._compare_workspace_expanded)

    def _set_compare_tab_visible(self, widget: QWidget, visible: bool) -> None:
        index = self.compare_workspace_tabs.indexOf(widget)
        if index < 0:
            return
        self.compare_workspace_tabs.setTabVisible(index, visible)
        if not visible and self.compare_workspace_tabs.currentIndex() == index:
            for candidate in (self.reference_card, self.compare_results_card, self.review_card):
                candidate_index = self.compare_workspace_tabs.indexOf(candidate)
                if candidate_index >= 0 and self.compare_workspace_tabs.isTabVisible(candidate_index):
                    self.compare_workspace_tabs.setCurrentIndex(candidate_index)
                    break

    def _sync_compare_workspace_tabs(self) -> None:
        self.compare_workspace_tabs.setTabText(self.compare_workspace_tabs.indexOf(self.reference_card), self.reference_card.title())
        self.compare_workspace_tabs.setTabText(self.compare_workspace_tabs.indexOf(self.compare_results_card), self.compare_results_card.title())
        self.compare_workspace_tabs.setTabText(self.compare_workspace_tabs.indexOf(self.review_card), self.review_card.title())
        self._set_compare_tab_visible(self.reference_card, not self.reference_card.isHidden())
        self._set_compare_tab_visible(self.compare_results_card, not self.compare_results_card.isHidden())
        self._set_compare_tab_visible(self.review_card, not self.review_card.isHidden())

    def _open_compare_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Compare Workspace Buckets")
        dialog.setMinimumWidth(760)
        layout = QVBoxLayout(dialog)

        prompt = QLabel("Compare any two buckets/lists. Role defaults are preselected but can be changed.")
        layout.addWidget(prompt)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Source bucket:"))
        source_combo = QComboBox()
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Compare against:"))
        target_combo = QComboBox()
        bucket_options = self._workspace_bucket_options()
        for bucket_id, label in bucket_options:
            source_combo.addItem(label, bucket_id)
            target_combo.addItem(label, bucket_id)
        defaults = self._workspace_compare_defaults()
        source_idx = source_combo.findData(defaults.get("source_bucket_id", ""))
        target_idx = target_combo.findData(defaults.get("target_bucket_id", ""))
        if source_idx >= 0:
            source_combo.setCurrentIndex(source_idx)
        if target_idx >= 0:
            target_combo.setCurrentIndex(target_idx)
        source_row.addWidget(source_combo, stretch=1)
        target_row.addWidget(target_combo, stretch=1)
        layout.addLayout(source_row)
        layout.addLayout(target_row)

        result_label = QLabel("Local entries missing from NCS list")
        layout.addWidget(result_label)

        result_box = QTextEdit()
        result_box.setReadOnly(True)
        layout.addWidget(result_box)

        btn_row = QHBoxLayout()
        compare_btn = QPushButton("Compare")
        copy_btn = QPushButton("Copy Missing")
        close_btn = QPushButton("Close")
        btn_row.addStretch()
        btn_row.addWidget(compare_btn)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        last_missing_text = ""
        theme = resolve_theme(self.settings)
        copy_btn.setStyleSheet(button_style("info", theme))

        def extract_callsigns(text: str) -> List[str]:
            seen = set()
            calls = []
            for line in (text or "").splitlines():
                cs, _, _ = self._parse_checkin_line(line)
                cs = (cs or "").strip().upper()
                if not cs or cs in seen:
                    continue
                seen.add(cs)
                calls.append(cs)
            return calls

        def extract_local_entries(text: str) -> List[Dict[str, str]]:
            entries: List[Dict[str, str]] = []
            seen = set()
            for line in (text or "").splitlines():
                cs, name, state = self._parse_checkin_line(line)
                cs = (cs or "").strip().upper()
                if not cs or cs in seen:
                    continue
                seen.add(cs)
                entry = {
                    "callsign": cs,
                    "name": (name or "").strip(),
                    "state": (state or "").strip().upper(),
                }
                entries.append(entry)
            return entries

        def run_compare() -> None:
            source_bucket = str(source_combo.currentData() or "")
            target_bucket = str(target_combo.currentData() or "")
            source_text = self._workspace_bucket_text(source_bucket)
            target_text = self._workspace_bucket_text(target_bucket)
            local_entries = extract_local_entries(source_text)
            pasted_calls = set(extract_callsigns(target_text))
            missing_entries = [
                self._format_entry(e["callsign"], e["name"], e["state"])
                for e in local_entries
                if e["callsign"] not in pasted_calls
            ]
            nonlocal last_missing_text
            last_missing_text = "\n".join(missing_entries) if missing_entries else ""

            lines = [
                f"Source bucket ({source_bucket}): {len(local_entries)}",
                f"Compare bucket ({target_bucket}): {len(pasted_calls)}",
                "",
                "Local entries missing from NCS list:",
                last_missing_text if missing_entries else "(none)",
            ]
            result_box.setPlainText("\n".join(lines))
            if not missing_entries:
                msg = QMessageBox(dialog)
                msg.setWindowTitle("Compare NCS Lists")
                msg.setText("Full Match")
                msg.addButton("Close", QMessageBox.AcceptRole)
                msg.exec()
                dialog.accept()

        compare_btn.clicked.connect(run_compare)
        def copy_and_close() -> None:
            if not last_missing_text:
                QMessageBox.information(dialog, "Compare Workspace Buckets", "No missing entries to copy.")
                return
            QApplication.clipboard().setText(last_missing_text)
            dialog.accept()

        copy_btn.clicked.connect(copy_and_close)
        close_btn.clicked.connect(dialog.accept)

        dialog.exec()

    def _workspace_bucket_options(self) -> List[tuple[str, str]]:
        role = normalize_role(self.role_combo.currentText())
        preset = get_role_workspace_preset(role)
        options: List[tuple[str, str]] = []
        visible_ids = set(self._workspace_visible_bucket_ids)
        visible_ids.add("roster")
        visible_ids.update({preset.compare_source_bucket_id, preset.compare_target_bucket_id})
        options.append(("roster", "Net Roster"))
        for bucket in preset.all_buckets():
            if bucket.bucket_id in visible_ids or bucket.visible_by_default:
                card = self._workspace_bucket_cards.get(bucket.bucket_id)
                options.append((bucket.bucket_id, card.title() if card is not None else bucket.title))
        review_card = self._workspace_bucket_cards.get("review")
        if review_card is not None and review_card.isVisible():
            options.append(("review", review_card.title()))
        for bucket_id, card in self._custom_bucket_cards.items():
            if bucket_id in self._workspace_visible_bucket_ids and card.isVisible():
                options.append((bucket_id, card.title()))
        if not options:
            options = [("tfc", "TFC"), ("late", "LATE")]
        return options

    def _workspace_compare_defaults(self) -> Dict[str, str]:
        defaults = role_compare_defaults(self.role_combo.currentText(), self._role_workspace_prefs)
        defaults["source_bucket_id"] = "seen_locally" if normalize_role(self.role_combo.currentText()) == "JOINER" else "roster"
        return defaults

    def _workspace_bucket_text(self, bucket_id: str) -> str:
        if bucket_id in {"roster", "local_roster"}:
            return self._roster_table_text()
        if bucket_id in {"reference", "ncs_reference", "ancs_reference"}:
            return self.reference_text.toPlainText()
        if bucket_id == "compare_results":
            return self.compare_results_text.toPlainText()
        if bucket_id == "review":
            return self.review_card.text()
        if bucket_id == "seen_locally":
            return self.main_text.toPlainText()
        bucket = self._workspace_bucket_cards.get(bucket_id)
        return bucket.text() if bucket else ""

    def _roster_table_rows(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        if not hasattr(self, "roster_table"):
            return rows
        for row in range(self.roster_table.rowCount()):
            seq_item = self.roster_table.item(row, self.COL_SEQ)
            callsign_item = self.roster_table.item(row, self.COL_CALLSIGN)
            name_item = self.roster_table.item(row, self.COL_NAME)
            state_item = self.roster_table.item(row, self.COL_STATE)
            keyword_item = self.roster_table.item(row, self.COL_KEYWORD)
            traffic_item = self.roster_table.item(row, self.COL_TRAFFIC)
            notes_item = self.roster_table.item(row, self.COL_NOTES)
            role_item = self.roster_table.item(row, self.COL_ROLE)
            category_widget = self.roster_table.cellWidget(row, self.COL_CATEGORY)
            category = ""
            if category_widget is not None and hasattr(category_widget, "currentText"):
                category = str(category_widget.currentText() or "").strip().upper()
            elif self.roster_table.item(row, self.COL_CATEGORY) is not None:
                category = str(self.roster_table.item(row, self.COL_CATEGORY).text() or "").strip().upper()
            heard_by = self._roster_side_value(row, self.COL_HEARD)
            acked_by = self._roster_side_value(row, self.COL_ACKED)
            station_role = (role_item.text() if role_item else "").strip().upper()
            seq_text = (seq_item.text() if seq_item else "").strip()
            rows.append({
                "checkin_seq": seq_text,
                "callsign": (callsign_item.text() if callsign_item else "").strip().upper(),
                "name": (name_item.text() if name_item else "").strip(),
                "state": (state_item.text() if state_item else "").strip().upper(),
                "keyword": (keyword_item.text() if keyword_item else "").strip(),
                "traffic": (traffic_item.text() if traffic_item else "").strip(),
                "category": category or "TFC",
                "notes": (notes_item.text() if notes_item else "").strip(),
                "heard_by": self._roster_normalize_side(heard_by),
                "acked_by": self._roster_normalize_side(acked_by),
                "station_role": station_role,
                "source": station_role,
            })
        return rows

    def _roster_table_text(self, category: Optional[str] = None) -> str:
        wanted = (category or "").strip().upper()
        lines: List[str] = []
        for row in self._ordered_roster_rows():
            if wanted and row["category"].upper() != wanted:
                continue
            formatted = self._format_roster_row_for_copy(row)
            if formatted.strip():
                lines.append(formatted)
        return "\n".join(lines)

    def _roster_table_text_for_rows(self, rows: List[Dict[str, str]]) -> str:
        lines: List[str] = []
        for row in self._ordered_roster_rows(rows):
            formatted = self._format_roster_row_for_copy(row)
            if formatted.strip():
                lines.append(formatted)
        return "\n".join(lines)

    def _roster_clipboard_text(self, text: str) -> str:
        body = (text or "").strip()
        return f"\n{body}\n" if body else ""

    def _show_roster_action_status(self, message: str, level: str = "success", timeout_ms: int = 5000) -> None:
        if not hasattr(self, "roster_action_status"):
            return
        text = (message or "").strip()
        self.roster_action_status.setText(text)
        theme = resolve_theme(self.settings)
        if not text:
            self.roster_action_status.setStyleSheet("")
            return
        if level == "warning":
            bg = theme.get("warning_bg", "#FFF3CD")
            fg = theme.get("warning_text", "#6B4F00")
            border = theme.get("warning", "#D39E00")
        elif level == "info":
            bg = theme.get("surface_alt", "#DDE1E6")
            fg = theme.get("text", "#1C1F21")
            border = theme.get("accent", "#2E6F9E")
        else:
            bg = theme.get("success", "#2E7D32")
            fg = contrast_text_for_background(bg, theme)
            border = bg
        self.roster_action_status.setStyleSheet(
            "QLabel {"
            f" background-color: {bg}; color: {fg}; border: 1px solid {border};"
            " border-radius: 5px; padding: 3px 8px; font-weight: 600;"
            " }"
        )
        if timeout_ms > 0:
            QTimer.singleShot(timeout_ms, lambda expected=text: self._clear_roster_action_status(expected))

    def _clear_roster_action_status(self, expected: str = "") -> None:
        if not hasattr(self, "roster_action_status"):
            return
        if expected and self.roster_action_status.text() != expected:
            return
        self.roster_action_status.setText("")
        self.roster_action_status.setStyleSheet("")

    def _roster_side_has_role(self, value: object, role: str) -> bool:
        normalized = self._roster_normalize_side(value)
        role_key = self._exact_net_control_role(role)
        if role_key not in {"NCS", "ANCS"}:
            return False
        return normalized == "Both" or normalized.upper() == role_key

    def _roster_directed_to_current_role(self, row: Dict[str, str], role: str = "") -> bool:
        role_key = self._exact_net_control_role(role) or self._current_net_control_role()
        return self._roster_side_has_role(row.get("heard_by", ""), role_key)

    def _roster_acked_by_current_role(self, row: Dict[str, str], role: str = "") -> bool:
        role_key = self._exact_net_control_role(role) or self._current_net_control_role()
        return self._roster_side_has_role(row.get("acked_by", ""), role_key)

    def _is_current_role_leadership_row(self, row: Dict[str, str], role: str = "") -> bool:
        role_key = self._exact_net_control_role(role) or self._current_net_control_role()
        return bool(role_key and self._roster_station_role(row) == role_key)

    def _role_filtered_category_rows(self, category: str, role: str = "") -> List[Dict[str, str]]:
        role_key = self._exact_net_control_role(role) or self._current_net_control_role()
        wanted = (category or "").strip().upper()
        if role_key not in {"NCS", "ANCS"} or wanted not in {"TFC", "QRU", "LATE"}:
            return []
        rows: List[Dict[str, str]] = []
        for row in self._roster_table_rows():
            if str(row.get("category") or "").strip().upper() != wanted:
                continue
            if not self._roster_directed_to_current_role(row, role_key):
                continue
            rows.append(row)
        return self._ordered_roster_rows(rows)

    def _roster_row_matches_action_scope(self, row: Dict[str, str], scope: str) -> bool:
        scope_key = str(scope or "").strip().upper()
        directed = self._roster_normalize_side(row.get("heard_by", ""))
        if scope_key == "ALL":
            return True
        if scope_key == "SHARED":
            return directed == "Both"
        if scope_key in {"NCS", "ANCS"}:
            return self._roster_side_has_role(directed, scope_key)
        return False

    def _scope_filtered_rows(self, scope: str = "", category: str = "") -> List[Dict[str, str]]:
        scope_key = str(scope or self._current_roster_action_scope()).strip().upper()
        wanted = str(category or "").strip().upper()
        rows: List[Dict[str, str]] = []
        for row in self._roster_table_rows():
            if wanted and str(row.get("category") or "").strip().upper() != wanted:
                continue
            if not self._roster_row_matches_action_scope(row, scope_key):
                continue
            rows.append(row)
        return self._ordered_roster_rows(rows)

    def _traffic_metadata(self, text: object) -> tuple[int, str, str]:
        raw = str(text or "").strip()
        upper = raw.upper()
        if not raw or re.search(r"\b(QRU|NO\s+TFC|NO\s+TRAFFIC)\b", upper):
            return 0, "", "QRU"
        match = re.search(r"\b([1-9]\d*)?\s*(PP|RR)\b", upper)
        if not match:
            return 0, "", "TFC"
        count = int(match.group(1) or "1")
        priority = match.group(2).upper()
        return count, priority, "TFC"

    def _normalize_traffic_text(self, text: object) -> tuple[str, str]:
        raw = str(text or "").strip()
        count, priority, category = self._traffic_metadata(raw)
        if category == "QRU":
            return "No TFC", "QRU"
        if priority:
            return f"{count}{priority}", "TFC"
        return raw, "TFC"

    def _split_keyword_and_traffic(self, text: object) -> tuple[str, str, str]:
        raw = str(text or "").strip()
        if not raw:
            return "", "", "TFC"
        slash_parts = [p.strip() for p in raw.split("/") if p.strip()]
        if len(slash_parts) >= 2:
            traffic, category = self._normalize_traffic_text(slash_parts[-1])
            if traffic and (category == "QRU" or self._traffic_metadata(traffic)[1]):
                return " / ".join(slash_parts[:-1]).strip(), traffic, category
        traffic_match = re.search(r"\b([1-9]\d*)?\s*(PP|RR)\b", raw, re.IGNORECASE)
        if traffic_match:
            traffic_raw = traffic_match.group(0)
            traffic, category = self._normalize_traffic_text(traffic_raw)
            keyword = (raw[: traffic_match.start()] + raw[traffic_match.end() :]).strip(" /")
            return keyword.strip(), traffic, category
        if re.search(r"\b(QRU|NO\s+TFC|NO\s+TRAFFIC)\b", raw, re.IGNORECASE):
            return "", "No TFC", "QRU"
        return raw, "", "TFC"

    def _roster_operational_order_key(self, row: Dict[str, str], fallback_index: int = 0) -> tuple:
        role = self._roster_station_role(row)
        if role == "NCS":
            group = 0
        elif role == "ANCS":
            group = 1
        else:
            _count, priority, category = self._traffic_metadata(row.get("traffic", ""))
            if priority == "PP":
                group = 2
            elif priority == "RR":
                group = 3
            elif str(row.get("category") or "").strip().upper() == "TFC" and category != "QRU":
                group = 4
            else:
                group = 5
        seq = row.get("checkin_seq", "")
        try:
            seq_val = int(seq)
        except Exception:
            seq_val = 999999 + fallback_index
        return (group, seq_val, str(row.get("callsign") or ""), fallback_index)

    def _ordered_roster_rows(self, rows: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, str]]:
        source_rows = list(self._roster_table_rows() if rows is None else rows)
        return [
            row
            for _key, row in sorted(
                ((self._roster_operational_order_key(row, idx), row) for idx, row in enumerate(source_rows)),
                key=lambda item: item[0],
            )
        ]

    def _format_roster_row_for_copy(self, row: Dict[str, str]) -> str:
        formatted = self._format_entry(row.get("callsign", ""), row.get("name", ""), row.get("state", ""))
        role = self._roster_station_role(row)
        keyword = str(row.get("keyword") or "").strip()
        traffic = str(row.get("traffic") or "").strip()
        if role:
            formatted = f"{formatted} / {role}" if formatted else role
        if keyword:
            formatted = f"{formatted} / {keyword}" if formatted else keyword
        if traffic:
            formatted = f"{formatted} / {traffic}" if formatted else traffic
        return formatted.strip()

    def _roster_role_for_source(self, source: object) -> str:
        text = str(source or "").strip().upper()
        if text.startswith("NCS"):
            return "NCS"
        if text.startswith("ANCS"):
            return "ANCS"
        return ""

    def _roster_station_role(self, row: Dict[str, str]) -> str:
        role = str(row.get("station_role") or "").strip().upper()
        if role in {"NCS", "ANCS"}:
            return role
        return self._roster_role_for_source(row.get("source"))

    def _roster_normalize_side(self, value: object) -> str:
        text = str(value or "").strip().upper()
        if not text:
            return ""
        if text == "BOTH":
            return "Both"
        tokens = set(re.findall(r"ANCS|NCS", text))
        if "ANCS" in tokens and "NCS" in tokens:
            return "Both"
        if "ANCS" in tokens:
            return "ANCS"
        if "NCS" in tokens:
            return "NCS"
        return ""

    def _roster_promote_side(self, current: object, role: str) -> str:
        normalized = self._roster_normalize_side(current)
        incoming = self._roster_normalize_side(role)
        if incoming == "Both":
            return "Both"
        role_key = self._exact_net_control_role(role)
        if role_key not in {"NCS", "ANCS"}:
            role_key = incoming.upper() if incoming in {"NCS", "ANCS"} else ""
        if role_key not in {"NCS", "ANCS"}:
            return normalized
        if not normalized:
            return role_key
        if normalized.upper() == role_key:
            return normalized
        return "Both"

    def _exact_net_control_role(self, value: object) -> str:
        text = str(value or "").strip().upper()
        return text if text in {"NCS", "ANCS"} else ""

    def _current_net_control_role(self) -> str:
        role = normalize_role(self.role_combo.currentText()) if hasattr(self, "role_combo") else ""
        return role if role in {"NCS", "ANCS"} else ""

    def _current_roster_action_scope(self) -> str:
        scope = str(getattr(self, "_roster_action_scope", "") or "").strip().upper()
        return scope if scope in ACTION_SCOPES else (self._current_net_control_role() or "ALL")

    def _set_roster_action_scope(self, scope: str, *, user_selected: bool = False) -> None:
        scope_key = str(scope or "").strip().upper()
        if scope_key not in ACTION_SCOPES:
            scope_key = self._current_net_control_role() or "ALL"
        self._roster_action_scope = scope_key
        if user_selected:
            self._roster_action_scope_user_selected = True
            receiver = getattr(self, '_prepared_receiver', None)
            if receiver is not None:
                receiver.edited()
        if hasattr(self, "roster_scope_buttons"):
            for key, button in self.roster_scope_buttons.items():
                button.blockSignals(True)
                try:
                    button.setChecked(key == scope_key)
                finally:
                    button.blockSignals(False)
        self._refresh_roster_action_scope_styles()

    def _sync_roster_action_scope_to_role(self, *, force: bool = False) -> None:
        role = self._current_net_control_role()
        if role not in {"NCS", "ANCS"}:
            return
        if force or not getattr(self, "_roster_action_scope_user_selected", False):
            self._set_roster_action_scope(role, user_selected=False)

    def _roster_action_scope_label(self, scope: str = "") -> str:
        scope_key = str(scope or self._current_roster_action_scope()).strip().upper()
        if scope_key == "NCS":
            return f"NCS {self._ncs_partner_call}" if self._ncs_partner_call else "NCS"
        if scope_key == "ANCS":
            return f"ANCS {self._ancs_partner_call}" if self._ancs_partner_call else "ANCS"
        return {"SHARED": "Shared", "ALL": "All"}.get(scope_key, "All")

    def _refresh_roster_action_scope_styles(self) -> None:
        if not hasattr(self, "roster_scope_buttons"):
            return
        theme = resolve_theme(self.settings)
        surface = theme.get("surface", "#F0F2F4")
        surface_alt = theme.get("surface_alt", "#DDE1E6")
        border = theme.get("border", "#D3D7DD")
        text = theme.get("text", "#1C1F21")
        muted = theme.get("text_muted", "#5B6570")
        accent = theme.get("accent", "#2E6F9E")
        accent_active = theme.get("accent_active", accent)
        selected = self._current_roster_action_scope()
        for scope, button in self.roster_scope_buttons.items():
            label = self._roster_action_scope_label(scope)
            button.setText(label)
            button.setToolTip(f"Manage roster changes as {label}.")
            checked_bg = accent if scope in {"NCS", "ANCS"} else surface_alt
            checked_border = accent_active if scope in {"NCS", "ANCS"} else accent
            checked_text = contrast_text_for_background(checked_bg, theme)
            button.setStyleSheet(
                "QToolButton {"
                f" background-color: {surface}; color: {muted}; border: 1px solid {border};"
                " border-radius: 5px; padding: 3px 8px; font-weight: 700;"
                " }"
                " QToolButton:hover {"
                f" background-color: {surface_alt}; color: {text};"
                " }"
                " QToolButton:checked {"
                f" background-color: {checked_bg}; color: {checked_text}; border: 2px solid {checked_border};"
                " padding: 2px 7px;"
                " }"
            )
            button.setChecked(scope == selected)

    def _attendance_rows_for_state_summary(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        seen_callsigns: set[str] = set()
        for row in self._roster_table_rows():
            callsign = str(row.get("callsign") or "").strip().upper()
            if not callsign or callsign in seen_callsigns:
                continue
            if self._roster_station_role(row) in {"NCS", "ANCS"}:
                continue
            seen_callsigns.add(callsign)
            rows.append(row)
        return rows

    def _state_summary_text(self) -> str:
        rows = self._attendance_rows_for_state_summary()
        if not rows:
            return ""
        counts: Dict[str, int] = {}
        for row in rows:
            state = str(row.get("state") or "").strip().upper() or "Unknown"
            counts[state] = counts.get(state, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (item[0] == "Unknown", item[0]))
        parts = [f"{state}: {count}" for state, count in ordered]
        net_name = self.net_name_combo.currentText().strip() if hasattr(self, "net_name_combo") else ""
        prefix = f"{net_name} " if net_name else ""
        return f"{prefix}Check-ins by state/province: {', '.join(parts)}. Total: {len(rows)}."

    def _roster_table_style(self, theme: Dict[str, str]) -> str:
        surface = theme.get("surface", "#F0F2F4")
        surface_alt = theme.get("surface_alt", "#DDE1E6")
        text = theme.get("text", "#1C1F21")
        border = theme.get("border", "#D3D7DD")
        selected_bg = theme.get("accent_active") or theme.get("accent", "#1F5A83")
        selected_fg = contrast_text_for_background(selected_bg, theme)
        return (
            "QTableWidget#fldigiRosterTable {"
            f" background-color: {surface}; color: {text}; border: 1px solid {border};"
            f" gridline-color: {border};"
            " selection-background-color: "
            f"{selected_bg}; selection-color: {selected_fg};"
            "}"
            "QTableWidget#fldigiRosterTable::item {"
            " padding: 3px 5px;"
            "}"
            "QTableWidget#fldigiRosterTable::item:selected {"
            f" background-color: {selected_bg}; color: {selected_fg};"
            "}"
            "QTableWidget#fldigiRosterTable::item:alternate {"
            f" background-color: {surface_alt};"
            "}"
            "QTableWidget#fldigiRosterTable::item:selected:active,"
            "QTableWidget#fldigiRosterTable::item:selected:!active {"
            f" background-color: {selected_bg}; color: {selected_fg};"
            "}"
        )

    def _default_heard_by_for_source(self, source: object, station_role: str = "") -> str:
        source_role = self._roster_role_for_source(source)
        if source_role:
            return source_role
        role_key = self._exact_net_control_role(station_role)
        if role_key in {"NCS", "ANCS"}:
            return role_key
        return self._current_net_control_role()

    def _roster_role_rank(self, row: Dict[str, str], index: int) -> tuple:
        return self._roster_operational_order_key(row, index)

    def _roster_merge_duplicate_rows(self, rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
        merged_by_call: Dict[str, Dict[str, str]] = {}
        order: List[str] = []

        def completeness(entry: Dict[str, str]) -> int:
            return sum(1 for key in ("name", "state", "keyword", "traffic", "notes") if str(entry.get(key) or "").strip())

        def role_rank(entry: Dict[str, str]) -> int:
            role = self._roster_station_role(entry)
            if role == "NCS":
                return 3
            if role == "ANCS":
                return 2
            return 0

        for row in rows:
            cs = str(row.get("callsign") or "").strip().upper()
            if not cs:
                continue
            normalized = dict(row)
            normalized["callsign"] = cs
            if cs not in merged_by_call:
                merged_by_call[cs] = normalized
                order.append(cs)
                continue
            current = merged_by_call[cs]
            for key in ("name", "state", "keyword", "traffic", "notes"):
                if not str(current.get(key) or "").strip() and str(normalized.get(key) or "").strip():
                    current[key] = normalized.get(key, "")
            if completeness(normalized) > completeness(current):
                for key in ("name", "state", "keyword", "traffic", "notes"):
                    if str(normalized.get(key) or "").strip():
                        current[key] = normalized.get(key, "")
            if not str(current.get("checkin_seq") or "").strip() and str(normalized.get("checkin_seq") or "").strip():
                current["checkin_seq"] = normalized.get("checkin_seq", "")
            for key in ("heard_by", "acked_by"):
                if normalized.get(key):
                    current[key] = self._roster_promote_side(current.get(key), normalized.get(key, ""))
            if role_rank(normalized) >= role_rank(current):
                if str(normalized.get("station_role") or "").strip():
                    current["station_role"] = normalized.get("station_role", "")
                    current["source"] = normalized.get("station_role", "")
            if current.get("category") == "TFC" and normalized.get("category") in {"QRU", "LATE"} and not current.get("traffic"):
                current["category"] = normalized.get("category", "TFC")
        return [merged_by_call[cs] for cs in order]

    def _roster_deduplicate_callsigns(self) -> bool:
        if self._roster_syncing or not hasattr(self, "roster_table"):
            return False
        rows = self._roster_table_rows()
        merged = self._roster_merge_duplicate_rows(rows)
        if len(merged) == len(rows) and merged == rows:
            return False
        self._roster_syncing = True
        try:
            was_sorting = self.roster_table.isSortingEnabled()
            self.roster_table.setSortingEnabled(False)
            self.roster_table.setRowCount(0)
            for entry in merged:
                row = self.roster_table.rowCount()
                self.roster_table.insertRow(row)
                self._roster_init_table_row(row)
                self._roster_set_row(
                    row,
                    entry.get("callsign", ""),
                    entry.get("name", ""),
                    entry.get("state", ""),
                    entry.get("traffic", ""),
                    entry.get("category", "TFC"),
                    entry.get("source", ""),
                    entry.get("heard_by", ""),
                    entry.get("acked_by", ""),
                    entry.get("notes", ""),
                    entry.get("station_role", ""),
                    keyword=entry.get("keyword", ""),
                    checkin_seq=entry.get("checkin_seq", ""),
                )
            self.roster_table.setSortingEnabled(was_sorting)
        finally:
            self._roster_syncing = False
            self._update_roster_empty_state()
        return True

    def _roster_rebuild_rows(self, rows: List[Dict[str, str]]) -> None:
        self.roster_table.setRowCount(0)
        for entry in rows:
            row = self.roster_table.rowCount()
            self.roster_table.insertRow(row)
            self._roster_init_table_row(row)
            self._roster_set_row(
                row,
                entry.get("callsign", ""),
                entry.get("name", ""),
                entry.get("state", ""),
                entry.get("traffic", ""),
                entry.get("category", "TFC"),
                entry.get("source", ""),
                entry.get("heard_by", ""),
                entry.get("acked_by", ""),
                entry.get("notes", ""),
                entry.get("station_role", ""),
                keyword=entry.get("keyword", ""),
                checkin_seq=entry.get("checkin_seq", ""),
            )
        self._update_roster_empty_state()

    def _sort_roster_table_by_column(self, column: int) -> None:
        if self._roster_syncing or not hasattr(self, "roster_table"):
            return
        rows = self._roster_table_rows()
        if len(rows) < 2:
            return
        previous_column = getattr(self, "_roster_last_sort_column", None)
        ascending = not getattr(self, "_roster_last_sort_ascending", True) if previous_column == column else True
        self._roster_last_sort_column = column
        self._roster_last_sort_ascending = ascending

        def seq_value(row: Dict[str, str], fallback: int) -> int:
            try:
                return int(row.get("checkin_seq", ""))
            except Exception:
                return 999999 + fallback

        def text_value(row: Dict[str, str], key: str) -> str:
            return str(row.get(key) or "").strip().upper()

        key_map = {
            self.COL_CALLSIGN: "callsign",
            self.COL_NAME: "name",
            self.COL_STATE: "state",
            self.COL_KEYWORD: "keyword",
            self.COL_TRAFFIC: "traffic",
            self.COL_CATEGORY: "category",
        }
        control_rows = [
            (self._roster_operational_order_key(row, idx), row)
            for idx, row in enumerate(rows)
            if self._roster_station_role(row) in {"NCS", "ANCS"}
        ]
        station_rows = [
            (idx, row)
            for idx, row in enumerate(rows)
            if self._roster_station_role(row) not in {"NCS", "ANCS"}
        ]
        pinned_rows = [row for _key, row in sorted(control_rows, key=lambda item: item[0])]
        if column == self.COL_SEQ:
            keyed = sorted(((seq_value(row, idx), idx, row) for idx, row in station_rows), reverse=not ascending)
        elif column in key_map:
            keyed = sorted(((text_value(row, key_map[column]), seq_value(row, idx), row) for idx, row in station_rows), reverse=not ascending)
        else:
            keyed = sorted(((self._roster_operational_order_key(row, idx), row) for idx, row in station_rows), key=lambda item: item[0], reverse=not ascending)
            sorted_rows = pinned_rows + [row for _key, row in keyed]
            self._roster_syncing = True
            try:
                self._roster_rebuild_rows(sorted_rows)
            finally:
                self._roster_syncing = False
            return
        sorted_rows = pinned_rows + [item[-1] for item in keyed]
        self._roster_syncing = True
        try:
            self._roster_rebuild_rows(sorted_rows)
        finally:
            self._roster_syncing = False

    def _restore_default_roster_sort(self) -> None:
        if self._roster_syncing or not hasattr(self, "roster_table"):
            return
        rows = self._roster_table_rows()
        if len(rows) < 2:
            self._show_roster_action_status("Roster already in default order.", "info")
            return
        ordered = self._ordered_roster_rows(rows)
        self._roster_last_sort_column = None
        self._roster_last_sort_ascending = True
        if ordered == rows:
            self._show_roster_action_status("Roster already in default order.", "info")
            return
        self._roster_syncing = True
        try:
            was_sorting = self.roster_table.isSortingEnabled()
            self.roster_table.setSortingEnabled(False)
            self._roster_rebuild_rows(ordered)
            self.roster_table.setSortingEnabled(was_sorting)
        finally:
            self._roster_syncing = False
        self._show_roster_action_status("Default roster sort restored.", "info")

    def _roster_apply_pinned_order(self) -> None:
        if self._roster_syncing or not hasattr(self, "roster_table"):
            return
        rows = self._roster_table_rows()
        if len(rows) < 2:
            return
        ordered = self._ordered_roster_rows(rows)
        if ordered == rows:
            return
        self._roster_syncing = True
        try:
            was_sorting = self.roster_table.isSortingEnabled()
            self.roster_table.setSortingEnabled(False)
            self._roster_rebuild_rows(ordered)
            self.roster_table.setSortingEnabled(was_sorting)
        finally:
            self._roster_syncing = False

    def _roster_unique_callsigns(self) -> List[str]:
        seen = set()
        callsigns: List[str] = []
        for row in self._roster_table_rows():
            cs = row["callsign"]
            if not cs or cs in seen:
                continue
            seen.add(cs)
            callsigns.append(cs)
        return callsigns

    def _roster_sync_legacy_buffers(self, *, write_files: bool = True) -> None:
        if self._roster_syncing:
            return
        self._roster_syncing = True
        try:
            main_text = self._roster_table_text("TFC")
            qru_text = self._roster_table_text("QRU")
            late_text = self._roster_table_text("LATE")
            self.main_text.setPlainText(main_text)
            self.qru_text.setPlainText(qru_text)
            self.late_text.setPlainText(late_text)
            self._refresh_tfc_status_cells()
            self._update_bucket_card_states()
            if write_files and self._net_in_progress:
                main_path, qru_path, late_path = self._checkin_file_paths()
                self._write_file(main_path, main_text)
                self._write_file(qru_path, qru_text)
                self._write_file(late_path, late_text)
                self._write_file(self._all_checkins_file_path(), self._roster_table_text())
                self._sync_role_roster_files()
                self._sync_role_ack_pending_files()
                self._sync_next_tfc_action_files()
                self._sync_mapped_roster_files()
        finally:
            self._roster_syncing = False

    def _role_roster_text(self, role: str, category: str = "") -> str:
        role_key = self._exact_net_control_role(role)
        if role_key not in {"NCS", "ANCS"}:
            return ""
        return self._roster_table_text_for_rows(self._scope_filtered_rows(role_key, category))

    def _sync_role_roster_files(self) -> None:
        for role in ("NCS", "ANCS"):
            for key in ("TFC", "QRU", "LATE"):
                self._write_file(self._role_checkin_file_path(role, key), self._role_roster_text(role, key))
            self._write_file(self._role_checkin_file_path(role, "ALL"), self._role_roster_text(role))

    def _sync_role_ack_pending_files(self) -> None:
        for role in ("NCS", "ANCS"):
            text = self._needs_ack_text(role)
            self._write_file(self._role_ack_pending_file_path(role), self._roster_clipboard_text(text) if text else "")

    def _sync_next_tfc_action_files(self) -> None:
        for role in ("NCS", "ANCS"):
            last = str(self._next_tfc_last_served.get(role, "") or "").strip().upper()
            eligible = {
                str(row.get("callsign") or "").strip().upper()
                for row in self._next_tfc_eligible_rows(role)
            }
            if last and last in eligible:
                self._write_file(self._role_next_tfc_file_path(role), self._next_tfc_payload(last))
                continue
            if last and last not in eligible:
                self._next_tfc_last_served[role] = ""
            self._write_file(self._role_next_tfc_file_path(role), "")

    def _reference_source_label(self) -> str:
        role = normalize_role(self.role_combo.currentText())
        if role == "NCS":
            return "ANCS"
        return "NCS"

    def _extract_unique_entries(self, text: str) -> List[Dict[str, str]]:
        entries: List[Dict[str, str]] = []
        seen = set()
        for line in (text or "").splitlines():
            cs, name, state, extra = self._checkin_line_details(line)
            cs = (cs or "").strip().upper()
            if not cs or cs in seen:
                continue
            seen.add(cs)
            keyword, traffic, category = self._split_keyword_and_traffic(extra)
            entries.append({
                "callsign": cs,
                "name": (name or "").strip(),
                "state": (state or "").strip().upper(),
                "keyword": keyword,
                "traffic": traffic,
                "category": category,
            })
        return entries

    def _reference_entries_missing_from_roster(self) -> List[Dict[str, str]]:
        roster_calls = {row["callsign"] for row in self._roster_table_rows() if row["callsign"]}
        missing: List[Dict[str, str]] = []
        for entry in self._extract_unique_entries(self.reference_text.toPlainText()):
            if entry["callsign"] not in roster_calls:
                missing.append(entry)
        return missing

    def _roster_clear(self) -> None:
        self.roster_table.setRowCount(0)
        self._next_roster_seq = 1
        self._update_roster_empty_state()
        self._mark_roster_dirty()

    def _roster_find_row(self, callsign: str) -> int:
        target = (callsign or "").strip().upper()
        if not target:
            return -1
        for row in range(self.roster_table.rowCount()):
            item = self.roster_table.item(row, self.COL_CALLSIGN)
            if item is not None and item.text().strip().upper() == target:
                return row
        return -1

    def _roster_find_role_row(self, role: str) -> int:
        role_key = self._exact_net_control_role(role)
        if role_key not in {"NCS", "ANCS"}:
            return -1
        for index, row in enumerate(self._roster_table_rows()):
            if self._roster_station_role(row) == role_key:
                return index
        return -1

    def _roster_remove_callsign(self, callsign: str) -> None:
        row = self._roster_find_row(callsign)
        if row >= 0:
            self.roster_table.removeRow(row)
            self._update_roster_empty_state()
            self._mark_roster_dirty()

    def _roster_text_columns(self) -> tuple[int, ...]:
        return (
            self.COL_SEQ,
            self.COL_CALLSIGN,
            self.COL_NAME,
            self.COL_STATE,
            self.COL_KEYWORD,
            self.COL_TRAFFIC,
            self.COL_NOTES,
            self.COL_ROLE,
        )

    def _roster_init_table_row(self, row: int) -> None:
        for col in self._roster_text_columns():
            item = NumericTableItem("") if col == self.COL_SEQ else QTableWidgetItem("")
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            self.roster_table.setItem(row, col, item)
        self._roster_configure_category_editor(row)
        self._roster_configure_tfc_status_cell(row)
        self._roster_configure_side_editor(row, self.COL_HEARD)
        self._roster_configure_side_editor(row, self.COL_ACKED)
        self._update_roster_empty_state()

    def _update_roster_empty_state(self) -> None:
        if not hasattr(self, "roster_empty_label") or not hasattr(self, "roster_table"):
            return
        has_rows = self.roster_table.rowCount() > 0
        self.roster_empty_label.setVisible(not has_rows)
        self.roster_table.setVisible(has_rows)

    def _roster_set_category(self, row: int, category: str) -> None:
        widget = self.roster_table.cellWidget(row, self.COL_CATEGORY)
        target = (category or "TFC").strip().upper()
        if widget is not None and hasattr(widget, "setCurrentText"):
            widget.blockSignals(True)
            try:
                widget.setCurrentText(target)
            finally:
                widget.blockSignals(False)
        else:
            item = self.roster_table.item(row, self.COL_CATEGORY)
            if item is None:
                item = QTableWidgetItem(target)
                self.roster_table.setItem(row, self.COL_CATEGORY, item)
            item.setText(target)

    def _roster_set_row(
        self,
        row: int,
        callsign: str,
        name: str,
        state: str,
        traffic: str,
        category: str,
        source: str,
        heard_by: str = "",
        acked_by: str = "",
        notes: str = "",
        station_role: str = "",
        keyword: str = "",
        checkin_seq: str = "",
    ) -> None:
        previous_syncing = self._roster_syncing
        self._roster_syncing = True
        try:
            role_value = self._exact_net_control_role(station_role) or self._roster_role_for_source(source)
            heard_value = self._roster_normalize_side(heard_by) or self._default_heard_by_for_source(source, role_value)
            seq_value = str(checkin_seq or "").strip()
            if seq_value:
                try:
                    seq_value = str(int(seq_value))
                except Exception:
                    pass
            values = [
                seq_value,
                callsign.strip().upper(),
                name.strip(),
                state.strip().upper(),
                keyword.strip(),
                traffic.strip(),
                category.strip().upper() or "TFC",
                heard_value,
                self._roster_normalize_side(acked_by),
                notes.strip(),
                role_value,
            ]
            text_columns = [
                self.COL_SEQ,
                self.COL_CALLSIGN,
                self.COL_NAME,
                self.COL_STATE,
                self.COL_KEYWORD,
                self.COL_TRAFFIC,
                self.COL_NOTES,
            ]
            for value, col in zip([values[0], values[1], values[2], values[3], values[4], values[5], values[9]], text_columns):
                item = self.roster_table.item(row, col)
                if item is None:
                    item = NumericTableItem("") if col == self.COL_SEQ else QTableWidgetItem("")
                    item.setFlags(item.flags() | Qt.ItemIsEditable)
                    self.roster_table.setItem(row, col, item)
                item.setText(value)
                if col == self.COL_SEQ:
                    try:
                        item.setData(Qt.UserRole, int(value))
                    except Exception:
                        item.setData(Qt.UserRole, None)
            self._roster_set_category(row, values[6])
            self._roster_set_side(row, self.COL_HEARD, values[7])
            self._roster_set_side(row, self.COL_ACKED, values[8])
            role_item = self.roster_table.item(row, self.COL_ROLE)
            if role_item is None:
                role_item = QTableWidgetItem("")
                role_item.setFlags(role_item.flags() | Qt.ItemIsEditable)
                self.roster_table.setItem(row, self.COL_ROLE, role_item)
            role_item.setText(values[10])
            if self.roster_table.cellWidget(row, self.COL_TFC_STATUS) is None:
                self._roster_configure_tfc_status_cell(row)
            else:
                self._update_tfc_status_cell(row)
        finally:
            self._roster_syncing = previous_syncing

    def _roster_append_row(
        self,
        callsign: str,
        name: str,
        state: str,
        traffic: str = "",
        category: str = "TFC",
        source: str = "Local",
        *,
        overwrite_source: bool = False,
    ) -> int:
        cs = (callsign or "").strip().upper()
        if not cs:
            return -1
        # Exact FCC fallback is memory-only here. A cold/missing reference never
        # delays roster entry or changes its eligibility.
        prefetch = getattr(self, "_fcc_identity", None)
        if prefetch is not None:
            name, state = prefetch.fill(cs, name, state, self._known_operator_by_callsign.get(cs, {}))
        keyword, parsed_traffic, parsed_category = self._split_keyword_and_traffic(traffic)
        traffic_value = parsed_traffic
        category_value = (category or "TFC").strip().upper() or "TFC"
        if parsed_category == "QRU":
            category_value = "QRU"
        elif parsed_traffic and category_value == "QRU":
            category_value = "TFC"
        existing = self._roster_find_row(cs)
        if existing >= 0:
            current_rows = self._roster_table_rows()
            current = current_rows[existing] if 0 <= existing < len(current_rows) else {}
            next_role = self._roster_role_for_source(source) if overwrite_source else current.get("station_role", "")
            if next_role not in {"NCS", "ANCS"}:
                next_role = current.get("station_role", "")
            heard = self._roster_promote_side(current.get("heard_by", ""), self._default_heard_by_for_source(source, next_role))
            acked = current.get("acked_by", "")
            notes = current.get("notes", "")
            if not keyword:
                keyword = current.get("keyword", "")
            if not traffic_value:
                traffic_value = current.get("traffic", "")
            if category_value == "TFC" and current.get("category") in {"QRU", "LATE"} and not traffic_value:
                category_value = current.get("category", "TFC")
            self._roster_set_row(
                existing,
                cs,
                name or current.get("name", ""),
                state or current.get("state", ""),
                traffic_value,
                category_value,
                source,
                heard,
                acked,
                notes,
                next_role,
                keyword=keyword,
                checkin_seq=current.get("checkin_seq", ""),
            )
            self._roster_apply_pinned_order()
            self._roster_sync_legacy_buffers()
            self._mark_roster_dirty()
            return existing
        row = self.roster_table.rowCount()
        previous_syncing = self._roster_syncing
        self._roster_syncing = True
        try:
            was_sorting = self.roster_table.isSortingEnabled()
            self.roster_table.setSortingEnabled(False)
            self.roster_table.insertRow(row)
            self._roster_init_table_row(row)
            role_value = self._roster_role_for_source(source) if overwrite_source else ""
            if role_value not in {"NCS", "ANCS"}:
                role_value = ""
            seq_value = str(self._next_roster_seq)
            self._next_roster_seq += 1
            self._roster_set_row(
                row,
                cs,
                name,
                state,
                traffic_value,
                category_value,
                source,
                station_role=role_value,
                keyword=keyword,
                checkin_seq=seq_value,
            )
            self.roster_table.setSortingEnabled(was_sorting)
        finally:
            self._roster_syncing = previous_syncing
        self._roster_apply_pinned_order()
        self._roster_sync_legacy_buffers()
        self._mark_roster_dirty()
        return row

    def _roster_row_widget(self, row: int) -> Optional[QComboBox]:
        widget = self.roster_table.cellWidget(row, self.COL_CATEGORY)
        return widget if isinstance(widget, QComboBox) else None

    def _roster_configure_category_editor(self, row: int) -> None:
        combo = QComboBox(self.roster_table)
        categories = ["TFC", "QRU", "LATE"]
        combo.addItems(categories)
        combo.setCurrentText("TFC")
        combo.setMinimumContentsLength(5)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)

        def _on_category_changed(value: str, row_index: int = row) -> None:
            if self._roster_syncing:
                return
            self._mark_roster_dirty()
            self._roster_apply_pinned_order()
            self._roster_sync_legacy_buffers()
            self._refresh_tfc_status_cells()

        combo.currentTextChanged.connect(_on_category_changed)
        self.roster_table.setCellWidget(row, self.COL_CATEGORY, combo)

    def _roster_configure_tfc_status_cell(self, row: int) -> None:
        label = QLabel("", self.roster_table)
        label.setAlignment(Qt.AlignCenter)
        label.setProperty("tfc_status_chip", True)
        self.roster_table.setCellWidget(row, self.COL_TFC_STATUS, label)
        self._update_tfc_status_cell(row)

    def _tfc_status_style(self, status: str) -> str:
        theme = resolve_theme(self.settings)
        border = theme.get("border", "#D3D7DD")
        muted = theme.get("text_muted", "#5B6570")
        if status == "Now":
            background = theme.get("accent", "#2E6F9E")
            text = contrast_text_for_background(background, theme)
            border = theme.get("accent_active", background)
        elif status == "Called":
            background = theme.get("success", "#2E7D32")
            text = contrast_text_for_background(background, theme)
            border = background
        else:
            background = theme.get("warning_bg", "#FFF3CD")
            text = theme.get("warning_text", "#6B4F00")
        return (
            "QLabel {"
            f" background-color: {background}; color: {text}; border: 1px solid {border};"
            " border-radius: 5px; padding: 2px 7px; font-weight: 700;"
            " }"
        )

    def _tfc_status_for_row(self, row_data: Dict[str, str], role: str = "") -> str:
        if str(row_data.get("category") or "").strip().upper() != "TFC":
            return ""
        callsign = str(row_data.get("callsign") or "").strip().upper()
        if not callsign:
            return ""
        role_key = self._exact_net_control_role(role) or self._current_net_control_role()
        if role_key not in {"NCS", "ANCS"}:
            return "Pending"
        if self._next_tfc_last_served.get(role_key, "") == callsign:
            return "Now"
        if callsign in self._next_tfc_called_by_role.get(role_key, set()):
            return "Called"
        return "Pending"

    def _update_tfc_status_cell(self, row: int) -> None:
        if not hasattr(self, "roster_table") or row < 0 or row >= self.roster_table.rowCount():
            return
        widget = self.roster_table.cellWidget(row, self.COL_TFC_STATUS)
        if not isinstance(widget, QLabel):
            return
        rows = self._roster_table_rows()
        row_data = rows[row] if 0 <= row < len(rows) else {}
        status = self._tfc_status_for_row(row_data)
        widget.setText(status)
        widget.setVisible(bool(status))
        widget.setStyleSheet(self._tfc_status_style(status) if status else "")

    def _refresh_tfc_status_cells(self) -> None:
        if not hasattr(self, "roster_table"):
            return
        for row in range(self.roster_table.rowCount()):
            if self.roster_table.cellWidget(row, self.COL_TFC_STATUS) is None:
                self._roster_configure_tfc_status_cell(row)
            else:
                self._update_tfc_status_cell(row)

    def _roster_configure_side_editor(self, row: int, column: int) -> None:
        widget = QWidget(self.roster_table)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(2)
        for role in ("NCS", "ANCS"):
            chip = QToolButton(widget)
            chip.setText(role)
            chip.setCheckable(True)
            chip.setAutoRaise(False)
            chip.setToolTip(("Directed by " if column == self.COL_HEARD else "Acked by ") + role)
            chip.setProperty("sync_role", role)
            chip.setStyleSheet(self._roster_side_chip_style(role=role, column=column))
            chip.toggled.connect(self._on_roster_side_chip_toggled)
            layout.addWidget(chip)
        layout.addStretch(1)
        self.roster_table.setCellWidget(row, column, widget)

    def _roster_side_chip_style(self, *, role: str = "", column: int = -1) -> str:
        theme = resolve_theme(self.settings)
        surface = theme.get("surface", "#F0F2F4")
        surface_alt = theme.get("surface_alt", "#DDE1E6")
        border = theme.get("border", "#D3D7DD")
        text = theme.get("text", "#1C1F21")
        muted = theme.get("text_muted", "#5B6570")
        accent = theme.get("accent", "#2E6F9E")
        accent_active = theme.get("accent_active", accent)
        focus = theme.get("focus", accent)
        checked_bg = accent
        checked_border = accent_active
        checked_text = contrast_text_for_background(checked_bg, theme)
        if column == self.COL_HEARD and role and role != self._current_net_control_role():
            checked_bg = surface_alt
            checked_border = accent
            checked_text = text
        return (
            "QToolButton {"
            f" background-color: {surface}; color: {muted}; border: 1px solid {border};"
            " border-radius: 5px; padding: 2px 7px; font-weight: 700;"
            " min-width: 34px;"
            " }"
            " QToolButton:hover {"
            f" background-color: {surface_alt}; color: {text};"
            " }"
            " QToolButton:checked {"
            f" background-color: {checked_bg}; color: {checked_text}; border: 2px solid {checked_border};"
            " padding: 1px 6px;"
            " }"
            " QToolButton:checked:hover {"
            f" background-color: {checked_border}; color: {contrast_text_for_background(checked_border, theme)};"
            " }"
            " QToolButton:focus {"
            f" border: 2px solid {focus}; padding: 1px 6px;"
            " }"
        )

    def _refresh_roster_side_chip_styles(self) -> None:
        if not hasattr(self, "roster_table"):
            return
        for row in range(self.roster_table.rowCount()):
            for column in (self.COL_HEARD, self.COL_ACKED):
                for role, button in self._roster_side_buttons(row, column).items():
                    button.setStyleSheet(self._roster_side_chip_style(role=role, column=column))

    def _roster_side_buttons(self, row: int, column: int) -> Dict[str, QToolButton]:
        widget = self.roster_table.cellWidget(row, column)
        buttons: Dict[str, QToolButton] = {}
        if widget is None:
            return buttons
        for button in widget.findChildren(QToolButton):
            role = str(button.property("sync_role") or "").strip().upper()
            if role in {"NCS", "ANCS"}:
                buttons[role] = button
        return buttons

    def _roster_side_value(self, row: int, column: int) -> str:
        buttons = self._roster_side_buttons(row, column)
        if buttons:
            active = {role for role, button in buttons.items() if button.isChecked()}
            if active == {"NCS", "ANCS"}:
                return "Both"
            if "ANCS" in active:
                return "ANCS"
            if "NCS" in active:
                return "NCS"
            return ""
        item = self.roster_table.item(row, column)
        return item.text().strip() if item else ""

    def _set_roster_side_buttons_blocked(self, row: int, column: int, value: str) -> bool:
        buttons = self._roster_side_buttons(row, column)
        if not buttons:
            return False
        target = self._roster_normalize_side(value)
        active = set()
        if target == "Both":
            active = {"NCS", "ANCS"}
        elif target in {"NCS", "ANCS"}:
            active = {target}
        for role, button in buttons.items():
            button.blockSignals(True)
            try:
                button.setChecked(role in active)
            finally:
                button.blockSignals(False)
        return True

    def _roster_side_sender_location(self, sender: object) -> tuple[int, int, str]:
        if not isinstance(sender, QToolButton) or not hasattr(self, "roster_table"):
            return -1, -1, ""
        sender_role = str(sender.property("sync_role") or "").strip().upper()
        for row in range(self.roster_table.rowCount()):
            for column in (self.COL_HEARD, self.COL_ACKED):
                if self._roster_side_buttons(row, column).get(sender_role) is sender:
                    return row, column, sender_role
        return -1, -1, sender_role

    def _on_roster_side_chip_toggled(self, _checked: bool) -> None:
        if self._roster_syncing:
            return
        sender = self.sender()
        row, column, clicked_role = self._roster_side_sender_location(sender)
        if column == self.COL_HEARD and row >= 0 and clicked_role in {"NCS", "ANCS"}:
            current_role = self._current_net_control_role()
            rows = self._roster_table_rows()
            station_role = self._roster_station_role(rows[row]) if 0 <= row < len(rows) else ""
            active_value = self._roster_side_value(row, column)
            if (
                station_role not in {"NCS", "ANCS"}
                and current_role in {"NCS", "ANCS"}
                and clicked_role != current_role
                and isinstance(sender, QToolButton)
                and sender.isChecked()
                and active_value == "Both"
            ):
                self._set_roster_side_buttons_blocked(row, column, clicked_role)
        self._mark_roster_dirty()
        self._roster_sync_legacy_buffers()
        self._refresh_tfc_status_cells()
        self._update_bucket_card_states()

    def _roster_set_side(self, row: int, column: int, value: str) -> None:
        widget = self.roster_table.cellWidget(row, column)
        target = self._roster_normalize_side(value)
        if widget is None:
            self._roster_configure_side_editor(row, column)
            widget = self.roster_table.cellWidget(row, column)
        if self._set_roster_side_buttons_blocked(row, column, target):
            return
        if widget is not None and hasattr(widget, "setCurrentText"):
            widget.blockSignals(True)
            try:
                widget.setCurrentText(target)
            finally:
                widget.blockSignals(False)
        else:
            item = self.roster_table.item(row, column)
            if item is None:
                item = QTableWidgetItem("")
                self.roster_table.setItem(row, column, item)
            item.setText(target)

    def _roster_rebuild_category_editors(self) -> None:
        for row in range(self.roster_table.rowCount()):
            if self.roster_table.cellWidget(row, self.COL_CATEGORY) is None:
                self._roster_configure_category_editor(row)
            if self.roster_table.cellWidget(row, self.COL_TFC_STATUS) is None:
                self._roster_configure_tfc_status_cell(row)
            if self.roster_table.cellWidget(row, self.COL_HEARD) is None:
                self._roster_configure_side_editor(row, self.COL_HEARD)
            if self.roster_table.cellWidget(row, self.COL_ACKED) is None:
                self._roster_configure_side_editor(row, self.COL_ACKED)
            category = self.roster_table.item(row, self.COL_CATEGORY).text().strip().upper() if self.roster_table.item(row, self.COL_CATEGORY) else "TFC"
            self._roster_set_category(row, category or "TFC")

    def _roster_reset_from_text(self, text: str, category: str = "TFC") -> None:
        self._roster_syncing = True
        try:
            was_sorting = self.roster_table.isSortingEnabled()
            self.roster_table.setSortingEnabled(False)
            self.roster_table.setRowCount(0)
            self._next_roster_seq = 1
            for line in (text or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                cs, name, state, extra = self._split_checkin_with_extra(line)
                if not cs and not name and not state:
                    continue
                self.roster_table.insertRow(self.roster_table.rowCount())
                row = self.roster_table.rowCount() - 1
                self._roster_init_table_row(row)
                keyword, traffic, parsed_category = self._split_keyword_and_traffic(extra)
                row_category = "QRU" if parsed_category == "QRU" else category
                self._roster_set_row(
                    row,
                    cs,
                    name,
                    state,
                    traffic,
                    row_category,
                    "Local",
                    keyword=keyword,
                    checkin_seq=str(self._next_roster_seq),
                )
                self._next_roster_seq += 1
            self.roster_table.setSortingEnabled(was_sorting)
        finally:
            self._roster_syncing = False
            self._update_roster_empty_state()
        self._roster_apply_pinned_order()
        self._roster_sync_legacy_buffers(write_files=False)
        self._mark_roster_dirty()

    def _copy_roster_category(self, category: str) -> None:
        scope = self._current_roster_action_scope()
        rows = self._scope_filtered_rows(scope, category)
        text = self._roster_table_text_for_rows(rows)
        wanted = (category or "").strip().upper()
        scope_label = self._roster_action_scope_label(scope)
        if text:
            QApplication.clipboard().setText(self._roster_clipboard_text(text))
            self._show_roster_action_status(f"{wanted} list copied for {scope_label}.")
            return
        self._show_roster_action_status(f"No {wanted} stations for {scope_label}.", "info")

    def _copy_roster_seen_locally(self) -> None:
        text = self._roster_table_text()
        if text:
            QApplication.clipboard().setText(self._roster_clipboard_text(text))
            self._show_roster_action_status("Seen locally list copied.")
        else:
            self._show_roster_action_status("No seen locally rows to copy.", "info")

    def _copy_roster_summary(self) -> None:
        scope = self._current_roster_action_scope()
        rows = self._scope_filtered_rows(scope)
        text = self._roster_table_text_for_rows(rows)
        if text:
            QApplication.clipboard().setText(self._roster_clipboard_text(text))
            self._roster_sync_legacy_buffers()
            self._show_roster_action_status(f"Check-ins copied for {self._roster_action_scope_label(scope)}.")
        else:
            self._show_roster_action_status(f"No check-ins for {self._roster_action_scope_label(scope)}.", "info")

    def _selected_roster_rows(self) -> List[Dict[str, str]]:
        selected = self.roster_table.selectionModel().selectedRows() if hasattr(self, "roster_table") else []
        if not selected:
            return []
        row_numbers = sorted({index.row() for index in selected if index.isValid()})
        rows = self._roster_table_rows()
        return [rows[row] for row in row_numbers if 0 <= row < len(rows) and rows[row].get("callsign")]

    def _copy_selected_relays(self) -> None:
        scope = self._current_roster_action_scope()
        role_key = self._exact_net_control_role(scope) or self._current_net_control_role() or "NCS"
        selected_rows = self._selected_roster_rows()
        text = self._roster_table_text_for_rows(selected_rows)
        self._write_file(self._role_relay_file_path(role_key), text)
        if text:
            QApplication.clipboard().setText(self._roster_clipboard_text(text))
            self._show_roster_action_status(f"Relay list copied for {self._roster_action_scope_label(role_key)}.")
            return
        self._show_roster_action_status(f"No selected relays for {self._roster_action_scope_label(role_key)}.", "info")

    def _gap_candidate_rows(self, role: str) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        for row in self._scope_filtered_rows(role):
            callsign = str(row.get("callsign") or "").strip().upper()
            if not callsign:
                continue
            if self._roster_station_role(row) in {"NCS", "ANCS"}:
                continue
            rows.append(row)
        return rows

    def _roster_gap_sections(self) -> List[tuple[str, List[Dict[str, str]]]]:
        role_key = self._exact_net_control_role(self._current_roster_action_scope()) or self._current_net_control_role() or "ANCS"
        reference_text = self.reference_text.toPlainText() if hasattr(self, "reference_text") else ""
        reference_entries = self._extract_unique_entries(reference_text)
        if reference_entries:
            reference_label = self._reference_role_label_for_role(role_key)
            return [(f"Missing from {reference_label}", self._relay_entries_missing_from_reference(role_key))]

        ncs_rows = self._gap_candidate_rows("NCS")
        ancs_rows = self._gap_candidate_rows("ANCS")
        if not ncs_rows or not ancs_rows:
            return []

        ncs_calls = {str(row.get("callsign") or "").strip().upper() for row in ncs_rows}
        ancs_calls = {str(row.get("callsign") or "").strip().upper() for row in ancs_rows}
        missing_from_ncs = [row for row in ancs_rows if str(row.get("callsign") or "").strip().upper() not in ncs_calls]
        missing_from_ancs = [row for row in ncs_rows if str(row.get("callsign") or "").strip().upper() not in ancs_calls]
        return [
            ("Missing from NCS", missing_from_ncs),
            ("Missing from ANCS", missing_from_ancs),
        ]

    def _roster_gap_text_for_sections(self, sections: List[tuple[str, List[Dict[str, str]]]]) -> str:
        parts: List[str] = []
        for title, rows in sections:
            if not rows:
                continue
            text = self._roster_table_text_for_rows(rows)
            if not text:
                continue
            parts.append(f"{title}:\n{text}")
        return "\n\n".join(parts)

    def _update_roster_compare_status(self) -> None:
        if not hasattr(self, "roster_compare_status_btn"):
            return
        sections = self._roster_gap_sections()
        gap_sections = [(title, rows) for title, rows in sections if rows]
        if gap_sections:
            labels = []
            for title, rows in gap_sections:
                suffix = title.replace("Missing from ", "").strip()
                labels.append(f"{suffix} +{len(rows)}")
            text = "Compare: " + " | ".join(labels)
            tooltip = "Roster gaps found. Show Roster Compare for details."
            self.copy_roster_gap_btn.setEnabled(True)
        elif sections:
            text = "Compare: Match"
            tooltip = "NCS and ANCS rosters match for the current compare source."
            self.copy_roster_gap_btn.setEnabled(False)
        else:
            text = "Compare Rosters"
            tooltip = "Paste or load a partner roster in Roster Compare to check NCS/ANCS gaps."
            self.copy_roster_gap_btn.setEnabled(False)
        self.roster_compare_status_btn.setText(text)
        self.roster_compare_status_btn.setToolTip(tooltip)

    def _show_roster_compare(self) -> None:
        self._run_relay_compare()
        if not self._roster_gap_sections():
            self.compare_workspace_tabs.setCurrentWidget(self.reference_card)
        self._scroll_to_roster_compare()

    def _copy_roster_gap(self) -> None:
        sections = self._roster_gap_sections()
        text = self._roster_gap_text_for_sections(sections)
        if text:
            QApplication.clipboard().setText(text)
            self._show_roster_action_status("Roster gap copied.")
            self._run_relay_compare()
            return
        self._show_roster_action_status("No roster gap to copy. Set a reference roster if needed.", "info")

    def _needs_ack_row_indexes(self, role: str = "") -> List[int]:
        role_key = self._exact_net_control_role(role) or self._current_net_control_role()
        indexes: List[int] = []
        if role_key not in {"NCS", "ANCS"}:
            return indexes
        for index, row in enumerate(self._roster_table_rows()):
            if not self._roster_directed_to_current_role(row, role_key):
                continue
            if self._roster_acked_by_current_role(row, role_key):
                continue
            if self._is_current_role_leadership_row(row, role_key):
                continue
            line = self._format_roster_row_for_copy(row)
            if line:
                indexes.append(index)
        return indexes

    def _row_needs_any_required_ack(self, row: Dict[str, str]) -> bool:
        required_roles: List[str] = []
        for role in ("NCS", "ANCS"):
            if self._roster_directed_to_current_role(row, role):
                required_roles.append(role)
        for role in required_roles:
            if self._is_current_role_leadership_row(row, role):
                continue
            if not self._roster_acked_by_current_role(row, role):
                return True
        return False

    def _needs_ack_row_indexes_for_scope(self, scope: str = "") -> List[int]:
        scope_key = str(scope or self._current_roster_action_scope()).strip().upper()
        if scope_key in {"NCS", "ANCS"}:
            return self._needs_ack_row_indexes(scope_key)
        indexes: List[int] = []
        for index, row in enumerate(self._roster_table_rows()):
            if not self._roster_row_matches_action_scope(row, scope_key):
                continue
            if not self._row_needs_any_required_ack(row):
                continue
            line = self._format_roster_row_for_copy(row)
            if line:
                indexes.append(index)
        return indexes

    def _needs_ack_lines(self, role: str = "") -> List[str]:
        rows = self._roster_table_rows()
        lines: List[str] = []
        for index in self._needs_ack_row_indexes(role):
            if 0 <= index < len(rows):
                line = self._format_roster_row_for_copy(rows[index])
                if line:
                    lines.append(line)
        return lines

    def _needs_ack_text(self, role: str = "") -> str:
        return "\n".join(self._needs_ack_lines(role)).strip()

    def _needs_ack_text_for_scope(self, scope: str = "") -> str:
        rows = self._roster_table_rows()
        lines: List[str] = []
        for index in self._needs_ack_row_indexes_for_scope(scope):
            if 0 <= index < len(rows):
                line = self._format_roster_row_for_copy(rows[index])
                if line:
                    lines.append(line)
        return "\n".join(lines).strip()

    def _mark_needs_ack_rows_copied(self, role: str = "") -> int:
        role_key = self._exact_net_control_role(role) or self._current_net_control_role()
        if role_key not in {"NCS", "ANCS"}:
            return 0
        row_indexes = self._needs_ack_row_indexes(role_key)
        if not row_indexes:
            return 0
        for row in row_indexes:
            current = self._roster_side_value(row, self.COL_ACKED)
            self._roster_set_side(row, self.COL_ACKED, self._roster_promote_side(current, role_key))
        self._mark_roster_dirty()
        self._roster_sync_legacy_buffers()
        self._update_bucket_card_states()
        return len(row_indexes)

    def _copy_needs_sync(self) -> None:
        scope = self._current_roster_action_scope()
        scope_label = self._roster_action_scope_label(scope)
        text = self._needs_ack_text_for_scope(scope)
        if not text:
            self._sync_role_ack_pending_files()
            self._show_roster_action_status(f"No ACKs are pending for {scope_label}.", "info")
            return
        QApplication.clipboard().setText(self._roster_clipboard_text(text))
        if scope in {"NCS", "ANCS"}:
            mapped = self._macro_action_file_is_mapped("ACK_PENDING", ROLE_CHECKIN_FILE_NAMES[scope]["ACK_PENDING"])
            marked = self._mark_needs_ack_rows_copied(scope)
            if marked:
                file_text = f" {ROLE_CHECKIN_FILE_NAMES[scope]['ACK_PENDING']} updated." if mapped else ""
                self._show_roster_action_status(f"ACK list copied for {scope_label}.{file_text} {marked} marked acked by {scope_label}.")
            else:
                self._show_roster_action_status(f"ACK Needed list copied for {scope_label}.")
        else:
            self._show_roster_action_status(f"ACK Needed list copied for {scope_label}. No ACK chips changed.")

    def _next_tfc_eligible_rows(self, role: str = "") -> List[Dict[str, str]]:
        role_key = self._exact_net_control_role(role) or self._current_net_control_role()
        if role_key not in {"NCS", "ANCS"}:
            return []
        rows: List[Dict[str, str]] = []
        for row in self._role_filtered_category_rows("TFC", role_key):
            if self._is_current_role_leadership_row(row, role_key):
                continue
            if not str(row.get("callsign") or "").strip():
                continue
            rows.append(row)
        return rows

    def _next_tfc_payload(self, callsign: str) -> str:
        cs = (callsign or "").strip().upper()
        return f"{cs} {cs}" if cs else ""

    def _next_tfc_row(self, role: str = "") -> Optional[Dict[str, str]]:
        role_key = self._exact_net_control_role(role) or self._current_net_control_role()
        rows = self._next_tfc_eligible_rows(role_key)
        if not rows:
            return None
        last = self._next_tfc_last_served.get(role_key, "")
        if not last:
            return rows[0]
        callsigns = [str(row.get("callsign") or "").strip().upper() for row in rows]
        if last not in callsigns:
            return rows[0]
        next_index = callsigns.index(last) + 1
        if next_index >= len(rows):
            return None
        return rows[next_index]

    def _copy_next_tfc(self) -> None:
        role = self._current_roster_action_scope()
        if role not in {"NCS", "ANCS"}:
            self._show_roster_action_status("Select NCS or ANCS for Next TFC.", "info")
            return
        row = self._next_tfc_row(role)
        if not row:
            self._write_file(self._role_next_tfc_file_path(role), "")
            if role in {"NCS", "ANCS"}:
                last = self._next_tfc_last_served.get(role, "")
                if last:
                    self._next_tfc_called_by_role.setdefault(role, set()).add(last)
                self._next_tfc_last_served[role] = ""
            self._sync_mapped_roster_files()
            self._show_roster_action_status(f"No more directed TFC stations for {role or 'this role'}.", "info")
            self._refresh_tfc_status_cells()
            return
        callsign = str(row.get("callsign") or "").strip().upper()
        payload = self._next_tfc_payload(callsign)
        QApplication.clipboard().setText(payload)
        self._write_file(self._role_next_tfc_file_path(role), payload)
        self._next_tfc_last_served[role] = callsign
        self._next_tfc_called_by_role.setdefault(role, set()).add(callsign)
        self._refresh_tfc_status_cells()
        self._sync_mapped_roster_files()
        mapped = self._macro_action_file_is_mapped("NEXT_TFC", ROLE_CHECKIN_FILE_NAMES[role]["NEXT_TFC"])
        suffix = f" {ROLE_CHECKIN_FILE_NAMES[role]['NEXT_TFC']} updated." if mapped else ""
        self._show_roster_action_status(f"{payload} copied.{suffix}")

    def _workspace_custom_bucket_id(self, mapping: Dict[str, object], index: int) -> str:
        scope = str(mapping.get("scope") or "").strip().upper() or "SHARED"
        function = str(mapping.get("function") or "").strip().upper() or "CUSTOM"
        macro_id = str(mapping.get("macro_id") or "").strip()
        source_file = str(mapping.get("source_file") or "").strip()
        custom_name = str(mapping.get("custom_name") or "").strip()
        identity = macro_id or source_file or custom_name or f"row_{index + 1}"
        return f"custom::{scope}::{function}::{identity}"

    def _workspace_custom_bucket_title(self, mapping: Dict[str, object], index: int) -> str:
        custom_name = str(mapping.get("custom_name") or "").strip()
        if custom_name:
            return custom_name
        macro_label = str(mapping.get("macro_label") or "").strip()
        if macro_label:
            return macro_label
        macro_id = str(mapping.get("macro_id") or "").strip()
        if macro_id:
            return macro_id
        return f"CUSTOM_{index + 1}"

    def _paste_into_review_card(self) -> None:
        from PySide6.QtWidgets import QApplication

        self.review_card.set_text(QApplication.clipboard().text())

    @staticmethod
    def _strip_inline_review_context(line: str) -> str:
        return re.sub(r"\s*\[ctx:\s*.*\]\s*$", "", str(line or "").strip(), flags=re.IGNORECASE)

    def _log_assisted_enabled(self) -> bool:
        if not bool(getattr(self, "LOG_ASSISTED_INTAKE_VISIBLE", True)):
            return False
        checkbox = getattr(self, "log_assisted_enable_chk", None)
        if checkbox is None or not hasattr(checkbox, "isChecked"):
            return False
        try:
            return bool(checkbox.isChecked())
        except Exception:
            return False

    def _fldigi_log_path(self) -> str:
        try:
            return str(self.settings.get("fldigi_log_path", "") or "").strip()
        except Exception:
            return ""

    def _log_assisted_session_start_utc(self) -> datetime.datetime:
        raw = str(getattr(self, "_net_start_utc", "") or "").strip()
        if raw:
            try:
                parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=datetime.timezone.utc)
                return parsed.astimezone(datetime.timezone.utc)
            except Exception:
                pass
        return datetime.datetime.now(datetime.timezone.utc)

    def _log_assisted_candidate_line(self, candidate: object) -> str:
        parts = [
            str(getattr(candidate, "callsign", "") or "").strip().upper(),
            str(getattr(candidate, "name", "") or "").strip(),
            str(getattr(candidate, "state", "") or "").strip().upper(),
        ]
        traffic = str(getattr(candidate, "traffic", "") or "").strip()
        if traffic:
            parts.append(traffic)
        line = " / ".join(part for part in parts if part)
        context = str(getattr(candidate, "tx_context", "") or "").strip()
        if context:
            line = f"{line} [ctx: {context}]"
        return line

    @staticmethod
    def _log_assisted_candidate_key(candidate: object) -> str:
        callsign = str(getattr(candidate, "callsign", "") or "").strip().upper()
        bucket = str(getattr(candidate, "bucket", "") or "").strip().upper()
        traffic = str(getattr(candidate, "traffic", "") or "").strip().upper()
        return "|".join(part for part in (callsign, bucket, traffic) if part)

    def _clear_log_assisted_candidates(self, *, remove_applied: bool = True) -> None:
        candidates = list(getattr(self, "_log_assisted_candidates_by_callsign", {}).values())
        if remove_applied and hasattr(self, "roster_table"):
            for candidate in candidates:
                bucket = str(getattr(candidate, "bucket", "") or "").strip().upper()
                callsign = str(getattr(candidate, "callsign", "") or "").strip().upper()
                if callsign and bucket in {"TFC", "QRU", "LATE"}:
                    self._roster_remove_callsign(callsign)
        if hasattr(self, "review_card"):
            self.review_card.set_text("")
        self._log_assisted_candidates_by_callsign = {}
        self._log_assisted_seen_normalized = set()
        self._log_assisted_session_tx_context = ""

    def _capture_log_assisted_session(self) -> None:
        path = self._fldigi_log_path()
        self._log_assisted_session_path = path
        self._log_assisted_session_offset = 0
        self._log_assisted_session_tx_context = ""
        self._log_assisted_seen_normalized = set()
        self._log_assisted_candidates_by_callsign = {}
        if path:
            try:
                self._log_assisted_session_offset = Path(path).stat().st_size
            except Exception:
                self._log_assisted_session_offset = 0

    def _apply_log_assisted_candidate(self, candidate: object) -> None:
        callsign = str(getattr(candidate, "callsign", "") or "").strip().upper()
        if not callsign:
            return
        bucket = str(getattr(candidate, "bucket", "REVIEW") or "REVIEW").strip().upper()
        name = str(getattr(candidate, "name", "") or "").strip()
        state = str(getattr(candidate, "state", "") or "").strip().upper()
        traffic = str(getattr(candidate, "traffic", "") or "").strip()
        self._log_assisted_candidates_by_callsign[self._log_assisted_candidate_key(candidate)] = candidate  # type: ignore[assignment]
        if bucket in {"TFC", "QRU", "LATE"}:
            self._roster_append_row(callsign, name, state, traffic, bucket, "Local")
            return
        line = self._log_assisted_candidate_line(candidate)
        existing = self.review_card.text().splitlines() if hasattr(self, "review_card") else []
        if line and line not in existing:
            self.review_card.set_text("\n".join([*existing, line]).strip())
            self._update_bucket_card_states()

    def _poll_log_assisted_intake(self) -> None:
        if not self._log_assisted_enabled():
            self._stop_log_assisted_timer()
            return
        path = self._fldigi_log_path()
        if not path:
            return
        if path != getattr(self, "_log_assisted_session_path", ""):
            self._clear_log_assisted_candidates(remove_applied=True)
            self._capture_log_assisted_session()
            return
        candidates, offset, tx_context = scan_fldigi_log_file(
            path,
            start_offset=int(getattr(self, "_log_assisted_session_offset", 0) or 0),
            session_start_utc=self._log_assisted_session_start_utc(),
            seen_normalized=getattr(self, "_log_assisted_seen_normalized", set()),
            last_tx_context=str(getattr(self, "_log_assisted_session_tx_context", "") or ""),
            include_tx_context=True,
            lookup_identity=(lambda call: lookup_operator_identity(call, db_path=Path(self.settings.db_path).parent / 'freqinout_nets.db'))
                            if getattr(self, '_preparation_scope', None) is not None else lookup_operator_identity,
        )
        self._log_assisted_session_offset = int(offset or 0)
        self._log_assisted_session_tx_context = str(tx_context or "")
        for candidate in candidates:
            if self._log_assisted_candidate_key(candidate) not in self._log_assisted_candidates_by_callsign:
                self._apply_log_assisted_candidate(candidate)

    def _start_log_assisted_timer(self) -> None:
        if not self._log_assisted_enabled():
            return
        timer = getattr(self, "_log_assisted_timer", None)
        if timer is not None and hasattr(timer, "isActive") and not timer.isActive():
            timer.start()

    def _stop_log_assisted_timer(self) -> None:
        timer = getattr(self, "_log_assisted_timer", None)
        if timer is not None and hasattr(timer, "isActive") and timer.isActive():
            timer.stop()

    def _on_log_assisted_toggled(self, enabled: bool) -> None:
        if not enabled or not self._log_assisted_enabled():
            self._stop_log_assisted_timer()
            return
        self._capture_log_assisted_session()
        if self._net_in_progress:
            self._start_log_assisted_timer()

    def _insert_left_bucket_widget(self, widget: WorkspaceBucketCard) -> None:
        if self._left_bucket_col.indexOf(widget) >= 0:
            return
        insert_at = max(self._left_bucket_col.count() - 1, 0)
        self._left_bucket_col.insertWidget(insert_at, widget)

    def _enabled_custom_bucket_mappings(self, role: Optional[str] = None) -> List[Dict[str, object]]:
        if getattr(self, '_preparation_scope', None) is not None:
            mappings = self._preparation_scope.settings().get('_preparation_macro_mappings', [])
            active_role = normalize_role(role or self.role_combo.currentText())
            return [dict(m) for m in mappings if str(m.get('function') or '').upper() == 'CUSTOM'
                    and active_role != 'JOINER' and str(m.get('scope') or '').upper() in {active_role, 'SHARED'}]
        selected = self._selected_macro_profile_path()
        record = self._macro_profile_record(selected)
        mappings = record.get("mappings")
        if not isinstance(mappings, list):
            return []
        active_role = normalize_role(role or self.role_combo.currentText())
        if active_role == "JOINER":
            return []
        custom_mappings: List[Dict[str, object]] = []
        for mapping in mappings:
            if not self._macro_profile_mapping_is_complete(mapping):
                continue
            if str(mapping.get("function") or "").strip().upper() != "CUSTOM":
                continue
            mapping_role = str(mapping.get("scope") or "").strip().upper()
            if mapping_role not in {active_role, "SHARED"}:
                continue
            custom_mappings.append(mapping)
        return custom_mappings

    def _enabled_standard_roster_mappings(self, role: Optional[str] = None) -> List[Dict[str, object]]:
        if getattr(self, '_preparation_scope', None) is not None:
            mappings = self._preparation_scope.settings().get('_preparation_macro_mappings', [])
            return [dict(m) for m in mappings if str(m.get('function') or '').upper()
                    in {'TFC', 'QRU', 'LATE', 'ALL', 'ACK_PENDING', 'NEXT_TFC', 'RELAYS'} and m.get('source_file')]
        selected = self._selected_macro_profile_path()
        record = self._macro_profile_record(selected)
        mappings = record.get("mappings")
        if not isinstance(mappings, list):
            return []
        standard_functions = {"TFC", "QRU", "LATE", "ALL", "ACK_PENDING", "NEXT_TFC", "RELAYS"}
        roster_mappings: List[Dict[str, object]] = []
        for mapping in mappings:
            if not self._macro_profile_mapping_is_complete(mapping):
                continue
            function = str(mapping.get("function") or "").strip().upper()
            if function not in standard_functions:
                continue
            source_file = str(mapping.get("source_file") or "").strip()
            if not source_file:
                continue
            roster_mappings.append(mapping)
        return roster_mappings

    def _mapping_scope_key(self, mapping: Dict[str, object]) -> str:
        scope = str(mapping.get("scope") or "").strip().upper()
        return scope if scope in ACTION_SCOPES else "ALL"

    def _roster_text_for_mapping_function(self, function: object, scope: str = "") -> str:
        normalized = str(function or "").strip().upper()
        scope_key = str(scope or "").strip().upper()
        if normalized in {"TFC", "QRU", "LATE"}:
            if scope_key in ACTION_SCOPES:
                return self._roster_table_text_for_rows(self._scope_filtered_rows(scope_key, normalized))
            return self._roster_table_text(normalized)
        if normalized == "ALL":
            if scope_key in ACTION_SCOPES:
                return self._roster_table_text_for_rows(self._scope_filtered_rows(scope_key))
            return self._roster_table_text()
        if normalized == "ACK_PENDING" and scope_key in {"NCS", "ANCS"}:
            return self._roster_clipboard_text(self._needs_ack_text(scope_key))
        if normalized == "NEXT_TFC" and scope_key in {"NCS", "ANCS"}:
            last = str(self._next_tfc_last_served.get(scope_key, "") or "").strip().upper()
            return self._next_tfc_payload(last) if last else ""
        if normalized == "RELAYS" and scope_key in {"NCS", "ANCS"}:
            return self._read_file(self._role_relay_file_path(scope_key))
        return ""

    def _sync_mapped_roster_files(self) -> None:
        for mapping in self._enabled_standard_roster_mappings():
            source_file = str(mapping.get("source_file") or "").strip()
            if not source_file:
                continue
            text = self._roster_text_for_mapping_function(mapping.get("function"), self._mapping_scope_key(mapping))
            self._write_file(source_file, text)

    def _refresh_custom_bucket_cards(self, role: Optional[str] = None) -> None:
        active_role = normalize_role(role or self.role_combo.currentText())
        self._workspace_visible_bucket_ids.difference_update(self._custom_bucket_cards.keys())
        for bucket_id, card in self._custom_bucket_cards.items():
            card.setVisible(False)

        custom_mappings = self._enabled_custom_bucket_mappings(active_role)
        for index, mapping in enumerate(custom_mappings):
            bucket_id = self._workspace_custom_bucket_id(mapping, index)
            title = self._workspace_custom_bucket_title(mapping, index)
            source_file = str(mapping.get("source_file") or "").strip()
            card = self._custom_bucket_cards.get(bucket_id)
            if card is None:
                card = WorkspaceBucketCard(self.settings, title=title, allow_paste=True)
                self._custom_bucket_cards[bucket_id] = card
                self._workspace_bucket_cards[bucket_id] = card
                self._insert_left_bucket_widget(card)
            card.set_title(title)
            card.set_read_only(False)
            if getattr(self, '_preparation_scope', None) is not None:
                # Worker-published content only; role changes never erase entered
                # bucket text or discover files on the Qt thread.
                if not card.property('preparedBucketInitialized'):
                    card.set_text(self._preparation_scope.settings().get('_preparation_custom_content', {}).get(source_file, ''))
                    card.setProperty('preparedBucketInitialized', True)
            elif source_file:
                loaded_text = self._read_file(source_file)
                card.set_text(loaded_text)
            else:
                card.set_text('')
            self._custom_bucket_sources[bucket_id] = source_file
            card.setVisible(True)
            self._workspace_visible_bucket_ids.add(bucket_id)

        receiver = getattr(self, '_prepared_receiver', None)
        if receiver is not None:
            receiver._connect_edits()
        for bucket_id in list(self._custom_bucket_cards.keys()):
            if bucket_id not in self._workspace_visible_bucket_ids:
                self._custom_bucket_cards[bucket_id].setVisible(False)

    def _refresh_partner_controls(self) -> None:
        role = normalize_role(self.role_combo.currentText())
        joiner = role == "JOINER"
        self.partner_primary_label.setVisible(not joiner)
        self.partner_primary_edit.setVisible(not joiner)
        self.partner_primary_btn.setVisible(not joiner)
        self.joiner_ncs_label.setVisible(joiner)
        self.joiner_ncs_edit.setVisible(joiner)
        self.joiner_ancs_label.setVisible(joiner)
        self.joiner_ancs_edit.setVisible(joiner)
        self.joiner_add_btn.setVisible(joiner)
        if role == "ANCS":
            self.partner_primary_label.setText("NCS Callsign:")
            self.partner_primary_btn.setText("Set NCS")
            if not getattr(self, '_restoring_preparation', False):
                self.partner_primary_edit.setText(self._ncs_partner_call)
        else:
            self.partner_primary_label.setText("ANCS Callsign:")
            self.partner_primary_btn.setText("Set ANCS")
            if not getattr(self, '_restoring_preparation', False):
                self.partner_primary_edit.setText(self._ancs_partner_call)
        self._update_partner_action_state()
        if hasattr(self, "_responsive_geometry_timer"):
            self._responsive_geometry_timer.start(0)

    def _update_partner_action_state(self) -> None:
        if not hasattr(self, "partner_primary_edit"):
            return
        enabled = bool(self._net_in_progress)
        tooltip = "" if enabled else "Start the net before setting NCS/ANCS roster entries."
        for widget in (
            self.partner_primary_edit,
            self.partner_primary_btn,
            self.joiner_ncs_edit,
            self.joiner_ancs_edit,
            self.joiner_add_btn,
        ):
            widget.setEnabled(enabled)
            widget.setToolTip(tooltip)

    def _add_net_control_roster_row(self, callsign: str, role: str, *, local: bool = False) -> int:
        cs = (callsign or "").strip().upper()
        role_key = normalize_role(role)
        if not cs or role_key not in {"NCS", "ANCS"}:
            return -1
        name, state, _exists = self._lookup_operator_name_state(cs)
        if local:
            name = (self.settings.get("operator_name", "") or "").strip() or name
            state = (self.settings.get("operator_state", "") or "").strip().upper() or state
        source = f"{role_key} - Net Control"
        role_row = self._roster_find_role_row(role_key)
        callsign_row = self._roster_find_row(cs)
        existing = role_row if role_row >= 0 else callsign_row
        if role_row >= 0 and callsign_row >= 0 and callsign_row != role_row:
            self.roster_table.removeRow(role_row)
            existing = callsign_row - 1 if role_row < callsign_row else callsign_row
        category = "QRU"
        traffic = ""
        notes = ""
        if existing >= 0:
            widget = self.roster_table.cellWidget(existing, self.COL_CATEGORY)
            if widget is not None and hasattr(widget, "currentText"):
                category = str(widget.currentText() or "").strip().upper() or "QRU"
            item = self.roster_table.item(existing, self.COL_TRAFFIC)
            traffic = (item.text() if item else "").strip()
            notes_item = self.roster_table.item(existing, self.COL_NOTES)
            notes = (notes_item.text() if notes_item else "").strip()
            self._roster_set_row(existing, cs, name, state, traffic, category, source, "Both", "Both", notes, role_key)
            row = existing
        else:
            row = self._roster_append_row(cs, name, state, traffic, category, source, overwrite_source=True)
        if row >= 0:
            self._roster_set_side(row, self.COL_HEARD, "Both")
            self._roster_set_side(row, self.COL_ACKED, "Both")
            self._sync_partner_fields_from_roster()
            self._roster_apply_pinned_order()
            self._roster_sync_legacy_buffers()
            self._mark_roster_dirty()
            self._update_bucket_card_states()
        return row

    def _sync_partner_fields_from_roster(self) -> None:
        ncs_call = ""
        ancs_call = ""
        for row in self._roster_table_rows():
            role = self._roster_station_role(row)
            if role == "NCS" and not ncs_call:
                ncs_call = row.get("callsign", "")
            elif role == "ANCS" and not ancs_call:
                ancs_call = row.get("callsign", "")
        self._ncs_partner_call = ncs_call
        self._ancs_partner_call = ancs_call
        self._refresh_roster_action_scope_styles()
        if not hasattr(self, "partner_primary_edit"):
            return
        active_role = normalize_role(self.role_combo.currentText()) if hasattr(self, "role_combo") else ""
        self.partner_primary_edit.blockSignals(True)
        try:
            if active_role == "ANCS":
                self.partner_primary_edit.setText(self._ncs_partner_call)
            elif active_role == "NCS":
                self.partner_primary_edit.setText(self._ancs_partner_call)
        finally:
            self.partner_primary_edit.blockSignals(False)

    def _apply_local_net_control_role(self) -> None:
        if not self._net_in_progress:
            return
        role = normalize_role(self.role_combo.currentText())
        cs = (self.settings.get("operator_callsign", "") or "").strip().upper()
        if cs and role in {"NCS", "ANCS"}:
            self._add_net_control_roster_row(cs, role, local=True)

    def _set_partner_from_primary_controls(self) -> None:
        if not self._net_in_progress:
            self.partner_status_label.setText("Start Net before setting NCS/ANCS.")
            return
        role = normalize_role(self.role_combo.currentText())
        cs, match_status = self._resolve_role_callsign_from_field(self.partner_primary_edit)
        if not cs:
            self.partner_status_label.setText("No callsign entered.")
            return
        if match_status == "ambiguous":
            self.partner_status_label.setText("Multiple HF operators match. Enter the callsign.")
            return
        if role == "ANCS":
            self._ncs_partner_call = cs
            self._add_net_control_roster_row(cs, "NCS")
            self._apply_local_net_control_role()
            self.partner_status_label.setText(f"NCS {cs} set in roster.")
        else:
            self._ancs_partner_call = cs
            self._apply_local_net_control_role()
            self._add_net_control_roster_row(cs, "ANCS")
            self.partner_status_label.setText(f"ANCS {cs} set in roster.")
        self._refresh_roster_action_scope_styles()

    def _add_joiner_net_control_rows(self) -> None:
        if not self._net_in_progress:
            self.partner_status_label.setText("Start Net before setting NCS/ANCS.")
            return
        ncs, ncs_status = self._resolve_role_callsign_from_field(self.joiner_ncs_edit)
        ancs, ancs_status = self._resolve_role_callsign_from_field(self.joiner_ancs_edit)
        if ncs_status == "ambiguous" or ancs_status == "ambiguous":
            self.partner_status_label.setText("Multiple HF operators match. Enter each callsign.")
            return
        added: List[str] = []
        if ncs:
            self._ncs_partner_call = ncs
            added.append(f"NCS {ncs}")
        if ancs:
            self._ancs_partner_call = ancs
            added.append(f"ANCS {ancs}")
        if ncs:
            self._add_net_control_roster_row(ncs, "NCS")
        if ancs:
            self._add_net_control_roster_row(ancs, "ANCS")
        self.partner_status_label.setText(
            f"{', '.join(added)} added to roster." if added else "No net control callsigns entered."
        )
        self._refresh_roster_action_scope_styles()

    def _on_role_changed(self, role: str) -> None:
        if self._workspace_role_loading:
            return
        self._apply_role_workspace(role)
        self._refresh_partner_controls()
        self._apply_local_net_control_role()
        self._sync_roster_action_scope_to_role()
        self._roster_apply_pinned_order()
        self._refresh_roster_side_chip_styles()
        self._refresh_roster_action_scope_styles()
        self._refresh_tfc_status_cells()
        self._roster_sync_legacy_buffers()

    def _on_workspace_text_changed(self) -> None:
        self._update_bucket_card_states()

    def _on_roster_item_changed(self, item: QTableWidgetItem) -> None:
        if self._roster_syncing or self._roster_loading or item is None:
            return
        row = item.row()
        if row < 0:
            return
        if item.column() == self.COL_CALLSIGN:
            item.setText(item.text().strip().upper())
            self._roster_deduplicate_callsigns()
            self._roster_apply_pinned_order()
            self._sync_partner_fields_from_roster()
        elif item.column() == self.COL_STATE:
            item.setText(item.text().strip().upper())
        elif item.column() == self.COL_TRAFFIC:
            keyword, traffic, category = self._split_keyword_and_traffic(item.text())
            if keyword or traffic:
                previous_syncing = self._roster_syncing
                self._roster_syncing = True
                try:
                    if keyword:
                        keyword_item = self.roster_table.item(row, self.COL_KEYWORD)
                        if keyword_item is None:
                            keyword_item = QTableWidgetItem("")
                            keyword_item.setFlags(keyword_item.flags() | Qt.ItemIsEditable)
                            self.roster_table.setItem(row, self.COL_KEYWORD, keyword_item)
                        existing_keyword = keyword_item.text().strip()
                        keyword_item.setText(" / ".join(part for part in (existing_keyword, keyword) if part))
                    item.setText(traffic)
                    if category == "QRU":
                        self._roster_set_category(row, "QRU")
                finally:
                    self._roster_syncing = previous_syncing
            self._roster_apply_pinned_order()
        elif item.column() in {self.COL_HEARD, self.COL_ACKED}:
            item.setText(self._roster_normalize_side(item.text()))
        elif item.column() == self.COL_ROLE:
            item.setText(self._roster_role_for_source(item.text()))
            self._roster_apply_pinned_order()
        self._mark_roster_dirty()
        self._roster_sync_legacy_buffers()

    def _on_roster_model_data_changed(self, *_args) -> None:
        if self._roster_syncing or self._roster_loading:
            return
        self._mark_roster_dirty()
        self._roster_sync_legacy_buffers()

    def _on_roster_model_rows_changed(self, *_args) -> None:
        if self._roster_syncing or self._roster_loading:
            return
        self._mark_roster_dirty()
        self._roster_sync_legacy_buffers()

    def _known_add_button_role_map(self, role: Optional[str] = None) -> List[tuple[str, str, str]]:
        active_role = normalize_role(role or self.role_combo.currentText())
        if active_role == "JOINER":
            return [("seen_locally", "Add to Seen Locally", "success")]
        return [
            ("tfc", "Add to TFC", "success"),
            ("qru", "Add to QRU", "info"),
            ("late", "Add to LATE", "warning"),
        ]

    def _known_target_widget(self, target: str):
        return None

    def _require_active_net_for_checkin_add(self) -> bool:
        if self._net_in_progress:
            return True
        QMessageBox.information(
            self,
            "Start Net Required",
            "Start the net before adding check-ins to the roster. Start Net clears pre-net roster entries so stale check-ins are not carried forward.",
        )
        return False

    def _refresh_known_action_band(self, role: Optional[str] = None) -> None:
        active_role = normalize_role(role or self.role_combo.currentText())
        visible_targets = {target for target, _, _ in self._known_add_button_role_map(active_role)}
        for target, button in self._known_add_buttons.items():
            button.setVisible(target in visible_targets)

        ordered_buttons = [self._known_add_buttons[target] for target, _, _ in self._known_add_button_role_map(active_role)]
        if ordered_buttons:
            self.setTabOrder(self.known_op_edit, ordered_buttons[0])
            for prev, nxt in zip(ordered_buttons, ordered_buttons[1:]):
                self.setTabOrder(prev, nxt)
        self._update_add_buttons_state()

    def _insert_known_into_bucket(self, target: str) -> None:
        if not self._require_active_net_for_checkin_add():
            return
        line = self.known_op_edit.text().strip()
        if not line:
            return
        cs, name, state, extra = self._split_checkin_with_extra(line)
        if not cs and not name and not state:
            return
        lookup = self._known_operator_by_callsign.get(cs or "")
        if lookup:
            cs = lookup.get("callsign", cs)
            name = lookup.get("name", name)
            state = lookup.get("state", state)
        category = {"tfc": "TFC", "qru": "QRU", "late": "LATE", "seen_locally": "TFC"}.get(target, "TFC")
        self._roster_append_row(cs, name, state, extra, category)
        self.known_op_edit.clear()
        self.known_op_edit.setFocus()

    def _workspace_line_count(self, text: str) -> int:
        return sum(1 for line in (text or "").splitlines() if line.strip())

    def _workspace_unique_entries(self, text: str) -> List[str]:
        entries: List[str] = []
        seen = set()
        for line in (text or "").splitlines():
            cs, name, state = self._parse_checkin_line(line)
            normalized = self._format_entry(cs, name, state)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            entries.append(normalized)
        return entries

    def _reference_compare_label(self) -> str:
        return self.reference_card.title()

    def _review_entries_missing_from_roster(self) -> List[Dict[str, str]]:
        roster_calls = {row["callsign"] for row in self._roster_table_rows() if row["callsign"]}
        missing: List[Dict[str, str]] = []
        for entry in self._extract_unique_entries(self.review_card.text()):
            if entry["callsign"] not in roster_calls:
                missing.append(entry)
        return missing

    def _checkin_line_details(self, line: str) -> tuple[str, str, str, str]:
        raw_line = self._strip_inline_review_context(line)
        if not raw_line:
            return "", "", "", ""
        if "/" in raw_line and not raw_line.startswith("#"):
            parts = [p.strip() for p in raw_line.split("/") if p.strip()]
            if len(parts) >= 3:
                cs = parts[0].split()[0].upper()
                name = parts[1]
                state_tokens = parts[2].split()
                state = state_tokens[0].upper() if state_tokens else ""
                extra_parts = []
                trailing = " ".join(state_tokens[1:]).strip()
                if trailing:
                    extra_parts.append(trailing)
                if len(parts) > 3:
                    extra_parts.extend(parts[3:])
                extra = " / ".join(part for part in extra_parts if part).strip()
                return cs, name, state, extra
        return self._split_checkin_with_extra(raw_line)

    def _traffic_category_from_text(self, text: str) -> tuple[str, str]:
        _keyword, traffic, category = self._split_keyword_and_traffic(text)
        return traffic, category

    def _normalize_merge_entry_fields(self, entry: Dict[str, str]) -> tuple[str, str, str]:
        state = str(entry.get("state", "") or "").strip().upper()
        traffic = str(entry.get("traffic", "") or "").strip()
        keyword = str(entry.get("keyword", "") or "").strip()
        category = str(entry.get("category", "") or "TFC").strip().upper() or "TFC"

        state_tokens = state.split()
        merged_extra_parts = []
        if len(state_tokens) > 1:
            state = state_tokens[0]
            merged_extra_parts.extend(state_tokens[1:])
        if traffic:
            merged_extra_parts.append(traffic)
        if keyword:
            merged_extra_parts.insert(0, keyword)
        merged_extra = " ".join(part for part in merged_extra_parts if part).strip()
        if merged_extra:
            parsed_keyword, parsed_traffic, parsed_category = self._split_keyword_and_traffic(merged_extra)
            traffic = f"{parsed_keyword} / {parsed_traffic}".strip(" /") if parsed_keyword and parsed_traffic else (parsed_traffic or parsed_keyword)
            category = parsed_category or category
        return state, traffic, category

    def _workspace_compare_payload(self, source_bucket: str, target_bucket: str) -> tuple[str, str]:
        source_text = self._workspace_bucket_text(source_bucket)
        target_text = self._workspace_bucket_text(target_bucket)
        local_entries = self._extract_unique_entries(source_text)
        compare_entries = self._extract_unique_entries(target_text)
        local_calls = {entry["callsign"] for entry in local_entries}
        mergeable_entries = [
            self._format_entry(entry["callsign"], entry["name"], entry["state"])
            for entry in compare_entries
            if entry["callsign"] not in local_calls
        ]
        compare_label = self._reference_compare_label() if target_bucket in {"reference", "ncs_reference", "ancs_reference"} else target_bucket
        lines = [
            f"Net Roster: {len(local_entries)}",
            f"{compare_label}: {len(compare_entries)}",
            "",
            f"Entries in {compare_label} not in Net Roster:",
            "\n".join(mergeable_entries) if mergeable_entries else "(none)",
        ]
        return "\n".join(lines), "\n".join(mergeable_entries)

    def _relay_entries_missing_from_reference(self, role: str = "") -> List[Dict[str, str]]:
        reference_calls = {
            entry["callsign"]
            for entry in self._extract_unique_entries(self.reference_text.toPlainText())
            if entry["callsign"]
        }
        scope_key = self._exact_net_control_role(role) or self._current_roster_action_scope()
        rows = self._scope_filtered_rows(scope_key)
        missing: List[Dict[str, str]] = []
        for row in rows:
            callsign = str(row.get("callsign") or "").strip().upper()
            if not callsign or callsign in reference_calls:
                continue
            if self._roster_station_role(row) in {"NCS", "ANCS"}:
                continue
            missing.append(row)
        return missing

    def _reference_role_label_for_role(self, role: str = "") -> str:
        role_key = self._exact_net_control_role(role) or self._current_net_control_role()
        return "NCS" if role_key == "ANCS" else "ANCS"

    def _run_relay_compare(self) -> None:
        sections = self._roster_gap_sections()
        gap_text = self._roster_gap_text_for_sections(sections)
        gap_rows = [row for _title, rows in sections for row in rows]
        if gap_text:
            result_text = gap_text
        elif sections:
            result_text = "NCS and ANCS rosters match for this comparison."
        else:
            result_text = "Set or paste a partner roster reference to compare NCS/ANCS roster gaps."
        self.compare_results_card.set_title("Roster Gap")
        self.compare_results_card.set_text(result_text)
        self.compare_results_card.set_count(len(gap_rows))
        self._compare_missing_text = gap_text
        self._compare_reference_missing_entries = gap_rows
        self.compare_workspace_tabs.setCurrentWidget(self.compare_results_card)
        self._set_compare_workspace_expanded(True)
        self._update_roster_compare_status()

    def _run_inline_compare(self) -> None:
        defaults = self._workspace_compare_defaults()
        source_bucket = defaults.get("source_bucket_id", "tfc")
        target_bucket = defaults.get("target_bucket_id", "reference")
        role_key = self._current_net_control_role()
        if role_key in {"NCS", "ANCS"} and source_bucket in {"roster", "local_roster"} and target_bucket in {"reference", "ncs_reference", "ancs_reference"}:
            reference_label = self._reference_role_label_for_role(role_key)
            relay_rows = self._relay_entries_missing_from_reference(role_key)
            relay_text = self._roster_table_text_for_rows(relay_rows)
            result_text = "\n".join(
                [
                    f"Net Roster: {len(self._extract_unique_entries(self._roster_table_text()))}",
                    f"{reference_label} List: {len(self._extract_unique_entries(self.reference_text.toPlainText()))}",
                    "",
                    f"Missing from {reference_label}:",
                    relay_text if relay_text else "(none)",
                ]
            )
            self.compare_results_card.set_title("Roster Gap")
            self.compare_results_card.set_text(result_text)
            self.compare_results_card.set_count(len(relay_rows))
            self._compare_missing_text = relay_text
            self._compare_reference_missing_entries = relay_rows
            self.compare_workspace_tabs.setCurrentWidget(self.compare_results_card)
            return
        result_text, missing_text = self._workspace_compare_payload(source_bucket, target_bucket)
        self.compare_results_card.set_title("Compare Results")
        self.compare_results_card.set_text(result_text)
        self.compare_results_card.set_count(len(self._reference_entries_missing_from_roster()))
        self._compare_missing_text = missing_text
        self._compare_reference_missing_entries = self._reference_entries_missing_from_roster()
        self.compare_workspace_tabs.setCurrentWidget(self.compare_results_card)

    def _copy_compare_results(self) -> None:
        if not getattr(self, "_compare_missing_text", ""):
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self._compare_missing_text)

    def _import_reference_missing(self) -> None:
        if not self._require_active_net_for_checkin_add():
            return
        missing_entries = self._reference_entries_missing_from_roster()
        if not missing_entries:
            QMessageBox.information(self, "Reference Import", "No reference entries are missing from the net roster.")
            return
        source_label = self._reference_source_label()
        added = 0
        for entry in missing_entries:
            state, traffic, category = self._normalize_merge_entry_fields(entry)
            self._roster_append_row(
                entry["callsign"],
                entry["name"],
                state,
                traffic,
                category,
                source_label,
            )
            added += 1
        self._compare_reference_missing_entries = self._reference_entries_missing_from_roster()
        self._run_inline_compare()
        QMessageBox.information(self, "Reference Import", f"Added {added} reference entr{'y' if added == 1 else 'ies'} to the net roster.")

    def _merge_compare_missing(self) -> None:
        if not self._require_active_net_for_checkin_add():
            return
        missing_entries = self._reference_entries_missing_from_roster()
        if not missing_entries:
            QMessageBox.information(self, "Merge Missing", f"No entries from {self._reference_compare_label()} are missing from the net roster.")
            return
        source_label = self._reference_source_label()
        added = 0
        for entry in missing_entries:
            state, traffic, category = self._normalize_merge_entry_fields(entry)
            self._roster_append_row(
                entry["callsign"],
                entry["name"],
                state,
                traffic,
                category,
                source_label,
            )
            added += 1
        self._run_inline_compare()
        QMessageBox.information(self, "Merge Missing", f"Added {added} entr{'y' if added == 1 else 'ies'} from {source_label} to the net roster.")

    def _merge_review_candidates(self) -> None:
        if not self._require_active_net_for_checkin_add():
            return
        missing_entries = self._review_entries_missing_from_roster()
        if not missing_entries:
            QMessageBox.information(self, "Merge Reviewed", "No reviewed entries are missing from the net roster.")
            return
        added_calls = {entry["callsign"] for entry in missing_entries}
        added = 0
        for entry in missing_entries:
            state, traffic, category = self._normalize_merge_entry_fields(entry)
            self._roster_append_row(
                entry["callsign"],
                entry["name"],
                state,
                traffic,
                category,
                "Review",
            )
            added += 1
        remaining_lines = []
        for raw_line in self.review_card.text().splitlines():
            callsign, _name, _state = self._parse_checkin_line(raw_line)
            if (callsign or "").strip().upper() in added_calls:
                continue
            remaining_lines.append(raw_line)
        self.review_card.set_text("\n".join(remaining_lines).strip())
        self._run_inline_compare()
        QMessageBox.information(self, "Merge Reviewed", f"Added {added} reviewed entr{'y' if added == 1 else 'ies'} to the net roster.")

    def _apply_role_workspace(self, role: str) -> None:
        normalized = normalize_role(role)
        preset = get_role_workspace_preset(normalized)
        self._workspace_role_loading = True
        try:
            self.roster_frame.setVisible(True)
            self.roster_table.setColumnHidden(self.COL_CATEGORY, normalized == "JOINER")
            self.roster_table.setColumnHidden(self.COL_TFC_STATUS, normalized == "JOINER")
            self.roster_table.setColumnHidden(self.COL_HEARD, False)
            self.roster_table.setColumnHidden(self.COL_ACKED, False)
            self.roster_table.setColumnHidden(self.COL_ROLE, True)
            if normalized == "JOINER":
                self.copy_tfc_btn.setVisible(False)
                self.next_tfc_btn.setVisible(False)
                self.copy_qru_btn.setVisible(False)
                self.copy_late_btn.setVisible(False)
                self.copy_seen_locally_btn.setVisible(True)
                self.default_sort_btn.setVisible(False)
                self.relay_compare_btn.setVisible(False)
                self.copy_relays_btn.setVisible(False)
                self.copy_needs_sync_btn.setVisible(False)
            else:
                self.copy_tfc_btn.setVisible(True)
                self.next_tfc_btn.setVisible(True)
                self.copy_qru_btn.setVisible(True)
                self.copy_late_btn.setVisible(True)
                self.copy_seen_locally_btn.setVisible(False)
                self.default_sort_btn.setVisible(True)
                self.relay_compare_btn.setVisible(False)
                self.copy_relays_btn.setVisible(False)
                self.copy_needs_sync_btn.setVisible(True)
            self.tfc_card.set_title("Seen Locally" if normalized == "JOINER" else f"{normalized} / TFC")
            self.qru_card.set_title(f"{normalized} / QRU")
            self.late_card.set_title(f"{normalized} / LATE")
            self.reference_card.set_title("ANCS List" if normalized == "NCS" else "NCS List")

            visible_ids = set(preset.visible_bucket_ids())
            # Local category panels are no longer operator-facing UI. Keep them
            # hidden even though their text buffers are still used internally.
            self.tfc_card.setVisible(False)
            self.qru_card.setVisible(False)
            self.late_card.setVisible(False)
            self.reference_card.setVisible("ncs_reference" in visible_ids or "ancs_reference" in visible_ids)
            self.compare_results_card.setVisible(True)

            self.reference_card.set_read_only(False)
            self.reference_card.set_placeholder("Paste or edit the ANCS list here.")
            if normalized != "NCS":
                self.reference_card.set_placeholder("Paste or edit the NCS reference list here.")
            self.compare_results_card.set_read_only(True)
            self.compare_results_card.set_placeholder("Run Compare to list entries that can be merged into the net roster.")
            self.compare_results_card.set_count(len(self._reference_entries_missing_from_roster()))
            self.review_card.setVisible(False)
            self.review_card.set_read_only(False)
            self.review_card.set_placeholder("Review held candidates here.")
            self._workspace_bucket_defaults = {
                "source_bucket_id": "roster",
                "target_bucket_id": preset.compare_target_bucket_id,
            }
            self._workspace_visible_bucket_ids = set(visible_ids)
            self._workspace_visible_bucket_ids.add("roster")
            self._workspace_visible_bucket_ids.add("compare_results")
            if (not getattr(self, '_preparation_shell', False) or getattr(self, '_preparation_scope', None) is not None) and not getattr(self, '_restoring_preparation', False):
                self._refresh_custom_bucket_cards(normalized)
            self._refresh_known_action_band(normalized)
            self._sync_compare_workspace_tabs()
            self._update_bucket_card_states()
        finally:
            self._workspace_role_loading = False

    def _paste_into_reference_card(self) -> None:
        from PySide6.QtWidgets import QApplication

        self.reference_text.setPlainText(QApplication.clipboard().text())

    def _update_bucket_card_states(self) -> None:
        theme = resolve_theme(self.settings)
        roster_rows = self._roster_table_rows()
        self.roster_total_label.setText(f"Total Check-ins: {len(self._roster_unique_callsigns())}")
        self.roster_tfc_label.setText(f"TFC: {sum(1 for row in roster_rows if row['category'] == 'TFC')}")
        self.roster_qru_label.setText(f"QRU: {sum(1 for row in roster_rows if row['category'] == 'QRU')}")
        self.roster_late_label.setText(f"LATE: {sum(1 for row in roster_rows if row['category'] == 'LATE')}")
        self.tfc_card.set_count(self._workspace_line_count(self.tfc_card.text()))
        self.qru_card.set_count(self._workspace_line_count(self.qru_card.text()))
        self.late_card.set_count(self._workspace_line_count(self.late_card.text()))
        self.reference_card.set_count(self._workspace_line_count(self.reference_card.text()))
        self.compare_results_card.set_count(len(self._reference_entries_missing_from_roster()))
        self.review_card.set_count(self._workspace_line_count(self.review_card.text()))
        for bucket_id, card in self._custom_bucket_cards.items():
            card.set_count(self._workspace_line_count(card.text()))
        total = len(self._roster_unique_callsigns())
        self.total_checkins_label.setText(f"Total Check-ins: {total}")
        bucket_states = [
            (card, bool(card.text().strip()), "eligible_success" if bucket_id in {"tfc", "late"} else "eligible_info")
            for bucket_id, card in self._workspace_bucket_cards.items()
        ]
        for card, has_text, role in bucket_states:
            if hasattr(card, "copy_btn") and card.copy_btn is not None:
                card.copy_btn.setStyleSheet(button_style(role if has_text else "muted", theme))
        self._refresh_save_button_style()

    def _set_roster_dirty(self, dirty: bool) -> None:
        self._roster_dirty = bool(dirty)
        self._refresh_save_button_style()

    def _mark_roster_dirty(self) -> None:
        if self._roster_loading:
            return
        self._set_roster_dirty(True)

    def _refresh_save_button_style(self) -> None:
        if not hasattr(self, "save_btn"):
            return
        theme = resolve_theme(self.settings)
        self.save_btn.setStyleSheet(button_style("eligible_success" if self._roster_dirty else "muted", theme))

    def _set_net_button_styles(self, active: bool):
        """
        Update button highlight when a net is running.
        """
        theme = resolve_theme(self.settings)
        if active:
            self.start_btn.setStyleSheet(button_style("muted", theme))
            self._refresh_save_button_style()
            self.end_btn.setStyleSheet(button_style("eligible_danger", theme))
            self.ad_hoc_btn.setStyleSheet(button_style("muted", theme))
            self.start_btn.setEnabled(False)
            self.ad_hoc_btn.setEnabled(False)
        else:
            self.start_btn.setStyleSheet(button_style("eligible_success", theme))
            self._refresh_save_button_style()
            self.end_btn.setStyleSheet(button_style("muted", theme))
            self.ad_hoc_btn.setStyleSheet(button_style("eligible_info", theme))
            self.start_btn.setEnabled(True)
            self.ad_hoc_btn.setEnabled(True)
        self._update_partner_action_state()
        self._update_add_buttons_state()

    def _refresh_operator_history_views(self) -> None:
        try:
            win = self.window()
            if win and hasattr(win, "refresh_operator_history_views"):
                win.refresh_operator_history_views()
        except Exception:
            pass

    def on_settings_saved(self):
        if getattr(self, "_preparation_shell", False):
            return
        """
        Refresh QSY options when settings are saved.
        """
        self._maybe_reload_operating_groups()
        self._apply_theme()
        self._refresh_macro_profile_choices()

    def show_loading_toast(self) -> None:
        # NCS tabs do not use a loading banner/toast.
        return

    def on_tab_activated(self) -> None:
        if getattr(self, "_preparation_shell", False):
            return
        with perf_span("digi_ncs.on_tab_activated", settings=self.settings, min_ms=5.0):
            self._update_clock_labels()
            self._update_suspend_state()
            self._update_next_change_display()
            self._schedule_activation_secondary_refresh()

    def _schedule_activation_secondary_refresh(self) -> None:
        if self._activation_secondary_refresh_pending:
            return
        self._activation_secondary_refresh_pending = True
        QTimer.singleShot(120, self._run_activation_secondary_refresh)

    def _run_activation_secondary_refresh(self) -> None:
        if getattr(self, '_preparation_shell', False):
            self._activation_secondary_refresh_pending = False
            return
        if self._activation_secondary_refresh_inflight:
            self._activation_secondary_refresh_pending = False
            self._schedule_activation_secondary_refresh()
            return
        self._activation_secondary_refresh_pending = False
        self._activation_secondary_refresh_inflight = True
        try:
            self._maybe_reload_operating_groups()
            self._refresh_macro_profile_choices()
        finally:
            self._activation_secondary_refresh_inflight = False

    def set_tab_active(self, active: bool) -> None:
        receiver = getattr(self, '_prepared_receiver', None)
        if not active and receiver is not None:
            receiver.deactivate()
        self._active = bool(active)
        if active and receiver is not None:
            receiver.changed.emit()
        if getattr(self, '_preparation_shell', False) and not self._net_in_progress:
            return
        if self._active:
            if self._clock_timer and not self._clock_timer.isActive():
                self._clock_timer.start(1000)
            QTimer.singleShot(0, self.on_tab_activated)
            return
        if self._clock_timer and self._clock_timer.isActive() and not self._net_in_progress:
            self._clock_timer.stop()

    # ---------------- TIMERS & CLOCKS ---------------- #

    def _setup_timers(self):
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._on_timer_tick)
        self._log_assisted_timer = QTimer(self)
        self._log_assisted_timer.setInterval(5000)
        self._log_assisted_timer.timeout.connect(self._poll_log_assisted_intake)
        self._update_clock_labels()
        self._update_suspend_state()
        self._update_next_change_display()

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

    def _current_freq_mhz(self) -> Optional[float]:
        try:
            cur = self._current_scheduler_freq()
            if cur:
                return float(cur)
        except Exception:
            pass
        meta = self._selected_qsy_meta()
        try:
            if meta and meta.get("freq") is not None:
                return float(meta.get("freq"))
        except Exception:
            pass
        return None

    def _operating_group_for_freq(self, freq_mhz: Optional[float]) -> str:
        coordinator = getattr(self, '_prepared_receiver', None)
        prepared = getattr(coordinator, 'prepared', None)
        if prepared is not None:
            if coordinator.manual:
                return ''
            return str(prepared.request.source.values.values().get('group_name') or '').strip().upper()
        if not freq_mhz:
            return ""
        ops = self._load_operating_groups()
        for g in ops:
            try:
                fval = float(g.get("frequency", 0))
            except Exception:
                continue
            if abs(fval - float(freq_mhz)) <= 0.0005:
                name = (g.get("group") or g.get("group_name") or "").strip()
                if name:
                    return name.upper()
        return ""

    def _lookup_operator_groups(self, callsign: str) -> List[str]:
        cs = (callsign or "").strip().upper()
        if not cs:
            return []
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = Path(self.settings.db_path).parent / 'freqinout_nets.db' if getattr(self, '_preparation_scope', None) is not None else get_config_dir() / 'config' / 'freqinout_nets.db'
        except Exception:
            return []
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT group1, group2, group3, groups_json FROM operator_checkins WHERE callsign=?", (cs,))
            row = cur.fetchone()
            conn.close()
        except Exception:
            return []
        if not row:
            return []
        g1, g2, g3, gj = row
        groups: List[str] = []
        for g in (g1, g2, g3):
            val = (g or "").strip().upper()
            if val and val not in groups:
                groups.append(val)
        if gj:
            try:
                parsed = json.loads(gj)
                if isinstance(parsed, list):
                    for g in parsed:
                        val = (str(g) or "").strip().upper()
                        if val and val not in groups:
                            groups.append(val)
            except Exception:
                pass
        return groups

    def _refresh_qsy_options(self, og_list: Optional[List[Dict]] = None):
        """
        Build a unique frequency list from Operating Groups (auto-tune wins on duplicates).
        """
        ops = og_list if og_list is not None else self._load_operating_groups()
        self._opgroups_sig = self._snapshot_operating_groups(ops)
        self._qsy_options = build_qsy_options(ops)
        refresh_qsy_combo(self.qsy_combo, self._qsy_options)
        refresh_hold_duration_combo(self.hold_duration_combo, self.settings)
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

    def _on_timer_tick(self):
        self._update_clock_labels()
        self._update_next_change_display()

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
        UTC from system, local derived from SettingsManager timezone (via get_timezone),
        with a short UI label like ET/CT/MT/PT.
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

    # --------- Next frequency change display / countdown --------- #

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

    def _update_suspend_state(self, snapshot: Optional[Dict[str, object]] = None):
        try:
            if not self.hold_duration_combo.view().isVisible() and not self.hold_duration_combo.hasFocus():
                refresh_hold_duration_combo(self.hold_duration_combo, self.settings)
        except Exception:
            pass
        enabled = self._scheduler_enabled()
        self.suspend_btn.setEnabled(enabled)
        if not enabled:
            self._set_suspend_button(active=False)
            self._update_qsy_button_enabled()
            return

        if not isinstance(snapshot, dict):
            snapshot = suspend_snapshot(self.settings)
        if not snapshot.get("active"):
            if snapshot.get("until"):
                resume_schedule_hold(self.window(), self.settings)
            self._set_suspend_button(active=False)
            self._update_qsy_button_enabled()
            return

        self._set_suspend_button(active=True, remaining_sec=snapshot.get("remaining_sec"))
        self._update_qsy_button_enabled()

    def on_hold_state_changed(self, snapshot: Optional[Dict[str, object]] = None) -> None:
        self._update_suspend_state(snapshot=snapshot)

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

    def _apply_theme(self) -> None:
        theme = resolve_theme(self.settings)
        count_style = self._count_chip_style(theme)
        for label in (
            self.total_checkins_label,
            self.roster_total_label,
            self.roster_tfc_label,
            self.roster_qru_label,
            self.roster_late_label,
        ):
            label.setStyleSheet(count_style)
        self.start_btn.setStyleSheet(button_style("success", theme))
        self.end_btn.setStyleSheet(button_style("danger", theme))
        self._start_btn_default_style = self.start_btn.styleSheet()
        self._save_btn_default_style = button_style("success", theme)
        self.save_btn.setStyleSheet(self._save_btn_default_style)
        if hasattr(self, "help_btn"):
            self.help_btn.setStyleSheet(button_style("secondary", theme))
        self.suspend_btn.setStyleSheet(button_style("warning", theme))
        self._set_net_button_styles(self._net_in_progress)
        if hasattr(self, "roster_table"):
            self.roster_table.setStyleSheet(self._roster_table_style(theme))
        self._refresh_roster_side_chip_styles()
        self._refresh_tfc_status_cells()
        self._update_copy_buttons_state()
        self._apply_known_op_styles(theme)
        self._update_add_buttons_state()
        self._set_setup_details_expanded(self._setup_details_expanded)
        self._refresh_ncs_session_context()

    def apply_theme(self) -> None:
        self._apply_theme()

    def _open_context_help(self, context_key: str) -> None:
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_context_help"):
            try:
                host.open_context_help(context_key)
            except Exception:
                pass

    def _on_suspend_clicked(self):
        scope = getattr(self, '_preparation_scope', None)
        if scope is not None:
            self._prepared_receiver._set_status('Prepared', f'Use {scope.radio_name} in the Station Control Bar to QSY or set a hold')
            return
        if self._suspend_active():
            # Resume immediately
            resume_schedule_hold(self.window(), self.settings)
            self._set_suspend_button(active=False)
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
            self._set_suspend_button(active=True, remaining_sec=snapshot.get("remaining_sec"))
            QMessageBox.information(
                self,
                "QSY Applied",
                f"Frequency changed and scheduling paused for {mins} minutes.",
            )

    def _compute_next_change_utc(self) -> Optional[datetime.datetime]:
        """
        Ask scheduler_engine for the next scheduled frequency change time (UTC).
        """
        try:
            from freqinout.core.scheduler_engine import compute_next_change_time as cnext
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            hf_active, net_active = self._active_schedule_entries(now_utc)
            dt = cnext(now_utc, hf_active, net_active)
        except Exception as e:
            log.error("FldigiNetControl: compute_next_change_time failed: %s", e)
            return None

        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        else:
            dt = dt.astimezone(datetime.timezone.utc)
        return dt

    def _active_schedule_entries(
        self, now_utc: Optional[datetime.datetime] = None
    ) -> tuple[Optional[Dict], Optional[Dict]]:
        """
        Return (hf_active, net_active) entries for the current UTC time.
        Prefers the newer hf_schedule key but falls back to daily_schedule.
        """
        if now_utc is None:
            now_utc = datetime.datetime.now(datetime.timezone.utc)

        data = self.settings.all()
        hf_sched = data.get("hf_schedule") or data.get("daily_schedule") or []
        net_sched = data.get("net_schedule") or []
        if not isinstance(hf_sched, list):
            hf_sched = []
        if not isinstance(net_sched, list):
            net_sched = []

        weekday_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][now_utc.weekday()]
        prev_day = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"][(now_utc.weekday() - 1) % 7]
        now_min = now_utc.hour * 60 + now_utc.minute

        def parse_minutes(text: str) -> Optional[int]:
            txt = (text or "").strip()
            if not txt:
                return None
            try:
                h, m = [int(x) for x in txt.split(":")]
                if 0 <= h <= 23 and 0 <= m <= 59:
                    return h * 60 + m
            except Exception:
                return None
            return None

        hf_active = None
        hf_best_start = -1
        for row in hf_sched:
            try:
                day = (row.get("day_utc") or "ALL").strip()
                smin = parse_minutes(row.get("start_utc", ""))
                emin = parse_minutes(row.get("end_utc", ""))
                if smin is None or emin is None:
                    continue
                overnight = smin > emin
                active = False
                if day.upper() == "ALL" or day == weekday_name:
                    active = smin <= now_min < emin if not overnight else (now_min >= smin or now_min < emin)
                elif overnight and day == prev_day:
                    active = now_min < emin
                if active and smin > hf_best_start:
                    hf_best_start = smin
                    hf_active = row
            except Exception:
                continue

        net_active = None
        net_best_start = -1
        for row in net_sched:
            try:
                day = (row.get("day_utc") or "").strip()
                smin = parse_minutes(row.get("start_utc", ""))
                emin = parse_minutes(row.get("end_utc", ""))
                if smin is None or emin is None:
                    continue
                early = int(row.get("early_checkin", 0) or 0)
                window_start = max(0, smin - early)
                overnight = smin > emin
                active = False
                if day == weekday_name:
                    active = window_start <= now_min < emin if not overnight else (now_min >= window_start or now_min < emin)
                elif overnight and day == prev_day:
                    active = now_min < emin
                if active and smin > net_best_start:
                    net_best_start = smin
                    net_active = row
            except Exception:
                continue

        return hf_active, net_active

    def _current_schedule_entry(
        self, now_utc: Optional[datetime.datetime] = None
    ) -> tuple[str, Optional[Dict]]:
        """
        Return (source, entry) where source is NET/HF/NONE.
        """
        hf_active, net_active = self._active_schedule_entries(now_utc)
        if net_active:
            return "NET", net_active
        if hf_active:
            return "HF", hf_active
        return "NONE", None

    def _format_current_band(self, now_utc: Optional[datetime.datetime] = None) -> str:
        source, entry = self._current_schedule_entry(now_utc)
        if not entry:
            return "Current Band: (none active)"
        band = (entry.get("band") or "").strip()
        freq = (entry.get("frequency") or "").strip()
        details = " ".join([p for p in (band, freq) if p]).strip()
        return f"Current Band: {details or '(unknown)'}"

    def _next_net_occurrence(self, row: Dict, now: datetime.datetime) -> Optional[Dict]:
        from freqinout.core.net_schedule_occurrences import next_net_occurrence
        return next_net_occurrence(row, now)

    def _format_next_net_summary(self) -> str:
        occ = self._next_net_from_schedule()
        if not occ:
            return "(none scheduled)"
        row = occ["row"]
        start_dt = occ.get("start_dt")
        net_name = (row.get("net_name") or "").strip()
        band = (row.get("band") or "").strip()
        freq = (row.get("frequency") or "").strip()
        parts = [net_name, band, freq]
        if isinstance(start_dt, datetime.datetime):
            parts.append(start_dt.strftime("%a %H:%M UTC"))
        summary = " - ".join([p for p in parts if p])
        return summary or "(none scheduled)"

    def _update_next_change_display(self):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        theme = resolve_theme(self.settings)
        muted = theme.get("text_muted", theme.get("text", "#1C1F21"))
        accent = theme.get("accent", theme.get("text", "#1C1F21"))
        base_style = f"QLabel {{ color: {muted}; padding: 2px 4px; }}"
        alert_style = f"QLabel {{ color: {accent}; padding: 2px 4px; font-weight: 700; }}"

        # If schedule is suspended, show resume info and skip change handling
        if self._suspend_active():
            su = self._get_suspend_until()
            resume_str = su.strftime("%H:%M UTC") if su else ""
            suspended_text = f"Next Scheduled Net: (suspended until {resume_str})"
            suspended_text = f"{suspended_text} : {self._format_current_band(now_utc)}"
            self.next_change_label.setText(f"Suspended until {resume_str or 'set time'}")
            self.next_change_label.setToolTip(suspended_text)
            self.next_change_label.setStyleSheet(alert_style)
            return

        # Refresh next_change_utc if we don't have one or it's in the past
        if self._next_change_utc is None or self._next_change_utc <= now_utc:
            self._next_change_utc = self._compute_next_change_utc()
            self._auto_end_done = False  # reset flag when we get a new target

        delta_sec = None
        if self._next_change_utc:
            delta_sec = (self._next_change_utc - now_utc).total_seconds()

        next_net_text = self._format_next_net_summary()
        current_band_text = self._format_current_band(now_utc)
        display_text = f"Next Scheduled Net: {next_net_text} : {current_band_text}"
        compact_current = current_band_text.replace("Current Band:", "Now:").strip()
        compact_next = next_net_text.replace(" - ", " ").strip()
        self.next_change_label.setText(f"Next: {compact_next} | {compact_current}")
        self.next_change_label.setToolTip(display_text)
        self.next_change_label.setStyleSheet(base_style)

        # Auto end net exactly at change time if not paused
        if (
            delta_sec is not None
            and delta_sec <= 0
            and self._net_in_progress
            and not self._suspend_active()
            and not self._auto_end_done
        ):
            self._auto_end_done = True
            log.info("FldigiNetControl: auto-ending net for scheduled frequency change.")
            self._end_net()
            # Next scheduler tick will pick up any new next_change_utc

    def _effective_fldigi_log_root(self) -> Optional[Path]:
        raw_value = str(self.settings.get("fldigi_log_path", "") or "").strip()
        if not raw_value:
            return None
        path = Path(raw_value).expanduser()
        if path.exists():
            return path if path.is_dir() else path.parent
        if path.suffix.lower() == ".log" or "log" in path.name.lower():
            return path.parent
        return path

    def _macro_profiles_dir(self) -> Optional[Path]:
        root = self._effective_fldigi_log_root()
        if root is None:
            return None
        return root / "macros"

    def _discover_macro_profile_paths(self) -> list[str]:
        macros_dir = self._macro_profiles_dir()
        if macros_dir is None or not macros_dir.exists() or not macros_dir.is_dir():
            return []
        paths = [self._normalize_macro_profile_path(str(path)) for path in macros_dir.glob("*.mdf") if path.is_file()]
        return sorted({path for path in paths if path}, key=lambda item: Path(item).name.lower())

    def _refresh_macro_profile_choices(self, *_args, select_path: Optional[str] = None) -> None:
        selected = self._normalize_macro_profile_path(select_path or self._selected_macro_profile_path())
        discovered = self._discover_macro_profile_paths()
        current_items = ["Select macro set"]
        current_data = [""]
        for path in discovered:
            current_items.append(Path(path).stem or Path(path).name)
            current_data.append(path)
        if selected and selected not in discovered:
            current_items.append(f"{Path(selected).stem or Path(selected).name} (custom)")
            current_data.append(selected)
        self._macro_profile_combo_loading = True
        try:
            self.macro_profile_combo.clear()
            for label, data in zip(current_items, current_data):
                self.macro_profile_combo.addItem(label, data)
            index = self.macro_profile_combo.findData(selected)
            if index < 0:
                index = 0
            self.macro_profile_combo.setCurrentIndex(index)
        finally:
            self._macro_profile_combo_loading = False
        self.macro_profile_combo.setToolTip(selected or "Select a discovered macro set from the FLDigi macros directory.")
        self._refresh_setup_summary()

    def _on_macro_profile_combo_changed(self, index: int) -> None:
        if self._macro_profile_combo_loading or index < 0:
            return
        path = str(self.macro_profile_combo.itemData(index) or "").strip()
        if not path:
            if self._selected_macro_profile_path():
                self._clear_macro_profile()
            return
        if path == self._normalize_macro_profile_path(self._selected_macro_profile_path()):
            return
        self._set_macro_profile_text(path)
        self._save_macro_profile_selection(path, refresh_metadata=True)

    def _refresh_setup_summary(self) -> None:
        if getattr(self, '_preparation_shell', False) and getattr(self, '_preparation_scope', None) is None:
            if hasattr(self, 'macro_profile_status'):
                self.macro_profile_status.setText('Select a net and radio to review local setup.')
            return
        if not hasattr(self, "macro_profile_details_btn"):
            return
        mode = self._macro_profile_mode()
        record = self._macro_profile_record(self._selected_macro_profile_path())
        mapping_count = len(record.get("mappings", [])) if isinstance(record.get("mappings", []), list) else 0
        selected_path = self._selected_macro_profile_path()
        selected_name = Path(selected_path).stem if selected_path else ""
        needs_mapping = bool(selected_path and mode != "mapped")
        if not self._selected_macro_profile_path():
            header_text = "Macro: None"
        elif mode == "mapped":
            header_text = f"Macro: Mapped - {selected_name}" if selected_name else "Macro: Mapped"
        elif mapping_count:
            header_text = f"Macro: Needs Mapping - {selected_name}" if selected_name else "Macro: Needs Mapping"
        else:
            header_text = f"Macro: Needs Mapping - {selected_name}" if selected_name else "Macro: Needs Mapping"
        self.macro_profile_details_btn.setText(header_text)
        self.macro_profile_details_btn.setStyleSheet(self._macro_header_style())
        if hasattr(self, "macro_setup_controls"):
            self.macro_setup_controls.setVisible(not selected_path or needs_mapping)
        if hasattr(self, "macro_profile_edit_mappings_btn"):
            self.macro_profile_edit_mappings_btn.setVisible(bool(selected_path))
            self.macro_profile_edit_mappings_btn.setEnabled(bool(selected_path))
        self._refresh_macro_mapping_locations()

    def _macro_mapping_locations_text(self) -> str:
        selected = self._normalize_macro_profile_path(self._selected_macro_profile_path())
        record = self._macro_profile_record(selected)
        mappings = record.get("mappings")
        active_mappings = [
            mapping for mapping in mappings if self._macro_profile_mapping_is_complete(mapping)
        ] if isinstance(mappings, list) else []
        if active_mappings:
            lines = ["Mapped macro files:"]
            for mapping in active_mappings:
                lines.append(self._macro_mapping_location_line(mapping))
            return "\n".join(lines)

        checkin_dir = self._resolve_checkin_dir()
        default_paths = [
            (label, checkin_dir / filename)
            for label, filename in CURRENT_CHECKIN_FILE_NAMES.items()
        ]
        for role in ("NCS", "ANCS"):
            default_paths.extend(
                (f"{role}_{label}", checkin_dir / filename)
                for label, filename in ROLE_CHECKIN_FILE_NAMES[role].items()
            )
        lines = ["Default macro check-in files:"]
        lines.extend(f"{label}: {path}" for label, path in default_paths)
        lines.append(f"Archive: {checkin_dir / 'archive'}")
        if selected:
            lines.append("Mapped macro files: none active.")
            return "\n".join(lines)
        return "\n".join(lines)

    @staticmethod
    def _macro_mapping_location_line(mapping: Dict[str, object]) -> str:
        function = str(mapping.get("function") or "").strip().upper() or "CUSTOM"
        if function == "CUSTOM":
            function = str(mapping.get("custom_name") or "").strip() or "CUSTOM"
        scope = str(mapping.get("scope") or "").strip().upper()
        macro_label = str(mapping.get("macro_label") or "").strip()
        macro_id = str(mapping.get("macro_id") or "").strip()
        source_file = str(mapping.get("source_file") or "").strip()
        label = f"{scope} {function}".strip()
        macro_bits = " / ".join(bit for bit in (macro_label, macro_id) if bit)
        if macro_bits:
            label = f"{label} ({macro_bits})"
        return f"{label}: {source_file or '(macro-only mapping)'}"

    def _refresh_macro_mapping_locations(self) -> None:
        if hasattr(self, "macro_mapping_locations_label"):
            self.macro_mapping_locations_label.setText(self._macro_mapping_locations_text())

    def _macro_action_file_is_mapped(self, function: str, filename: str) -> bool:
        selected = self._normalize_macro_profile_path(self._selected_macro_profile_path())
        record = self._macro_profile_record(selected)
        mappings = record.get("mappings")
        wanted_function = str(function or "").strip().upper()
        wanted_filename = str(filename or "").strip().casefold()
        if not isinstance(mappings, list):
            return False
        for mapping in mappings:
            if not self._macro_profile_mapping_is_complete(mapping):
                continue
            mapped_function = str(mapping.get("function") or "").strip().upper()
            source_name = macro_mapping_path_leaf(mapping.get("source_file") or "").casefold()
            if mapped_function == wanted_function or source_name == wanted_filename:
                return True
        return False

    # ---------------- SETTINGS LOAD ---------------- #

    def _load_settings(self):
        self._role_workspace_prefs = load_role_workspace_prefs(self.settings)
        self._load_macro_profile_state()
        self._resolve_checkin_dir()
        self._refresh_macro_profile_choices()

        self._populate_net_name_from_schedule()
        self._update_net_name_min_width()
        self._refresh_ncs_session_context()

    def _macro_profile_store(self) -> Dict[str, Dict[str, object]]:
        data = self.settings.all()
        store = data.get(self.FLDIGI_MACRO_PROFILES_KEY, {})
        return store if isinstance(store, dict) else {}

    def _selected_macro_profile_path(self) -> str:
        return str(self.settings.get(self.FLDIGI_SELECTED_MACRO_PROFILE_KEY, "") or "").strip()

    def _normalize_macro_profile_path(self, path: str) -> str:
        text = str(path or "").strip()
        if not text:
            return ""
        try:
            return str(Path(text).expanduser().resolve())
        except Exception:
            return str(Path(text).expanduser().absolute())

    def _canonical_macro_profile_store(self) -> Dict[str, Dict[str, object]]:
        store = self._macro_profile_store()
        normalized: Dict[str, Dict[str, object]] = {}
        changed = False
        for raw_path, record in store.items():
            canon_path = self._normalize_macro_profile_path(raw_path)
            if not canon_path:
                continue
            if canon_path != raw_path:
                changed = True
            normalized[canon_path] = dict(record) if isinstance(record, dict) else {}
        if changed:
            self.settings.set(self.FLDIGI_MACRO_PROFILES_KEY, normalized)
        return normalized

    def _macro_profile_record(self, path: str) -> Dict[str, object]:
        canonical = self._normalize_macro_profile_path(path)
        if not canonical:
            return {}
        store = self._canonical_macro_profile_store()
        record = store.get(canonical, {})
        return dict(record) if isinstance(record, dict) else {}

    def _macro_profile_metadata(self, path: str) -> Dict[str, object]:
        canonical = self._normalize_macro_profile_path(path)
        if not canonical:
            return {}
        return scan_macro_profile(canonical)

    def _macro_profile_mapping_is_complete(self, mapping: Dict[str, object]) -> bool:
        if not isinstance(mapping, dict):
            return False
        if not mapping.get("enabled"):
            return False
        if bool(mapping.get("read_only")):
            return False
        scope = str(mapping.get("scope", "") or "").strip()
        function = str(mapping.get("function", "") or "").strip()
        source_file = str(mapping.get("source_file", "") or "").strip()
        macro_id = str(mapping.get("macro_id", "") or "").strip()
        if not scope or not function:
            return False
        if not source_file and not macro_id:
            return False
        return True

    def _macro_profile_has_enabled_mappings(self, record: Dict[str, object]) -> bool:
        mappings = record.get("mappings")
        if not isinstance(mappings, list):
            return False
        for mapping in mappings:
            if self._macro_profile_mapping_is_complete(mapping):
                return True
        return False

    def _macro_profile_mode(self) -> str:
        selected = self._normalize_macro_profile_path(self._selected_macro_profile_path())
        if not selected:
            return "legacy"
        if not Path(selected).exists():
            return "legacy"
        record = self._macro_profile_record(selected)
        return "mapped" if self._macro_profile_has_enabled_mappings(record) else "legacy"

    def _format_macro_profile_timestamp(self, value: object) -> str:
        try:
            dt = datetime.datetime.fromtimestamp(float(value))
        except Exception:
            return ""
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _macro_profile_status_text(self) -> str:
        selected = self._normalize_macro_profile_path(self._selected_macro_profile_path())
        if not selected:
            return "No macro set selected."
        p = Path(selected)
        record = self._macro_profile_record(selected)
        display_name = str(record.get("profile_name") or p.stem or p.name or selected)
        detected_macros = record.get("detected_macros", [])
        meta_bits = []
        if isinstance(detected_macros, list) and detected_macros:
            refs = count_detected_file_references(record)
            meta_bits.append(f"{len(detected_macros)} macros")
            meta_bits.append(f"{refs} file refs")
        meta_suffix = f" ({', '.join(meta_bits)})" if meta_bits else ""
        if not p.exists():
            return f"Selected macro file not found.\nProfile: {display_name}{meta_suffix}"
        enabled = self._macro_profile_has_enabled_mappings(record)
        total_mappings = len(record.get("mappings", [])) if isinstance(record.get("mappings", []), list) else 0
        if enabled:
            mapping_count = len(
                [m for m in record.get("mappings", []) if self._macro_profile_mapping_is_complete(m)]
            )
            return f"Mapped mode ready.\nProfile: {display_name}{meta_suffix}\nActive mappings: {mapping_count}"
        if total_mappings:
            return (
                "Macro set selected. Mapped mode activates when at least one mapping is enabled and complete.\n"
                f"Profile: {display_name}{meta_suffix}\nSaved mappings: {total_mappings}"
            )
        return f"Macro set selected.\nProfile: {display_name}{meta_suffix}\nNo mappings saved yet."

    def _set_macro_profile_text(self, path: str) -> None:
        self._macro_profile_loading = True
        try:
            self.macro_profile_edit.setText(path)
        finally:
            self._macro_profile_loading = False
        self._refresh_macro_profile_choices(select_path=path)

    def _save_macro_profile_selection(self, path: str, *, refresh_metadata: bool = True) -> None:
        canonical = self._normalize_macro_profile_path(path)
        if not canonical:
            self.settings.set(self.FLDIGI_SELECTED_MACRO_PROFILE_KEY, "")
            self.macro_profile_status.setText(self._macro_profile_status_text())
            self._refresh_macro_profile_choices(select_path="")
            self._refresh_custom_bucket_cards()
            self._refresh_setup_summary()
            return
        store = self._canonical_macro_profile_store()
        record = dict(store.get(canonical, {}))
        metadata = self._macro_profile_metadata(canonical) if refresh_metadata else {"profile_path": canonical}
        record.update(metadata)
        store[canonical] = record
        self.settings.set(self.FLDIGI_MACRO_PROFILES_KEY, store)
        self.settings.set(self.FLDIGI_SELECTED_MACRO_PROFILE_KEY, canonical)
        self.macro_profile_status.setText(self._macro_profile_status_text())
        self._refresh_macro_profile_choices(select_path=canonical)
        self._refresh_custom_bucket_cards()
        self._refresh_setup_summary()

    def _load_macro_profile_state(self) -> None:
        selected = self._normalize_macro_profile_path(self._selected_macro_profile_path())
        if selected:
            self._set_macro_profile_text(selected)
            if Path(selected).exists():
                self._save_macro_profile_selection(selected, refresh_metadata=True)
            else:
                self.macro_profile_status.setText(self._macro_profile_status_text())
                self._refresh_setup_summary()
            return
        self._set_macro_profile_text("")
        self.macro_profile_status.setText(self._macro_profile_status_text())
        self._refresh_setup_summary()

    def _choose_macro_profile(self) -> None:
        fn, _ = QFileDialog.getOpenFileName(
            self,
            "Select FLDigi macro profile",
            str(Path(self._selected_macro_profile_path()).parent) if self._selected_macro_profile_path() else "",
            "FLDigi macro files (*.mdf);;All Files (*)",
        )
        if not fn:
            return
        self._set_macro_profile_text(fn)
        self._save_macro_profile_selection(fn, refresh_metadata=True)

    def _clear_macro_profile(self) -> None:
        self._set_macro_profile_text("")
        self._save_macro_profile_selection("", refresh_metadata=False)

    def _on_macro_profile_editing_finished(self) -> None:
        if self._macro_profile_loading:
            return
        path = self.macro_profile_edit.text().strip()
        if not path:
            self._clear_macro_profile()
            return
        canonical = self._normalize_macro_profile_path(path)
        if not canonical:
            self._clear_macro_profile()
            return
        if not Path(canonical).exists():
            QMessageBox.warning(
                self,
                "Macro Profile Not Found",
                "The selected macro profile file does not exist. The previous selection has been kept.",
            )
            self._set_macro_profile_text(self._selected_macro_profile_path())
            return
        self._set_macro_profile_text(canonical)
        self._save_macro_profile_selection(canonical, refresh_metadata=True)

    def _open_macro_mapping_dialog(self) -> None:
        profile_path = self._selected_macro_profile_path()
        if not profile_path:
            QMessageBox.information(self, "Macro Discovery & Mapping", "Select a macro profile first.")
            return
        dialog = FldigiMacroMappingDialog(self.settings, profile_path, self)
        if dialog.exec() == QDialog.Accepted:
            self._load_macro_profile_state()
            self.macro_profile_status.setText(self._macro_profile_status_text())
            self._refresh_setup_summary()

    # ---------------- KNOWN OPERATORS FROM DB ---------------- #

    def _load_known_operators(self):
        """
        Load known operators from the SQLite DB (operator_checkins table) and
        hook them into a QCompleter for the 'known_op_edit' field.

        Format for suggestions: "CALLSIGN NAME STATE"
        """
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = Path(self.settings.db_path).parent / 'freqinout_nets.db' if getattr(self, '_preparation_scope', None) is not None else get_config_dir() / 'config' / 'freqinout_nets.db'
        except Exception as e:
            log.error("Unable to resolve DB path for known operators: %s", e)
            return

        if not db_path.exists():
            return

        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT callsign, IFNULL(name,''), IFNULL(state,'') "
                "FROM operator_checkins ORDER BY callsign ASC"
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log.error("Failed to load known operators from DB: %s", e)
            return

        suggestions: List[str] = []
        self._known_operator_rows = []
        self._known_operator_by_callsign = {}
        for callsign, name, state in rows:
            cs = (callsign or "").strip().upper()
            nm = (name or "").strip()
            st = (state or "").strip().upper()
            if not cs:
                continue
            parts = [cs]
            if nm:
                parts.append(nm)
            if st:
                parts.append(st)
            display = " ".join(parts)
            suggestions.append(display)
            row = {"callsign": cs, "name": nm, "state": st, "display": display}
            self._known_operator_rows.append(row)
            self._known_operator_by_callsign[cs] = row

        self._install_known_operator_completers(suggestions)

    def _build_known_operator_completer(self, suggestions: List[str], parent=None) -> Optional[QCompleter]:
        if not suggestions:
            return None
        completer = QCompleter(sorted(suggestions), parent or self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        return completer

    def _install_known_operator_completers(self, suggestions: List[str]) -> None:
        self.known_op_edit.setCompleter(self._build_known_operator_completer(suggestions, self.known_op_edit))
        for field in (
            getattr(self, "partner_primary_edit", None),
            getattr(self, "joiner_ncs_edit", None),
            getattr(self, "joiner_ancs_edit", None),
        ):
            if field is not None:
                field.setCompleter(self._build_known_operator_completer(suggestions, field))

    def _matching_known_operators(self, query: str) -> List[Dict[str, str]]:
        needle = (query or "").strip().lower()
        if not needle:
            return []
        matches = []
        for row in self._known_operator_rows:
            if needle in row.get("display", "").lower():
                matches.append(row)
        return matches

    def _resolve_known_operator_callsign(self, text: str) -> tuple[str, Optional[Dict[str, str]], str]:
        raw = (text or "").strip()
        if not raw:
            return "", None, "empty"
        token_match = re.search(r"[A-Za-z0-9/]+", raw)
        token = (token_match.group(0) if token_match else raw).strip().upper()
        if not token:
            return "", None, "empty"
        exact = self._known_operator_by_callsign.get(token)
        if exact:
            return exact.get("callsign", token), exact, "exact"
        matches = self._matching_known_operators(raw)
        unique_by_call: Dict[str, Dict[str, str]] = {}
        for row in matches:
            cs = (row.get("callsign") or "").strip().upper()
            if cs:
                unique_by_call[cs] = row
        if len(unique_by_call) == 1:
            cs, row = next(iter(unique_by_call.items()))
            return cs, row, "unique"
        if len(unique_by_call) > 1:
            return token, None, "ambiguous"
        return token, None, "manual"

    def _resolve_role_callsign_from_field(self, field: QLineEdit) -> tuple[str, str]:
        callsign, _row, status = self._resolve_known_operator_callsign(field.text())
        if callsign and status in {"exact", "unique"}:
            field.setText(callsign)
        return callsign, status

    def _extract_extra_after_tokens(self, text: str, *, min_tokens: int) -> str:
        matches = list(re.finditer(r"[A-Za-z0-9]+", text))
        if len(matches) < min_tokens:
            return ""
        end = matches[min_tokens - 1].end()
        return text[end:].strip()

    def _split_checkin_with_extra(self, line: str) -> tuple[str, str, str, str]:
        raw = self._strip_inline_review_context(line)
        if not raw:
            return "", "", "", ""
        if "/" in raw and not raw.startswith("#"):
            parts = [p.strip() for p in raw.split("/") if p.strip()]
            if len(parts) >= 3:
                cs = parts[0].split()[0].upper()
                name = parts[1]
                state_tokens = parts[2].split()
                state = state_tokens[0].upper() if state_tokens else ""
                trailing = " ".join(state_tokens[1:]).strip()
                extra_parts = []
                if trailing:
                    extra_parts.append(trailing)
                if len(parts) > 3:
                    extra_parts.extend(parts[3:])
                extra = " / ".join(part for part in extra_parts if part).strip()
                return cs, name, state, extra
        matches = list(re.finditer(r"[A-Za-z0-9]+", raw))
        if not matches:
            return "", "", "", ""
        cs = matches[0].group(0).upper()
        name = matches[1].group(0) if len(matches) > 1 else ""
        state = matches[2].group(0).upper() if len(matches) > 2 else ""
        extra = self._extract_extra_after_tokens(raw, min_tokens=3)
        return cs, name, state, extra

    def _apply_known_autofill(self) -> bool:
        text = self.known_op_edit.text().strip()
        if not text:
            return False
        matches = self._matching_known_operators(text)
        if not matches:
            comp = self.known_op_edit.completer()
            if comp:
                comp.setCompletionPrefix(text)
                model = comp.completionModel()
                if model and model.rowCount() == 1:
                    completion = model.index(0, 0).data()
                    if completion:
                        matches = self._matching_known_operators(str(completion))
        row = matches[0] if len(matches) == 1 else None
        if row is None:
            tokens = list(re.finditer(r"[A-Za-z0-9]+", text))
            if tokens:
                cs = tokens[0].group(0).upper()
                row = self._known_operator_by_callsign.get(cs)
        if row is None:
            return False
        tokens = list(re.finditer(r"[A-Za-z0-9]+", text))
        if len(tokens) >= 3:
            extra = self._extract_extra_after_tokens(text, min_tokens=3)
        else:
            extra = self._extract_extra_after_tokens(text, min_tokens=1)
        formatted = self._format_entry(row["callsign"], row["name"], row["state"])
        new_text = f"{formatted} {extra}".strip() if extra else formatted
        self.known_op_edit.setText(new_text)
        self._known_op_autofilled_prefix = formatted
        self._known_op_autofill_consumed = True
        self._known_op_tab_stage = 1
        self.known_op_edit.setFocus()
        self.known_op_edit.setCursorPosition(len(new_text))
        return True

    def _focus_add_button(self, target: str) -> None:
        button = self._known_add_buttons.get(target)
        if button is not None:
            button.setFocus()

    def eventFilter(self, obj, event):
        if obj in self._known_add_button_targets and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                self._insert_known_into_bucket(self._known_add_button_targets[obj])
                event.accept()
                return True
        if obj is self.known_op_edit and event.type() == QEvent.FocusIn:
            if self._known_op_autofilled_prefix and self.known_op_edit.text().startswith(
                self._known_op_autofilled_prefix
            ):
                self._known_op_tab_stage = 1
            else:
                self._known_op_tab_stage = 0
            return False
        if obj is self.known_op_edit and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Tab, Qt.Key_Return, Qt.Key_Enter):
                if self._known_op_pending_focus:
                    target = self._known_op_pending_focus
                    self._known_op_pending_focus = None
                    QTimer.singleShot(0, lambda t=target: self._focus_add_button(t))
                    event.accept()
                    return True
        if obj is self.known_op_edit and event.type() == QEvent.ShortcutOverride:
            if event.key() in (Qt.Key_Tab, Qt.Key_Return, Qt.Key_Enter):
                has_prefix = self._known_op_autofilled_prefix and self.known_op_edit.text().startswith(
                    self._known_op_autofilled_prefix
                )
                if has_prefix and self._known_op_tab_stage == 1:
                    self._known_op_pending_focus = "tfc"
                    self._known_op_tab_stage = 2
                    event.accept()
                    return True
                if has_prefix and self._known_op_tab_stage >= 2:
                    self._known_op_pending_focus = "late"
                    event.accept()
                    return True
                if self._apply_known_autofill():
                    QTimer.singleShot(0, self.known_op_edit.setFocus)
                    event.accept()
                    return True
        if obj in self._known_add_button_targets and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                self._insert_known_into_bucket(self._known_add_button_targets[obj])
                event.accept()
                return True
        return super().eventFilter(obj, event)

    def _on_known_op_return(self) -> None:
        if self._apply_known_autofill():
            return
        text = self.known_op_edit.text().strip()
        cs, name, state = self._parse_checkin_line(text)
        if cs and name and state:
            formatted = self._format_entry(cs, name, state)
            self.known_op_edit.setText(formatted)

    def _on_known_op_text_changed(self) -> None:
        if (
            self._known_op_autofilled_prefix
            and not self.known_op_edit.text().startswith(self._known_op_autofilled_prefix)
        ):
            self._known_op_autofilled_prefix = ""
            self._known_op_autofill_consumed = False
            self._known_op_tab_stage = 0
        self._update_add_buttons_state()

    def _update_copy_buttons_state(self) -> None:
        self._update_bucket_card_states()
        self._update_roster_compare_status()
        if hasattr(self, "copy_state_summary_btn"):
            rows_available = bool(self._attendance_rows_for_state_summary())
            post_net_available = bool(self._net_end_utc) and not bool(self._net_in_progress)
            visible = post_net_available and rows_available
            self.copy_state_summary_btn.setVisible(visible)
            self.copy_state_summary_btn.setEnabled(visible)
            if hasattr(self, "post_net_actions_label"):
                self.post_net_actions_label.setVisible(visible)

    def _update_add_buttons_state(self) -> None:
        theme = resolve_theme(self.settings)
        net_active = bool(self._net_in_progress)
        if hasattr(self, "known_op_edit"):
            self.known_op_edit.setEnabled(net_active)
            self.known_op_edit.setToolTip(
                "" if net_active else "Start the net before entering check-ins."
            )
        text = self.known_op_edit.text().strip()
        cs, name, state = self._parse_checkin_line(text)
        ready = bool(cs and name and state)
        for target, button in self._known_add_buttons.items():
            if not button.isVisible():
                continue
            role = {"tfc": "success", "qru": "info", "late": "warning", "seen_locally": "success"}.get(target, "info")
            button.setEnabled(net_active)
            button.setToolTip(
                "" if net_active else "Start the net before adding check-ins to the roster."
            )
            button.setStyleSheet(self._known_action_button_style(role, theme, ready and net_active))

    def _apply_known_op_styles(self, theme: Dict[str, str]) -> None:
        focus = theme.get("focus", "#7FB5FF")
        border = theme.get("border", "#D3D7DD")
        surface = theme.get("surface", "#F0F2F4")
        text = theme.get("text", "#1C1F21")
        self.known_op_edit.setStyleSheet(
            "QLineEdit {"
            f" background-color: {surface}; color: {text}; border: 1px solid {border};"
            " border-radius: 4px; padding: 4px 6px;"
            " }"
            " QLineEdit:focus {"
            f" border: 2px solid {focus}; padding: 3px 5px;"
            " }"
        )

    def _known_action_button_style(self, role: str, theme: Dict[str, str], ready: bool) -> str:
        focus = theme.get("focus", "#7FB5FF")
        role_color = theme.get(role, theme.get("accent", "#2E6F9E"))
        style = button_style(f"eligible_{role}" if ready else "muted", theme)
        if ready:
            style += (
                " QPushButton:focus {"
                f" background-color: {role_color}; color: {contrast_text_for_background(role_color, theme)}; border: 2px solid {role_color};"
                " padding: 3px 9px;"
                " }"
            )
        else:
            style += (
                " QPushButton:focus {"
                f" border: 2px solid {focus}; padding: 3px 9px;"
                " }"
            )
        return style

    # ---------------- Net name auto-fill ---------------- #

    def _next_net_from_schedule(self) -> Optional[Dict]:
        occurrences = self._net_schedule_occurrences()
        return occurrences[0] if occurrences else None

    def _net_schedule_occurrences(self, *, window_hours: int = 12) -> List[Dict]:
        data = self.settings.all()
        net_sched = data.get("net_schedule", [])
        if not isinstance(net_sched, list):
            return []

        now = datetime.datetime.now(datetime.timezone.utc)
        window_end = now + datetime.timedelta(hours=max(1, int(window_hours or 12)))
        occurrences: List[Dict] = []
        seen_keys: set[tuple[str, str, str, str, str]] = set()

        for row in net_sched:
            if not isinstance(row, dict):
                continue
            occ = self._next_net_occurrence(row, now)
            if not occ:
                continue
            start_dt = occ.get("start_dt")
            end_dt = occ.get("end_dt")
            if not isinstance(start_dt, datetime.datetime) or not isinstance(end_dt, datetime.datetime):
                continue
            if not occ.get("active") and start_dt > window_end:
                continue
            if end_dt < now:
                continue
            key = (
                str(row.get("net_name") or "").strip().upper(),
                str(row.get("mode") or "").strip().upper(),
                str(row.get("band") or "").strip().upper(),
                str(row.get("frequency") or "").strip(),
                str(row.get("start_utc") or "").strip(),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            occurrences.append(occ)

        occurrences.sort(key=lambda item: (0 if item.get("active") else 1, item.get("start_dt")))
        return occurrences

    def _populate_net_name_from_schedule(self):
        occurrences = self._net_schedule_occurrences()
        if not occurrences:
            return
        selected_text = self.net_name_combo.currentText().strip()
        self.net_name_combo.blockSignals(True)
        try:
            self.net_name_combo.clear()
            first_active_idx = -1
            for occ in occurrences:
                row = occ["row"]
                net_name = (row.get("net_name") or "").strip()
                mode = (row.get("mode") or "").strip()
                band = (row.get("band") or "").strip()
                freq = (row.get("frequency") or "").strip()
                start_dt = occ.get("start_dt")
                start = start_dt.strftime("%a %H:%M UTC") if isinstance(start_dt, datetime.datetime) else f"{(row.get('start_utc') or '').strip()} UTC"
                status = "ACTIVE" if occ.get("active") else "UPCOMING"
                parts = [status, net_name, mode, band, freq, start]
                formatted = " - ".join([p for p in parts if p])
                if not formatted:
                    continue
                self.net_name_combo.addItem(formatted)
                if occ.get("active") and first_active_idx < 0:
                    first_active_idx = self.net_name_combo.count() - 1
            idx = self.net_name_combo.findText(selected_text) if selected_text else -1
            if idx < 0:
                idx = first_active_idx if first_active_idx >= 0 else 0
            self.net_name_combo.setCurrentIndex(idx)
        finally:
            self.net_name_combo.blockSignals(False)
        self._update_net_name_min_width()

    def _update_net_name_min_width(self):
        """
        Set the minimum width based on the widest current entry plus space for ~10 extra characters.
        """
        metrics = QFontMetrics(self.net_name_combo.font())
        pad = metrics.horizontalAdvance("0" * 10)
        max_w = 0
        for i in range(self.net_name_combo.count()):
            txt = self.net_name_combo.itemText(i)
            if not txt:
                continue
            w = metrics.horizontalAdvance(txt) + pad
            if w > max_w:
                max_w = w
        if max_w > 0:
            # Add a small safety margin
            self.net_name_combo.setMinimumWidth(min(max_w + 8, 300))
        else:
            # Fallback if no items exist yet
            self.net_name_combo.setMinimumWidth(320)

    # ---------------- Browse / HINT ---------------- #

    # ---------------- FILE HELPERS ---------------- #

    def _read_file(self, path: str) -> str:
        if not path:
            return ""
        try:
            p = Path(path)
            if not p.exists():
                return ""
            return p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            log.error("Failed to read file %s: %s", path, e)
            return ""

    def _append_file(self, path: str, text: str):
        if not path:
            return
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            log.error("Failed to append to file %s: %s", path, e)

    def _write_file(self, path: str, text: str):
        if not path:
            return
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(p)
        except Exception as e:
            if getattr(self, '_preparation_scope', None) is not None:
                raise
            log.error("Failed to write file %s: %s", path, e)

    def _resolve_checkin_dir(self) -> Path:
        if getattr(self, '_preparation_shell', False) and getattr(self, '_preparation_scope', None) is None:
            raise RuntimeError('Check-in records folder is not yet validated for the selected radio')
        scope = getattr(self, '_preparation_scope', None)
        if scope is not None:
            if not scope.checkin_dir:
                raise RuntimeError('Selected radio check-in records folder is not configured')
            return Path(scope.checkin_dir)
        stored = (self.settings.get("fldigi_checkin_dir", "") or "").strip()
        if not stored:
            stored = str(get_fldigi_checkin_dir())
            try:
                self.settings.set("fldigi_checkin_dir", stored)
            except Exception:
                pass
        return Path(stored)

    def _checkin_file_paths(self) -> tuple[str, str, str]:
        base = self._resolve_checkin_dir()
        return (
            str(base / CURRENT_CHECKIN_FILE_NAMES["TFC"]),
            str(base / CURRENT_CHECKIN_FILE_NAMES["QRU"]),
            str(base / CURRENT_CHECKIN_FILE_NAMES["LATE"]),
        )

    def _all_checkins_file_path(self) -> str:
        return str(self._resolve_checkin_dir() / CURRENT_CHECKIN_FILE_NAMES["ALL"])

    def _role_checkin_file_path(self, role: str, key: str) -> str:
        role_key = self._exact_net_control_role(role)
        file_key = str(key or "").strip().upper()
        filename = ROLE_CHECKIN_FILE_NAMES.get(role_key, {}).get(file_key, "")
        if not filename:
            filename = f"{role_key}_{file_key}.txt" if role_key and file_key else ""
        return str(self._resolve_checkin_dir() / filename)

    def _role_ack_pending_file_path(self, role: str) -> str:
        return self._role_checkin_file_path(role, "ACK_PENDING")

    def _role_next_tfc_file_path(self, role: str) -> str:
        return self._role_checkin_file_path(role, "NEXT_TFC")

    def _role_relay_file_path(self, role: str) -> str:
        return self._role_checkin_file_path(role, "RELAYS")

    def _generated_checkin_file_paths(self) -> List[Path]:
        base = self._resolve_checkin_dir()
        paths = [base / filename for filename in CURRENT_CHECKIN_FILE_NAMES.values()]
        for role in ("NCS", "ANCS"):
            paths.extend(base / filename for filename in ROLE_CHECKIN_FILE_NAMES[role].values())
        return paths

    def _ack_pending_file_path(self) -> str:
        return self._role_ack_pending_file_path(self._current_net_control_role() or "NCS")

    def _next_tfc_file_path(self) -> str:
        return self._role_next_tfc_file_path(self._current_net_control_role() or "NCS")

    def _checkin_archive_dir(self) -> Path:
        return self._resolve_checkin_dir() / "archive"

    def _ensure_checkin_files(self) -> tuple[str, str, str]:
        base = self._resolve_checkin_dir()
        main_path = base / CURRENT_CHECKIN_FILE_NAMES["TFC"]
        qru_path = base / CURRENT_CHECKIN_FILE_NAMES["QRU"]
        late_path = base / CURRENT_CHECKIN_FILE_NAMES["LATE"]
        base.mkdir(parents=True, exist_ok=True)
        for path in self._generated_checkin_file_paths():
            if not path.exists():
                path.touch()
        return str(main_path), str(qru_path), str(late_path)

    def _archive_checkin_files(self) -> List[Path]:
        archive_dir = self._checkin_archive_dir()
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        net_name = self.net_name_combo.currentText().strip() if hasattr(self, "net_name_combo") else ""
        net_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", net_name).strip("._-")
        prefix = f"{stamp}_{net_slug}" if net_slug else stamp
        archived: List[Path] = []
        paths = self._generated_checkin_file_paths()
        for source in paths:
            if not source.exists() or not source.is_file():
                continue
            target = archive_dir / f"{prefix}_{source.name}"
            counter = 2
            while target.exists():
                target = archive_dir / f"{prefix}_{source.stem}-{counter}{source.suffix}"
                counter += 1
            target.write_text(source.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
            archived.append(target)
        notes_text = self._roster_notes_archive_text(net_name=net_name, archived_utc=stamp)
        if notes_text:
            target = archive_dir / f"{prefix}_checkin_notes.txt"
            counter = 2
            while target.exists():
                target = archive_dir / f"{prefix}_checkin_notes-{counter}.txt"
                counter += 1
            target.write_text(notes_text, encoding="utf-8")
            archived.append(target)
        if archived:
            log.info("Archived %d FLDigi check-in files to %s", len(archived), archive_dir)
        return archived

    def _preserve_pre_start_files(self) -> Optional[Path]:
        """Copy known surviving working records before a fresh session clears them."""
        sources = list(self._generated_checkin_file_paths())
        sources.extend(Path(str(mapping["source_file"]).strip())
                       for mapping in self._enabled_standard_roster_mappings())
        survivors = []
        by_identity = {}
        for source in sources:
            source = Path(source).expanduser().absolute()
            try:
                stat = source.stat()
            except FileNotFoundError:
                continue
            if not source.is_file():
                raise OSError(f"Working record is not a file: {source}")
            if not stat.st_size:
                continue
            identity = ("inode", stat.st_dev, stat.st_ino) if stat.st_ino else ("path", str(source.resolve()))
            if identity in by_identity:
                by_identity[identity]["sources"].append(str(source))
                continue
            entry = {"sources": [str(source)], "content": source.read_bytes()}
            by_identity[identity] = entry
            survivors.append(entry)
        notes = self._roster_notes_archive_text()
        if not survivors and not notes:
            return None
        archive = self._checkin_archive_dir() / (
            "pre-start-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%SZ")
            + "-" + uuid.uuid4().hex
        )
        archive.mkdir(parents=True, exist_ok=False)
        manifest = []
        for index, entry in enumerate(survivors, 1):
            name = f"{index:03d}-{Path(entry['sources'][0]).name}"
            with (archive / name).open("xb") as target:
                target.write(entry["content"])
            manifest.append({"archive_file": name, "source_paths": entry["sources"]})
        if notes:
            with (archive / "roster-notes.txt").open("x", encoding="utf-8") as target:
                target.write(notes)
        with (archive / "manifest.json").open("x", encoding="utf-8") as target:
            json.dump({"working_files": manifest, "roster_notes": bool(notes)}, target, indent=2)
            target.write("\n")
        return archive

    def _roster_notes_archive_text(self, *, net_name: str = "", archived_utc: str = "") -> str:
        note_rows = [row for row in self._roster_table_rows() if str(row.get("notes") or "").strip()]
        if not note_rows:
            return ""
        title = (net_name or "").strip() or "FLDigi Net"
        stamp = (archived_utc or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%SZ")).strip()
        lines = [f"Check-in Notes - {title}", f"Archived: {stamp}", ""]
        for row in note_rows:
            base = self._format_entry(row.get("callsign", ""), row.get("name", ""), row.get("state", ""))
            role = self._roster_station_role(row) or "-"
            category = str(row.get("category") or "-").strip().upper() or "-"
            notes = str(row.get("notes") or "").strip()
            if base and notes:
                lines.append(f"{base} / {role} / {category} / {notes}")
        return "\n".join(lines).strip() + "\n"

    # ---------------- BUTTON LOGIC ---------------- #

    def _validate_before_start(self) -> bool:
        net_name = self.net_name_combo.currentText().strip()

        if not net_name:
            QMessageBox.warning(self, "Missing Net Name", "Enter Net Name before starting the net.")
            return False

        try:
            self._ensure_checkin_files()
        except Exception as e:
            QMessageBox.critical(
                self,
                "File Error",
                f"Unable to create or access log files:\n{e}",
            )
            return False

        return True

    def _start_net(self):
        receiver = getattr(self, '_prepared_receiver', None)
        if receiver is not None and receiver.prepared is not None and not receiver.dispatching:
            receiver.start()
            return
        if self._net_in_progress:
            QMessageBox.information(self, "Net In Progress", "A net is already active. End it before starting a new one.")
            return
        if not self._validate_before_start():
            return
        main_path, qru_path, late_path = self._ensure_checkin_files()
        try:
            preserved = self._preserve_pre_start_files()
        except Exception as exc:
            log.error("FLDigi NCS: pre-start preservation failed: %s", exc)
            QMessageBox.critical(self, "Unable to Preserve Records",
                                 f"Start was cancelled. Working records were retained.\n{exc}")
            return
        if preserved:
            QMessageBox.information(self, "Working Records Preserved",
                                    f"Previous working records were copied to:\n{preserved}")

        # Clear files to avoid loading stale/pre-populated data
        for path in self._generated_checkin_file_paths():
            self._write_file(path, "")
        self._next_tfc_last_served = {"NCS": "", "ANCS": ""}
        self._next_tfc_called_by_role = {"NCS": set(), "ANCS": set()}
        self._roster_action_scope_user_selected = False
        self._sync_roster_action_scope_to_role(force=True)
        self._roster_clear()
        self._sync_mapped_roster_files()
        self.main_text.setPlainText("")
        self.qru_text.setPlainText("")
        self.late_text.setPlainText("")
        self.known_op_edit.clear()
        self._known_op_autofilled_prefix = ""
        self._known_op_autofill_consumed = False
        self._known_op_tab_stage = 0
        self._known_op_pending_focus = None
        self.reference_card.set_text("")
        self.compare_results_card.set_text("")
        self.review_card.set_text("")
        self._compare_missing_text = ""
        self._compare_reference_missing_entries = []
        self._sync_compare_workspace_tabs()
        self.compare_workspace_tabs.setCurrentWidget(self.reference_card)
        self._update_bucket_card_states()

        self._net_in_progress = True
        profiles = self._ncs_radio_profiles()
        self._ncs_session_radio_id = self._ncs_selected_radio_id(profiles) or None
        self._ncs_session_id = getattr(getattr(self, '_prepared_receiver', None), 'pending_session_token', '') or uuid.uuid4().hex
        self._net_start_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        self._net_end_utc = None
        if self._log_assisted_enabled():
            self._capture_log_assisted_session()
            self._start_log_assisted_timer()
        role = normalize_role(self.role_combo.currentText())
        if role in {"NCS", "ANCS"}:
            self._apply_local_net_control_role()
            partner = (self.partner_primary_edit.text() or "").strip().upper()
            if partner:
                if role == "NCS":
                    self._ancs_partner_call = partner
                    self._add_net_control_roster_row(partner, "ANCS")
                else:
                    self._ncs_partner_call = partner
                    self._add_net_control_roster_row(partner, "NCS")
        else:
            self._add_joiner_net_control_rows()
        self._roster_sync_legacy_buffers(write_files=True)
        self._set_roster_dirty(False)
        self._set_net_button_styles(active=True)
        self._update_copy_buttons_state()
        self._refresh_ncs_session_context()
        log.info("FLDigi net started: %s (%s)", self.net_name_combo.currentText().strip(), self.role_combo.currentText())
        if getattr(self, '_preparation_scope', None) is None:
            QTimer.singleShot(0, self._refresh_operator_history_views)
        self._persist_ncs_session_snapshot(timing_state="active")
        self.net_status_changed.emit("FLDIGI", True)
        self.net_session_status_changed.emit(
            "FLDIGI", True, int(self._ncs_session_radio_id or 0), self._ncs_session_id
        )

    def _start_ad_hoc_net(self):
        """
        Generate and start an ad hoc net with a UTC timestamped name.
        """
        if self._net_in_progress:
            QMessageBox.information(self, "Net In Progress", "End the current net before starting an ad hoc net.")
            return
        current_name = self.net_name_combo.currentText().strip()
        if current_name:
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
        ad_hoc_name = f"FLDIGI - Ad Hoc - {ts} UTC"
        self.net_name_combo.setEditText(ad_hoc_name)
        self._start_net()

    def _save_checkins(self, *, quiet: bool = False):
        main_path, qru_path, late_path = self._ensure_checkin_files()
        self._roster_sync_legacy_buffers(write_files=False)
        main_text = self._roster_table_text("TFC")
        qru_text = self._roster_table_text("QRU")
        late_text = self._roster_table_text("LATE")
        self._write_file(main_path, main_text)
        self._write_file(qru_path, qru_text)
        self._write_file(late_path, late_text)
        self._write_file(self._all_checkins_file_path(), self._roster_table_text())
        self._sync_role_roster_files()
        self._sync_role_ack_pending_files()
        self._sync_next_tfc_action_files()
        self._sync_mapped_roster_files()
        self._set_roster_dirty(False)

        if not quiet:
            QMessageBox.information(self, "Saved", "Check-in logs saved.")
        self._refresh_operator_history_views()

    def _confirm_end_net(self) -> tuple[bool, bool]:
        rows_available = bool(self._attendance_rows_for_state_summary())
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("End Net")
        box.setText("End net now?")
        if rows_available:
            box.setInformativeText("Accepted check-ins are available for the post-net state summary.")
        else:
            box.setInformativeText("No accepted check-ins are currently logged.")
        end_button = box.addButton("End Net", QMessageBox.AcceptRole)
        cancel_button = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel_button)
        summary_chk = None
        if rows_available:
            summary_chk = QCheckBox("Copy state summary when net ends")
            box.setCheckBox(summary_chk)
        box.exec()
        if box.clickedButton() is not end_button:
            return False, False
        return True, bool(summary_chk and summary_chk.isChecked())

    def _merge_late_into_main(self):
        main_path, _qru_path, late_path = self._ensure_checkin_files()
        late_text = self._roster_table_text("LATE")
        if not late_text.strip():
            late_text = self._read_file(late_path)
        if not late_text.strip():
            self._write_file(late_path, "")
            return

        for raw in late_text.splitlines():
            cs, name, state, extra = self._split_checkin_with_extra(raw)
            if not cs and not name and not state:
                continue
            self._roster_append_row(cs, name, state, extra, "TFC")
        for row in range(self.roster_table.rowCount()):
            item = self.roster_table.item(row, self.COL_CATEGORY)
            widget = self.roster_table.cellWidget(row, self.COL_CATEGORY)
            current = ""
            if widget is not None and hasattr(widget, "currentText"):
                current = str(widget.currentText() or "").strip().upper()
            elif item is not None:
                current = item.text().strip().upper()
            if current == "LATE":
                self._roster_set_category(row, "TFC")
        self._roster_sync_legacy_buffers(write_files=True)
        self._write_file(late_path, "")

        # No popup needed; UI visibly clears the late list.

    def _copy_summary(self):
        """
        Copy the consolidated check-in log to the clipboard.
        """
        text = self._roster_table_text()
        if text:
            QApplication.clipboard().setText(self._roster_clipboard_text(text))
            self._write_file(self._all_checkins_file_path(), text)
            self._show_roster_action_status("Check-ins copied.")
        else:
            self._show_roster_action_status("No check-ins to copy.", "info")

    def _copy_state_summary(self) -> None:
        """
        Copy an aggregate attendance summary grouped by state/province.
        """
        text = self._state_summary_text()
        if text:
            QApplication.clipboard().setText(text)
            self._show_roster_action_status("State summary copied.")
        else:
            self._show_roster_action_status("No accepted check-ins to summarize.", "info")

    def _copy_text_to_clipboard(self, text: str):
        """
        Copy raw text (already normalized per line) to clipboard.
        """
        QApplication.clipboard().setText(text)

    def _format_freq(self, val) -> str:
        try:
            return f"{float(val):.3f}"
        except Exception:
            return str(val) if val is not None else ""

    # ---------------- NORMALIZATION ---------------- #

    def _on_main_text_changed(self):
        # Avoid auto-normalizing while the operator is editing in real time.
        self._update_copy_buttons_state()

    def _on_late_text_changed(self):
        # Avoid auto-normalizing while the operator is editing in real time.
        self._update_copy_buttons_state()

    def _normalize_text_edit(self, edit: QTextEdit, flag_attr: str) -> None:
        """
        Keep entries normalized to 'CALL / Name / ST' as users type or paste.
        """
        if getattr(self, flag_attr, False):
            return
        setattr(self, flag_attr, True)
        try:
            original = edit.toPlainText()
            if original is None:
                return
            lines = original.splitlines()
            normalized_lines = []
            for line in lines:
                raw_line = line
                if "/" in raw_line and raw_line.count("/") < 2:
                    normalized_lines.append(raw_line)
                    continue
                cs, name, state = self._parse_checkin_line(line)
                if cs or name or state:
                    normalized_lines.append(self._format_entry(cs, name, state))
                else:
                    normalized_lines.append(raw_line)
            normalized = "\n".join(normalized_lines)
            if original.endswith("\n"):
                normalized += "\n"
            if normalized != original:
                cursor = edit.textCursor()
                pos = cursor.position()
                edit.blockSignals(True)
                try:
                    edit.setPlainText(normalized)
                    new_cursor = edit.textCursor()
                    new_cursor.setPosition(min(pos, len(normalized)))
                    edit.setTextCursor(new_cursor)
                finally:
                    edit.blockSignals(False)
        finally:
            setattr(self, flag_attr, False)

    def _end_net(self):
        if getattr(self, '_preparation_scope', None) is None:
            return self._end_net_impl()
        was_active = self._net_in_progress
        token, radio = self._ncs_session_id, self._ncs_session_radio_id
        previous_end = self._net_end_utc
        try:
            return self._end_net_impl()
        except Exception as exc:
            self._net_in_progress = was_active
            self._ncs_session_id, self._ncs_session_radio_id = token, radio
            self._net_end_utc = previous_end
            self._set_net_button_styles(active=was_active)
            self._update_copy_buttons_state()
            self._prepared_receiver._set_status('Another net active', f'End could not save all records: {exc}; work retained, review records and retry End')
            if was_active:
                self._start_log_assisted_timer()
            return None

    def _end_net_impl(self):
        confirmed, copy_state_summary = self._confirm_end_net()
        if not confirmed:
            return
        session_radio_id = getattr(self, "_ncs_session_radio_id", None)
        session_id = getattr(self, "_ncs_session_id", "")
        self._stop_log_assisted_timer()
        if not self._net_in_progress:
            log.info("End Net clicked but no net_in_progress flag set; proceeding with DB load from file.")
        self._save_checkins(quiet=True)

        main_path, qru_path, _ = self._checkin_file_paths()

        main_text = self._roster_table_text("TFC") or self._read_file(main_path)
        qru_text = self._roster_table_text("QRU") or self._read_file(qru_path)
        late_path = self._checkin_file_paths()[2]
        late_text = self._roster_table_text("LATE") or self._read_file(late_path)
        combined_text = "\n".join(text for text in (main_text, qru_text, late_text) if text.strip())
        if not combined_text.strip():
            self._archive_checkin_files()
            # End net even though no check-ins exist
            self._net_in_progress = False
            self._net_end_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
            self._stop_log_assisted_timer()
            self._set_net_button_styles(active=False)
            self._update_copy_buttons_state()
            self._refresh_ncs_session_context()
            self._show_roster_action_status("Net ended. No check-ins were logged.", "info")
            log.info("FLDigi net ended (no check-ins file content).")
            self._persist_ncs_session_snapshot(timing_state="ended")
            self.net_status_changed.emit("FLDIGI", False)
            self.net_session_status_changed.emit(
                "FLDIGI", False, int(session_radio_id or 0), session_id
            )
            self._ncs_session_radio_id = None
            self._ncs_session_id = ""
            return

        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        net_name = self.net_name_combo.currentText().strip()
        role = self.role_combo.currentText().strip().upper()
        group_name = self._operating_group_for_freq(self._current_freq_mhz())

        entries: List[Dict] = []
        seen_callsigns = set()
        for line in combined_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cs, name, state, _extra = self._split_checkin_with_extra(line)
            cs, name, state = self._normalize_checkin_fields(cs, name, state)
            if not cs:
                continue
            if cs in seen_callsigns:
                continue
            seen_callsigns.add(cs)
            existing_name, existing_state, exists = self._lookup_operator_name_state(cs)
            name_out = existing_name
            if name and (not existing_name or name.strip().lower() != existing_name.strip().lower()):
                name_out = name
            state_out = existing_state
            if not existing_state and state:
                state_out = state
            if not exists:
                name_out = name
                state_out = state
            group1 = group2 = group3 = None
            groups_json = None
            if group_name:
                existing_groups = self._lookup_operator_groups(cs)
                if group_name not in existing_groups:
                    existing_groups.append(group_name)
                group1 = existing_groups[0] if len(existing_groups) > 0 else ""
                group2 = existing_groups[1] if len(existing_groups) > 1 else ""
                group3 = existing_groups[2] if len(existing_groups) > 2 else ""
                groups_json = existing_groups if existing_groups else None
            entries.append(
                {
                    "callsign": cs,
                    "name": name_out,
                    "state": state_out,
                    "first_seen_utc": now_utc,
                    "last_seen_utc": now_utc,
                    "net_name": net_name,
                    "role": role,
                    "trusted": None,
                    "group1": group1 or "",
                    "group2": group2 or "",
                    "group3": group3 or "",
                    "groups_json": json.dumps(groups_json) if groups_json else None,
                }
            )

        if entries:
            if getattr(self, '_preparation_scope', None) is not None:
                token = self._ncs_session_id
                if getattr(self, '_prepared_end_counter_token', '') != token:
                    self._prepared_end_counter_token, self._prepared_end_counted_calls = token, set()
                counted = self._prepared_end_counted_calls
                committed = upsert_checkins(entries, db_path=Path(self.settings.db_path).parent / 'freqinout_nets.db',
                                            raise_on_error=True, already_counted=frozenset(counted))
                counted.update(committed)
                self._bump_operator_history(entries)
            else:
                upsert_checkins(entries)
                self._bump_operator_history(entries)
            self._roster_sync_legacy_buffers(write_files=True)
            status_message = f"Net ended. {len(entries)} check-ins imported."
        else:
            status_message = "Net ended. No valid check-ins found to import."

        self._archive_checkin_files()
        self._net_in_progress = False
        self._net_end_utc = now_utc
        self._stop_log_assisted_timer()
        self._set_net_button_styles(active=False)
        self._update_copy_buttons_state()
        self._refresh_ncs_session_context()
        if copy_state_summary:
            state_summary = self._state_summary_text()
            if state_summary:
                QApplication.clipboard().setText(state_summary)
                status_message += " State summary copied."
            else:
                status_message += " No state summary was available."
        self._show_roster_action_status(status_message)
        log.info("FLDigi net ended: %s (%s)", net_name, role)
        self._persist_ncs_session_snapshot(timing_state="ended")
        self.net_status_changed.emit("FLDIGI", False)
        self.net_session_status_changed.emit(
            "FLDIGI", False, int(session_radio_id or 0), session_id
        )
        self._ncs_session_radio_id = None
        self._ncs_session_id = ""



    # ---------------- INSERT KNOWN OPERATOR ---------------- #

    def _insert_known_into_main(self):
        if not self._require_active_net_for_checkin_add():
            return
        line = self.known_op_edit.text().strip()
        if not line:
            return
        cs, name, state, extra = self._split_checkin_with_extra(line)
        if not cs and not name and not state:
            return
        lookup = self._known_operator_by_callsign.get(cs or "")
        if lookup:
            cs = lookup.get("callsign", cs)
            name = lookup.get("name", name)
            state = lookup.get("state", state)
        self._roster_append_row(cs, name, state, extra, "TFC")
        self.known_op_edit.clear()
        self.known_op_edit.setFocus()

    def _insert_known_into_late(self):
        if not self._require_active_net_for_checkin_add():
            return
        line = self.known_op_edit.text().strip()
        if not line:
            return
        cs, name, state, extra = self._split_checkin_with_extra(line)
        if not cs and not name and not state:
            return
        lookup = self._known_operator_by_callsign.get(cs or "")
        if lookup:
            cs = lookup.get("callsign", cs)
            name = lookup.get("name", name)
            state = lookup.get("state", state)
        self._roster_append_row(cs, name, state, extra, "LATE")
        self.known_op_edit.clear()
        self.known_op_edit.setFocus()

    # ---------------- PARSING ---------------- #

    def _parse_checkin_line(self, line: str):
        """
        Accept both:
          - CALLSIGN/NAME/STATE/
          - CALLSIGN NAME STATE [traffic...]

        Returns (callsign, name, state)
        """
        line = self._strip_inline_review_context(line)
        if not line:
            return "", "", ""

        if "/" in line and not line.startswith("#"):
            parts = [p.strip() for p in line.split("/") if p.strip()]
            if len(parts) >= 3:
                state_tokens = parts[2].split()
                state = state_tokens[0].upper() if state_tokens else ""
                return parts[0].split()[0].upper(), parts[1], state
            elif len(parts) == 2:
                return parts[0].split()[0].upper(), parts[1], ""
            else:
                pass

        tokens = line.split()
        if len(tokens) >= 3:
            return tokens[0].upper(), tokens[1], tokens[2]
        elif len(tokens) == 2:
            return tokens[0].upper(), tokens[1], ""
        elif tokens:
            return tokens[0].upper(), "", ""
        return "", "", ""

    def _format_entry(self, cs: str, name: str, state: str) -> str:
        """
        Normalize check-in display to 'CALL / Name / ST' with single separators.
        """
        parts = [p for p in (cs.strip().upper(), name.strip(), state.strip().upper()) if p]
        return " / ".join(parts)

    @staticmethod
    def _normalize_checkin_fields(cs: str, name: str, state: str) -> tuple[str, str, str]:
        cs_norm = (cs or "").strip().upper()
        state_norm = (state or "").strip().upper()
        name_norm = (name or "").strip()
        if name_norm:
            name_norm = name_norm[0].upper() + name_norm[1:]
        return cs_norm, name_norm, state_norm

    def _lookup_operator_name_state(self, callsign: str) -> tuple[str, str, bool]:
        cs = (callsign or "").strip().upper()
        if not cs:
            return "", "", False
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = Path(self.settings.db_path).parent / 'freqinout_nets.db' if getattr(self, '_preparation_scope', None) is not None else get_config_dir() / 'config' / 'freqinout_nets.db'
        except Exception:
            return "", "", False
        if not db_path.exists():
            return "", "", False
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT name, state FROM operator_checkins WHERE callsign=?", (cs,))
            row = cur.fetchone()
            conn.close()
        except Exception:
            return "", "", False
        if not row:
            return "", "", False
        return (row[0] or "").strip(), (row[1] or "").strip().upper(), True

    def _bump_operator_history(self, entries: List[Dict]):
        if getattr(self, '_preparation_scope', None) is not None:
            # upsert_checkins already updates shared operator counters. Keep only
            # the FLDigi recent marker here, in the selected configuration root.
            db_path = Path(self.settings.db_path).parent / 'freqinout_nets.db'
            with sqlite3.connect(db_path) as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS fldigi_checkins (callsign TEXT PRIMARY KEY, last_seen_ts REAL)')
                stamp = datetime.datetime.now(datetime.timezone.utc).timestamp()
                conn.executemany('INSERT OR REPLACE INTO fldigi_checkins VALUES (?, ?)',
                                 [(str(e.get('callsign') or '').strip().upper(), stamp) for e in entries])
            return
        """
        Update operator_checkins table with new/updated operator info and increment checkin_count by 1.
        Schema matches OperatorHistoryTab.
        """
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = Path(self.settings.db_path).parent / 'freqinout_nets.db' if getattr(self, '_preparation_scope', None) is not None else get_config_dir() / 'config' / 'freqinout_nets.db'
        except Exception:
            return
        if not db_path.exists():
            return
        try:
            # Ensure schema matches OperatorHistoryTab expectations
            from freqinout.gui.operator_history_tab import OperatorHistoryTab
            dummy = OperatorHistoryTab()
            conn = sqlite3.connect(db_path)
            try:
                dummy._ensure_schema(conn)  # type: ignore[attr-defined]
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fldigi_checkins (
                        callsign TEXT PRIMARY KEY,
                        last_seen_ts REAL
                    )
                    """
                )
                today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
                for e in entries:
                    cs = (e.get("callsign") or "").strip().upper()
                    if not cs:
                        continue
                    first_seen = (e.get("first_seen_utc") or "").strip() or today_str
                    last_seen = (e.get("last_seen_utc") or "").strip() or today_str
                    trusted_in = e.get("trusted")
                    trusted_val = 0 if trusted_in is False or trusted_in == 0 else None
                    cur.execute(
                        """
                        INSERT INTO operator_checkins (
                            callsign, name, state, first_seen_utc, last_seen_utc, checkin_count, trusted
                        )
                        VALUES (?, ?, ?, ?, ?, 1, COALESCE(?, 0))
                        ON CONFLICT(callsign) DO UPDATE SET
                            name=excluded.name,
                            state=excluded.state,
                            first_seen_utc=COALESCE(operator_checkins.first_seen_utc, excluded.first_seen_utc),
                            last_seen_utc=COALESCE(excluded.last_seen_utc, operator_checkins.last_seen_utc),
                            checkin_count=operator_checkins.checkin_count + 1,
                            trusted=COALESCE(operator_checkins.trusted, excluded.trusted)
                        """,
                        (
                            cs,
                            (e.get("name") or "").strip(),
                            (e.get("state") or "").strip().upper(),
                            first_seen,
                            last_seen,
                            trusted_val,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO fldigi_checkins (callsign, last_seen_ts)
                        VALUES (?, ?)
                        ON CONFLICT(callsign) DO UPDATE SET last_seen_ts=excluded.last_seen_ts
                        """,
                        (cs, float(datetime.datetime.now(datetime.timezone.utc).timestamp())),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as ex:
            log.error("Failed to bump operator history: %s", ex)
