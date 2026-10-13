from __future__ import annotations

import datetime
import sqlite3
import platform
import subprocess
import json
import time
import uuid
from pathlib import Path
from typing import Any, List, Dict, Optional, Set, Tuple

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QComboBox,
    QHeaderView,
    QMessageBox,
    QCheckBox,
    QApplication,
    QFileDialog,
    QGroupBox,
    QMenu,
    QInputDialog,
    QScrollArea,
    QDialog,
    QDialogButtonBox,
    QSizePolicy,
)
from PySide6.QtGui import QAction, QColor

from freqinout.core.settings_manager import SettingsManager
from freqinout.core.daily_schedule_resolution import rows_have_ambiguous_overlap
from freqinout.core.operating_group_frequency import (
    ensure_operating_group_frequency_keys,
    format_frequency_mhz,
    normalize_frequency_hz,
    reattach_schedule_frequency_keys,
    resolve_operating_group_frequency,
)
from freqinout.core.plan_context_service import PlanContextService
from freqinout.core.schedule_source_sets import (
    LIVE_SOURCE_SET_ID,
    HF_DAILY_SOURCE_CATEGORY,
    HF_DAILY_SOURCE_SETS_KEY,
    SELECTED_HF_DAILY_SOURCE_SET_KEY,
    assigned_plan_rf_guard_impacts_for_source_update,
    delete_source_schedule,
    rename_source_schedule,
    reproject_frequency_plans_for_source_update,
    save_source_schedule,
    plan_source_usage_summary,
    selected_source_set_id,
    source_set_row_by_id_for_category,
    source_sets_for_category,
)
from freqinout.core.software_status_service import SoftwareStatusService
from freqinout.core.logger import log
from freqinout.core.multi_radio_store import MultiRadioStore, settings_db_path
from freqinout.core.perf_metrics import span as perf_span
from freqinout.core.schedule_targeting import (
    TARGET_SCOPE_DEVICE_PROFILE,
    TARGET_SCOPE_OPERATING_PROFILE,
    TARGET_SCOPE_STATION,
    normalize_schedule_target,
    normalize_schedule_target_fields,
    normalize_target_scope,
    schedule_target_identity_parts,
    schedule_targets_may_overlap,
)
from freqinout.core.sop_manager import SOPManager
from freqinout.utils.timezones import get_timezone
from freqinout.gui.help_registry import resolve_help_host
from freqinout.gui.plan_context_label import PlanContextLabel
from freqinout.gui.theme import (
    resolve_theme,
    button_style,
    font_css,
    item_view_height_for_rows,
    font_derived_widget_height,
    control_height_for_font,
    fit_combo_box_to_contents,
)
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


DAY_OPTIONS = [
    "ALL",
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]
DAY_CANON = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
DAY_INDEX = {name: idx for idx, name in enumerate(DAY_CANON)}

BAND_OPTIONS = [
    "20M",
    "40M",
    "80M",
    "--",
    "2M",
    "6M",
    "10M",
    "12M",
    "15M",
    "17M",
    "30M",
    "60M",
]

MODE_OPTIONS = ["Digi", "SSB"]
SCHEDULE_TARGET_SCOPE_ITEMS = [
    ("Station", TARGET_SCOPE_STATION),
    ("Radio Profile", TARGET_SCOPE_DEVICE_PROFILE),
    ("Frequency Plan", TARGET_SCOPE_OPERATING_PROFILE),
]


class _SortKeyTableWidgetItem(QTableWidgetItem):
    def __init__(self, text: str = "", *, sort_key: Any = None) -> None:
        super().__init__(text)
        if sort_key is not None:
            self.setData(Qt.UserRole, sort_key)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, QTableWidgetItem):
            left_key = self.data(Qt.UserRole)
            right_key = other.data(Qt.UserRole)
            if left_key is not None and right_key is not None:
                try:
                    return left_key < right_key
                except Exception:
                    pass
        return super().__lt__(other)


# Radio program metadata (must match SettingsTab keys)
PROGRAMS = {
    "FLRig": {
        "path_key": "path_flrig",
        "autostart_key": "autostart_flrig",
        "default_cmd": "flrig",
    },
    "FLDigi": {
        "path_key": "path_fldigi",
        "autostart_key": "autostart_fldigi",
        "default_cmd": "fldigi",
    },
    "FLMsg": {
        "path_key": "path_flmsg",
        "autostart_key": "autostart_flmsg",
        "default_cmd": "flmsg",
    },
    "FLAmp": {
        "path_key": "path_flamp",
        "autostart_key": "autostart_flamp",
        "default_cmd": "flamp",
    },
    "JS8Call": {
        "path_key": "path_js8call",
        "autostart_key": "autostart_js8call",
        "default_cmd": "js8call",
    },
}


class DailyScheduleTab(QWidget):
    schedule_saved = Signal()
    """
    HF Frequency Schedule tab.

    This tab is intentionally very similar to the Net Schedule tab, with
    the following differences:

      - The 'Net Name' column is renamed 'Group Name'.
      - The 'Day' column allows 'ALL' in addition to each day of week.
        ('ALL' means the entry is used every day.)
      - No limit to the number of rows.

    Data is stored in settings/DB; offsets/comments are no longer used.
    """

    # Column indices
    COL_SELECT = 0
    COL_DAY = 1
    COL_SOURCE = 2
    COL_GROUP = 3
    COL_MODE = 4
    COL_BAND = 5
    COL_FREQ = 6
    COL_START = 7
    COL_END = 8
    COL_AUTOTUNE = 9
    COL_TARGET_SCOPE = 10
    COL_TARGET = 11
    COMPACT_VISIBLE_COLUMNS = frozenset(
        {
            COL_SELECT,
            COL_DAY,
            COL_GROUP,
            COL_MODE,
            COL_BAND,
            COL_FREQ,
            COL_START,
            COL_END,
        }
    )

    # Resource table column indices
    RES_COL_SELECT = 0
    RES_COL_SET = 1
    RES_COL_DAY = 2
    RES_COL_GROUP = 3
    RES_COL_MODE = 4
    RES_COL_BAND = 5
    RES_COL_FREQ = 6
    RES_COL_START = 7
    RES_COL_END = 8
    RES_COL_SOURCE = 9
    RES_COL_UPDATED = 10
    RES_COL_CONFLICT = 11

    def __init__(self, parent=None, *, plan_context_service: Optional[PlanContextService] = None):
        super().__init__(parent)
        self.settings = SettingsManager()
        self.plan_context_service = plan_context_service or PlanContextService()
        self._status_service = SoftwareStatusService(self.settings)
        try:
            self.settings.reload()
        except Exception:
            pass
        self.operating_groups: List[Dict] = self._load_operating_groups()
        self._operating_groups_sig = self._snapshot_operating_groups(self.operating_groups)
        self.device_profiles: List[Dict[str, Any]] = []
        self.operating_profiles: List[Dict[str, Any]] = []
        self._refresh_schedule_target_catalogs()
        default_mode = (self.settings.get("display_time_mode", "LOCAL") or "LOCAL").upper()
        self._show_local: bool = default_mode != "UTC"
        self._raw_schedule: List[Dict] = []
        self._dirty: bool = False
        self._suspend_dirty_tracking: bool = False
        self._saved_rows_signature: str = ""
        self._active: bool = False

        self._clock_timer: Optional[QTimer] = None
        self._sop_panel_timer: Optional[QTimer] = None
        self._suppress_autostart: bool = True  # avoid auto-start during initial load
        self._qsy_options: Dict[str, Dict] = {}
        self._sop_manager = SOPManager()
        self._last_sop_panel_refresh_ts: float = 0.0
        self._schedule_resource_rows: List[Dict[str, Any]] = []
        self._resource_view_rows: List[Dict[str, Any]] = []
        self._schedule_resource_token: Tuple[Any, ...] | None = None
        self._sop_profile_lookup: Dict[int, Dict[str, Any]] = {}
        self._sop_group_profile_choice: Dict[str, int] = {}
        self._hidden_sop_overlay_keys: Set[str] = set()
        self._effective_projection_cache_token: Tuple[Any, ...] | None = None
        self._effective_projection_cache_rows: List[Dict[str, Any]] = []
        self._active_conflict_pairs_cache: List[Tuple[int, int, str]] | None = None
        self._active_conflict_rows_cache: Set[int] | None = None
        self._schedule_resource_view_token: Tuple[Any, ...] | None = None
        self._has_active_hf_sop_profiles: bool = False
        self._has_active_hf_sop_conflicts: bool = False
        self._sop_session_journal_cache: Dict[str, Any] | None = None
        self._sop_return_to_normal_prompt_active: bool = False
        self._pending_table_conflict_refresh: bool = False
        self._table_conflict_refresh_timer: Optional[QTimer] = None
        self._last_tab_activation_refresh_ts: float = 0.0
        self._tab_activation_refresh_interval_sec: float = 10.0
        self._last_activation_schedule_token: Tuple[Any, ...] | None = None
        self._responsive_layout_mode = "wide"
        self._responsive_compact_width = 1200
        self._daily_geometry_pending = False
        self._daily_geometry_timer = QTimer(self)
        self._daily_geometry_timer.setSingleShot(True)
        self._daily_geometry_timer.timeout.connect(self._refresh_daily_geometry)
        self._daily_toolbar_pool: List[QHBoxLayout] = [QHBoxLayout() for _ in range(4)]
        self._daily_filter_pool: List[QHBoxLayout] = [QHBoxLayout() for _ in range(2)]
        self._daily_toolbar_rows: List[QHBoxLayout] = []
        self._daily_filter_rows: List[QHBoxLayout] = []

        self._build_ui()
        self._refresh_qsy_options()
        self._load_schedule()
        self._setup_clock_timer()
        self._setup_sop_panel_timer()
        self._suppress_autostart = False

    def _open_context_help(self, context_key: str) -> None:
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_context_help"):
            try:
                host.open_context_help(context_key)
            except Exception:
                pass

    def _format_freq(self, val) -> str:
        try:
            return f"{float(val):.3f}"
        except Exception:
            return str(val) if val is not None else ""

    def _refresh_schedule_target_catalogs(self) -> None:
        try:
            store = MultiRadioStore(settings_db_path())
            self.device_profiles = [dict(row) for row in store.list_device_profiles()]
            self.operating_profiles = [dict(row) for row in store.list_operating_profiles()]
        except Exception as e:
            log.debug("HF Schedule: failed loading target catalogs: %s", e)
            self.device_profiles = []
            self.operating_profiles = []

    @staticmethod
    def _device_target_label(row: Dict[str, Any]) -> str:
        name = str(row.get("name") or "").strip()
        device_id = int(row.get("id", 0) or 0)
        return name or f"Device #{device_id}"

    @staticmethod
    def _operating_target_label(row: Dict[str, Any]) -> str:
        name = str(row.get("name") or "").strip()
        profile_id = int(row.get("id", 0) or 0)
        return name or f"Frequency Plan #{profile_id}"

    @staticmethod
    def _target_scope_tooltip() -> str:
        return (
            "Station rows apply to any current station-default runtime. "
            "Radio Profile rows apply only when that radio is the station default. "
            "Frequency Plan rows apply only when the station-default radio carries that assigned plan."
        )

    def _populate_target_value_combo(
        self,
        combo: QComboBox,
        scope: str,
        *,
        target_device_profile_id: Optional[int] = None,
        target_operating_profile_id: Optional[int] = None,
        editable: bool = True,
        fixed_label: str = "",
    ) -> None:
        prev_block = combo.blockSignals(True)
        try:
            combo.clear()
            combo.setToolTip(self._target_scope_tooltip())
            if not editable:
                combo.addItem(fixed_label or "SOP Layer", None)
                combo.setEnabled(False)
                return
            if scope == TARGET_SCOPE_STATION:
                combo.addItem("Station-wide", None)
                combo.setEnabled(False)
                return
            if scope == TARGET_SCOPE_DEVICE_PROFILE:
                for row in self.device_profiles:
                    combo.addItem(self._device_target_label(row), int(row.get("id", 0) or 0))
                if target_device_profile_id is not None and combo.findData(int(target_device_profile_id)) < 0:
                    combo.addItem(f"Missing device #{int(target_device_profile_id)}", int(target_device_profile_id))
                if combo.count() <= 0:
                    combo.addItem("No device profiles", None)
                    combo.setEnabled(False)
                    return
                combo.setEnabled(True)
                if target_device_profile_id is not None:
                    idx = combo.findData(int(target_device_profile_id))
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                return
            for row in self.operating_profiles:
                combo.addItem(self._operating_target_label(row), int(row.get("id", 0) or 0))
            if target_operating_profile_id is not None and combo.findData(int(target_operating_profile_id)) < 0:
                combo.addItem(
                    f"Missing Frequency Plan #{int(target_operating_profile_id)}",
                    int(target_operating_profile_id),
                )
            if combo.count() <= 0:
                combo.addItem("No Frequency Plans", None)
                combo.setEnabled(False)
                return
            combo.setEnabled(True)
            if target_operating_profile_id is not None:
                idx = combo.findData(int(target_operating_profile_id))
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        finally:
            combo.blockSignals(prev_block)

    def _selected_schedule_target(self, row_index: int) -> Tuple[str, Optional[int], Optional[int]]:
        scope_widget = self.table.cellWidget(row_index, self.COL_TARGET_SCOPE)
        target_widget = self.table.cellWidget(row_index, self.COL_TARGET)
        if not isinstance(scope_widget, QComboBox):
            return TARGET_SCOPE_STATION, None, None
        scope = normalize_target_scope(scope_widget.currentData())
        target_id = target_widget.currentData() if isinstance(target_widget, QComboBox) else None
        return normalize_schedule_target(
            scope,
            target_device_profile_id=target_id if scope == TARGET_SCOPE_DEVICE_PROFILE else None,
            target_operating_profile_id=target_id if scope == TARGET_SCOPE_OPERATING_PROFILE else None,
        )

    def _refresh_schedule_target_widgets(self) -> None:
        self._refresh_schedule_target_catalogs()
        for row_index in range(self.table.rowCount()):
            scope_widget = self.table.cellWidget(row_index, self.COL_TARGET_SCOPE)
            target_widget = self.table.cellWidget(row_index, self.COL_TARGET)
            if not isinstance(scope_widget, QComboBox) or not isinstance(target_widget, QComboBox):
                continue
            if self._is_sop_overlay_row(row_index):
                self._populate_target_value_combo(
                    target_widget,
                    TARGET_SCOPE_STATION,
                    editable=False,
                    fixed_label="SOP Layer",
                )
                continue
            scope, target_device_profile_id, target_operating_profile_id = self._selected_schedule_target(row_index)
            self._populate_target_value_combo(
                target_widget,
                scope,
                target_device_profile_id=target_device_profile_id,
                target_operating_profile_id=target_operating_profile_id,
            )

    # ---------------- UI ---------------- #

    def _build_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        self.daily_schedule_scroll_area = QScrollArea()
        self.daily_schedule_scroll_area.setObjectName("dailyScheduleScrollArea")
        self.daily_schedule_scroll_area.setWidgetResizable(True)
        self.daily_schedule_scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.daily_schedule_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer_layout.addWidget(self.daily_schedule_scroll_area)

        content = QWidget()
        content.setObjectName("dailyScheduleScrollContent")
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.daily_schedule_scroll_area.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setSpacing(10)

        header = QHBoxLayout()
        self.header_title_label = QLabel("<h3>HF Daily Source Schedule</h3>")
        header.addWidget(self.header_title_label)
        self.help_btn = QPushButton("Help")
        self.help_btn.setToolTip("Open HF Frequency Schedule help.")
        self.help_btn.clicked.connect(lambda: self._open_context_help("tab.hf-daily"))
        header.addStretch()
        header.addWidget(self.help_btn)

        self.utc_label = QLabel()
        self.local_label = QLabel()
        self.utc_label.setVisible(False)
        self.local_label.setVisible(False)
        self.time_toggle_btn = QPushButton("Times: Local" if self._show_local else "Times: UTC")
        theme = resolve_theme(self.settings)
        self.time_toggle_btn.setStyleSheet(button_style("primary", theme))
        self.time_toggle_btn.clicked.connect(self._toggle_time_view)
        self.effective_source_label = QLabel("")
        self.effective_source_label.setVisible(False)
        layout.addLayout(header)

        self.plan_context_label = PlanContextLabel(
            "hf_schedule",
            service=self.plan_context_service,
            fallback_text="HF schedule workspace context is available from Help.",
        )
        self.plan_context_label.setToolTip(
            "Use this context to confirm which radio and assigned Frequency Plan schedule changes apply to."
        )
        self.plan_context_label.setVisible(False)
        self.plan_context_label.refresh_context(refresh=True)
        self.source_usage_label = QLabel("")
        self.source_usage_label.setObjectName("dailyScheduleSourceUsage")
        self.source_usage_label.setWordWrap(True)
        self.source_usage_label.setToolTip(
            "Shows which linked Frequency Plan(s) and assigned radio(s) use the selected Daily schedule."
        )
        layout.addWidget(self.source_usage_label)

        # QSY controls row (right aligned under time bar)
        qsy_row = QHBoxLayout()
        qsy_row.addStretch()
        self.qsy_combo = QComboBox()
        self.qsy_combo.currentIndexChanged.connect(self._update_qsy_button_enabled)
        qsy_row.addWidget(self.qsy_combo)
        self.hold_duration_combo = QComboBox()
        self.hold_duration_combo.setToolTip("Temporary schedule hold duration after QSY.")
        self.hold_duration_combo.currentIndexChanged.connect(self._on_hold_duration_changed)
        qsy_row.addWidget(self.hold_duration_combo)
        self.suspend_btn = QPushButton("QSY + Hold")
        self.suspend_btn.clicked.connect(self._on_suspend_clicked)
        qsy_row.addWidget(self.suspend_btn)
        self.qsy_controls_row_widget = QWidget()
        self.qsy_controls_row_widget.setLayout(qsy_row)
        self.qsy_controls_row_widget.setVisible(False)
        layout.addWidget(self.qsy_controls_row_widget)

        # SOP status panel (merged runtime + issues)
        self.sop_runtime_box = QGroupBox("SOP Schedule Status (HF)")
        sop_layout = QVBoxLayout(self.sop_runtime_box)
        sop_layout.setContentsMargins(6, 6, 6, 6)
        sop_layout.setSpacing(4)
        self.sop_runtime_summary_label = QLabel("Now: -- | Next: --")
        self.sop_runtime_summary_label.setWordWrap(True)
        sop_layout.addWidget(self.sop_runtime_summary_label)
        self.sop_profile_summary_label = QLabel("HF SOP Sets: --")
        self.sop_profile_summary_label.setWordWrap(True)
        sop_layout.addWidget(self.sop_profile_summary_label)
        self.sop_indicator_container = QWidget()
        self.sop_indicator_layout = QVBoxLayout(self.sop_indicator_container)
        self.sop_indicator_layout.setContentsMargins(0, 0, 0, 0)
        self.sop_indicator_layout.setSpacing(4)
        sop_layout.addWidget(self.sop_indicator_container)
        self.sop_runtime_box.setMaximumHeight(max(0, self.fontMetrics().lineSpacing() * 6 + 36))
        self.sop_runtime_box.setVisible(False)
        layout.addWidget(self.sop_runtime_box)

        self._daily_action_layout = QGridLayout()
        self._daily_action_layout.setContentsMargins(0, 0, 0, 0)
        self._daily_action_layout.setSpacing(8)
        layout.addLayout(self._daily_action_layout)

        # Active schedule section
        active_header = QHBoxLayout()
        active_header.addWidget(QLabel("<h3>Active Schedule</h3>"))
        active_header.addWidget(self.effective_source_label)
        self.show_sop_overlay_chk = QCheckBox("Show Effective Schedule")
        self.show_sop_overlay_chk.setToolTip(
            "Show a read-only HF/SOP runtime projection for the next 7 days. This does not change scheduler behavior."
        )
        self.show_sop_overlay_chk.setEnabled(False)
        active_header.addWidget(self.show_sop_overlay_chk)
        active_header.addStretch()
        layout.addLayout(active_header)

        # Active schedule table
        self.table = QTableWidget()
        self.table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self._set_headers()

        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(self.COL_SELECT, QHeaderView.ResizeToContents)
        hv.setMinimumSectionSize(self.fontMetrics().horizontalAdvance("Mode") + 20)
        hv.setDefaultSectionSize(  # uia-0: ignore[fixed-table-row-height] horizontal column width derives from live font content
            self.fontMetrics().horizontalAdvance("Wednesday schedule") + 20
        )
        for col in (
            self.COL_DAY,
            self.COL_SOURCE,
            self.COL_GROUP,
            self.COL_MODE,
            self.COL_BAND,
            self.COL_FREQ,
            self.COL_START,
            self.COL_END,
            self.COL_AUTOTUNE,
            self.COL_TARGET_SCOPE,
            self.COL_TARGET,
        ):
            hv.setSectionResizeMode(col, QHeaderView.Stretch)

        layout.addWidget(self.table)

        self.sop_overlay_box = QGroupBox("Effective Schedule (Read-Only)")
        self.sop_overlay_box.setVisible(False)
        sop_overlay_layout = QVBoxLayout(self.sop_overlay_box)
        sop_overlay_layout.setContentsMargins(6, 6, 6, 6)
        sop_overlay_layout.setSpacing(4)
        self.sop_overlay_summary_label = QLabel("Projection hidden.")
        self.sop_overlay_summary_label.setWordWrap(True)
        sop_overlay_layout.addWidget(self.sop_overlay_summary_label)
        self.sop_overlay_table = QTableWidget()
        self.sop_overlay_table.setColumnCount(8)
        self.sop_overlay_table.setHorizontalHeaderLabels(
            [
                "Source",
                "Day",
                "Group",
                "Band",
                "Freq (MHz)",
                "Start",
                "End",
                "Detail",
            ]
        )
        self.sop_overlay_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sop_overlay_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.sop_overlay_table.setFocusPolicy(Qt.NoFocus)
        self.sop_overlay_table.verticalHeader().setVisible(False)
        self.sop_overlay_table.setSortingEnabled(True)
        overlay_hv = self.sop_overlay_table.horizontalHeader()
        overlay_hv.setSortIndicator(1, Qt.AscendingOrder)
        overlay_hv.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        overlay_hv.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        overlay_hv.setSectionResizeMode(2, QHeaderView.Stretch)
        overlay_hv.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        overlay_hv.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        overlay_hv.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        overlay_hv.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        overlay_hv.setSectionResizeMode(7, QHeaderView.Stretch)
        sop_overlay_layout.addWidget(self.sop_overlay_table)
        layout.addWidget(self.sop_overlay_box)

        self.add_row_btn = QPushButton("Add Row")
        self.del_row_btn = QPushButton("Delete Selected")
        self.view_edit_btn = QPushButton("View/Edit")
        self.view_edit_btn.setCheckable(True)
        self.view_edit_btn.setToolTip("Show or hide the full editable HF schedule fields.")
        self.move_to_resources_btn = QPushButton("Copy Selected to Library")
        self.move_to_resources_btn.setVisible(False)
        self.resources_resolve_btn = QPushButton("Resolve Conflicts")
        self.schedule_source_label = QLabel("HF Daily Schedule:")
        self.schedule_source_combo = QComboBox()
        self.schedule_source_combo.setObjectName("dailyScheduleSourceCombo")
        self.schedule_source_combo.setEditable(True)
        self.schedule_source_combo.setInsertPolicy(QComboBox.NoInsert)
        self.schedule_source_combo.setMinimumWidth(360)
        if self.schedule_source_combo.lineEdit() is not None:
            self.schedule_source_combo.lineEdit().setPlaceholderText("Name or select a daily schedule")
        self.schedule_source_combo.setToolTip(
            "Select a saved HF Daily schedule, or type a clear name here before Save / Update."
        )
        self.new_source_btn = QPushButton("New Schedule")
        self.new_source_btn.setToolTip("Start a blank HF Daily schedule. A name typed before or after New Schedule is retained until Save.")
        self.rename_source_btn = QPushButton("Rename Schedule")
        self.rename_source_btn.setToolTip("Rename the selected HF Daily schedule without changing its rows.")
        self.save_btn = QPushButton("Assign with RF Guard")
        self.save_btn.setToolTip(
            "Save this named schedule, then use Plan Builder to blend and assign it to radio(s) with RF Guard checks."
        )
        self.save_source_btn = QPushButton("Save / Update Schedule")
        self.save_source_btn.setToolTip("Save the visible rows as the selected HF Daily schedule, or create a new named schedule.")
        self.delete_source_btn = QPushButton("Delete Schedule")
        self.delete_source_btn.setToolTip("Delete the selected saved HF Daily schedule. The live schedule is not changed.")
        self.import_export_btn = QToolButton()
        self.import_export_btn.setText("Import/Export")
        self.import_export_btn.setPopupMode(QToolButton.InstantPopup)
        self.import_export_btn.setFont(self.add_row_btn.font())
        self.import_export_menu = QMenu(self.import_export_btn)
        self.import_hf_schedule_action = self.import_export_menu.addAction("Import HF Schedule")
        self.export_hf_schedule_action = self.import_export_menu.addAction("Export HF Schedule")
        self.import_export_btn.setMenu(self.import_export_menu)

        resources_header = QHBoxLayout()
        resources_header.addWidget(QLabel("<h3>Daily Row Library</h3>"))
        resources_header.addStretch()
        self.resources_count_label = QLabel("")
        self.resources_count_label.setObjectName("dailyScheduleResourcesCount")
        resources_header.addWidget(self.resources_count_label)
        layout.addLayout(resources_header)
        self.resources_empty_label = QLabel(
            "No saved HF Daily schedules yet. Save a named schedule above to make its rows available here."
        )
        self.resources_empty_label.setObjectName("dailyScheduleResourcesEmptyState")
        self.resources_empty_label.setWordWrap(True)
        self.resources_empty_label.setVisible(False)
        layout.addWidget(self.resources_empty_label)

        self.resources_set_label = QLabel("Library:")
        self.resources_set_combo = QComboBox()
        self.resources_set_combo.addItem("All schedules", "All")
        self.resources_set_combo.setMinimumWidth(260)
        self.resources_filter_label = QLabel("Filter:")
        self.resources_group_filter = QLineEdit()
        self.resources_group_filter.setPlaceholderText("Search schedule, group, band, time...")
        self.resources_group_filter.setMaximumWidth(360)
        self.add_to_schedule_btn = QToolButton()
        self.add_to_schedule_btn.setPopupMode(QToolButton.MenuButtonPopup)
        self.add_to_schedule_btn.setFont(self.add_row_btn.font())
        add_menu = QMenu(self.add_to_schedule_btn)
        self.add_selected_resource_action = QAction("Add Selected Rows", self)
        self.add_filtered_resource_action = QAction("Add Filtered Rows", self)
        add_menu.addAction(self.add_selected_resource_action)
        add_menu.addAction(self.add_filtered_resource_action)
        self.add_to_schedule_btn.setMenu(add_menu)
        self.add_to_schedule_default_action = QAction("Add Selected Rows", self)
        self.add_to_schedule_btn.setDefaultAction(self.add_to_schedule_default_action)
        self.add_to_schedule_btn.setToolTip(
            "Copy reusable library rows into the HF Daily schedule being edited. Library rows stay saved."
        )
        self.resources_delete_btn = QPushButton("Delete Library Rows")
        self.resources_delete_btn.setVisible(False)
        self.resources_refresh_btn = QPushButton("Refresh")
        self._daily_resource_filter_layout = QGridLayout()
        self._daily_resource_filter_layout.setContentsMargins(0, 0, 0, 0)
        self._daily_resource_filter_layout.setSpacing(8)
        layout.addLayout(self._daily_resource_filter_layout)
        self._arrange_daily_action_rows(compact=False)

        self.resources_table = QTableWidget()
        self.resources_table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.resources_table.setColumnCount(self.RES_COL_CONFLICT + 1)
        self.resources_table.setHorizontalHeaderLabels(
            [
                "Select",
                "Schedule",
                "Day",
                "Group Name",
                "Mode",
                "Band",
                "Freq (MHz)",
                "Start",
                "End",
                "Saved From",
                "Age",
                "Conflict",
            ]
        )
        self.resources_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.resources_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.resources_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.resources_table.verticalHeader().setVisible(False)
        self.resources_table.setSortingEnabled(True)
        self.resources_table.setMinimumHeight(220)
        resources_hv = self.resources_table.horizontalHeader()
        resources_hv.setSectionResizeMode(self.RES_COL_SELECT, QHeaderView.ResizeToContents)
        resources_hv.setSectionResizeMode(self.RES_COL_SET, QHeaderView.ResizeToContents)
        resources_hv.setSectionResizeMode(self.RES_COL_DAY, QHeaderView.ResizeToContents)
        resources_hv.setSectionResizeMode(self.RES_COL_GROUP, QHeaderView.Stretch)
        resources_hv.setSectionResizeMode(self.RES_COL_MODE, QHeaderView.ResizeToContents)
        resources_hv.setSectionResizeMode(self.RES_COL_BAND, QHeaderView.ResizeToContents)
        resources_hv.setSectionResizeMode(self.RES_COL_FREQ, QHeaderView.ResizeToContents)
        resources_hv.setSectionResizeMode(self.RES_COL_START, QHeaderView.ResizeToContents)
        resources_hv.setSectionResizeMode(self.RES_COL_END, QHeaderView.ResizeToContents)
        resources_hv.setSectionResizeMode(self.RES_COL_SOURCE, QHeaderView.ResizeToContents)
        resources_hv.setSectionResizeMode(self.RES_COL_UPDATED, QHeaderView.ResizeToContents)
        resources_hv.setSectionResizeMode(self.RES_COL_CONFLICT, QHeaderView.Stretch)
        self.resources_table.setColumnWidth(self.RES_COL_SET, 210)
        self.resources_table.setColumnWidth(self.RES_COL_GROUP, 150)
        self.resources_table.setColumnWidth(self.RES_COL_UPDATED, 90)
        layout.addWidget(self.resources_table, 1)

        # Signals
        self.add_row_btn.clicked.connect(self._add_row)
        self.del_row_btn.clicked.connect(self._delete_selected_rows)
        self.view_edit_btn.toggled.connect(self._apply_compact_schedule_view)
        self.move_to_resources_btn.clicked.connect(self._move_selected_schedule_rows_to_resources)
        self.schedule_source_combo.currentIndexChanged.connect(self._on_freqplanner_source_selected)
        self.new_source_btn.clicked.connect(self._on_new_freqplanner_source_clicked)
        self.rename_source_btn.clicked.connect(self._on_rename_freqplanner_source_clicked)
        self.save_btn.clicked.connect(self._on_assign_with_rf_guard_clicked)
        self.save_source_btn.clicked.connect(self._on_save_freqplanner_source_clicked)
        self.delete_source_btn.clicked.connect(self._on_delete_freqplanner_source_clicked)
        self.table.itemSelectionChanged.connect(self._update_delete_button_state)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.show_sop_overlay_chk.toggled.connect(self._on_toggle_sop_overlay_visibility)
        self.resources_set_combo.currentIndexChanged.connect(self._populate_schedule_resources_table)
        self.resources_group_filter.textChanged.connect(self._populate_schedule_resources_table)
        self.resources_table.itemSelectionChanged.connect(self._update_resource_action_state)
        self.add_to_schedule_default_action.triggered.connect(self._add_resources_default)
        self.add_selected_resource_action.triggered.connect(self._add_selected_resources_to_schedule)
        self.add_filtered_resource_action.triggered.connect(self._add_filtered_resources_to_schedule)
        self.resources_delete_btn.clicked.connect(self._delete_selected_resources)
        self.resources_resolve_btn.clicked.connect(self._resolve_resource_conflicts)
        self.resources_refresh_btn.clicked.connect(lambda: self._refresh_schedule_resources(force=True))
        self.import_hf_schedule_action.triggered.connect(self._import_schedule)
        self.export_hf_schedule_action.triggered.connect(self._export_schedule)

        self._table_conflict_refresh_timer = QTimer(self)
        self._table_conflict_refresh_timer.setSingleShot(True)
        self._table_conflict_refresh_timer.timeout.connect(self._flush_table_conflict_refresh)

        # Initialize clock labels once
        self._update_clock_labels()
        self._update_effective_source_label()
        self._update_suspend_state()
        self._apply_theme(refresh_dynamic=False)
        self._apply_compact_schedule_view(False)
        self._update_delete_button_state()
        self._update_resource_action_state()
        self._refresh_freqplanner_source_combo()
        self._sync_selected_freqplanner_source_table()
        self._update_header_title()
        self._update_daily_responsive_layout()
        self._update_source_action_state()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._queue_daily_geometry_refresh()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange):
            self._queue_daily_geometry_refresh()

    def _queue_daily_geometry_refresh(self) -> None:
        if not hasattr(self, "_daily_geometry_timer") or self._daily_geometry_pending:
            return
        self._daily_geometry_pending = True
        self._daily_geometry_timer.start(0)

    def _refresh_daily_geometry(self) -> None:
        self._daily_geometry_pending = False
        self._responsive_layout_mode = ""
        self._update_daily_responsive_layout()
        self._apply_schedule_table_height_hints()

    def _daily_responsive_mode_for_width(self, width: int) -> str:
        try:
            controls = [
                self.time_toggle_btn, self.schedule_source_label,
                self.save_source_btn, self.new_source_btn,
                self.rename_source_btn, self.delete_source_btn,
            ]
            natural = sum(control.sizeHint().width() for control in controls)
            natural += self.schedule_source_combo.fontMetrics().horizontalAdvance("M" * 24)
            natural += self._daily_action_layout.spacing() * len(controls)
            return "compact" if int(width) < natural else "wide"
        except Exception:
            return "compact" if int(width) < int(self._responsive_compact_width) else "wide"

    def _current_radio_context_name(self) -> str:
        try:
            win = self.window()
            combo = getattr(win, "station_command_radio_combo", None)
            if combo is not None:
                text = str(combo.currentText() or "").strip()
                if text:
                    return text
        except Exception:
            pass
        try:
            profile = MultiRadioStore().get_runtime_primary_device_profile()
            name = str((profile or {}).get("name") or "").strip()
            if name:
                role = "SDR" if str((profile or {}).get("device_class") or "").strip().lower() == "observer" else "HF"
                return f"{name} ({role})"
        except Exception:
            pass
        return ""

    def _update_header_title(self) -> None:
        if not hasattr(self, "header_title_label"):
            return
        self.header_title_label.setText("<h3>HF Daily Source Schedule</h3>")
        self.header_title_label.setToolTip(
            "Edit a reusable Daily schedule source. Assign linked Frequency Plans to radio(s) through Plan Builder or Schedule Assignment so RF Guard can validate the result."
        )

    def _update_daily_responsive_layout(self) -> None:
        if not hasattr(self, "_daily_resource_filter_layout"):
            return
        mode = self._daily_responsive_mode_for_width(int(self.width() or 0))
        if mode == self._responsive_layout_mode and self._daily_resource_filter_layout.count() > 0:
            return
        self._responsive_layout_mode = mode
        self._arrange_daily_action_rows(compact=(mode == "compact"))
        self._apply_daily_compact_table_sizing(compact=(mode == "compact"))

    @staticmethod
    def _clear_grid_layout(layout: QGridLayout) -> None:
        while layout.count():
            layout.takeAt(0)

    @staticmethod
    def _place_grid_widgets(layout: QGridLayout, placements: list[tuple]) -> None:
        for col in range(12):
            layout.setColumnStretch(col, 0)
        for item in placements:
            widget, row, col, *span = item
            row_span, col_span = span if span else (1, 1)
            layout.addWidget(widget, row, col, row_span, col_span)

    def _arrange_daily_action_rows(self, *, compact: bool) -> None:
        for grid in (self._daily_action_layout, self._daily_resource_filter_layout):
            self._clear_grid_layout(grid)

        for row in self._daily_toolbar_pool + self._daily_filter_pool:
            while row.count():
                row.takeAt(0)
        self._daily_toolbar_rows.clear()
        self._daily_filter_rows.clear()

        self.schedule_source_combo.setMinimumWidth(0)
        self.schedule_source_combo.setMaximumWidth(self.schedule_source_combo.fontMetrics().horizontalAdvance("M" * 40))
        if compact:
            self.schedule_source_combo.setProperty("fio_no_auto_fit", True)
            self.schedule_source_combo.setMinimumContentsLength(18)
            self.schedule_source_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.schedule_source_combo.setMaximumWidth(
                self.schedule_source_combo.fontMetrics().horizontalAdvance("M" * 20)
            )
        else:
            self.schedule_source_combo.setProperty("fio_no_auto_fit", False)
            self.schedule_source_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.resources_set_combo.setMinimumWidth(
            0 if compact else self.resources_set_combo.fontMetrics().horizontalAdvance("M" * 18)
        )
        self.resources_set_combo.setMaximumWidth(self.resources_set_combo.fontMetrics().horizontalAdvance("M" * 28))
        self.resources_group_filter.setMaximumWidth(
            self.resources_group_filter.fontMetrics().horizontalAdvance("M" * (48 if not compact else 64))
        )

        def add_toolbar_row(widgets: list[QWidget]) -> None:
            row = self._daily_toolbar_pool[len(self._daily_toolbar_rows)]
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(self._daily_action_layout.spacing())
            for widget in widgets:
                if widget is self.schedule_source_combo:
                    row.addWidget(widget, 1)
                else:
                    row.addWidget(widget, 0, Qt.AlignLeft)
            row.addStretch()
            self._daily_action_layout.addLayout(row, len(self._daily_toolbar_rows), 0)
            self._daily_toolbar_rows.append(row)

        context = [self.time_toggle_btn, self.schedule_source_label, self.schedule_source_combo]
        management = [self.new_source_btn, self.rename_source_btn, self.delete_source_btn]
        editing = [self.add_row_btn, self.del_row_btn, self.view_edit_btn, self.resources_resolve_btn]
        utilities = [self.import_export_btn, self.save_btn]
        if compact:
            add_toolbar_row(context + [self.save_source_btn])
            add_toolbar_row(management)
            add_toolbar_row(editing)
            add_toolbar_row(utilities)
        else:
            add_toolbar_row(context + [self.save_source_btn] + management)
            add_toolbar_row(editing + utilities)

        filter_widgets = [self.resources_set_label, self.resources_set_combo,
                          self.resources_filter_label, self.resources_group_filter]
        filter_row = self._daily_filter_pool[0]
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(self._daily_resource_filter_layout.spacing())
        for widget in filter_widgets:
            if widget is self.resources_group_filter:
                filter_row.addWidget(widget, 1)
            else:
                filter_row.addWidget(widget, 0, Qt.AlignLeft)
        self._daily_resource_filter_layout.addLayout(filter_row, 0, 0)
        self._daily_filter_rows.append(filter_row)
        if compact:
            action_row = self._daily_filter_pool[1]
            action_row.setContentsMargins(0, 0, 0, 0)
            action_row.setSpacing(self._daily_resource_filter_layout.spacing())
            for widget in (self.add_to_schedule_btn, self.resources_refresh_btn):
                action_row.addWidget(widget, 0, Qt.AlignLeft)
            action_row.addStretch()
            self._daily_resource_filter_layout.addLayout(action_row, 1, 0)
            self._daily_filter_rows.append(action_row)
        else:
            for widget in (self.add_to_schedule_btn, self.resources_refresh_btn):
                filter_row.addWidget(widget, 0, Qt.AlignLeft)
        filter_row.addStretch()
        self._size_daily_group_column(compact=compact)
        self._apply_schedule_table_height_hints()

    def _size_daily_group_column(self, *, compact: bool) -> None:
        if not hasattr(self, "table"):
            return
        header = self.table.horizontalHeader()
        if compact:
            header.setSectionResizeMode(self.COL_GROUP, QHeaderView.Stretch)
            return
        header.setSectionResizeMode(self.COL_GROUP, QHeaderView.Interactive)
        try:
            unit = max(1, self.table.fontMetrics().horizontalAdvance("M"))
            viewport_width = max(1, self.table.viewport().width())
            target = max(unit * 16, min(unit * 36, int(viewport_width * 0.24)))
            self.table.setColumnWidth(self.COL_GROUP, target)
        except Exception:
            pass

    def _apply_schedule_table_height_hints(self) -> None:
        if not hasattr(self, "table") or not hasattr(self, "resources_table"):
            return
        try:
            row_height = control_height_for_font(self.table)
            for row in range(self.table.rowCount()):
                for col in range(self.table.columnCount()):
                    editor = self.table.cellWidget(row, col)
                    if editor is None:
                        continue
                    if isinstance(editor, QComboBox):
                        # The Group editor owns a flexible table column.  Its
                        # popup still fits long labels, while the closed
                        # editor must follow the cell through responsive
                        # resizes instead of imposing a long-label minimum.
                        if col == self.COL_GROUP:
                            editor.setProperty("fio_no_auto_fit", False)
                            fit_combo_box_to_contents(editor)
                            editor.setMinimumWidth(0)
                            editor.setMinimumContentsLength(12)
                            editor.setProperty("fio_no_auto_fit", True)
                        else:
                            fit_combo_box_to_contents(editor)
                    editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                    row_height = max(row_height, font_derived_widget_height(editor))
            self.table.verticalHeader().setMinimumSectionSize(row_height)
            self.table.verticalHeader().setDefaultSectionSize(row_height)
            self.table.horizontalHeader().setMinimumHeight(
                font_derived_widget_height(self.table.horizontalHeader())
            )
            self.table.doItemsLayout()
            row_count = max(1, int(self.table.rowCount()))
            visible_rows = max(4, min(row_count, 10))
            self.table.setMaximumHeight(
                item_view_height_for_rows(self.table, visible_rows=visible_rows)
            )
            self.resources_table.setMinimumHeight(
                item_view_height_for_rows(
                    self.resources_table,
                    visible_rows=4 if self.resources_table.isVisible() else 3,
                )
            )
        except Exception:
            pass

    def _refresh_freqplanner_source_combo(self) -> None:
        if not hasattr(self, "schedule_source_combo"):
            return
        selected = selected_source_set_id(self.settings, SELECTED_HF_DAILY_SOURCE_SET_KEY)
        self.schedule_source_combo.blockSignals(True)
        self.schedule_source_combo.clear()
        self.schedule_source_combo.addItem("Active Daily Schedule", LIVE_SOURCE_SET_ID)
        for row in source_sets_for_category(self.settings, HF_DAILY_SOURCE_SETS_KEY, HF_DAILY_SOURCE_CATEGORY):
            set_id = str(row.get("id") or "").strip()
            if set_id:
                self.schedule_source_combo.addItem(str(row.get("name") or set_id), set_id)
        idx = self.schedule_source_combo.findData(selected)
        self.schedule_source_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._editing_freqplanner_source_id = str(self.schedule_source_combo.currentData() or LIVE_SOURCE_SET_ID)
        self.schedule_source_combo.blockSignals(False)
        self._update_source_action_state()
        self._update_source_usage_label()

    def _update_source_usage_label(self) -> None:
        if not hasattr(self, "source_usage_label"):
            return
        set_id = self._selected_freqplanner_source_id()
        name = str(self.schedule_source_combo.currentText() or "Active Daily Schedule").strip()
        try:
            usage = plan_source_usage_summary(
                self.plan_context_service.store,
                category=HF_DAILY_SOURCE_CATEGORY,
                set_id=set_id,
                live_label="hf_daily",
            )
            usage_text = str(usage.get("text") or "").strip()
        except Exception as exc:
            log.debug("HF Daily: source usage summary skipped: %s", exc)
            usage_text = "Usage: --"
        self.source_usage_label.setText(f"<b>Editing:</b> {name or 'Daily schedule'} | {usage_text}")
        self.source_usage_label.setToolTip(
            "Named schedules stay linked to plans by default. Updating this source refreshes dependent plans after RF Guard review."
        )

    def _selected_freqplanner_source_id(self) -> str:
        if not hasattr(self, "schedule_source_combo"):
            return str(getattr(self, "_editing_freqplanner_source_id", "") or LIVE_SOURCE_SET_ID)
        set_id = str(self.schedule_source_combo.currentData() or "").strip()
        return set_id or str(getattr(self, "_editing_freqplanner_source_id", "") or LIVE_SOURCE_SET_ID)

    def _update_source_action_state(self) -> None:
        set_id = self._selected_freqplanner_source_id()
        is_saved = bool(set_id and set_id != LIVE_SOURCE_SET_ID)
        if hasattr(self, "delete_source_btn"):
            self.delete_source_btn.setEnabled(is_saved)
        if hasattr(self, "rename_source_btn"):
            self.rename_source_btn.setEnabled(is_saved)

    def _on_new_freqplanner_source_clicked(self) -> None:
        if not hasattr(self, "schedule_source_combo"):
            return
        if not self._confirm_discard_unsaved_source_load():
            return
        current_name = str(self.schedule_source_combo.currentText() or "").strip()
        current_index = self.schedule_source_combo.currentIndex()
        selected_label = (
            str(self.schedule_source_combo.itemText(current_index) or "").strip()
            if current_index >= 0
            else ""
        )
        # Text that differs from the selected item is an operator-entered new
        # name. Preserve it while detaching from the saved source. Merely
        # selecting an existing schedule must not copy its name into the new
        # draft.
        draft_name = current_name if current_index < 0 or current_name != selected_label else ""
        self._editing_freqplanner_source_id = LIVE_SOURCE_SET_ID
        self.schedule_source_combo.blockSignals(True)
        self.schedule_source_combo.setCurrentIndex(-1)
        self.schedule_source_combo.setEditText(draft_name)
        self.schedule_source_combo.blockSignals(False)
        self.settings.set(SELECTED_HF_DAILY_SOURCE_SET_KEY, LIVE_SOURCE_SET_ID)
        try:
            self.settings.save()
        except Exception:
            pass
        self._update_source_action_state()
        self._update_source_usage_label()
        line_edit = self.schedule_source_combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("New HF Daily schedule name")
            line_edit.setFocus(Qt.OtherFocusReason)
        self._load_source_rows_into_table([])
        self._refresh_freq_planner()

    def _on_freqplanner_source_selected(self, *_args: Any) -> None:
        if not hasattr(self, "schedule_source_combo"):
            return
        set_id = str(self.schedule_source_combo.currentData() or LIVE_SOURCE_SET_ID)
        previous_id = str(getattr(self, "_editing_freqplanner_source_id", "") or LIVE_SOURCE_SET_ID)
        if set_id != previous_id and not self._confirm_discard_unsaved_source_load():
            self.schedule_source_combo.blockSignals(True)
            idx = self.schedule_source_combo.findData(previous_id)
            self.schedule_source_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.schedule_source_combo.blockSignals(False)
            self._update_source_action_state()
            self._update_source_usage_label()
            return
        self._editing_freqplanner_source_id = set_id
        self.settings.set(SELECTED_HF_DAILY_SOURCE_SET_KEY, set_id)
        self._update_source_action_state()
        self._update_source_usage_label()
        try:
            self.settings.save()
        except Exception:
            pass
        self._load_selected_freqplanner_source_now()
        self._refresh_freq_planner()

    def _selected_freqplanner_source_row(self) -> Optional[Dict[str, Any]]:
        if not hasattr(self, "schedule_source_combo"):
            return None
        set_id = str(self.schedule_source_combo.currentData() or "").strip()
        if not set_id:
            set_id = str(getattr(self, "_editing_freqplanner_source_id", "") or LIVE_SOURCE_SET_ID)
        return source_set_row_by_id_for_category(
            self.settings,
            HF_DAILY_SOURCE_SETS_KEY,
            HF_DAILY_SOURCE_CATEGORY,
            set_id,
        )

    def _current_freqplanner_source_name(self) -> str:
        if not hasattr(self, "schedule_source_combo"):
            return ""
        text = str(self.schedule_source_combo.currentText() or "").strip()
        if text == "Active Daily Schedule":
            return ""
        return text

    @staticmethod
    def _default_daily_schedule_name() -> str:
        return "HF Daily Schedule"

    @staticmethod
    def _format_age_label(value: Any, *, now: Optional[datetime.datetime] = None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "--"
        try:
            normalized = raw.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            dt = dt.astimezone(datetime.timezone.utc)
            current = now or datetime.datetime.now(datetime.timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=datetime.timezone.utc)
            delta = current.astimezone(datetime.timezone.utc) - dt
            seconds = max(0, int(delta.total_seconds()))
        except Exception:
            return "--"
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} min"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} h"
        days = hours // 24
        if days < 14:
            return f"{days} day" if days == 1 else f"{days} days"
        weeks = days // 7
        if days < 60:
            return f"{weeks} wk" if weeks == 1 else f"{weeks} wks"
        months = max(1, days // 30)
        if months < 24:
            return f"{months} mo"
        years = max(1, days // 365)
        return f"{years} yr" if years == 1 else f"{years} yrs"

    def _load_source_rows_into_table(self, rows: List[Dict[str, Any]]) -> None:
        rows = [normalize_schedule_target_fields(dict(row)) for row in rows if isinstance(row, dict)]
        self._suspend_dirty_tracking = True
        try:
            self.table.setRowCount(0)
            self._raw_schedule = rows
            for entry in rows:
                self._append_entry_row(self._entry_for_display(entry))
            if self.table.rowCount() == 0:
                self._add_row()
        finally:
            self._suspend_dirty_tracking = False
        self._set_headers()
        self._apply_compact_schedule_view()
        self._update_clock_labels()
        self._saved_rows_signature = self._rows_signature(self._collect_rows_for_signature())
        self.table.clearSelection()
        self._set_dirty(False)
        self._invalidate_active_schedule_views()
        self._highlight_time_conflicts()
        self._update_resource_action_state()
        self._apply_schedule_table_height_hints()

    def _confirm_discard_unsaved_source_load(self) -> bool:
        if not bool(getattr(self, "_dirty", False)):
            return True
        response = QMessageBox.question(
            self,
            "Load Schedule",
            "Load the selected HF Daily schedule? Unsaved edits in the current table will be discarded.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return response == QMessageBox.Yes

    def _load_selected_freqplanner_source_now(self) -> None:
        row = self._selected_freqplanner_source_row()
        if row is None:
            self._load_schedule()
            return
        self._load_source_rows_into_table([dict(item) for item in row.get("rows", []) if isinstance(item, dict)])

    def _sync_selected_freqplanner_source_table(self) -> None:
        if bool(getattr(self, "_dirty", False)):
            return
        row = self._selected_freqplanner_source_row()
        if row is None:
            return
        source_rows = [dict(item) for item in row.get("rows", []) if isinstance(item, dict)]
        if self._rows_signature(source_rows) == self._rows_signature(self._collect_rows_for_signature()):
            return
        self._load_source_rows_into_table(source_rows)

    def _on_load_freqplanner_source_clicked(self) -> None:
        if not self._confirm_discard_unsaved_source_load():
            return
        self._load_selected_freqplanner_source_now()

    def _on_rename_freqplanner_source_clicked(self) -> None:
        row = self._selected_freqplanner_source_row()
        if row is None:
            QMessageBox.information(self, "Rename Schedule", "Select a saved HF Daily schedule before renaming.")
            return
        set_id = str(row.get("id") or self._selected_freqplanner_source_id()).strip()
        new_name = self._current_freqplanner_source_name()
        old_name = str(row.get("name") or "").strip()
        if not new_name:
            QMessageBox.warning(self, "Rename Schedule", "Type a clear HF Daily schedule name before renaming.")
            return
        if old_name and new_name == old_name:
            QMessageBox.information(self, "Rename Schedule", f"'{new_name}' is already the selected schedule name.")
            return
        try:
            saved = rename_source_schedule(
                self.settings,
                HF_DAILY_SOURCE_CATEGORY,
                SELECTED_HF_DAILY_SOURCE_SET_KEY,
                set_id,
                new_name,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Rename Failed", f"Could not rename HF Daily schedule:\n{exc}")
            return
        self._refresh_freqplanner_source_combo()
        if hasattr(self, "resources_set_combo"):
            self._refresh_schedule_resources(force=True)
        self._refresh_freq_planner()
        QMessageBox.information(self, "HF Daily Schedule Renamed", f"Renamed schedule to '{saved['name']}'.")

    def _prompt_for_freqplanner_source_name(self, title: str, label: str, default_name: str) -> Tuple[str, bool]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        prompt = QLabel(label)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        name_edit = QLineEdit(str(default_name or "").strip())
        name_edit.setObjectName("dailyScheduleFreqPlannerSourceNameEdit")
        name_edit.selectAll()
        layout.addWidget(name_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        name_edit.setFocus(Qt.OtherFocusReason)
        if dialog.exec() != QDialog.Accepted:
            return "", False
        return name_edit.text().strip(), True

    def _commit_active_table_editor(self) -> None:
        """Ask Qt to commit an in-progress table edit without pumping events."""
        fw = QApplication.focusWidget()
        if fw is None or not self.table.isAncestorOf(fw):
            return
        try:
            fw.clearFocus()
            self.table.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass

    def _source_rows_for_freqplanner_snapshot(self) -> List[Dict[str, Any]]:
        self._commit_active_table_editor()

        hf_rows: List[Dict[str, Any]] = []
        format_errors: List[str] = []
        for r in range(self.table.rowCount()):
            if self._is_sop_overlay_row(r):
                continue
            day = self._get_combo_value(r, self.COL_DAY, default="ALL")
            group_name = self._get_combo_value(r, self.COL_GROUP, default="")
            mode = self._get_combo_value(r, self.COL_MODE, default="Digi")
            band = self._get_combo_value(r, self.COL_BAND, default="")
            freq_text = self._get_text_value(r, self.COL_FREQ)
            start_val = self._get_text_value(r, self.COL_START)
            end_val = self._get_text_value(r, self.COL_END)
            auto_tune = self._get_checkbox_value(r, self.COL_AUTOTUNE)
            target_scope, target_device_profile_id, target_operating_profile_id = self._selected_schedule_target(r)

            if group_name and band and mode and not freq_text:
                resolution = resolve_operating_group_frequency(
                    self.operating_groups, group=group_name, band=band, mode=mode
                )
                if resolution.state == "ambiguous":
                    format_errors.append(f"Row {r+1}: Select a frequency for {group_name} {band}.")
                    continue
            if not group_name or not band or not freq_text or not start_val or not end_val:
                continue
            if target_scope == TARGET_SCOPE_DEVICE_PROFILE and target_device_profile_id is None:
                format_errors.append(f"Row {r+1}: Device-targeted rows require a device profile.")
                continue
            if target_scope == TARGET_SCOPE_OPERATING_PROFILE and target_operating_profile_id is None:
                format_errors.append(f"Row {r+1}: Frequency Plan-targeted rows require a Frequency Plan.")
                continue
            if not self._validate_frequency(band, mode, freq_text):
                return []
            freq_text = self._format_freq(freq_text)
            start_val = self._normalize_hhmm(start_val)
            end_val = self._normalize_hhmm(end_val)
            if not self._validate_time(start_val) or not self._validate_time(end_val):
                format_errors.append(f"Row {r+1}: Start/End must be HH:MM (24h)")
                continue
            self._set_text_value(r, self.COL_START, start_val)
            self._set_text_value(r, self.COL_END, end_val)
            if self._show_local:
                day_utc, start_utc = self._convert_day_time(day, start_val, to_local=False)
                _, end_utc = self._convert_day_time(day, end_val, to_local=False)
            else:
                day_utc = day
                start_utc = start_val
                end_utc = end_val
            hf_rows.append(
                normalize_schedule_target_fields(
                    {
                        "day_utc": day_utc,
                        "band": band,
                        "mode": mode,
                        "vfo": "A",
                        "frequency": freq_text,
                        "start_utc": start_utc,
                        "end_utc": end_utc,
                        "group_name": group_name,
                        "fldigi_offset": "",
                        "js8_offset": "",
                        "primary_js8call_group": "",
                        "comment": "",
                        "auto_tune": bool(auto_tune),
                        "target_scope": target_scope,
                        "target_device_profile_id": target_device_profile_id,
                        "target_operating_profile_id": target_operating_profile_id,
                    }
                )
            )
        if format_errors:
            raise ValueError("Fix formatting issues before saving:\n" + "\n".join(format_errors))
        return hf_rows

    def _on_save_freqplanner_source_clicked(self) -> None:
        try:
            rows = self._source_rows_for_freqplanner_snapshot()
        except ValueError as exc:
            QMessageBox.warning(self, "Save Blocked", str(exc))
            return
        if not rows:
            QMessageBox.warning(
                self,
                "No HF Daily Rows",
                "Add at least one HF Daily row before saving this schedule.",
            )
            return
        selected = self._selected_freqplanner_source_row()
        existing_id = int(selected.get("db_id", 0) or 0) if selected else 0
        name = self._current_freqplanner_source_name()
        if not name:
            name = self._default_daily_schedule_name()
            if hasattr(self, "schedule_source_combo"):
                self.schedule_source_combo.setEditText(name)
        if selected:
            selected_name = str(selected.get("name") or "").strip()
            existing_rows_signature = self._rows_signature([dict(item) for item in selected.get("rows", []) if isinstance(item, dict)])
            new_rows_signature = self._rows_signature(rows)
            if name and selected_name and name != selected_name and existing_rows_signature == new_rows_signature:
                QMessageBox.information(
                    self,
                    "Rename Schedule",
                    "Use Rename Schedule to change the HF Daily schedule name without updating row data.",
                )
                return
        if not selected:
            existing_id = 0
        if existing_id and not self._confirm_rf_guard_source_update(
            HF_DAILY_SOURCE_CATEGORY,
            f"plan:{existing_id}",
            rows,
            name,
        ):
            return
        try:
            saved = save_source_schedule(
                self.settings,
                HF_DAILY_SOURCE_CATEGORY,
                SELECTED_HF_DAILY_SOURCE_SET_KEY,
                name,
                rows,
                existing_plan_id=existing_id or None,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", f"Could not save HF Daily schedule:\n{exc}")
            return
        updated_plans: List[Dict[str, Any]] = []
        saved_id = str(saved.get("id") or "").strip()
        if saved_id:
            try:
                updated_plans = reproject_frequency_plans_for_source_update(
                    self.settings,
                    HF_DAILY_SOURCE_CATEGORY,
                    saved_id,
                    rows,
                )
            except Exception as exc:
                log.exception("HF Daily Schedule: failed refreshing dependent Frequency Plans.")
                QMessageBox.warning(
                    self,
                    "Plan Refresh Warning",
                    "The HF Daily schedule was saved, but CommsPlan could not refresh dependent Frequency Plans.\n\n"
                    f"{exc}",
                )
        try:
            self.plan_context_service.invalidate()
        except Exception:
            pass
        if hasattr(self, "schedule_source_combo"):
            self._refresh_freqplanner_source_combo()
            if saved_id:
                idx = self.schedule_source_combo.findData(saved_id)
                if idx >= 0:
                    self.schedule_source_combo.blockSignals(True)
                    self.schedule_source_combo.setCurrentIndex(idx)
                    self.schedule_source_combo.blockSignals(False)
                    self._editing_freqplanner_source_id = saved_id
        if hasattr(self, "table"):
            self._load_source_rows_into_table(rows)
        if hasattr(self, "resources_set_combo"):
            self._refresh_schedule_resources(force=True)
        self._refresh_freq_planner()
        verb = "Updated" if existing_id else "Saved"
        plan_note = f" Refreshed {len(updated_plans)} dependent Frequency Plan(s)." if updated_plans else ""
        QMessageBox.information(
            self,
            f"HF Daily Schedule {verb}",
            f"{verb} '{saved['name']}' with {len(rows)} HF Daily row(s).{plan_note} Select it in Plan Builder.",
        )

    def _confirm_rf_guard_source_update(
        self,
        category: str,
        set_id: str,
        rows: List[Dict[str, Any]],
        name: str,
    ) -> bool:
        try:
            impacts = assigned_plan_rf_guard_impacts_for_source_update(self.settings, category, set_id, rows)
        except Exception as exc:
            log.exception("HF Daily Schedule: RF Guard impact scan failed.")
            response = QMessageBox.question(
                self,
                "RF Guard Check Unavailable",
                "RF Guard could not check assigned master schedules before updating this HF Daily schedule.\n\n"
                f"{exc}\n\nSave the schedule anyway?",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            return response == QMessageBox.Save
        if not impacts:
            return True
        lines: List[str] = []
        blocked = False
        for impact in impacts:
            validation = impact.get("validation", {})
            state = str(validation.get("state") or "").strip().lower()
            blocked = blocked or state == "blocked"
            plan = impact.get("plan", {})
            device = impact.get("device", {})
            plan_name = str(plan.get("name") or "assigned Frequency Plan")
            radio_name = str(device.get("name") or f"Radio {impact.get('assignment', {}).get('device_profile_id')}")
            messages = [str(item) for item in validation.get("messages", []) if str(item or "").strip()]
            detail = messages[0] if messages else "RF Guard reported a schedule conflict."
            lines.append(f"- {radio_name} / {plan_name}: {detail}")
        body = (
            f"Updating '{name}' affects one or more master schedules assigned to radios.\n\n"
            + "\n".join(lines[:6])
        )
        if len(lines) > 6:
            body += f"\n- +{len(lines) - 6} more"
        if blocked:
            QMessageBox.warning(self, "RF Guard Blocked Update", body + "\n\nFix the conflict before saving this update.")
            return False
        response = QMessageBox.question(
            self,
            "RF Guard Warning",
            body + "\n\nSave this HF Daily schedule update anyway?",
            QMessageBox.Save | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return response == QMessageBox.Save

    def _on_delete_freqplanner_source_clicked(self) -> None:
        row = self._selected_freqplanner_source_row()
        if row is None:
            return
        name = str(row.get("name") or "selected HF Daily schedule")
        response = QMessageBox.question(
            self,
            "Delete HF Daily Schedule",
            f"Delete '{name}'? This removes the saved HF Daily schedule but does not change the live HF Daily schedule.",
            QMessageBox.Delete | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if response != QMessageBox.Delete:
            return
        try:
            delete_source_schedule(
                self.settings,
                HF_DAILY_SOURCE_SETS_KEY,
                SELECTED_HF_DAILY_SOURCE_SET_KEY,
                str(row.get("id") or ""),
            )
        except Exception as exc:
            QMessageBox.critical(self, "Delete Failed", f"Could not delete HF Daily schedule:\n{exc}")
            return
        if hasattr(self, "schedule_source_combo"):
            self._refresh_freqplanner_source_combo()
        if hasattr(self, "resources_set_combo"):
            self._refresh_schedule_resources(force=True)
        self._refresh_freq_planner()

    def _on_assign_with_rf_guard_clicked(self) -> None:
        QMessageBox.information(
            self,
            "Assign with RF Guard",
            "Save or update this HF Daily schedule, then select it in Plan Builder with the desired HF Net schedule. "
            "Save the blended Frequency Plan and assign that plan to radio(s) so RF Guard can validate the assignment.",
        )
        try:
            win = self.window()
            for name in ("show_freq_planner_tab", "open_freq_planner_tab", "switch_to_freq_planner"):
                fn = getattr(win, name, None)
                if callable(fn):
                    fn()
                    return
            tabs = getattr(win, "tabs", None) or getattr(win, "tab_widget", None)
            if tabs is not None:
                for idx in range(int(tabs.count())):
                    if "freq" in str(tabs.tabText(idx)).strip().lower() and "planner" in str(tabs.tabText(idx)).strip().lower():
                        tabs.setCurrentIndex(idx)
                        return
        except Exception:
            pass

    @staticmethod
    def _set_table_resize_modes(
        table: QTableWidget,
        resize_to_contents: set[int],
        stretch: set[int],
    ) -> None:
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        for col in range(table.columnCount()):
            if col in stretch:
                header.setSectionResizeMode(col, QHeaderView.Stretch)
            elif col in resize_to_contents:
                header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
            else:
                header.setSectionResizeMode(col, QHeaderView.Interactive)

    def _apply_daily_compact_table_sizing(self, *, compact: bool) -> None:
        if not hasattr(self, "table") or not hasattr(self, "resources_table"):
            return
        show_all = bool(getattr(self, "view_edit_btn", None) and self.view_edit_btn.isChecked())
        if compact and not show_all:
            self._set_table_resize_modes(
                self.table,
                {
                    self.COL_SELECT,
                    self.COL_DAY,
                    self.COL_MODE,
                    self.COL_BAND,
                    self.COL_FREQ,
                    self.COL_START,
                    self.COL_END,
                },
                {self.COL_GROUP},
            )
        else:
            self._set_table_resize_modes(
                self.table,
                {
                    self.COL_SELECT, self.COL_DAY, self.COL_SOURCE, self.COL_MODE,
                    self.COL_BAND, self.COL_FREQ, self.COL_START, self.COL_END,
                    self.COL_AUTOTUNE, self.COL_TARGET_SCOPE, self.COL_TARGET,
                },
                {self.COL_GROUP},
            )

        self._set_table_resize_modes(
            self.resources_table,
            {
                self.RES_COL_SELECT,
                self.RES_COL_SET,
                self.RES_COL_DAY,
                self.RES_COL_MODE,
                self.RES_COL_BAND,
                self.RES_COL_FREQ,
                self.RES_COL_START,
                self.RES_COL_END,
                self.RES_COL_SOURCE,
                self.RES_COL_UPDATED,
            },
            {self.RES_COL_GROUP, self.RES_COL_CONFLICT},
        )
        self._size_daily_group_column(compact=compact)
        self._apply_schedule_table_height_hints()

    def _load_operating_groups(self) -> List[Dict]:
        return ensure_operating_group_frequency_keys(qsy_load_operating_groups(self.settings))

    def _snapshot_operating_groups(self, og_list: List[Dict]) -> str:
        return qsy_snapshot_operating_groups(og_list)

    # ---------------- CLOCK / TIMEZONE (shared logic) ---------------- #

    def _setup_clock_timer(self):
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock_labels)

    def _setup_sop_panel_timer(self) -> None:
        self._sop_panel_timer = QTimer(self)
        self._sop_panel_timer.timeout.connect(lambda: self._refresh_sop_profiles_panel(force=False))
        self._sop_panel_timer.timeout.connect(lambda: self._refresh_schedule_resources(force=False))
        self._sop_panel_timer.timeout.connect(self._refresh_sop_overlay_rows_in_table)

    def _refresh_group_band_cells(self):
        """
        Update group/band combos and mode/frequency cells in-place based on refreshed operating_groups.
        """
        for r in range(self.table.rowCount()):
            if self._is_sop_overlay_row(r):
                continue
            group_combo = self.table.cellWidget(r, self.COL_GROUP)
            band_combo = self.table.cellWidget(r, self.COL_BAND)
            # repopulate group options
            if isinstance(group_combo, QComboBox):
                current_group = group_combo.currentText()
                group_combo.blockSignals(True)
                group_combo.clear()
                group_names = sorted({g.get("group", "") for g in self.operating_groups if g.get("group")})
                group_combo.addItems(group_names)
                if current_group in group_names:
                    group_combo.setCurrentText(current_group)
                group_combo.blockSignals(False)
            # repopulate band options based on selected group
            if isinstance(band_combo, QComboBox):
                current_band = band_combo.currentText()
                self._populate_band_combo(band_combo, self._get_combo_value(r, self.COL_GROUP, ""))
                if current_band and band_combo.findText(current_band) >= 0:
                    band_combo.setCurrentText(current_band)
            # refresh mode/freq cells
            self._update_mode_freq(r)
        self._update_clock_labels()

    def _current_timezone(self) -> tuple[str, datetime.tzinfo]:
        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        return tz_name, tz
    
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
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        utc_day = now_utc.strftime("%a")
        self.utc_label.setText(now_utc.strftime(f"<b>UTC ({utc_day}):</b> %y%m%d %H:%M:%S Z"))

        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        now_local = now_utc.astimezone(tz)
        # Prefer our short UI label, fall back to tzname or tz_name
        fallback = now_local.tzname() or tz_name
        ui_abbr = self._ui_tz_abbr(tz_name, fallback)

        local_day = now_local.strftime("%a")
        self.local_label.setText(
            now_local.strftime(f"<b>Local ({local_day}):</b> %y%m%d %H:%M:%S {ui_abbr}")
        )
        self.time_toggle_btn.setText("Times: Local" if self._show_local else "Times: UTC")
        self._update_time_toggle_style()
        self._update_effective_source_label()

    def _update_time_toggle_style(self, theme: Optional[Dict[str, str]] = None) -> None:
        if theme is None:
            theme = resolve_theme(self.settings)
        role = "info" if not self._show_local else "muted"
        self.time_toggle_btn.setStyleSheet(button_style(role, theme))

    def _update_effective_source_label(self, theme: Optional[Dict[str, str]] = None) -> None:
        if theme is None:
            theme = resolve_theme(self.settings)
        muted = str(theme.get("text_muted", "#888"))
        source_text = "Runtime Source: --"
        style = f"color: {muted};"
        tip = ""
        status: Optional[Dict[str, Any]] = None
        try:
            sched = getattr(self.window(), "scheduler", None)
            if sched and hasattr(sched, "get_status_summary"):
                status = sched.get_status_summary()
                source = str(status.get("source") or "").strip().upper()
                net_kind = str(status.get("net_kind") or "").strip()
                source_reason_detail = str(status.get("source_reason_detail") or "").strip()
                sop_contention = bool(status.get("sop_contention"))
                sop_profiles = [str(x).strip() for x in (status.get("sop_contention_profiles") or []) if str(x).strip()]
                sop_selected = str(status.get("sop_selected_profile") or "").strip()
                next_source = str(status.get("next_source") or "").strip().upper()
                next_net_kind = str(status.get("next_net_kind") or "").strip()
                next_source_change = bool(status.get("next_source_change"))
                source_key = source
                if source_key not in {"NET", "SOP", "HF"}:
                    if "NET" in source_key:
                        source_key = "NET"
                    elif "SOP" in source_key:
                        source_key = "SOP"
                    elif "HF" in source_key:
                        source_key = "HF"
                if source_key == "SOP":
                    source_text = "Runtime Source: SOP Layer"
                    style = f"font-weight: 600; color: {theme.get('warning', '#C99700')};"
                    tip = "SOP Layer currently overrides the baseline HF schedule."
                    if net_kind:
                        tip = f"{tip}\nSOP runtime kind: {net_kind}"
                    if sop_contention:
                        others = [p for p in sop_profiles if p and p != sop_selected]
                        if others:
                            source_text += " (Contention)"
                            tip = f"SOP contention: selected {sop_selected or 'winner'} over {', '.join(others[:4])}."
                        else:
                            source_text += " (Contention)"
                            tip = "SOP contention detected across active profiles."
                    if source_reason_detail:
                        tip = f"{tip}\nSelection: {source_reason_detail}"
                elif source_key == "NET":
                    source_text = "Runtime Source: Net Schedule"
                    style = f"font-weight: 600; color: {theme.get('info', '#1E88E5')};"
                    tip = "Active net schedule has highest precedence."
                    if net_kind:
                        tip = f"{tip}\nActive net kind: {net_kind}"
                    if source_reason_detail:
                        tip = f"{tip}\nSelection: {source_reason_detail}"
                elif source_key == "HF":
                    source_text = "Runtime Source: HF Schedule"
                    style = f"color: {theme.get('text', '#111')};"
                    tip = "Baseline HF schedule is active."
                    if net_kind:
                        tip = f"{tip}\nRuntime kind: {net_kind}"
                    if source_reason_detail:
                        tip = f"{tip}\nSelection: {source_reason_detail}"
                if next_source_change and next_source and next_source != source_key:
                    next_label = next_net_kind or next_source
                    tip = (f"{tip}\n" if tip else "") + f"Next source transition: {next_label}."
        except Exception:
            pass
        self.effective_source_label.setText(source_text)
        self.effective_source_label.setStyleSheet(style)
        self.effective_source_label.setToolTip(tip)
        self._update_runtime_source_summary(status=status, theme=theme)

    @staticmethod
    def _source_label(source: str, net_kind: str = "") -> str:
        src = str(source or "").strip().upper()
        kind = str(net_kind or "").strip()
        if kind:
            return kind
        if src == "NET":
            return "Net Schedule"
        if src == "SOP":
            return "SOP Layer"
        if src == "HF":
            return "HF Schedule"
        return "None"

    def _format_transition_time(self, when_utc: Any) -> str:
        if not isinstance(when_utc, datetime.datetime):
            return ""
        try:
            if self._show_local:
                _tz_name, tz = self._current_timezone()
                return when_utc.astimezone(tz).strftime("%a %H:%M")
            return when_utc.astimezone(datetime.timezone.utc).strftime("%a %H:%MZ")
        except Exception:
            return ""

    def _update_runtime_source_summary(
        self,
        *,
        status: Optional[Dict[str, Any]] = None,
        theme: Optional[Dict[str, str]] = None,
    ) -> None:
        if theme is None:
            theme = resolve_theme(self.settings)
        if status is None:
            try:
                sched = getattr(self.window(), "scheduler", None)
                if sched and hasattr(sched, "get_status_summary"):
                    status = sched.get_status_summary()
            except Exception:
                status = None
        if not status:
            self.sop_runtime_summary_label.setText("Now: -- | Next: --")
            self.sop_runtime_summary_label.setStyleSheet(f"color: {theme.get('text_muted', '#888')};")
            return
        now_source = self._source_label(str(status.get("source") or ""), str(status.get("net_kind") or ""))
        next_source = self._source_label(str(status.get("next_source") or ""), str(status.get("next_net_kind") or ""))
        next_change = bool(status.get("next_source_change"))
        transition_time = self._format_transition_time(status.get("next_transition_utc"))
        if next_change and next_source and next_source != "None":
            if transition_time:
                next_text = f"Next: {next_source} at {transition_time}"
            else:
                next_text = f"Next: {next_source}"
        else:
            next_text = "Next: no source change scheduled"
        self.sop_runtime_summary_label.setText(f"Now: {now_source} | {next_text}")
        self.sop_runtime_summary_label.setStyleSheet(f"color: {theme.get('text', '#111')};")

    @staticmethod
    def _sop_category_label(raw_category: str) -> str:
        text = str(raw_category or "").strip().upper()
        if text in {"HF + LOCAL NET", "LOCAL NET + HF", "SOP-MIXED", "MIXED"}:
            return "SOP-Mixed"
        if text in {"LOCAL NET", "SOP-LOCAL NET"}:
            return "SOP-Local Net"
        return "SOP-HF"

    def _load_sop_profile_catalog(self) -> List[Dict[str, Any]]:
        profiles: List[Dict[str, Any]] = []
        try:
            profiles = self._sop_manager.list_profiles_with_category()
        except Exception as e:
            log.debug("HF Schedule: failed loading SOP profiles: %s", e)
            profiles = []
        for profile in profiles:
            profile["sop_category"] = self._sop_category_label(str(profile.get("sop_category") or ""))
        self._sop_profile_lookup = {int(p.get("id") or 0): p for p in profiles if int(p.get("id") or 0) > 0}
        return profiles

    @staticmethod
    def _safe_mtime(path: Path) -> int:
        try:
            return int(path.stat().st_mtime_ns)
        except Exception:
            return 0

    def _scheduler_status_summary(self) -> Dict[str, Any]:
        try:
            sched = getattr(self.window(), "scheduler", None)
            if sched and hasattr(sched, "get_status_summary"):
                return sched.get_status_summary() or {}
        except Exception:
            pass
        return {}

    def _schedule_state_token(self) -> Tuple[Any, ...]:
        profiles = self._load_sop_profile_catalog()
        profile_sig = tuple(
            sorted(
                (
                    int(p.get("id") or 0),
                    bool(p.get("active")),
                    str(p.get("updated_utc") or ""),
                )
                for p in profiles
            )
        )
        status = self._scheduler_status_summary()
        status_sig = (
            str(status.get("source") or ""),
            str(status.get("sop_selected_profile") or ""),
            bool(status.get("sop_contention")),
            tuple(str(x) for x in (status.get("sop_contention_profiles") or [])),
            str(status.get("next_source") or ""),
        )
        return (
            self._safe_mtime(self._db_path()),
            self._safe_mtime(self._nets_db_path()),
            profile_sig,
            status_sig,
        )

    def _nets_db_path(self) -> Path:
        from freqinout.core.config_paths import get_config_dir

        return get_config_dir() / "config" / "freqinout_nets.db"

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

    def _sop_session_journal_path(self) -> Path:
        try:
            return self._db_path().parent / "sop_hf_session_journal.json"
        except Exception:
            from freqinout.core.config_paths import get_config_dir

            return get_config_dir() / "config" / "sop_hf_session_journal.json"

    def _empty_sop_session_journal(self) -> Dict[str, Any]:
        now_iso = self._utc_now_iso()
        return {
            "version": 1,
            "session_id": "",
            "status": "idle",
            "started_utc": "",
            "updated_utc": now_iso,
            "active_profile_ids": [],
            "profile_names": {},
            "hf_restore": {
                "available": False,
                "captured_utc": "",
                "pre_adjust_signature": "",
                "post_adjust_signature": "",
                "hf_rows_before": [],
                "restored_utc": "",
            },
            "temp_net_sop_policies": [],
        }

    def _load_sop_session_journal(self, *, force: bool = False) -> Dict[str, Any]:
        if not force and isinstance(self._sop_session_journal_cache, dict):
            return dict(self._sop_session_journal_cache)

        path = self._sop_session_journal_path()
        out = self._empty_sop_session_journal()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    out.update(raw)
            except Exception as e:
                log.debug("HF Schedule: failed loading SOP session journal %s: %s", path, e)

        hf_restore = out.get("hf_restore")
        if not isinstance(hf_restore, dict):
            hf_restore = {}
        base_restore = dict(self._empty_sop_session_journal().get("hf_restore") or {})
        base_restore.update(hf_restore)
        base_restore["available"] = bool(base_restore.get("available"))
        if not isinstance(base_restore.get("hf_rows_before"), list):
            base_restore["hf_rows_before"] = []
        out["hf_restore"] = base_restore

        if not isinstance(out.get("active_profile_ids"), list):
            out["active_profile_ids"] = []
        out["active_profile_ids"] = [
            int(v) for v in out.get("active_profile_ids", []) if str(v).strip().lstrip("-").isdigit() and int(v) > 0
        ]
        if not isinstance(out.get("profile_names"), dict):
            out["profile_names"] = {}
        if not isinstance(out.get("temp_net_sop_policies"), list):
            out["temp_net_sop_policies"] = []
        out["version"] = 1
        out["updated_utc"] = str(out.get("updated_utc") or "") or self._utc_now_iso()

        self._sop_session_journal_cache = dict(out)
        return dict(out)

    def _save_sop_session_journal(self, journal: Dict[str, Any]) -> None:
        data = dict(journal or {})
        data["version"] = 1
        data["updated_utc"] = self._utc_now_iso()
        path = self._sop_session_journal_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            self._sop_session_journal_cache = dict(data)
        except Exception as e:
            log.debug("HF Schedule: failed saving SOP session journal %s: %s", path, e)

    def _active_hf_sop_profiles(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in self._load_sop_profile_catalog():
            if not bool(row.get("active")):
                continue
            category = str(row.get("category") or "HF").strip().upper()
            if category != "HF":
                continue
            out.append(row)
        return out

    def _collect_current_hf_rows_utc(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for r in range(self.table.rowCount()):
            if self._is_sop_overlay_row(r):
                continue
            row = self._active_row_to_utc(r, include_sop_overlay=False)
            if not row:
                continue
            rows.append(
                normalize_schedule_target_fields(
                    {
                        "day_utc": str(row.get("day_utc") or "ALL"),
                        "band": str(row.get("band") or "").strip().upper(),
                        "mode": str(row.get("mode") or "").strip().upper(),
                        "vfo": "A",
                        "frequency": self._normalize_freq_text(str(row.get("frequency") or "")),
                        "start_utc": self._normalize_hhmm(str(row.get("start_utc") or "")),
                        "end_utc": self._normalize_hhmm(str(row.get("end_utc") or "")),
                        "group_name": str(row.get("group_name") or "").strip(),
                        "fldigi_offset": "",
                        "js8_offset": "",
                        "primary_js8call_group": "",
                        "comment": "",
                        "auto_tune": bool(row.get("auto_tune", False)),
                        "target_scope": row.get("target_scope"),
                        "target_device_profile_id": row.get("target_device_profile_id"),
                        "target_operating_profile_id": row.get("target_operating_profile_id"),
                    }
                )
            )
        return rows

    def _hf_rows_signature(self, rows: List[Dict[str, Any]]) -> str:
        sig_rows: List[Dict[str, Any]] = []
        for row in rows:
            sig_rows.append(
                {
                    "source": "HF",
                    "source_key": "",
                    "day_utc": str(row.get("day_utc") or ""),
                    "group_name": str(row.get("group_name") or ""),
                    "mode": str(row.get("mode") or ""),
                    "band": str(row.get("band") or ""),
                    "frequency": str(row.get("frequency") or ""),
                    "start_utc": str(row.get("start_utc") or ""),
                    "end_utc": str(row.get("end_utc") or ""),
                    "auto_tune": bool(row.get("auto_tune", False)),
                    "target_scope": str(row.get("target_scope") or TARGET_SCOPE_STATION),
                    "target_device_profile_id": row.get("target_device_profile_id"),
                    "target_operating_profile_id": row.get("target_operating_profile_id"),
                }
            )
        return self._rows_signature(sig_rows)

    def _current_hf_rows_signature(self) -> str:
        return self._hf_rows_signature(self._collect_current_hf_rows_utc())

    def _replace_hf_rows_in_table(self, hf_rows: List[Dict[str, Any]]) -> None:
        prev_suspend = self._suspend_dirty_tracking
        self._suspend_dirty_tracking = True
        try:
            self.table.setRowCount(0)
            self._raw_schedule = list(hf_rows)
            for entry in (hf_rows or []):
                self._append_entry_row(self._entry_for_display(dict(entry)))
            if self.table.rowCount() == 0:
                self._add_row()
        finally:
            self._suspend_dirty_tracking = prev_suspend
        self.table.clearSelection()
        self._update_delete_button_state()
        if self.table.rowCount() > 1:
            self._sort_active_schedule_by_time(refresh_post_sort=False)
        self._mark_dirty()
        self._highlight_time_conflicts()
        self._refresh_schedule_resources(force=True)
        self._refresh_schedule_issues(force=True)
        self._refresh_sop_overlay_rows_in_table()

    def _start_or_refresh_sop_session(self, profiles: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        active_profiles = profiles if isinstance(profiles, list) else self._active_hf_sop_profiles()
        active_ids = sorted({int(p.get("id") or 0) for p in active_profiles if int(p.get("id") or 0) > 0})
        profile_names = {
            str(int(p.get("id") or 0)): str(p.get("name") or "").strip()
            for p in active_profiles
            if int(p.get("id") or 0) > 0
        }
        now_iso = self._utc_now_iso()

        journal = self._load_sop_session_journal()
        status = str(journal.get("status") or "idle").strip().lower()
        existing_ids = sorted({int(v) for v in (journal.get("active_profile_ids") or []) if int(v) > 0})
        if status == "active" and existing_ids and existing_ids == active_ids:
            journal["profile_names"] = dict(journal.get("profile_names") or {})
            journal["profile_names"].update(profile_names)
            journal["updated_utc"] = now_iso
            self._save_sop_session_journal(journal)
            return journal

        if active_ids:
            journal = self._empty_sop_session_journal()
            journal["session_id"] = uuid.uuid4().hex
            journal["status"] = "active"
            journal["started_utc"] = now_iso
            journal["active_profile_ids"] = list(active_ids)
            journal["profile_names"] = dict(profile_names)
            self._save_sop_session_journal(journal)
        return journal

    def register_sop_session_activation(self, profile_id: int, profile_name: str = "") -> None:
        pid = int(profile_id or 0)
        if pid <= 0:
            return
        profiles = self._active_hf_sop_profiles()
        known = {int(p.get("id") or 0) for p in profiles}
        if pid not in known:
            profiles.append({"id": pid, "name": profile_name, "active": True, "category": "HF"})
        journal = self._start_or_refresh_sop_session(profiles)
        if str(profile_name or "").strip():
            names = dict(journal.get("profile_names") or {})
            names[str(pid)] = str(profile_name or "").strip()
            journal["profile_names"] = names
            journal["status"] = "active"
            journal["active_profile_ids"] = sorted({int(v) for v in (journal.get("active_profile_ids") or []) if int(v) > 0} | {pid})
            self._save_sop_session_journal(journal)

    def _record_sop_auto_adjust_snapshot_before(self) -> None:
        active_profiles = self._active_hf_sop_profiles()
        if not active_profiles:
            return
        journal = self._start_or_refresh_sop_session(active_profiles)
        hf_restore = dict(journal.get("hf_restore") or {})
        if bool(hf_restore.get("available")) and isinstance(hf_restore.get("hf_rows_before"), list) and hf_restore.get("hf_rows_before"):
            # Preserve the first pre-adjust snapshot for the current session.
            return
        before_rows = self._collect_current_hf_rows_utc()
        hf_restore = {
            "available": True,
            "captured_utc": self._utc_now_iso(),
            "pre_adjust_signature": self._hf_rows_signature(before_rows),
            "post_adjust_signature": "",
            "hf_rows_before": before_rows,
            "restored_utc": "",
        }
        journal["hf_restore"] = hf_restore
        self._save_sop_session_journal(journal)

    def _record_sop_auto_adjust_snapshot_after(self) -> None:
        journal = self._load_sop_session_journal()
        hf_restore = dict(journal.get("hf_restore") or {})
        if not bool(hf_restore.get("available")):
            return
        hf_restore["post_adjust_signature"] = self._current_hf_rows_signature()
        journal["hf_restore"] = hf_restore
        self._save_sop_session_journal(journal)

    def _policy_conflict_key_for_row(self, row: Dict[str, Any]) -> str:
        try:
            return self._sop_manager._policy_conflict_key(
                str(row.get("net_row_signature") or "").strip(),
                str(row.get("sop_row_signature") or "").strip(),
                str(row.get("window_start_utc") or "").strip(),
                str(row.get("window_end_utc") or "").strip(),
            )
        except Exception:
            return ""

    def _active_net_sop_policy_rows_by_key(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        try:
            rows = self._sop_manager.list_net_sop_conflict_policies(active_only=True)
        except Exception:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = self._policy_conflict_key_for_row(row)
            if key and key not in out:
                out[key] = row
        return out

    def _record_temp_session_net_sop_policy_decisions(
        self,
        decisions: List[Dict[str, Any]],
        *,
        before_by_key: Dict[str, Dict[str, Any]],
        origin: str,
        profiles_hint: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        active_profiles = self._active_hf_sop_profiles()
        if isinstance(profiles_hint, list):
            known_ids = {int(p.get("id") or 0) for p in active_profiles if int(p.get("id") or 0) > 0}
            for p in profiles_hint:
                if not isinstance(p, dict):
                    continue
                pid = int(p.get("id") or 0)
                if pid <= 0 or pid in known_ids:
                    continue
                active_profiles.append(dict(p))
                known_ids.add(pid)
        if not active_profiles:
            return
        journal = self._start_or_refresh_sop_session(active_profiles)
        after_by_key = self._active_net_sop_policy_rows_by_key()
        rows = list(journal.get("temp_net_sop_policies") or [])
        row_map: Dict[str, Dict[str, Any]] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            key = str(item.get("conflict_key") or "").strip()
            if key and key not in row_map:
                row_map[key] = item

        changed = False
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            key = self._policy_conflict_key_for_row(decision)
            if not key:
                continue
            before = before_by_key.get(key) or {}
            after = after_by_key.get(key) or {}
            expected_policy = str(after.get("policy") or decision.get("policy") or "").strip().upper()
            if expected_policy not in {"SOP_PRIORITY", "NET_PRIORITY"}:
                continue
            item = row_map.get(key)
            if not isinstance(item, dict):
                item = {
                    "conflict_key": key,
                    "net_row_signature": str(decision.get("net_row_signature") or "").strip(),
                    "sop_row_signature": str(decision.get("sop_row_signature") or "").strip(),
                    "window_start_utc": str(decision.get("window_start_utc") or "").strip(),
                    "window_end_utc": str(decision.get("window_end_utc") or "").strip(),
                    "sop_profile_id": int(decision.get("sop_profile_id") or 0),
                    "sop_layer_id": int(decision.get("sop_layer_id") or 0),
                    "prev_exists": bool(before),
                    "prev_policy": str(before.get("policy") or "").strip().upper(),
                    "prev_policy_id": int(before.get("id") or 0),
                    "prev_updated_utc": str(before.get("updated_utc") or "").strip(),
                    "expected_policy": expected_policy,
                    "expected_updated_utc": str(after.get("updated_utc") or "").strip(),
                    "origin": str(origin or "").strip(),
                    "saved_utc": self._utc_now_iso(),
                    "reverted": False,
                    "reverted_utc": "",
                }
                rows.append(item)
                row_map[key] = item
            else:
                item["expected_policy"] = expected_policy
                item["expected_updated_utc"] = str(after.get("updated_utc") or "").strip()
                item["origin"] = str(origin or item.get("origin") or "").strip()
                item["saved_utc"] = self._utc_now_iso()
                item["reverted"] = False
                item["reverted_utc"] = ""
            changed = True

        if changed:
            journal["temp_net_sop_policies"] = rows
            self._save_sop_session_journal(journal)

    def save_net_sop_conflict_policies_with_session_tracking(
        self,
        decisions: List[Dict[str, Any]],
        *,
        origin: str = "",
        session_profile_hint: Optional[Dict[str, Any]] = None,
    ) -> int:
        valid_decisions = [d for d in (decisions or []) if isinstance(d, dict)]
        if not valid_decisions:
            return 0
        before_by_key = self._active_net_sop_policy_rows_by_key()
        saved = int(self._sop_manager.save_net_sop_conflict_policies(valid_decisions) or 0)
        if saved > 0:
            try:
                self._record_temp_session_net_sop_policy_decisions(
                    valid_decisions,
                    before_by_key=before_by_key,
                    origin=origin or "Net/SOP conflict decision",
                    profiles_hint=[dict(session_profile_hint)] if isinstance(session_profile_hint, dict) else None,
                )
            except Exception as e:
                log.debug("HF Schedule: failed recording temp Net/SOP policy decisions: %s", e)
        return saved

    def _session_pending_temp_policy_entries(
        self,
        journal: Dict[str, Any],
        *,
        target_profile_ids: Optional[Set[int]] = None,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        target_ids = {int(v) for v in (target_profile_ids or set()) if int(v) > 0}
        for row in (journal.get("temp_net_sop_policies") or []):
            if not isinstance(row, dict):
                continue
            if bool(row.get("reverted")):
                continue
            pid = int(row.get("sop_profile_id") or 0)
            if target_ids and pid > 0 and pid not in target_ids:
                continue
            out.append(row)
        return out

    def _restore_hf_from_session_snapshot(self, *, force_overwrite: bool = False) -> Tuple[bool, str]:
        journal = self._load_sop_session_journal()
        hf_restore = dict(journal.get("hf_restore") or {})
        if not bool(hf_restore.get("available")):
            return False, "No pre-auto-adjust HF snapshot is available."
        rows_before = [dict(r) for r in (hf_restore.get("hf_rows_before") or []) if isinstance(r, dict)]
        if not rows_before:
            return False, "HF restore snapshot is empty."
        post_sig = str(hf_restore.get("post_adjust_signature") or "").strip()
        if post_sig and not force_overwrite:
            current_sig = self._current_hf_rows_signature()
            if current_sig and current_sig != post_sig:
                return False, "HF schedule changed after auto-adjust; restore requires overwrite confirmation."
        self._replace_hf_rows_in_table(rows_before)
        restore_rows = self._collect_current_hf_rows_utc()
        saved, save_detail = self._persist_hf_schedule_rows(
            restore_rows,
            prompt_after_save=False,
        )
        if not saved:
            return False, (
                "HF rows were restored in the table, but the saved Daily schedule could not be updated.\n"
                f"{save_detail}"
            )
        hf_restore["available"] = False
        hf_restore["restored_utc"] = self._utc_now_iso()
        journal["hf_restore"] = hf_restore
        self._save_sop_session_journal(journal)
        return True, f"Restored and saved {len(restore_rows)} HF row(s) from pre-auto-adjust snapshot."

    def _revert_session_temp_net_sop_policies(
        self,
        *,
        target_profile_ids: Optional[Set[int]] = None,
    ) -> Tuple[int, int, str]:
        journal = self._load_sop_session_journal()
        pending = self._session_pending_temp_policy_entries(journal, target_profile_ids=target_profile_ids)
        if not pending:
            return 0, 0, "No temporary Net/SOP policy decisions to revert."

        current_by_key = self._active_net_sop_policy_rows_by_key()
        restore_decisions: List[Dict[str, Any]] = []
        clear_ids: List[int] = []
        skipped = 0
        apply_keys: Set[str] = set()

        for item in pending:
            key = str(item.get("conflict_key") or "").strip()
            if not key:
                skipped += 1
                continue
            current = current_by_key.get(key) or {}
            expected_policy = str(item.get("expected_policy") or "").strip().upper()
            current_policy = str(current.get("policy") or "").strip().upper()
            if current and expected_policy and current_policy and current_policy != expected_policy:
                skipped += 1
                continue

            prev_exists = bool(item.get("prev_exists"))
            if prev_exists:
                prev_policy = str(item.get("prev_policy") or "").strip().upper()
                if prev_policy not in {"SOP_PRIORITY", "NET_PRIORITY"}:
                    prev_policy = "NET_PRIORITY"
                restore_decisions.append(
                    {
                        "sop_profile_id": int(item.get("sop_profile_id") or 0),
                        "sop_layer_id": int(item.get("sop_layer_id") or 0),
                        "net_row_signature": str(item.get("net_row_signature") or "").strip(),
                        "sop_row_signature": str(item.get("sop_row_signature") or "").strip(),
                        "window_start_utc": str(item.get("window_start_utc") or "").strip(),
                        "window_end_utc": str(item.get("window_end_utc") or "").strip(),
                        "policy": prev_policy,
                        "resolution_note": "SOP session return-to-normal restore",
                    }
                )
            else:
                cur_id = int(current.get("id") or 0)
                if cur_id > 0:
                    clear_ids.append(cur_id)
            apply_keys.add(key)

        restored = 0
        cleared = 0
        if restore_decisions:
            try:
                restored = int(self._sop_manager.save_net_sop_conflict_policies(restore_decisions) or 0)
            except Exception as e:
                log.debug("HF Schedule: failed restoring prior Net/SOP policies: %s", e)
        if clear_ids:
            try:
                cleared = int(self._sop_manager.clear_net_sop_conflict_policies(sorted(set(clear_ids))) or 0)
            except Exception as e:
                log.debug("HF Schedule: failed clearing temp Net/SOP policies: %s", e)

        if apply_keys:
            changed = False
            for item in (journal.get("temp_net_sop_policies") or []):
                if not isinstance(item, dict):
                    continue
                if str(item.get("conflict_key") or "").strip() not in apply_keys:
                    continue
                item["reverted"] = True
                item["reverted_utc"] = self._utc_now_iso()
                changed = True
            if changed:
                self._save_sop_session_journal(journal)

        summary = f"Reverted {restored + cleared} temporary Net/SOP decision(s)."
        if skipped > 0:
            summary += f" Skipped {skipped} modified decision(s)."
        return (restored + cleared), skipped, summary

    def _close_or_update_sop_session_after_deactivation(
        self,
        *,
        deactivated_profile_ids: Set[int],
    ) -> Dict[str, Any]:
        journal = self._load_sop_session_journal()
        active_ids = {int(v) for v in (journal.get("active_profile_ids") or []) if int(v) > 0}
        active_ids.difference_update({int(v) for v in deactivated_profile_ids if int(v) > 0})
        journal["active_profile_ids"] = sorted(active_ids)
        if active_ids:
            journal["status"] = "active"
            self._save_sop_session_journal(journal)
            return journal

        hf_restore = dict(journal.get("hf_restore") or {})
        pending_hf = bool(hf_restore.get("available"))
        pending_temp = bool(self._session_pending_temp_policy_entries(journal))
        journal["status"] = "inactive_pending_restore" if (pending_hf or pending_temp) else "closed"
        self._save_sop_session_journal(journal)
        return journal

    def _perform_sop_profile_deactivation(self, target_profile_ids: Set[int]) -> int:
        changed = 0
        for pid in sorted({int(v) for v in target_profile_ids if int(v) > 0}):
            try:
                if self._sop_manager.set_profile_active(pid, False):
                    changed += 1
            except Exception as e:
                log.debug("HF Schedule: failed deactivating SOP profile %s: %s", pid, e)
        return changed

    def deactivate_hf_sops_with_return_to_normal(
        self,
        profile_ids: Optional[List[int]] = None,
        *,
        origin_label: str = "Daily Schedule",
        already_deactivated: bool = False,
    ) -> bool:
        if self._sop_return_to_normal_prompt_active:
            return False
        active_profiles = self._active_hf_sop_profiles()
        active_ids = {int(p.get("id") or 0) for p in active_profiles if int(p.get("id") or 0) > 0}
        requested_ids = {int(v) for v in (profile_ids or []) if int(v) > 0}
        if not requested_ids:
            requested_ids = set(active_ids)
        target_ids = requested_ids if already_deactivated else (requested_ids & active_ids)
        if not target_ids and not already_deactivated:
            return False

        remaining_active_ids = set(active_ids) if already_deactivated else (set(active_ids) - set(target_ids))
        journal = self._load_sop_session_journal()
        target_pending_temp = self._session_pending_temp_policy_entries(journal, target_profile_ids=set(target_ids))
        hf_restore = dict(journal.get("hf_restore") or {})
        can_restore = not remaining_active_ids
        pending_hf_restore = bool(can_restore and hf_restore.get("available"))
        pending_temp_restore = bool(can_restore and target_pending_temp)
        has_pending_actions = bool(pending_hf_restore or pending_temp_restore)

        current_hf_sig = ""
        post_adjust_sig = ""
        hf_overwrite_warning = False
        if pending_hf_restore:
            try:
                current_hf_sig = self._current_hf_rows_signature()
            except Exception:
                current_hf_sig = ""
            post_adjust_sig = str(hf_restore.get("post_adjust_signature") or "").strip()
            hf_overwrite_warning = bool(post_adjust_sig and current_hf_sig and current_hf_sig != post_adjust_sig)

        do_return_to_normal = False
        if has_pending_actions:
            self._sop_return_to_normal_prompt_active = True
            try:
                names = []
                profile_name_map = dict(journal.get("profile_names") or {})
                for pid in sorted(target_ids):
                    txt = str(profile_name_map.get(str(pid)) or self._sop_profile_lookup.get(pid, {}).get("name") or "").strip()
                    if txt:
                        names.append(txt)
                target_label = ", ".join(names) if names else f"{len(target_ids)} HF SOP profile(s)"
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Warning)
                if already_deactivated:
                    box.setWindowTitle("Return to Normal After SOP Deactivation")
                    box.setText("SOP deactivation completed. Restore the pre-SOP HF/Net state now?")
                else:
                    box.setWindowTitle("Deactivate SOP and Return to Normal")
                    box.setText(f"Deactivate {target_label} and return to normal scheduling state?")
                detail_lines: List[str] = []
                if pending_hf_restore:
                    count_rows = len([r for r in (hf_restore.get("hf_rows_before") or []) if isinstance(r, dict)])
                    detail_lines.append(f"HF Schedule: restore {count_rows} row(s) from pre-auto-adjust snapshot.")
                if pending_temp_restore:
                    detail_lines.append(f"Net/SOP: revert {len(target_pending_temp)} temporary session decision(s).")
                if remaining_active_ids:
                    detail_lines.append("Other HF SOP profiles remain active; return-to-normal restore is not available yet.")
                if hf_overwrite_warning:
                    detail_lines.append("Warning: HF schedule changed after auto-adjust. Restore will overwrite newer HF edits.")
                if detail_lines:
                    box.setInformativeText("\n".join(detail_lines))

                if already_deactivated:
                    return_btn = box.addButton("Return to Normal", QMessageBox.AcceptRole)
                    keep_btn = box.addButton("Keep Current HF/Net", QMessageBox.RejectRole)
                    box.exec()
                    if box.clickedButton() is return_btn:
                        do_return_to_normal = True
                    elif box.clickedButton() is keep_btn:
                        do_return_to_normal = False
                    else:
                        return False
                else:
                    do_all_btn = box.addButton("Deactivate + Return to Normal", QMessageBox.AcceptRole)
                    deact_only_btn = box.addButton("Deactivate SOP Only", QMessageBox.ActionRole)
                    box.addButton("Cancel", QMessageBox.RejectRole)
                    box.exec()
                    clicked = box.clickedButton()
                    if clicked is do_all_btn:
                        do_return_to_normal = True
                    elif clicked is deact_only_btn:
                        do_return_to_normal = False
                    else:
                        return False
            finally:
                self._sop_return_to_normal_prompt_active = False

        changed = 0
        if not already_deactivated:
            changed = self._perform_sop_profile_deactivation(set(target_ids))
            if changed <= 0 and target_ids:
                QMessageBox.warning(self, "SOP", "Could not update SOP active state.")
                return False

        restore_msg = ""
        restore_ok = False
        policy_msg = ""
        reverted_count = 0
        skipped_policy = 0
        if do_return_to_normal and can_restore:
            if pending_hf_restore:
                restore_ok, restore_msg = self._restore_hf_from_session_snapshot(force_overwrite=hf_overwrite_warning)
            if pending_temp_restore:
                reverted_count, skipped_policy, policy_msg = self._revert_session_temp_net_sop_policies(
                    target_profile_ids=set(target_ids)
                )

        self._close_or_update_sop_session_after_deactivation(deactivated_profile_ids=set(target_ids))
        self._dispatch_sop_schedule_change()

        if has_pending_actions and do_return_to_normal:
            lines: List[str] = []
            if pending_hf_restore:
                lines.append(restore_msg or ("HF schedule restored." if restore_ok else "HF schedule restore skipped."))
            if pending_temp_restore:
                lines.append(policy_msg or f"Reverted {reverted_count} temporary Net/SOP decision(s).")
                if skipped_policy > 0:
                    lines.append(f"Skipped {skipped_policy} modified Net/SOP decision(s).")
            QMessageBox.information(self, "Return to Normal", "\n".join([ln for ln in lines if ln]) or "Completed.")
        elif not already_deactivated and changed > 0:
            QMessageBox.information(self, "SOP", f"Deactivated {changed} active HF SOP profile(s).")
        return True

    def prompt_sop_return_to_normal_after_deactivation(
        self,
        profile_ids: Optional[List[int]] = None,
        *,
        origin_label: str = "SOP Builder",
    ) -> bool:
        return self.deactivate_hf_sops_with_return_to_normal(
            profile_ids=profile_ids,
            origin_label=origin_label,
            already_deactivated=True,
        )

    @staticmethod
    def _normalize_freq_text(value: str) -> str:
        txt = str(value or "").strip()
        if not txt:
            return ""
        return format_frequency_mhz(txt)

    def _display_resource_day_time(self, day_utc: str, start_utc: str, end_utc: str) -> Tuple[str, str, str]:
        day_txt = str(day_utc or "ALL").strip() or "ALL"
        start_txt = str(start_utc or "").strip()
        end_txt = str(end_utc or "").strip()
        if not self._show_local:
            return day_txt, start_txt, end_txt
        if start_txt:
            day_txt, start_txt = self._convert_day_time(day_txt, start_txt, to_local=True)
        if end_txt:
            _, end_txt = self._convert_day_time(day_utc, end_txt, to_local=True)
        return day_txt, start_txt, end_txt

    def _is_hf_sop_profile(self, profile: Dict[str, Any]) -> bool:
        category = self._sop_category_label(str(profile.get("sop_category") or ""))
        if category == "SOP-Local Net":
            return False
        group_name = str(profile.get("operating_group") or "").strip()
        return bool(group_name)

    def _format_conflict_span(self, start_utc: Any, end_utc: Any) -> str:
        if not isinstance(start_utc, datetime.datetime):
            return "--"
        if self._show_local:
            _tz_name, tz = self._current_timezone()
            start_txt = start_utc.astimezone(tz).strftime("%a %H:%M")
            if isinstance(end_utc, datetime.datetime):
                end_txt = end_utc.astimezone(tz).strftime("%H:%M")
                return f"{start_txt}-{end_txt}"
            return start_txt
        start_txt = start_utc.astimezone(datetime.timezone.utc).strftime("%a %H:%MZ")
        if isinstance(end_utc, datetime.datetime):
            end_txt = end_utc.astimezone(datetime.timezone.utc).strftime("%H:%MZ")
            return f"{start_txt}-{end_txt}"
        return start_txt

    def _build_hf_sop_status_rows(self, *, force_conflict_refresh: bool = False) -> List[Dict[str, Any]]:
        profiles = [p for p in self._load_sop_profile_catalog() if self._is_hf_sop_profile(p)]
        conflict_map: Dict[int, List[Dict[str, Any]]] = {}
        if profiles:
            hf_profile_ids = {int(p.get("id") or 0) for p in profiles if int(p.get("id") or 0) > 0}
            try:
                conflict_rows = self._sop_manager.collect_active_hf_conflicts(
                    force_refresh=force_conflict_refresh,
                    include_suggestions=False,
                )
            except Exception as e:
                log.debug("HF Schedule: failed loading SOP conflicts: %s", e)
                conflict_rows = []
            for row in conflict_rows:
                profile_id = int(row.get("profile_id") or 0)
                if profile_id <= 0 or profile_id not in hf_profile_ids:
                    continue
                conflict_map.setdefault(profile_id, []).append(dict(row))

        rows: List[Dict[str, Any]] = []
        for profile in profiles:
            profile_id = int(profile.get("id") or 0)
            active = bool(profile.get("active"))
            conflicts = sorted(
                conflict_map.get(profile_id, []),
                key=lambda x: (
                    str(x.get("action_label") or "").upper(),
                    str(x.get("band") or "").upper(),
                    str(x.get("frequency") or ""),
                    str(x.get("daily_summary") or "").upper(),
                    str(x.get("net_summary") or "").upper(),
                    str(x.get("sop_summary") or "").upper(),
                ),
            )
            daily_conflicts = [
                row
                for row in conflicts
                if bool(row.get("daily_conflicts")) or bool(str(row.get("daily_summary") or "").strip())
            ]
            net_conflicts = [
                row
                for row in conflicts
                if bool(row.get("net_conflicts")) or bool(str(row.get("net_summary") or "").strip())
            ]
            sop_conflicts = [
                row
                for row in conflicts
                if bool(row.get("sop_conflicts")) or bool(str(row.get("sop_summary") or "").strip())
            ]
            if active and daily_conflicts:
                status = "Conflict"
                issue_summary = f"HF conflict: {len(daily_conflicts)} action(s) still overlap Daily HF."
                other_parts: List[str] = []
                if net_conflicts:
                    other_parts.append(f"Net: {len(net_conflicts)}")
                if sop_conflicts:
                    other_parts.append(f"SOP: {len(sop_conflicts)}")
                if other_parts:
                    issue_summary += " Remaining " + ", ".join(other_parts) + "."
            elif active and (net_conflicts or sop_conflicts):
                status = "Attention"
                remaining_parts: List[str] = []
                if net_conflicts:
                    remaining_parts.append(f"Net: {len(net_conflicts)}")
                if sop_conflicts:
                    remaining_parts.append(f"SOP: {len(sop_conflicts)}")
                issue_summary = "HF clear."
                if remaining_parts:
                    issue_summary += " Remaining " + ", ".join(remaining_parts) + "."
            elif active:
                status = "Active"
                issue_summary = "No Daily/Net/SOP conflicts."
            else:
                status = "Inactive"
                issue_summary = "Inactive."
            rows.append(
                {
                    "profile_id": profile_id,
                    "group_name": str(profile.get("operating_group") or "").strip(),
                    "profile_name": str(profile.get("name") or "").strip() or f"SOP {profile_id}",
                    "status": status,
                    "issue_summary": issue_summary,
                    "active": active,
                    "daily_conflict_count": len(daily_conflicts),
                    "net_conflict_count": len(net_conflicts),
                    "sop_conflict_count": len(sop_conflicts),
                }
            )
        status_order = {"Conflict": 0, "Attention": 1, "Active": 2, "Inactive": 3}
        rows.sort(
            key=lambda r: (
                status_order.get(str(r.get("status") or ""), 9),
                str(r.get("group_name") or "").upper(),
                str(r.get("profile_name") or "").upper(),
            )
        )
        return rows

    def _refresh_sop_profiles_panel(self, *, force: bool = False, theme: Optional[Dict[str, str]] = None) -> None:
        if theme is None:
            theme = resolve_theme(self.settings)
        now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
        if not force and (now_ts - float(self._last_sop_panel_refresh_ts or 0.0)) < 10.0:
            return
        self._last_sop_panel_refresh_ts = now_ts
        rows = self._build_hf_sop_status_rows(force_conflict_refresh=force)
        conflict_count = sum(1 for r in rows if str(r.get("status") or "") == "Conflict")
        attention_count = sum(1 for r in rows if str(r.get("status") or "") == "Attention")
        self._has_active_hf_sop_conflicts = bool(conflict_count > 0)
        active_count = sum(1 for r in rows if bool(r.get("active")))
        self._update_sop_overlay_control_state(active_count > 0)
        self.sop_runtime_box.setVisible(bool(conflict_count or attention_count))
        if rows:
            summary = f"HF SOP Sets: {len(rows)} | Active: {active_count} | HF Conflict: {conflict_count}"
            if attention_count > 0:
                summary += f" | Review: {attention_count}"
            self.sop_profile_summary_label.setText(summary)
        else:
            self.sop_profile_summary_label.setText("HF SOP Sets: none configured")
        self.sop_profile_summary_label.setStyleSheet(f"color: {theme.get('text', '#111')}; font-weight: 600;")
        while self.sop_indicator_layout.count():
            item = self.sop_indicator_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if not rows:
            self.sop_runtime_box.setVisible(False)
            hint = QLabel("No HF SOP configured.")
            hint.setStyleSheet(f"color: {theme.get('text_muted', '#888')};")
            self.sop_indicator_layout.addWidget(hint)
            self.sop_runtime_box.setMaximumHeight(max(0, self.fontMetrics().lineSpacing() * 5 + 32))
            return

        for row in rows[:2]:
            profile_id = int(row.get("profile_id") or 0)
            active = bool(row.get("active"))
            status_txt = str(row.get("status") or "Inactive").strip()
            issue_summary = str(row.get("issue_summary") or "").strip()

            btn_label = "Inactive"
            if status_txt == "Conflict":
                role = "eligible_warning"
                btn_label = "Conflict"
            elif status_txt == "Attention":
                role = "eligible_warning"
                btn_label = "Review"
            elif active:
                role = "eligible_success"
                btn_label = "Active"
            else:
                role = "muted"
            btn = QPushButton(f"HF SOP: {btn_label}")
            btn.setStyleSheet(button_style(role, theme))
            btn.setToolTip(issue_summary or "Toggle SOP active state.")
            btn.clicked.connect(lambda _=False, pid=profile_id, target=(not active): self._on_toggle_sop_profile_active(pid, target))
            self.sop_indicator_layout.addWidget(btn)

        if len(rows) > 2:
            extra = QLabel(f"+{len(rows) - 2} more HF SOP set(s)")
            extra.setStyleSheet(f"color: {theme.get('text_muted', '#888')};")
            self.sop_indicator_layout.addWidget(extra)
        self.sop_runtime_box.setMaximumHeight(max(0, self.fontMetrics().lineSpacing() * 6 + 36))

    def _on_toggle_sop_profile_active(self, profile_id: int, active: bool) -> None:
        try:
            profile_id = int(profile_id or 0)
            if profile_id <= 0:
                return
            profile = self._sop_profile_lookup.get(profile_id)
            if not profile:
                profile = self._sop_manager.get_profile(profile_id) or {}
            profile_name = str(profile.get("name") or "").strip()
            if not active:
                status = self._scheduler_status_summary()
                source = str(status.get("source") or "").strip().upper()
                selected = str(status.get("sop_selected_profile") or "").strip()
                if source == "SOP" and profile_name and profile_name == selected:
                    confirm = QMessageBox.question(
                        self,
                        "Deactivate Active SOP",
                        (
                            f"'{profile_name}' is currently selected as the active SOP layer.\n"
                            "Deactivate it now?"
                        ),
                    )
                    if confirm != QMessageBox.Yes:
                        return
                self.deactivate_hf_sops_with_return_to_normal([profile_id], origin_label="Daily Schedule")
                return
            if active:
                try:
                    conflicts = self._sop_manager.collect_active_net_sop_conflicts(
                        horizon_days=7,
                        include_profile_ids={profile_id},
                    )
                except Exception:
                    conflicts = []
                pending = [c for c in conflicts if not bool(c.get("has_policy"))]
                if pending:
                    lines = []
                    for row in pending[:8]:
                        sop_summary = str(row.get("sop_summary") or "").strip()
                        net_summary = str(row.get("net_summary") or "").strip()
                        lines.append(f"{sop_summary} vs {net_summary}")
                    if len(pending) > 8:
                        lines.append(f"...and {len(pending) - 8} more conflict(s).")
                    box = QMessageBox(self)
                    box.setIcon(QMessageBox.Warning)
                    box.setWindowTitle("Resolve Net/SOP Conflicts Before Activation")
                    box.setText(
                        "Active Net windows conflict with this SOP. Choose priority to continue.\n"
                        "These overlap decisions are temporary for this SOP session by default."
                    )
                    box.setInformativeText("\n".join(lines))
                    sop_btn = box.addButton("SOP Priority for All", QMessageBox.AcceptRole)
                    net_btn = box.addButton("Net Priority for All", QMessageBox.AcceptRole)
                    box.addButton("Cancel Activation", QMessageBox.RejectRole)
                    box.exec()
                    clicked = box.clickedButton()
                    if clicked not in {sop_btn, net_btn}:
                        return
                    policy = "SOP_PRIORITY" if clicked is sop_btn else "NET_PRIORITY"
                    decisions: List[Dict[str, Any]] = []
                    for row in pending:
                        net_sig = str(row.get("net_row_signature") or "").strip()
                        sop_sig = str(row.get("sop_row_signature") or "").strip()
                        start_utc = str(row.get("window_start_utc") or "").strip()
                        end_utc = str(row.get("window_end_utc") or "").strip()
                        if not net_sig or not sop_sig or not start_utc or not end_utc:
                            continue
                        decisions.append(
                            {
                                "sop_profile_id": int(row.get("sop_profile_id") or profile_id),
                                "sop_layer_id": int(row.get("sop_layer_id") or 0),
                                "net_row_signature": net_sig,
                                "sop_row_signature": sop_sig,
                                "window_start_utc": start_utc,
                                "window_end_utc": end_utc,
                                "policy": policy,
                                "resolution_note": "SOP activation conflict resolution",
                            }
                        )
                    if decisions:
                        saved = int(
                            self.save_net_sop_conflict_policies_with_session_tracking(
                                decisions,
                                origin="Daily SOP activation conflict resolution",
                                session_profile_hint={
                                    "id": int(profile_id or 0),
                                    "name": profile_name,
                                    "active": True,
                                    "category": "HF",
                                },
                            )
                            or 0
                        )
                        if saved <= 0:
                            QMessageBox.warning(self, "SOP", "Could not save Net/SOP conflict policy decisions.")
                            return
            if not self._sop_manager.set_profile_active(profile_id, active):
                QMessageBox.warning(self, "SOP", "Could not update SOP active state.")
                return
            if active:
                try:
                    self.register_sop_session_activation(profile_id, profile_name)
                except Exception as e:
                    log.debug("HF Schedule: failed registering SOP session activation: %s", e)
            self._schedule_resource_token = None
            win = self.window()
            dispatched = False
            try:
                if hasattr(win, "sop_tab") and hasattr(win.sop_tab, "on_sop_profiles_updated"):
                    win.sop_tab.on_sop_profiles_updated()
            except Exception:
                pass
            try:
                if hasattr(win, "_on_sop_data_changed"):
                    win._on_sop_data_changed()
                    dispatched = True
                elif hasattr(win, "scheduler"):
                    win.scheduler.force_refresh()
            except Exception:
                pass
            if not dispatched:
                self._refresh_sop_overlay_rows_in_table()
                self._refresh_sop_profiles_panel(force=True)
                self._update_effective_source_label()
                self._refresh_schedule_resources(force=True)
        except Exception as e:
            QMessageBox.warning(self, "SOP", f"Could not update SOP active state:\n{e}")

    def _load_schedule_resource_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        saved_schedule_rows = self._load_saved_schedule_resource_rows()
        rows.extend(saved_schedule_rows)
        dedup: Dict[Tuple[str, str, str, str, str, str, str, str, str], Dict[str, Any]] = {}
        for row in rows:
            source = str(row.get("source") or "manual").strip().lower()
            profile = str(row.get("sop_profile_name") or "").strip().upper()
            key = (
                source,
                str(row.get("resource_set") or "").strip().upper(),
                profile,
                self._normalize_day(str(row.get("day_utc") or "ALL")),
                str(row.get("group_name") or "").strip().upper(),
                str(row.get("mode") or "").strip().upper(),
                str(row.get("band") or "").strip().upper(),
                self._normalize_freq_text(str(row.get("frequency") or "")),
                f"{self._normalize_hhmm(str(row.get('start_utc') or ''))}-{self._normalize_hhmm(str(row.get('end_utc') or ''))}",
            )
            existing = dedup.get(key)
            if not existing:
                dedup[key] = dict(row)
                continue
            # Prefer persisted/manual rows over virtual rows for duplicates.
            if str(existing.get("source") or "").strip().lower() != "manual" and source == "manual":
                dedup[key] = dict(row)
        out = list(dedup.values())
        out.sort(
            key=lambda r: (
                str(r.get("resource_set") or "").upper(),
                str(r.get("sop_profile_name") or "").upper(),
                str(r.get("group_name") or "").upper(),
                self._normalize_day(str(r.get("day_utc") or "ALL")),
                self._normalize_hhmm(str(r.get("start_utc") or "")),
                self._normalize_hhmm(str(r.get("end_utc") or "")),
            )
        )
        return out

    def _load_saved_schedule_resource_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        schedules = source_sets_for_category(self.settings, HF_DAILY_SOURCE_SETS_KEY, HF_DAILY_SOURCE_CATEGORY)
        for schedule in schedules:
            schedule_id = str(schedule.get("id") or "").strip()
            schedule_name = str(schedule.get("name") or schedule_id or "Saved Schedule").strip()
            updated = str(schedule.get("updated_utc") or schedule.get("created_utc") or "").strip()
            for idx, raw_row in enumerate(schedule.get("rows") or []):
                if not isinstance(raw_row, dict):
                    continue
                row = normalize_schedule_target_fields(dict(raw_row))
                group_name = str(row.get("group_name") or row.get("group") or "").strip().upper()
                mode = str(row.get("mode") or "Digi").strip().upper()
                band = str(row.get("band") or "").strip().upper()
                freq = self._normalize_freq_text(str(row.get("frequency") or "").strip())
                start = self._normalize_hhmm(str(row.get("start_utc") or ""))
                end = self._normalize_hhmm(str(row.get("end_utc") or ""))
                if not (group_name and band and freq and start and end):
                    continue
                day = self._normalize_day(str(row.get("day_utc") or row.get("day") or "ALL"))
                rows.append(
                    {
                        "id": 0,
                        "source_key": f"saved:{schedule_id}:{idx}",
                        "resource_set": schedule_name,
                        "day_utc": day,
                        "group_name": group_name,
                        "mode": mode,
                        "band": band,
                        "frequency": freq,
                        "start_utc": start,
                        "end_utc": end,
                        "source": "saved_schedule",
                        "updated_utc": updated,
                        "recurrence": str(row.get("recurrence") or ("Daily" if day == "ALL" else "Weekly")),
                        "biweekly_offset_weeks": int(row.get("biweekly_offset_weeks") or 0),
                        "month_weeks": str(row.get("month_weeks") or "").strip(),
                        "vfo": str(row.get("vfo") or "A").strip().upper() or "A",
                    }
                )
        return rows

    def _load_manual_schedule_resource_rows(self) -> List[Dict[str, Any]]:
        db_path = self._db_path()
        if not db_path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        conn = sqlite3.connect(db_path)
        try:
            self._ensure_schedule_resources_table(conn)
            cur = conn.execute(
                """
                SELECT
                    id,
                    COALESCE(resource_set, 'Custom'),
                    COALESCE(day_utc, 'ALL'),
                    COALESCE(group_name, ''),
                    COALESCE(mode, ''),
                    COALESCE(band, ''),
                    COALESCE(frequency, ''),
                    COALESCE(start_utc, ''),
                    COALESCE(end_utc, ''),
                    COALESCE(source_type, 'manual'),
                    COALESCE(updated_utc, '')
                FROM hf_schedule_resources
                ORDER BY resource_set COLLATE NOCASE, group_name COLLATE NOCASE, day_utc, start_utc, id
                """
            )
            for row in cur.fetchall():
                rid = int(row[0] or 0)
                rows.append(
                    {
                        "id": rid,
                        "source_key": f"db:{rid}",
                        "resource_set": str(row[1] or "Custom").strip() or "Custom",
                        "day_utc": str(row[2] or "ALL").strip() or "ALL",
                        "group_name": str(row[3] or "").strip(),
                        "mode": str(row[4] or "").strip().upper(),
                        "band": str(row[5] or "").strip().upper(),
                        "frequency": self._normalize_freq_text(str(row[6] or "").strip()),
                        "start_utc": str(row[7] or "").strip(),
                        "end_utc": str(row[8] or "").strip(),
                        "source": str(row[9] or "manual").strip().lower(),
                        "updated_utc": str(row[10] or "").strip(),
                        "recurrence": "Daily" if str(row[2] or "ALL").strip().upper() == "ALL" else "Weekly",
                        "biweekly_offset_weeks": 0,
                        "month_weeks": "",
                        "vfo": "A",
                    }
                )
        except Exception as e:
            log.debug("HF Schedule: failed loading persisted schedule resources: %s", e)
            rows = []
        finally:
            conn.close()
        return rows

    def _load_sop_schedule_resource_rows(
        self,
        *,
        profiles: Optional[List[Dict[str, Any]]] = None,
        apply_condition_filter: bool = False,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        profiles = [dict(p) for p in (profiles or [p for p in self._load_sop_profile_catalog() if self._is_hf_sop_profile(p)])]
        condition_levels: Dict[str, int] = {}
        if apply_condition_filter:
            try:
                condition_levels = dict(getattr(self._sop_manager, "_condition_level_map")() or {})
            except Exception:
                condition_levels = {}
        for profile in profiles:
            pid = int(profile.get("id") or 0)
            if pid <= 0:
                continue
            profile_name = str(profile.get("name") or "").strip() or f"SOP {pid}"
            group_name = str(profile.get("operating_group") or "").strip()
            full = self._sop_manager.get_profile(pid)
            if not full:
                continue
            for idx, layer in enumerate(full.get("schedule_layer") or []):
                if not isinstance(layer, dict):
                    continue
                if not bool(layer.get("enabled", True)):
                    continue
                layer_id = int(layer.get("id") or 0)
                recurrence = str(
                    layer.get("recurrence")
                    or ("Daily" if str(layer.get("day_utc") or "ALL").strip().upper() == "ALL" else "Weekly")
                ).strip() or "Weekly"
                day_utc = self._normalize_day(str(layer.get("day_utc") or "ALL"))
                row_group_name = str(layer.get("group_name") or group_name).strip().upper()
                if apply_condition_filter:
                    group_level = condition_levels.get(row_group_name)
                    cond_levels = str(layer.get("condition_levels") or "ALL")
                    try:
                        matches = bool(getattr(self._sop_manager, "_action_condition_match")(cond_levels, group_level))
                    except Exception:
                        matches = True
                    if not matches:
                        continue
                band = str(layer.get("band") or "").strip().upper()
                mode = str(layer.get("mode") or "").strip().upper()
                vfo = str(layer.get("vfo") or "A").strip().upper() or "A"
                freq = self._normalize_freq_text(str(layer.get("frequency") or "").strip())
                start = self._normalize_hhmm(str(layer.get("start_utc") or ""))
                end = self._normalize_hhmm(str(layer.get("end_utc") or ""))
                if not (band and mode and freq and start and end):
                    continue
                key_layer = layer_id if layer_id > 0 else (idx + 1)
                rows.append(
                    {
                        "id": 0,
                        "source_key": f"sop:{pid}:{key_layer}",
                        "resource_set": f"SOP: {profile_name}",
                        "day_utc": day_utc,
                        "group_name": row_group_name,
                        "mode": mode,
                        "band": band,
                        "frequency": freq,
                        "start_utc": start,
                        "end_utc": end,
                        "source": "sop_layer",
                        "updated_utc": str(layer.get("updated_utc") or profile.get("updated_utc") or "").strip(),
                        "recurrence": recurrence,
                        "biweekly_offset_weeks": int(layer.get("biweekly_offset_weeks") or 0),
                        "month_weeks": str(layer.get("month_weeks") or "").strip(),
                        "vfo": vfo,
                        "sop_profile_id": pid,
                        "sop_profile_name": profile_name,
                    }
                )
        return rows

    def _sop_group_defaults(self, group_name: str) -> Tuple[str, str]:
        group = str(group_name or "").strip().upper()
        if not group:
            return "DIGI", "A"
        for g in self.operating_groups:
            if str(g.get("group") or "").strip().upper() != group:
                continue
            mode = str(g.get("mode") or "").strip().upper() or "DIGI"
            vfo = str(g.get("vfo") or "").strip().upper() or "A"
            return mode, vfo
        return "DIGI", "A"

    def _load_sop_gap_resource_rows(self, *, profiles: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        profile_map: Dict[int, Dict[str, Any]] = {}
        for p in (profiles or [p for p in self._load_sop_profile_catalog() if self._is_hf_sop_profile(p)]):
            pid = int(p.get("id") or 0)
            if pid > 0:
                profile_map[pid] = dict(p)
        if not profile_map:
            return rows
        try:
            upcoming = self._sop_manager.build_upcoming_actions(horizon_hours=24, only_active=True)
        except Exception as e:
            log.debug("HF Schedule: failed loading SOP gap resource rows: %s", e)
            return rows
        seen: Set[str] = set()
        for row in upcoming:
            if bool(row.get("aligned", True)):
                continue
            if str(row.get("contact_rule") or "").strip().lower() in {"local_profile", "local_group"}:
                continue
            profile_id = int(row.get("profile_id") or 0)
            profile = profile_map.get(profile_id)
            if not profile:
                continue
            due_utc = row.get("next_due_utc")
            if not isinstance(due_utc, datetime.datetime):
                continue
            band = str(row.get("band") or "").strip().upper()
            freq = self._normalize_freq_text(str(row.get("frequency") or "").strip())
            group_name = str(row.get("operating_group") or "").strip().upper()
            if not (band and freq and group_name):
                continue
            mode, vfo = self._sop_group_defaults(group_name)
            interval_minutes = max(1, int(row.get("interval_minutes") or 0 or 60))
            due_utc = due_utc.astimezone(datetime.timezone.utc)
            end_utc_dt = due_utc + datetime.timedelta(minutes=interval_minutes)
            start_utc = due_utc.strftime("%H:%M")
            end_utc = end_utc_dt.strftime("%H:%M")
            profile_name = str(profile.get("name") or row.get("profile_name") or f"SOP {profile_id}").strip() or f"SOP {profile_id}"
            action_id = int(row.get("action_id") or 0)
            source_key = f"sop_gap:{profile_id}:{action_id}:{band}:{freq}:{start_utc}:{end_utc}"
            if source_key in seen:
                continue
            seen.add(source_key)
            rows.append(
                {
                    "id": 0,
                    "source_key": source_key,
                    "resource_set": f"SOP Gap: {profile_name}",
                    "day_utc": "ALL",
                    "group_name": group_name,
                    "mode": mode,
                    "band": band,
                    "frequency": freq,
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                    "source": "sop_gap",
                    "updated_utc": self._utc_now_iso(),
                    "recurrence": "Daily",
                    "biweekly_offset_weeks": 0,
                    "month_weeks": "",
                    "vfo": vfo,
                    "sop_profile_id": profile_id,
                    "sop_profile_name": profile_name,
                }
            )
        return rows

    def _ensure_schedule_resources_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hf_schedule_resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_set TEXT NOT NULL DEFAULT 'Custom',
                day_utc TEXT NOT NULL DEFAULT 'ALL',
                group_name TEXT,
                mode TEXT,
                band TEXT,
                frequency TEXT,
                start_utc TEXT,
                end_utc TEXT,
                source_type TEXT DEFAULT 'manual',
                source_ref TEXT DEFAULT '',
                updated_utc TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_hf_resources_set_day_time
            ON hf_schedule_resources(resource_set, day_utc, start_utc)
            """
        )

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

    def _normalize_day(self, day: str) -> str:
        val = str(day or "ALL").strip()
        if not val:
            return "ALL"
        up = val.upper()
        if up == "ALL":
            return "ALL"
        for opt in DAY_OPTIONS:
            if opt.upper() == up:
                return opt
        return "ALL"

    def _normalize_hhmm(self, value: str) -> str:
        txt = str(value or "").strip()
        if not txt:
            return ""
        compact = txt.replace(" ", "")
        if compact.isdigit() and len(compact) in {3, 4}:
            if len(compact) == 3:
                compact = f"0{compact}"
            txt = f"{compact[:2]}:{compact[2:]}"
        try:
            hh, mm = txt.split(":")
            h = int(hh)
            m = int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}"
        except Exception:
            pass
        return txt

    def _schedule_resource_key(self, row: Dict[str, Any]) -> Tuple[str, str, str, str, str, str, str]:
        return (
            self._normalize_day(str(row.get("day_utc") or "")),
            str(row.get("group_name") or "").strip().upper(),
            str(row.get("mode") or "").strip().upper(),
            str(row.get("band") or "").strip().upper(),
            self._normalize_freq_text(str(row.get("frequency") or "").strip()),
            self._normalize_hhmm(str(row.get("start_utc") or "")),
            self._normalize_hhmm(str(row.get("end_utc") or "")),
        )

    def _refresh_schedule_resources(self, *, force: bool = False) -> None:
        token = (
            self._safe_mtime(self._db_path()),
            self._safe_mtime(self._nets_db_path()),
        )
        if force or self._schedule_resource_token != token:
            self._schedule_resource_rows = self._load_schedule_resource_rows()
            self._schedule_resource_token = token
            self._schedule_resource_view_token = None
            self._refresh_resource_set_combo()
        self._populate_schedule_resources_table()

    def _refresh_resource_set_combo(self) -> None:
        current = str(self.resources_set_combo.currentData() or "All")
        sets = sorted(
            {
                str(row.get("resource_set") or "Custom").strip() or "Custom"
                for row in self._schedule_resource_rows
            },
            key=lambda x: x.upper(),
        )
        self.resources_set_combo.blockSignals(True)
        self.resources_set_combo.clear()
        self.resources_set_combo.addItem("All", "All")
        for resource_set in sets:
            self.resources_set_combo.addItem(resource_set, resource_set)
        idx = self.resources_set_combo.findData(current)
        if idx >= 0:
            self.resources_set_combo.setCurrentIndex(idx)
        self.resources_set_combo.blockSignals(False)

    def _resource_source_key(self, row: Dict[str, Any]) -> str:
        source_key = str(row.get("source_key") or "").strip()
        if source_key:
            return source_key
        rid = int(row.get("id") or 0)
        return f"db:{rid}" if rid > 0 else ""

    def _selected_view_resource_rows(self) -> List[Dict[str, Any]]:
        selected = self._selected_resource_rows()
        if not selected:
            return []
        selected_keys = {self._resource_source_key(r) for r in selected if self._resource_source_key(r)}
        if not selected_keys:
            return []
        return [dict(r) for r in self._resource_view_rows if self._resource_source_key(r) in selected_keys]

    def _active_schedule_keys(self) -> Set[Tuple[str, str, str, str, str, str, str, str, str, str]]:
        keys: Set[Tuple[str, str, str, str, str, str, str, str, str, str]] = set()
        for r in range(self.table.rowCount()):
            row = self._active_row_to_utc(r)
            if row:
                keys.add(self._active_dup_key(row))
        return keys

    def _sop_profiles_by_group(self) -> Dict[str, List[Dict[str, Any]]]:
        by_group: Dict[str, List[Dict[str, Any]]] = {}
        profiles = [p for p in self._load_sop_profile_catalog() if self._is_hf_sop_profile(p)]
        for profile in profiles:
            group = str(profile.get("operating_group") or "").strip().upper()
            if not group:
                continue
            by_group.setdefault(group, []).append(profile)
        return by_group

    def _resource_conflict_summary(
        self,
        row: Dict[str, Any],
        *,
        active_keys: Optional[Set[Tuple[str, str, str, str, str, str, str]]] = None,
    ) -> Tuple[str, bool]:
        key = self._active_dup_key(row)
        if active_keys is not None and key in active_keys:
            return "Duplicate in HF Active", True
        return "Ready", False

    def _populate_schedule_resources_table(self) -> None:
        set_filter = str(self.resources_set_combo.currentData() or "All").strip()
        text_filter = str(self.resources_group_filter.text() or "").strip().upper()
        view_token = (
            self._schedule_resource_token,
            set_filter,
            text_filter,
            bool(self._show_local),
        )
        if self._schedule_resource_view_token == view_token:
            self._update_resource_action_state()
            return
        rows: List[Dict[str, Any]] = []
        for row in self._schedule_resource_rows:
            resource_set = str(row.get("resource_set") or "Custom").strip() or "Custom"
            if set_filter != "All" and resource_set != set_filter:
                continue
            if text_filter:
                hay = " ".join(
                    [
                        resource_set,
                        str(row.get("sop_profile_name") or ""),
                        str(row.get("group_name") or ""),
                        str(row.get("mode") or ""),
                        str(row.get("band") or ""),
                        str(row.get("frequency") or ""),
                        str(row.get("start_utc") or ""),
                        str(row.get("end_utc") or ""),
                        str(row.get("source") or ""),
                    ]
                ).upper()
                if text_filter not in hay:
                    continue
            rows.append(row)
        active_keys = self._active_schedule_keys()
        view_rows: List[Dict[str, Any]] = []
        for row in rows:
            view = dict(row)
            conflict_text, has_conflict = self._resource_conflict_summary(
                view,
                active_keys=active_keys,
            )
            view["_conflict_text"] = conflict_text
            view["_has_conflict"] = bool(has_conflict)
            view_rows.append(view)
        self._resource_view_rows = view_rows

        self.resources_table.setSortingEnabled(False)
        self.resources_table.setRowCount(0)
        for row in view_rows:
            r = self.resources_table.rowCount()
            self.resources_table.insertRow(r)
            source_key = self._resource_source_key(row)
            select_wrap = QWidget()
            select_layout = QHBoxLayout(select_wrap)
            select_layout.setContentsMargins(0, 0, 0, 0)
            select_layout.setAlignment(Qt.AlignCenter)
            select_chk = QCheckBox()
            select_chk.setProperty("source_key", source_key)
            select_chk.stateChanged.connect(lambda _=None: self._update_resource_action_state())
            select_layout.addWidget(select_chk)
            self.resources_table.setCellWidget(r, self.RES_COL_SELECT, select_wrap)
            day_txt, start_txt, end_txt = self._display_resource_day_time(
                str(row.get("day_utc") or "ALL"),
                str(row.get("start_utc") or ""),
                str(row.get("end_utc") or ""),
            )
            values = [
                str(row.get("resource_set") or "Custom"),
                day_txt,
                str(row.get("group_name") or ""),
                str(row.get("mode") or ""),
                str(row.get("band") or ""),
                str(row.get("frequency") or ""),
                start_txt,
                end_txt,
                (
                    "Saved Schedule"
                    if str(row.get("source") or "").strip().lower() == "saved_schedule"
                    else str(row.get("source") or "")
                ),
                self._format_age_label(row.get("updated_utc")),
                str(row.get("_conflict_text") or ""),
            ]
            for offset, val in enumerate(values):
                c = self.RES_COL_SET + offset
                item = QTableWidgetItem(val)
                if c == self.RES_COL_SET:
                    item.setData(Qt.UserRole, source_key)
                if c == self.RES_COL_UPDATED:
                    raw_updated = str(row.get("updated_utc") or "").strip()
                    item.setToolTip(raw_updated if raw_updated else "No saved timestamp available.")
                self.resources_table.setItem(r, c, item)
        self.resources_table.setSortingEnabled(True)
        self._schedule_resource_view_token = view_token
        self._update_schedule_resources_empty_state()
        self._update_resource_action_state()
        if hasattr(self, "resources_count_label"):
            total = len(getattr(self, "_schedule_resource_rows", []) or [])
            shown = len(view_rows)
            self.resources_count_label.setText(f"{shown} shown / {total} saved row(s)" if total else "0 saved rows")
        self._apply_schedule_table_height_hints()

    def _update_schedule_resources_empty_state(self) -> None:
        if not hasattr(self, "resources_empty_label") or not hasattr(self, "resources_table"):
            return
        has_rows = bool(getattr(self, "_resource_view_rows", []))
        has_any_resource = bool(getattr(self, "_schedule_resource_rows", []))
        text_filter = str(self.resources_group_filter.text() or "").strip()
        set_filter = str(self.resources_set_combo.currentData() or "All").strip()
        filtered = has_any_resource and not has_rows
        if filtered:
            self.resources_empty_label.setText(
                f"No schedule library rows match the current filters ({set_filter}, {text_filter or 'no search text'})."
            )
        else:
            self.resources_empty_label.setText(
                "No saved HF Daily schedules yet. Save a named schedule above to make its rows available here."
            )
        self.resources_empty_label.setVisible(not has_rows)
        self.resources_table.setVisible(has_rows)
        if hasattr(self, "resources_count_label") and not has_rows:
            total = len(getattr(self, "_schedule_resource_rows", []) or [])
            self.resources_count_label.setText(f"0 shown / {total} saved row(s)" if total else "0 saved rows")

    def _update_resource_action_state(
        self,
        *,
        active_conflicts_override: Optional[List[Tuple[int, int, str]]] = None,
    ) -> None:
        theme = resolve_theme(self.settings)
        selected_rows = self._selected_resource_rows()
        has_selected = bool(selected_rows)
        has_rows = bool(self._resource_view_rows)
        selected_view_rows = self._selected_view_resource_rows()
        conflict_scope = selected_view_rows if selected_view_rows else self._resource_view_rows
        has_conflicts = any(bool(r.get("_has_conflict")) for r in conflict_scope)
        has_resource_conflicts = any(bool(r.get("_has_conflict")) for r in self._resource_view_rows)
        if active_conflicts_override is not None:
            has_hf_conflicts = bool(active_conflicts_override)
        else:
            has_hf_conflicts = bool(self._collect_active_time_conflict_pairs())
        has_sop_conflicts = bool(getattr(self, "_has_active_hf_sop_conflicts", False))
        has_active_conflicts = bool(has_hf_conflicts or has_sop_conflicts)
        deletable_scope = selected_rows if selected_rows else self._resource_view_rows
        has_deletable = any(self._resource_row_is_deletable(r) for r in deletable_scope)
        self.add_selected_resource_action.setEnabled(has_selected)
        self.add_filtered_resource_action.setEnabled(has_rows)
        self.add_to_schedule_default_action.setEnabled(has_selected)
        self.add_to_schedule_default_action.setText("Add Selected Rows")
        self.add_selected_resource_action.setText("Add Selected Rows")
        self.add_filtered_resource_action.setText("Add Filtered Rows")
        self.add_to_schedule_btn.setEnabled(has_selected)
        self.add_to_schedule_btn.setToolTip(
            "Copy selected library rows into the HF Daily schedule being edited. Library rows stay saved."
            if has_selected
            else "Select one or more library rows to add to this schedule."
        )
        self.add_to_schedule_btn.setText("Add Selected Rows")
        self.add_to_schedule_btn.setFont(self.add_row_btn.font())
        self.add_to_schedule_btn.setStyleSheet(
            button_style(
                "eligible_warning" if has_selected and has_conflicts else ("eligible_info" if has_selected else "muted"),
                theme,
            )
        )
        can_resolve = has_active_conflicts or has_resource_conflicts
        self.resources_resolve_btn.setVisible(can_resolve)
        self.resources_resolve_btn.setEnabled(can_resolve)
        self.resources_resolve_btn.setStyleSheet(button_style("eligible_warning" if can_resolve else "muted", theme))
        self.resources_resolve_btn.setToolTip(
            "Resolve active schedule overlaps (HF/HF or HF/SOP)."
            if has_active_conflicts
            else "Resolve duplicate HF Active schedule entries."
        )
        self.resources_delete_btn.setEnabled(has_deletable)
        self.resources_delete_btn.setStyleSheet(button_style("eligible_danger" if has_deletable else "muted", theme))
        self.resources_delete_btn.setToolTip(
            "Delete selected HF library rows. SOP rows are managed in SOP Builder."
        )
        self.resources_refresh_btn.setStyleSheet(button_style("muted", theme))

    def _selected_resource_rows(self) -> List[Dict[str, Any]]:
        checked_keys: set[str] = set()
        for r in range(self.resources_table.rowCount()):
            wrap = self.resources_table.cellWidget(r, self.RES_COL_SELECT)
            chk: Optional[QCheckBox] = None
            if isinstance(wrap, QCheckBox):
                chk = wrap
            elif isinstance(wrap, QWidget):
                chk = wrap.findChild(QCheckBox)
            if chk is None or not chk.isChecked():
                continue
            key = str(chk.property("source_key") or "").strip()
            if key:
                checked_keys.add(key)

        source_keys: set[str]
        if checked_keys:
            source_keys = checked_keys
        else:
            source_keys = set()
            for idx in self.resources_table.selectionModel().selectedRows() if self.resources_table.selectionModel() else []:
                item = self.resources_table.item(idx.row(), self.RES_COL_SET)
                if item is None:
                    continue
                raw_key = str(item.data(Qt.UserRole) or "").strip()
                if raw_key:
                    source_keys.add(raw_key)
        if not source_keys:
            return []
        out: List[Dict[str, Any]] = []
        for row in self._schedule_resource_rows:
            source_key = self._resource_source_key(row)
            if source_key and source_key in source_keys:
                out.append(dict(row))
        return out

    @staticmethod
    def _resource_row_is_deletable(row: Dict[str, Any]) -> bool:
        source = str(row.get("source") or "").strip().lower()
        if source in {"sop_layer", "sop_gap"}:
            return False
        try:
            return int(row.get("id") or 0) > 0
        except Exception:
            return False

    def _delete_selected_resources(self) -> None:
        selected = self._selected_resource_rows()
        if not selected:
            QMessageBox.information(self, "Delete Library Rows", "No Daily Row Library rows selected.")
            return
        deletable_ids: List[int] = []
        blocked_count = 0
        for row in selected:
            if self._resource_row_is_deletable(row):
                rid = int(row.get("id") or 0)
                if rid > 0:
                    deletable_ids.append(rid)
            else:
                blocked_count += 1
        if not deletable_ids:
            QMessageBox.information(
                self,
                "Delete Library Rows",
                "Selected rows are SOP-derived and can only be managed in SOP Builder.",
            )
            return

        detail = f"Delete {len(deletable_ids)} HF library row(s)?"
        if blocked_count > 0:
            detail += f"\n\n{blocked_count} SOP-derived row(s) will be kept (managed in SOP Builder)."
        if QMessageBox.question(self, "Delete Library Rows", detail) != QMessageBox.Yes:
            return

        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        try:
            self._ensure_schedule_resources_table(conn)
            placeholders = ",".join(["?"] * len(deletable_ids))
            conn.execute(f"DELETE FROM hf_schedule_resources WHERE id IN ({placeholders})", tuple(deletable_ids))
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            QMessageBox.warning(self, "Delete Library Rows", f"Could not delete library rows:\n{e}")
            return
        finally:
            conn.close()

        self._refresh_schedule_resources(force=True)
        msg = f"Deleted {len(deletable_ids)} HF library row(s)."
        if blocked_count > 0:
            msg += f"\nSkipped {blocked_count} SOP-derived row(s)."
        QMessageBox.information(self, "Delete Library Rows", msg)

    def _active_row_is_empty(self, row_index: int) -> bool:
        day = self._get_combo_value(row_index, self.COL_DAY, "")
        group = self._get_combo_value(row_index, self.COL_GROUP, "")
        mode = self._get_combo_value(row_index, self.COL_MODE, "")
        band = self._get_combo_value(row_index, self.COL_BAND, "")
        freq = self._get_text_value(row_index, self.COL_FREQ)
        start = self._get_text_value(row_index, self.COL_START)
        end = self._get_text_value(row_index, self.COL_END)
        return not (day or group or mode or band or freq or start or end)

    def _active_row_to_utc(self, row_index: int, *, include_sop_overlay: bool = False) -> Optional[Dict[str, Any]]:
        is_sop_overlay = self._is_sop_overlay_row(row_index)
        if is_sop_overlay and not include_sop_overlay:
            return None
        day = self._normalize_day(self._get_combo_value(row_index, self.COL_DAY, "ALL"))
        group = self._get_combo_value(row_index, self.COL_GROUP, "")
        if is_sop_overlay:
            sel_wrap = self.table.cellWidget(row_index, self.COL_SELECT)
            if isinstance(sel_wrap, QWidget):
                raw_group = str(sel_wrap.property("sop_overlay_group_name") or "").strip()
                if raw_group:
                    group = raw_group
            if str(group).upper().startswith("SOP:"):
                group = str(group)[4:]
        mode = self._get_combo_value(row_index, self.COL_MODE, "Digi")
        band = self._get_combo_value(row_index, self.COL_BAND, "")
        freq = self._frequency_value(row_index)
        start = self._normalize_hhmm(self._get_text_value(row_index, self.COL_START))
        end = self._normalize_hhmm(self._get_text_value(row_index, self.COL_END))
        auto_tune = self._get_checkbox_value(row_index, self.COL_AUTOTUNE) if not is_sop_overlay else False
        if not (group and mode and band and freq and start and end):
            return None
        if self._show_local:
            source_day = day
            day, start = self._convert_day_time(source_day, start, to_local=False)
            _, end = self._convert_day_time(source_day, end, to_local=False)
        target_scope, target_device_profile_id, target_operating_profile_id = (
            (TARGET_SCOPE_STATION, None, None)
            if is_sop_overlay
            else self._selected_schedule_target(row_index)
        )
        return normalize_schedule_target_fields(
            {
                "day_utc": self._normalize_day(day),
                "group_name": str(group).strip(),
                "mode": str(mode).strip().upper(),
                "band": str(band).strip().upper(),
                "frequency": str(freq).strip(),
                "operating_group_frequency_key": self._frequency_key(row_index),
                "start_utc": self._normalize_hhmm(start),
                "end_utc": self._normalize_hhmm(end),
                "auto_tune": bool(auto_tune),
                "target_scope": target_scope,
                "target_device_profile_id": target_device_profile_id,
                "target_operating_profile_id": target_operating_profile_id,
            }
        )

    def _active_dup_key(self, row: Dict[str, Any]) -> Tuple[str, str, str, str, str, str, str, str, str, str]:
        target_scope, target_device_profile_id, target_operating_profile_id = schedule_target_identity_parts(row)
        return (
            self._normalize_day(str(row.get("day_utc") or "ALL")),
            str(row.get("group_name") or "").strip().upper(),
            str(row.get("mode") or "").strip().upper(),
            str(row.get("band") or "").strip().upper(),
            self._normalize_freq_text(str(row.get("frequency") or "")),
            self._normalize_hhmm(str(row.get("start_utc") or "")),
            self._normalize_hhmm(str(row.get("end_utc") or "")),
            target_scope,
            target_device_profile_id,
            target_operating_profile_id,
        )

    def _resource_rows_to_schedule_rows(self, resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for r in resources:
            day_utc = self._normalize_day(str(r.get("day_utc") or "ALL"))
            recurrence = str(r.get("recurrence") or "").strip()
            if not recurrence:
                recurrence = "Daily" if day_utc == "ALL" else "Weekly"
            rows.append(
                {
                    "day_utc": day_utc,
                    "group_name": str(r.get("group_name") or "").strip(),
                    "mode": str(r.get("mode") or "").strip().upper(),
                    "band": str(r.get("band") or "").strip().upper(),
                    "frequency": self._normalize_freq_text(str(r.get("frequency") or "")),
                    "start_utc": self._normalize_hhmm(str(r.get("start_utc") or "")),
                    "end_utc": self._normalize_hhmm(str(r.get("end_utc") or "")),
                    "recurrence": recurrence,
                    "biweekly_offset_weeks": int(r.get("biweekly_offset_weeks") or 0),
                    "month_weeks": str(r.get("month_weeks") or "").strip(),
                    "vfo": str(r.get("vfo") or "A").strip().upper() or "A",
                    "auto_tune": False,
                    "_resource_id": int(r.get("id") or 0),
                    "_resource_set": str(r.get("resource_set") or "").strip(),
                    "_source_key": str(r.get("source_key") or "").strip(),
                }
            )
        return rows

    def _add_resources_to_schedule(self, resources: List[Dict[str, Any]], *, origin: str) -> None:
        if not resources:
            QMessageBox.information(self, "Daily Row Library", "No library rows selected.")
            return
        active_rows: List[Dict[str, Any]] = []
        for r in range(self.table.rowCount()):
            row = self._active_row_to_utc(r)
            if row:
                active_rows.append(row)
        active_keys = {self._active_dup_key(r) for r in active_rows}
        candidates = self._resource_rows_to_schedule_rows(resources)
        duplicate_lines: List[str] = []
        for row in candidates:
            key = self._active_dup_key(row)
            if key in active_keys:
                duplicate_lines.append(
                    f"{key[0]} {key[5]}-{key[6]} {key[1]} {key[3]} {key[4]}"
                )
        if duplicate_lines:
            preview = "\n".join(duplicate_lines[:20])
            if len(duplicate_lines) > 20:
                preview += f"\n... and {len(duplicate_lines) - 20} more."
            QMessageBox.warning(
                self,
                "Duplicate HF Schedule Entries",
                "Add blocked due to duplicate day/time/group/band/frequency entries.\n\n" + preview,
            )
            return
        confirm = QMessageBox.question(
            self,
            "Add to Active Schedule",
            f"Add {len(candidates)} row(s) from {origin}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        prev_suppress = self._suppress_autostart
        self._suppress_autostart = True
        try:
            for row in candidates:
                self._append_entry_row(self._entry_for_display(row))
        finally:
            self._suppress_autostart = prev_suppress
        self._mark_dirty()
        self._update_delete_button_state()
        QMessageBox.information(
            self,
            "Daily Row Library",
            f"Added {len(candidates)} library row(s) to the HF Daily schedule being edited.",
        )

    def _resolve_resource_conflicts(self) -> None:
        active_selected = set(self._selected_active_row_indexes())
        if active_selected:
            active_conflicts = self._collect_active_time_conflict_pairs(active_selected)
            if active_conflicts:
                self._show_active_conflicts_summary(active_conflicts, selected_only=True)
                return

            sop_conflicts = self._collect_hf_sop_conflict_entries(selected_scope=active_selected)
            if sop_conflicts:
                self._show_hf_sop_conflicts_summary(sop_conflicts, selected_only=True)
                return

            all_active_conflicts = self._collect_active_time_conflict_pairs()
            if all_active_conflicts:
                self._show_active_conflicts_summary(all_active_conflicts, selected_only=False)
                return

            all_sop_conflicts = self._collect_hf_sop_conflict_entries()
            if all_sop_conflicts:
                self._show_hf_sop_conflicts_summary(all_sop_conflicts, selected_only=False)
                return

            QMessageBox.information(
                self,
                "Resolve Conflicts",
                "No active schedule time conflicts detected for selected row(s).",
            )
            return

        all_active_conflicts = self._collect_active_time_conflict_pairs()
        if all_active_conflicts:
            self._show_active_conflicts_summary(all_active_conflicts, selected_only=False)
            return

        all_sop_conflicts = self._collect_hf_sop_conflict_entries()
        if all_sop_conflicts:
            self._show_hf_sop_conflicts_summary(all_sop_conflicts, selected_only=False)
            return

        selected = self._selected_resource_rows()
        scope = selected if selected else [dict(r) for r in self._resource_view_rows]
        if not scope:
            QMessageBox.information(self, "Resolve Conflicts", "No active conflicts or resource rows available.")
            return

        active_keys = self._active_schedule_keys()
        duplicates: List[str] = []
        for row in scope:
            key = self._active_dup_key(row)
            if key in active_keys:
                duplicates.append(f"{key[0]} {key[5]}-{key[6]} {key[1]} {key[3]} {key[4]}")
        if not duplicates:
            QMessageBox.information(self, "Resolve Conflicts", "No HF schedule conflicts detected.")
            return
        preview = "\n".join(duplicates[:15])
        if len(duplicates) > 15:
            preview += f"\n... and {len(duplicates) - 15} more."
        QMessageBox.information(
            self,
            "HF Conflicts",
            (
                "These rows already exist in Active Schedule.\n"
                "Resolve by editing/deleting duplicates.\n\n"
                f"{preview}"
            ),
        )
        self._populate_schedule_resources_table()

    def _resolve_sop_profile_for_group(self, group_name: str, candidates: List[Dict[str, Any]]) -> int:
        group = str(group_name or "").strip().upper()
        valid: List[Dict[str, Any]] = [dict(c) for c in candidates if int(c.get("id") or 0) > 0]
        if not valid:
            return 0
        if len(valid) == 1:
            return int(valid[0].get("id") or 0)
        remembered = int(self._sop_group_profile_choice.get(group, 0) or 0)
        if remembered > 0 and any(int(v.get("id") or 0) == remembered for v in valid):
            return remembered

        labels: List[str] = []
        id_by_label: Dict[str, int] = {}
        for profile in valid:
            pid = int(profile.get("id") or 0)
            name = str(profile.get("name") or f"SOP {pid}").strip() or f"SOP {pid}"
            state = "active" if bool(profile.get("active")) else "inactive"
            label = f"{name} (#{pid}, {state})"
            labels.append(label)
            id_by_label[label] = pid
        choice, ok = QInputDialog.getItem(
            self,
            "Select SOP Profile",
            (
                f"Multiple SOP profiles are associated with HF group '{group}'.\n"
                "Select the SOP profile to receive these rows."
            ),
            labels,
            0,
            False,
        )
        if not ok:
            return 0
        selected = int(id_by_label.get(str(choice), 0) or 0)
        if selected > 0:
            self._sop_group_profile_choice[group] = selected
        return selected

    def _resource_row_to_sop_layer_row(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        day_utc = "ALL"
        recurrence = "Daily"
        band = str(row.get("band") or "").strip().upper()
        mode = str(row.get("mode") or "").strip().upper()
        vfo = str(row.get("vfo") or "A").strip().upper() or "A"
        freq = self._normalize_freq_text(str(row.get("frequency") or "").strip())
        start = self._normalize_hhmm(str(row.get("start_utc") or "").strip())
        end = self._normalize_hhmm(str(row.get("end_utc") or "").strip())
        if not (band and mode and freq and start and end):
            return None
        return {
            "day_utc": day_utc,
            "recurrence": recurrence,
            "biweekly_offset_weeks": 0,
            "month_weeks": "",
            "band": band,
            "mode": mode,
            "vfo": vfo,
            "frequency": freq,
            "start_utc": start,
            "end_utc": end,
            "enabled": True,
        }

    def _dispatch_sop_schedule_change(self) -> None:
        win = self.window()
        dispatched = False
        try:
            if hasattr(win, "sop_tab") and hasattr(win.sop_tab, "on_sop_profiles_updated"):
                win.sop_tab.on_sop_profiles_updated()
        except Exception:
            pass
        try:
            if hasattr(win, "_on_sop_data_changed"):
                win._on_sop_data_changed()
                dispatched = True
            elif hasattr(win, "scheduler"):
                win.scheduler.force_refresh()
        except Exception:
            pass
        if not dispatched:
            self._schedule_resource_token = None
            self._refresh_sop_overlay_rows_in_table()
            self._refresh_sop_profiles_panel(force=True)
            self._update_effective_source_label()
            self._refresh_schedule_resources(force=True)

    def _add_resources_to_sop_layer(self, resources: List[Dict[str, Any]], *, origin: str) -> None:
        if not resources:
            QMessageBox.information(self, "Daily Row Library", "No library rows selected.")
            return

        profiles = [p for p in self._load_sop_profile_catalog() if self._is_hf_sop_profile(p)]
        by_group: Dict[str, List[Dict[str, Any]]] = {}
        for profile in profiles:
            grp = str(profile.get("operating_group") or "").strip().upper()
            if not grp:
                continue
            by_group.setdefault(grp, []).append(profile)

        profile_rows: Dict[int, List[Dict[str, Any]]] = {}
        unresolved: List[str] = []
        for row in resources:
            group = str(row.get("group_name") or "").strip().upper()
            if not group:
                unresolved.append("Missing group name in one or more rows.")
                continue
            candidates = by_group.get(group, [])
            if not candidates:
                unresolved.append(f"{group}: no HF SOP profile found.")
                continue
            profile_id = self._resolve_sop_profile_for_group(group, candidates)
            if profile_id <= 0:
                unresolved.append(f"{group}: selection cancelled.")
                continue
            layer_row = self._resource_row_to_sop_layer_row(row)
            if not layer_row:
                unresolved.append(f"{group}: incomplete row (band/mode/frequency/time required).")
                continue
            profile_rows.setdefault(profile_id, []).append(layer_row)

        if not profile_rows:
            msg = "No rows could be applied to SOP Layer."
            if unresolved:
                msg += "\n\n" + "\n".join(unresolved[:12])
            QMessageBox.warning(self, "SOP Layer", msg)
            return

        apply_count = sum(len(v) for v in profile_rows.values())
        confirm = QMessageBox.question(
            self,
            "Apply to SOP Layer",
            f"Apply {apply_count} row(s) from {origin} to SOP Layer across {len(profile_rows)} profile(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return

        changed = 0
        for profile_id, rows in profile_rows.items():
            try:
                changed += int(self._sop_manager.upsert_schedule_layer_rows(profile_id, rows) or 0)
            except Exception as e:
                log.debug("HF Schedule: failed applying rows to SOP layer profile %s: %s", profile_id, e)
                unresolved.append(f"Profile {profile_id}: update failed.")

        if changed > 0:
            try:
                self.resources_set_combo.blockSignals(True)
                idx = self.resources_set_combo.findData("All")
                if idx >= 0:
                    self.resources_set_combo.setCurrentIndex(idx)
            finally:
                self.resources_set_combo.blockSignals(False)
            self._dispatch_sop_schedule_change()

        detail = f"Applied {changed} row(s) to SOP Layer."
        if unresolved:
            detail += "\n\nIssues:\n" + "\n".join(unresolved[:12])
        QMessageBox.information(self, "SOP Layer", detail)

    def _apply_resources(self, resources: List[Dict[str, Any]], *, origin: str) -> None:
        self._add_resources_to_schedule(resources, origin=origin)

    def _add_resources_default(self) -> None:
        selected = self._selected_resource_rows()
        if selected:
            self._apply_resources(selected, origin="selected resources")
            return
        self._apply_resources([dict(r) for r in self._resource_view_rows], origin="filtered resources")

    def _add_selected_resources_to_schedule(self) -> None:
        self._apply_resources(self._selected_resource_rows(), origin="selected resources")

    def _add_filtered_resources_to_schedule(self) -> None:
        self._apply_resources([dict(r) for r in self._resource_view_rows], origin="filtered resources")

    def _checked_schedule_row_indexes(self) -> List[int]:
        selected: List[int] = []
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, self.COL_SELECT)
            chk: Optional[QCheckBox] = None
            if isinstance(w, QCheckBox):
                chk = w
            elif isinstance(w, QWidget):
                chk = w.findChild(QCheckBox)
            if chk is not None and chk.isChecked():
                selected.append(r)
        return selected

    def _selected_active_row_indexes(self) -> List[int]:
        selected: Set[int] = set(self._checked_schedule_row_indexes())
        if not selected:
            try:
                sel_model = self.table.selectionModel()
                if sel_model is not None:
                    for idx in sel_model.selectedRows():
                        selected.add(int(idx.row()))
            except Exception:
                pass
        return sorted(
            int(r)
            for r in selected
            if int(r) >= 0 and not self._is_sop_overlay_row(int(r))
        )

    def _active_row_summary_text(
        self,
        row_index: int,
        row: Optional[Dict[str, Any]] = None,
        *,
        include_day: bool = True,
    ) -> str:
        row_data = dict(row) if isinstance(row, dict) else self._active_row_to_utc(row_index, include_sop_overlay=True) or {}
        src = "SOP" if self._is_sop_overlay_row(row_index) else "HF"
        day = self._normalize_day(str(row_data.get("day_utc") or "ALL"))
        start = self._normalize_hhmm(str(row_data.get("start_utc") or ""))
        end = self._normalize_hhmm(str(row_data.get("end_utc") or ""))
        group = str(row_data.get("group_name") or "").strip().upper()
        band = str(row_data.get("band") or "").strip().upper()
        freq = self._normalize_freq_text(str(row_data.get("frequency") or ""))
        if include_day:
            return f"Row {row_index + 1} [{src}] {day} {start}-{end} {group} {band} {freq}".strip()
        return f"Row {row_index + 1} [{src}] {group} {band} {freq} {start}-{end}".strip()

    @staticmethod
    def _format_conflict_day_scope(days: Set[str]) -> str:
        canon = [d for d in DAY_CANON if d in days]
        if not canon:
            return ""
        if len(canon) == len(DAY_CANON):
            return "All days"
        if len(canon) == 1:
            return canon[0]
        return ", ".join(d[:3] for d in canon)

    @staticmethod
    def _interval_segments_from_hhmm(start_hhmm: str, end_hhmm: str) -> List[Tuple[int, int]]:
        s = DailyScheduleTab._time_to_minutes(start_hhmm)
        e = DailyScheduleTab._time_to_minutes(end_hhmm)
        if s is None or e is None:
            return []
        if s < e:
            return [(s, e)]
        if s > e:
            return [(s, 24 * 60), (0, e)]
        return [(0, 24 * 60)]

    @staticmethod
    def _subtract_segments(
        base_segments: List[Tuple[int, int]],
        blockers: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        out = list(base_segments)
        for b_start, b_end in blockers:
            next_out: List[Tuple[int, int]] = []
            for seg_start, seg_end in out:
                if b_end <= seg_start or b_start >= seg_end:
                    next_out.append((seg_start, seg_end))
                    continue
                if b_start > seg_start:
                    left = (seg_start, min(b_start, seg_end))
                    if left[1] > left[0]:
                        next_out.append(left)
                if b_end < seg_end:
                    right = (max(b_end, seg_start), seg_end)
                    if right[1] > right[0]:
                        next_out.append(right)
            out = next_out
            if not out:
                break
        out.sort(key=lambda p: (p[0], p[1]))
        merged: List[Tuple[int, int]] = []
        for seg in out:
            if not merged:
                merged.append(seg)
                continue
            last = merged[-1]
            if seg[0] <= last[1]:
                merged[-1] = (last[0], max(last[1], seg[1]))
            else:
                merged.append(seg)
        return [p for p in merged if p[1] > p[0]]

    @staticmethod
    def _segment_to_hhmm(seg: Tuple[int, int]) -> Tuple[str, str]:
        start_m, end_m = int(seg[0]), int(seg[1])
        start_m = max(0, min(24 * 60, start_m))
        end_m = max(0, min(24 * 60, end_m))
        start_txt = f"{(start_m % (24 * 60)) // 60:02d}:{(start_m % 60):02d}"
        if end_m >= 24 * 60:
            end_txt = "00:00"
        else:
            end_txt = f"{(end_m % (24 * 60)) // 60:02d}:{(end_m % 60):02d}"
        return start_txt, end_txt

    @staticmethod
    def _row_index_or_default(value: Any, default: int = -1) -> int:
        if value is None:
            return int(default)
        if isinstance(value, str) and not value.strip():
            return int(default)
        try:
            return int(value)
        except Exception:
            return int(default)

    def _collect_hf_sop_conflict_entries(
        self,
        selected_scope: Optional[Set[int]] = None,
    ) -> List[Dict[str, Any]]:
        selected_rows: Optional[Set[int]] = None
        if selected_scope:
            selected_rows = {int(r) for r in selected_scope}

        hf_rows: List[Dict[str, Any]] = []
        hf_rows_by_index: Dict[int, Dict[str, Any]] = {}
        for r in range(self.table.rowCount()):
            if selected_rows is not None and int(r) not in selected_rows:
                continue
            row = self._active_row_to_utc(r, include_sop_overlay=False)
            if not row:
                continue
            day_utc = self._normalize_day(str(row.get("day_utc") or "ALL"))
            start_utc = self._normalize_hhmm(str(row.get("start_utc") or ""))
            end_utc = self._normalize_hhmm(str(row.get("end_utc") or ""))
            band = str(row.get("band") or "").strip().upper()
            freq = self._normalize_freq_text(str(row.get("frequency") or ""))
            if not (day_utc and start_utc and end_utc and band and freq):
                continue
            expanded = {
                "day_utc": day_utc,
                "recurrence": "Daily" if day_utc == "ALL" else "Weekly",
                "biweekly_offset_weeks": 0,
                "month_weeks": "",
                "group_name": str(row.get("group_name") or "").strip().upper(),
                "mode": str(row.get("mode") or "").strip().upper(),
                "band": band,
                "frequency": freq,
                "start_utc": start_utc,
                "end_utc": end_utc,
                "_hf_row_index": int(r),
            }
            hf_rows.append(expanded)
            hf_rows_by_index[int(r)] = dict(row)
        if not hf_rows:
            return []

        def _parse_iso_utc(value: Any) -> Optional[datetime.datetime]:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except Exception:
                return None
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.astimezone(datetime.timezone.utc)

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        window_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + datetime.timedelta(days=8)
        try:
            hf_windows = self._sop_manager._expand_schedule_rows_windows(
                hf_rows,
                window_start_utc=window_start,
                window_end_utc=window_end,
            )
            active_conflicts = self._sop_manager.collect_active_hf_conflicts(
                force_refresh=True,
                include_details=True,
            )
        except Exception as e:
            log.debug("HF Schedule: failed loading authoritative active HF/SOP conflicts: %s", e)
            return []
        if not hf_windows or not active_conflicts:
            return []

        def _overlap_day_segments(
            start_utc: datetime.datetime,
            end_utc: datetime.datetime,
        ) -> List[Tuple[str, int, int]]:
            out_segments: List[Tuple[str, int, int]] = []
            cur = start_utc.astimezone(datetime.timezone.utc)
            end = end_utc.astimezone(datetime.timezone.utc)
            while cur < end:
                day_start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = day_start + datetime.timedelta(days=1)
                seg_end = min(end, day_end)
                day_name = cur.strftime("%A")
                start_m = (cur.hour * 60) + cur.minute
                end_m = (seg_end.hour * 60) + seg_end.minute
                if seg_end == day_end:
                    end_m = 24 * 60
                if end_m > start_m:
                    out_segments.append((day_name, int(start_m), int(end_m)))
                cur = seg_end
            return out_segments

        authoritative_daily_overlaps: List[Dict[str, Any]] = []
        for row in active_conflicts:
            if not isinstance(row, dict):
                continue
            action_band = str(row.get("band") or "").strip().upper()
            action_freq = self._normalize_freq_text(str(row.get("frequency") or ""))
            action_label = str(row.get("action_label") or "").strip() or "Action"
            profile_name = str(row.get("profile_name") or "").strip() or "HF SOP"
            for detail in list(row.get("daily_details") or []):
                if not isinstance(detail, dict):
                    continue
                overlap_start = _parse_iso_utc(detail.get("overlap_start_utc"))
                overlap_end = _parse_iso_utc(detail.get("overlap_end_utc"))
                if not isinstance(overlap_start, datetime.datetime) or not isinstance(overlap_end, datetime.datetime):
                    continue
                if overlap_end <= overlap_start:
                    continue
                hf_band = str(detail.get("other_band") or "").strip().upper()
                hf_freq = self._normalize_freq_text(str(detail.get("other_frequency") or ""))
                if not (hf_band and hf_freq):
                    continue
                authoritative_daily_overlaps.append(
                    {
                        "overlap_start": overlap_start,
                        "overlap_end": overlap_end,
                        "hf_band": hf_band,
                        "hf_freq": hf_freq,
                        "sop_profile_name": profile_name,
                        "sop_group_name": str(row.get("operating_group") or "").strip().upper(),
                        "sop_band": action_band,
                        "sop_freq": action_freq,
                        "sop_label": action_label,
                    }
                )
        if not authoritative_daily_overlaps:
            return []

        out: List[Dict[str, Any]] = []
        seen: Set[Tuple[int, str, str, int, int]] = set()
        for hf_row in hf_windows:
            hf_idx = self._row_index_or_default(hf_row.get("_hf_row_index"), -1)
            hf_base = hf_rows_by_index.get(hf_idx)
            if hf_idx < 0 or not isinstance(hf_base, dict):
                continue
            hf_band = str(hf_row.get("band") or "").strip().upper()
            hf_freq = self._normalize_freq_text(str(hf_row.get("frequency") or ""))
            hf_day = self._normalize_day(str(hf_base.get("day_utc") or "ALL"))
            hf_start = hf_row.get("start_dt_utc")
            hf_end = hf_row.get("end_dt_utc")
            if not isinstance(hf_start, datetime.datetime) or not isinstance(hf_end, datetime.datetime):
                continue
            for detail in authoritative_daily_overlaps:
                detail_band = str(detail.get("hf_band") or "").strip().upper()
                detail_freq = self._normalize_freq_text(str(detail.get("hf_freq") or ""))
                if hf_band != detail_band or hf_freq != detail_freq:
                    continue
                if not self._sop_manager._ranges_overlap(
                    hf_start,
                    hf_end,
                    detail["overlap_start"],
                    detail["overlap_end"],
                ):
                    continue
                sop_band = str(detail.get("sop_band") or "").strip().upper()
                sop_freq = self._normalize_freq_text(str(detail.get("sop_freq") or ""))
                if hf_band and hf_freq and hf_band == sop_band and hf_freq == sop_freq:
                    continue
                overlap_start = max(
                    hf_start.astimezone(datetime.timezone.utc),
                    detail["overlap_start"].astimezone(datetime.timezone.utc),
                )
                overlap_end = min(
                    hf_end.astimezone(datetime.timezone.utc),
                    detail["overlap_end"].astimezone(datetime.timezone.utc),
                )
                if overlap_end <= overlap_start:
                    continue
                source_key = (
                    f"sop:{str(detail.get('sop_profile_name') or '')}:"
                    f"{str(detail.get('sop_label') or '')}:"
                    f"{overlap_start.isoformat()}"
                )
                for day_name, start_m, end_m in _overlap_day_segments(overlap_start, overlap_end):
                    seen_key = (int(hf_idx), source_key, str(day_name), int(start_m), int(end_m))
                    if seen_key in seen:
                        continue
                    seen.add(seen_key)
                    out.append(
                        {
                            "hf_row_index": int(hf_idx),
                            "hf_row": dict(hf_base),
                            "sop_row": {
                                "_source_key": source_key,
                                "_sop_profile_name": str(detail.get("sop_profile_name") or "").strip() or "HF SOP",
                                "group_name": str(detail.get("sop_group_name") or "").strip().upper(),
                                "day_utc": str(day_name),
                                "band": sop_band,
                                "frequency": sop_freq,
                                "start_utc": self._normalize_hhmm(overlap_start.astimezone(datetime.timezone.utc).strftime("%H:%M")),
                                "end_utc": self._normalize_hhmm(overlap_end.astimezone(datetime.timezone.utc).strftime("%H:%M")),
                                "action_label": str(detail.get("sop_label") or "").strip(),
                            },
                            "day_name": str(day_name),
                            "auto_adjust_eligible": bool(hf_day == "ALL" or hf_day == str(day_name)),
                            "blocker_start_min": int(start_m),
                            "blocker_end_min": int(end_m),
                            "overlap_start_utc": overlap_start.replace(microsecond=0).isoformat(),
                        }
                    )
        out.sort(
            key=lambda row: (
                int(row.get("hf_row_index") or 0),
                str(row.get("day_name") or ""),
                int(row.get("blocker_start_min") or 0),
                str((row.get("sop_row") or {}).get("_source_key") or ""),
            )
        )
        return out

    def _can_auto_adjust_hf_around_sop_conflicts(self, conflicts: List[Dict[str, Any]]) -> bool:
        return any(bool(row.get("auto_adjust_eligible")) for row in (conflicts or []))

    def _group_hf_sop_conflicts_for_display(self, conflicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for row in conflicts or []:
            if not isinstance(row, dict):
                continue
            hf_idx = self._row_index_or_default(row.get("hf_row_index"), -1)
            hf_row = dict(row.get("hf_row") or {})
            sop_row = dict(row.get("sop_row") or {})
            sop_profile = str(sop_row.get("_sop_profile_name") or "HF SOP").strip() or "HF SOP"
            sop_group = str(sop_row.get("group_name") or "").strip().upper()
            sop_band = str(sop_row.get("band") or "").strip().upper()
            sop_freq = self._normalize_freq_text(str(sop_row.get("frequency") or ""))
            sop_start = self._normalize_hhmm(str(sop_row.get("start_utc") or ""))
            sop_end = self._normalize_hhmm(str(sop_row.get("end_utc") or ""))
            day_name = self._normalize_day(str(row.get("day_name") or "ALL"))
            key = (
                int(hf_idx),
                sop_profile,
                sop_group,
                sop_band,
                sop_freq,
                sop_start,
                sop_end,
            )
            entry = grouped.get(key)
            if entry is None:
                grouped[key] = {
                    "hf_row_index": int(hf_idx),
                    "hf_row": hf_row,
                    "sop_profile": sop_profile,
                    "sop_group": sop_group,
                    "sop_band": sop_band,
                    "sop_freq": sop_freq,
                    "sop_start": sop_start,
                    "sop_end": sop_end,
                    "days": {day_name},
                    "sort_day_rank": self._sort_day_rank(day_name),
                    "sort_start_min": int(row.get("blocker_start_min") or 0),
                }
                continue
            entry_days = entry.get("days")
            if isinstance(entry_days, set):
                entry_days.add(day_name)
            entry["sort_day_rank"] = min(int(entry.get("sort_day_rank") or 99), self._sort_day_rank(day_name))
            entry["sort_start_min"] = min(int(entry.get("sort_start_min") or 0), int(row.get("blocker_start_min") or 0))

        out = list(grouped.values())
        out.sort(
            key=lambda item: (
                int(item.get("hf_row_index") or -1),
                int(item.get("sort_day_rank") or 99),
                int(item.get("sort_start_min") or 0),
                str(item.get("sop_profile") or "").upper(),
                str(item.get("sop_group") or "").upper(),
                str(item.get("sop_freq") or ""),
            )
        )
        return out

    def _auto_adjust_hf_around_sop_conflicts(self, conflicts: List[Dict[str, Any]]) -> Tuple[bool, str]:
        row_map: Dict[int, Dict[str, Any]] = {}
        for r in range(self.table.rowCount()):
            entry, _sort_key = self._snapshot_active_entry_for_sort(r)
            row_utc = self._active_row_to_utc(r, include_sop_overlay=True)
            if isinstance(row_utc, dict):
                # Keep adjustment math in UTC; convert to display only when re-rendering rows.
                entry.update(
                    {
                        "day_utc": self._normalize_day(str(row_utc.get("day_utc") or "ALL")),
                        "group_name": str(row_utc.get("group_name") or "").strip(),
                        "mode": str(row_utc.get("mode") or "").strip().upper(),
                        "band": str(row_utc.get("band") or "").strip().upper(),
                        "frequency": self._normalize_freq_text(str(row_utc.get("frequency") or "")),
                        "start_utc": self._normalize_hhmm(str(row_utc.get("start_utc") or "")),
                        "end_utc": self._normalize_hhmm(str(row_utc.get("end_utc") or "")),
                        "auto_tune": bool(row_utc.get("auto_tune", False)),
                    }
                )
            row_map[int(r)] = {
                "row_index": int(r),
                "entry": dict(entry),
            }

        blockers_by_hf_day: Dict[int, Dict[str, List[Tuple[int, int]]]] = {}
        skipped_pairs = 0
        for row in conflicts:
            if not isinstance(row, dict):
                skipped_pairs += 1
                continue
            hf_idx = self._row_index_or_default(row.get("hf_row_index"), -1)
            hf_item = row_map.get(hf_idx)
            if hf_item is None:
                skipped_pairs += 1
                continue
            day_name = self._normalize_day(str(row.get("day_name") or "ALL"))
            try:
                block_start = int(row.get("blocker_start_min"))
                block_end = int(row.get("blocker_end_min"))
            except Exception:
                skipped_pairs += 1
                continue
            if block_end <= block_start:
                skipped_pairs += 1
                continue
            hf_day = self._normalize_day(str((hf_item.get("entry") or {}).get("day_utc") or "ALL"))
            if hf_day != "ALL" and hf_day != day_name:
                skipped_pairs += 1
                continue
            blockers_by_hf_day.setdefault(hf_idx, {}).setdefault(day_name, []).append((block_start, block_end))

        if not blockers_by_hf_day:
            return False, "No eligible HF/SOP overlap pairs were available for auto-adjust."

        new_items: List[Dict[str, Any]] = []
        changed_rows = 0
        removed_rows = 0
        split_rows = 0
        for r in range(self.table.rowCount()):
            item = row_map.get(int(r))
            if not item:
                continue
            if int(r) not in blockers_by_hf_day:
                new_items.append(dict(item["entry"]))
                continue

            hf_entry = dict(item["entry"])
            hf_day = self._normalize_day(str(hf_entry.get("day_utc") or "ALL"))
            base_segments = self._interval_segments_from_hhmm(
                str(hf_entry.get("start_utc") or ""),
                str(hf_entry.get("end_utc") or ""),
            )
            if not base_segments:
                new_items.append(hf_entry)
                continue
            blockers_for_row = blockers_by_hf_day.get(int(r), {})
            if hf_day != "ALL":
                blockers = list(blockers_for_row.get(hf_day, []))
                if not blockers:
                    new_items.append(hf_entry)
                    continue
                remaining = self._subtract_segments(base_segments, blockers)
                if remaining == base_segments:
                    new_items.append(hf_entry)
                    continue
                changed_rows += 1
                if not remaining:
                    removed_rows += 1
                    continue
                first = True
                for seg in remaining:
                    seg_start, seg_end = self._segment_to_hhmm(seg)
                    e = dict(hf_entry)
                    e["start_utc"] = seg_start
                    e["end_utc"] = seg_end
                    new_items.append(e)
                    if not first:
                        split_rows += 1
                    first = False
                continue

            # Row spans ALL days; only explode to day-specific rows when the adjusted
            # segments differ by day. If the same result applies every day, preserve ALL.
            per_day_remaining: Dict[str, List[Tuple[int, int]]] = {}
            day_changed = False
            for day_name in DAY_CANON:
                blockers = list(blockers_for_row.get(day_name, []))
                if blockers:
                    remaining = self._subtract_segments(base_segments, blockers)
                    if remaining != base_segments:
                        day_changed = True
                else:
                    remaining = list(base_segments)
                per_day_remaining[day_name] = list(remaining)
            if not day_changed:
                new_items.append(hf_entry)
                continue
            normalized_by_day = [
                tuple((int(seg[0]), int(seg[1])) for seg in per_day_remaining.get(day_name, []))
                for day_name in DAY_CANON
            ]
            uniform_segments = normalized_by_day[0] if normalized_by_day else tuple()
            if all(day_segments == uniform_segments for day_segments in normalized_by_day[1:]):
                changed_rows += 1
                if not uniform_segments:
                    removed_rows += 1
                    continue
                first = True
                for seg in uniform_segments:
                    seg_start, seg_end = self._segment_to_hhmm(seg)
                    e = dict(hf_entry)
                    e["day_utc"] = "ALL"
                    e["start_utc"] = seg_start
                    e["end_utc"] = seg_end
                    new_items.append(e)
                    if not first:
                        split_rows += 1
                    first = False
                continue
            per_day_rows: List[Dict[str, Any]] = []
            for day_name in DAY_CANON:
                for seg in per_day_remaining.get(day_name, []):
                    seg_start, seg_end = self._segment_to_hhmm(seg)
                    e = dict(hf_entry)
                    e["day_utc"] = day_name
                    e["start_utc"] = seg_start
                    e["end_utc"] = seg_end
                    per_day_rows.append(e)
            changed_rows += 1
            if not per_day_rows:
                removed_rows += 1
                continue
            split_rows += max(0, len(per_day_rows) - 1)
            new_items.extend(per_day_rows)

        if changed_rows <= 0:
            return False, "No HF rows changed during auto-adjust."

        try:
            self._record_sop_auto_adjust_snapshot_before()
        except Exception as e:
            log.debug("HF Schedule: failed capturing pre-auto-adjust SOP snapshot: %s", e)

        prev_suspend = self._suspend_dirty_tracking
        self._suspend_dirty_tracking = True
        try:
            self.table.setRowCount(0)
            for entry in new_items:
                self._append_entry_row(self._entry_for_display(entry))
        finally:
            self._suspend_dirty_tracking = prev_suspend
        self.table.clearSelection()
        self._update_delete_button_state()
        self._invalidate_active_schedule_views()
        if self.table.rowCount() > 1:
            self._sort_active_schedule_by_time(refresh_post_sort=False)
        self._highlight_time_conflicts()
        self._update_resource_action_state()
        self._set_dirty(True)
        current_hf_rows = self._collect_current_hf_rows_utc()
        auto_saved, auto_save_detail = self._persist_hf_schedule_rows(
            current_hf_rows,
            prompt_after_save=False,
        )
        try:
            self._record_sop_auto_adjust_snapshot_after()
        except Exception as e:
            log.debug("HF Schedule: failed recording post-auto-adjust SOP snapshot: %s", e)

        detail = (
            f"Auto-adjust complete. Updated {changed_rows} HF row(s), removed {removed_rows}, created {split_rows} split row(s)."
        )
        if auto_saved:
            detail += "\nHF schedule was auto-saved and active HF conflicts were rechecked."
        else:
            detail += "\nAuto-save failed; review the adjusted rows and save manually."
            detail += f"\n{auto_save_detail}"
        detail += "\nA pre-adjust HF snapshot was saved and can be restored when SOP is deactivated."
        if skipped_pairs > 0:
            detail += f"\n{skipped_pairs} conflict pair(s) require manual resolution."
        return True, detail

    def _show_hf_sop_conflicts_summary(
        self,
        conflicts: List[Dict[str, Any]],
        *,
        selected_only: bool,
    ) -> None:
        grouped_conflicts = self._group_hf_sop_conflicts_for_display(conflicts)
        lines: List[str] = []
        shown = 0
        for row in grouped_conflicts:
            if shown >= 18:
                break
            hf_idx = self._row_index_or_default(row.get("hf_row_index"), -1)
            day_scope = self._format_conflict_day_scope(set(row.get("days") or set()))
            hf_line = self._active_row_summary_text(hf_idx, row=dict(row.get("hf_row") or {}), include_day=False)
            sop_profile = str(row.get("sop_profile") or "HF SOP").strip()
            sop_group = str(row.get("sop_group") or "").strip().upper()
            sop_band = str(row.get("sop_band") or "").strip().upper()
            sop_freq = self._normalize_freq_text(str(row.get("sop_freq") or ""))
            sop_start = self._normalize_hhmm(str(row.get("sop_start") or ""))
            sop_end = self._normalize_hhmm(str(row.get("sop_end") or ""))
            lines.append(f"{shown + 1}. {hf_line}")
            sop_line = f"   SOP {sop_profile}: {sop_group} {sop_band} {sop_freq} {sop_start}-{sop_end}"
            if day_scope and day_scope != "All days":
                sop_line += f" [{day_scope}]"
            elif day_scope == "All days":
                sop_line += " [All days]"
            lines.append(sop_line)
            shown += 1
        if len(grouped_conflicts) > shown:
            lines.append(f"... and {len(grouped_conflicts) - shown} more HF/SOP conflict pair(s).")

        summary = (
            "Selected HF rows overlap active SOP windows on different frequencies."
            if selected_only
            else "Active HF rows overlap active SOP windows on different frequencies."
        )
        body = (
            summary
            + "\n"
            + "Adjust HF Start/End manually, or use Auto-Adjust to split/remove conflicting HF windows.\n\n"
            + "\n".join(lines)
        )
        box = QMessageBox(self)
        box.setWindowTitle("HF/SOP Conflicts")
        box.setText(body)
        auto_btn = None
        if self._can_auto_adjust_hf_around_sop_conflicts(conflicts):
            auto_btn = box.addButton("Auto-Adjust HF Around SOP (Reversible)", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.exec()
        if auto_btn is not None and box.clickedButton() is auto_btn:
            changed, detail = self._auto_adjust_hf_around_sop_conflicts(conflicts)
            title = "Auto-Adjust HF"
            if changed:
                QMessageBox.information(self, title, detail)
            else:
                QMessageBox.information(self, title, f"Auto-adjust skipped.\n\n{detail}")

    def _show_active_conflicts_summary(
        self,
        conflicts: List[Tuple[int, int, str]],
        *,
        selected_only: bool,
    ) -> None:
        pair_days: Dict[Tuple[int, int], Set[str]] = {}
        for r1, r2, day_name in conflicts:
            key = (min(int(r1), int(r2)), max(int(r1), int(r2)))
            pair_days.setdefault(key, set()).add(str(day_name or "").strip() or "Unknown")

        lines: List[str] = []
        keys = sorted(pair_days.keys(), key=lambda k: (k[0], k[1]))
        for idx, key in enumerate(keys[:18], start=1):
            r1, r2 = key
            day_scope = self._format_conflict_day_scope(pair_days.get(key, set()))
            left = self._active_row_summary_text(r1, include_day=False)
            right = self._active_row_summary_text(r2, include_day=False)
            if day_scope and day_scope != "All days":
                lines.append(f"{idx}. {left}")
                lines.append(f"   {right} [{day_scope}]")
            else:
                lines.append(f"{idx}. {left}")
                lines.append(f"   {right}")
        if len(keys) > 18:
            lines.append(f"... and {len(keys) - 18} more conflict pair(s).")

        summary = (
            "Selected Active Schedule rows overlap in time."
            if selected_only
            else "Active Schedule has time-overlap conflicts."
        )
        body = (
            summary
            + "\n"
            + "Resolve by editing Start/End, copying one row to the library for later use, or deleting one row.\n\n"
            + "\n".join(lines)
        )
        QMessageBox.information(self, "HF Conflicts", body)

    def _compute_active_conflict_state(
        self,
        selected_scope: Optional[Set[int]] = None,
    ) -> Tuple[List[Tuple[int, int, str]], Set[int]]:
        with perf_span(
            "daily_schedule.active_conflict_state",
            settings=self.settings,
            meta={"rows": int(self.table.rowCount()), "selected_scope": bool(selected_scope)},
            min_ms=1.0,
        ):
            selected_rows: Optional[Set[int]] = None
            if selected_scope:
                selected_rows = {int(r) for r in selected_scope}
            day_intervals: Dict[str, List[Tuple[int, int, int, bool]]] = {d: [] for d in DAY_CANON}
            row_target_context: Dict[int, Dict[str, Any]] = {}
            for r in range(self.table.rowCount()):
                if self._is_sop_overlay_row(r):
                    continue
                row = self._active_row_to_utc(r, include_sop_overlay=False)
                if not row:
                    continue
                row_target_context[r] = dict(row)
                start_m = self._time_to_minutes(str(row.get("start_utc") or ""))
                end_m = self._time_to_minutes(str(row.get("end_utc") or ""))
                if start_m is None or end_m is None:
                    continue
                day_names = self._schedule_day_names(str(row.get("day_utc") or "ALL"))
                is_selected = bool(selected_rows is not None and int(r) in selected_rows)
                for day_name in day_names:
                    day_idx = DAY_INDEX.get(day_name, 0)
                    next_day = DAY_CANON[(day_idx + 1) % len(DAY_CANON)]
                    if start_m < end_m:
                        day_intervals[day_name].append((r, start_m, end_m, is_selected))
                    elif start_m > end_m:
                        day_intervals[day_name].append((r, start_m, 24 * 60, is_selected))
                        day_intervals[next_day].append((r, 0, end_m, is_selected))
                    else:
                        day_intervals[day_name].append((r, 0, 24 * 60, is_selected))

            out: List[Tuple[int, int, str]] = []
            seen: Set[Tuple[int, int, str]] = set()
            conflict_rows: Set[int] = set()

            def _targets_overlap(left_row: int, right_row: int) -> bool:
                left = row_target_context.get(int(left_row))
                right = row_target_context.get(int(right_row))
                if not left or not right:
                    return True
                return schedule_targets_may_overlap(left, right)

            def _is_ambiguous_pair(left_row: int, right_row: int) -> bool:
                left = row_target_context.get(int(left_row))
                right = row_target_context.get(int(right_row))
                if not left or not right:
                    return True
                return rows_have_ambiguous_overlap(left, right)

            for day_name, spans in day_intervals.items():
                spans_sorted = sorted(spans, key=lambda x: (x[1], x[2], x[0], int(x[3])))
                active_all: List[Tuple[int, int, bool]] = []
                active_selected: List[Tuple[int, int]] = []
                for row_idx, start_m, end_m, is_selected in spans_sorted:
                    if active_all:
                        active_all = [entry for entry in active_all if int(entry[1]) > int(start_m)]
                    if selected_rows is not None and active_selected:
                        active_selected = [entry for entry in active_selected if int(entry[1]) > int(start_m)]

                    if selected_rows is None or is_selected:
                        for other_row, _other_end, _other_selected in active_all:
                            if not _targets_overlap(row_idx, other_row):
                                continue
                            if not _is_ambiguous_pair(row_idx, other_row):
                                continue
                            pair_key = (min(row_idx, other_row), max(row_idx, other_row), day_name)
                            if pair_key in seen:
                                continue
                            seen.add(pair_key)
                            out.append((pair_key[0], pair_key[1], day_name))
                            conflict_rows.add(pair_key[0])
                            conflict_rows.add(pair_key[1])
                    else:
                        for other_row, _other_end in active_selected:
                            if not _targets_overlap(row_idx, other_row):
                                continue
                            if not _is_ambiguous_pair(row_idx, other_row):
                                continue
                            pair_key = (min(row_idx, other_row), max(row_idx, other_row), day_name)
                            if pair_key in seen:
                                continue
                            seen.add(pair_key)
                            out.append((pair_key[0], pair_key[1], day_name))
                            conflict_rows.add(pair_key[0])
                            conflict_rows.add(pair_key[1])

                    active_all.append((row_idx, end_m, bool(is_selected)))
                    if is_selected:
                        active_selected.append((row_idx, end_m))
            return out, conflict_rows

    def _collect_active_time_conflict_pairs(
        self,
        selected_scope: Optional[Set[int]] = None,
    ) -> List[Tuple[int, int, str]]:
        if selected_scope is None:
            out, _conflict_rows = self._get_active_conflict_state()
        else:
            out, _conflict_rows = self._compute_active_conflict_state(selected_scope=selected_scope)
        return out

    def _upsert_schedule_resource_row(
        self,
        conn: sqlite3.Connection,
        row: Dict[str, Any],
        *,
        resource_set: str,
        source_type: str,
        source_ref: str,
        resource_id: Optional[int] = None,
    ) -> int:
        now_utc = self._utc_now_iso()
        day = self._normalize_day(str(row.get("day_utc") or "ALL"))
        group_name = str(row.get("group_name") or "").strip()
        mode = str(row.get("mode") or "").strip().upper()
        band = str(row.get("band") or "").strip().upper()
        freq = self._normalize_freq_text(str(row.get("frequency") or ""))
        start = self._normalize_hhmm(str(row.get("start_utc") or ""))
        end = self._normalize_hhmm(str(row.get("end_utc") or ""))
        set_name = str(resource_set or "Custom").strip() or "Custom"
        rid = int(resource_id or 0)
        if rid > 0:
            conn.execute(
                """
                UPDATE hf_schedule_resources
                SET resource_set = ?, day_utc = ?, group_name = ?, mode = ?, band = ?, frequency = ?,
                    start_utc = ?, end_utc = ?, source_type = ?, source_ref = ?, updated_utc = ?
                WHERE id = ?
                """,
                (set_name, day, group_name, mode, band, freq, start, end, source_type, source_ref, now_utc, rid),
            )
            return rid
        cur = conn.execute(
            """
            SELECT id
            FROM hf_schedule_resources
            WHERE resource_set = ? AND day_utc = ? AND group_name = ? AND mode = ? AND band = ?
              AND frequency = ? AND start_utc = ? AND end_utc = ?
            LIMIT 1
            """,
            (set_name, day, group_name, mode, band, freq, start, end),
        )
        existing = cur.fetchone()
        if existing:
            rid = int(existing[0] or 0)
            conn.execute(
                """
                UPDATE hf_schedule_resources
                SET source_type = ?, source_ref = ?, updated_utc = ?
                WHERE id = ?
                """,
                (source_type, source_ref, now_utc, rid),
            )
            return rid
        cur = conn.execute(
            """
            INSERT INTO hf_schedule_resources
                (resource_set, day_utc, group_name, mode, band, frequency, start_utc, end_utc, source_type, source_ref, updated_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (set_name, day, group_name, mode, band, freq, start, end, source_type, source_ref, now_utc),
        )
        return int(cur.lastrowid or 0)

    def _move_selected_schedule_rows_to_resources(self) -> None:
        selected = self._checked_schedule_row_indexes()
        if not selected:
            QMessageBox.information(self, "Copy to Library", "No Active Schedule rows selected.")
            return
        target_set = str(self.resources_set_combo.currentData() or "All").strip()
        if target_set == "All":
            target_set = "Custom"
        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        moved = 0
        moved_hf = 0
        moved_sop = 0
        try:
            self._ensure_schedule_resources_table(conn)
            for r in selected:
                is_sop_overlay = self._is_sop_overlay_row(r)
                row = self._active_row_to_utc(r, include_sop_overlay=True)
                if not row:
                    continue
                sel_w = self.table.cellWidget(r, self.COL_SELECT)
                resource_id = None
                if isinstance(sel_w, QWidget) and not is_sop_overlay:
                    rid = sel_w.property("resource_id")
                    if rid not in (None, ""):
                        try:
                            resource_id = int(rid)
                        except Exception:
                            resource_id = None
                source_type = "moved"
                source_ref = "active_schedule"
                if is_sop_overlay:
                    source_type = "sop_overlay"
                    source_ref = self._sop_overlay_source_key(r) or "sop_overlay"
                self._upsert_schedule_resource_row(
                    conn,
                    row,
                    resource_set=target_set,
                    source_type=source_type,
                    source_ref=source_ref,
                    resource_id=resource_id,
                )
                moved += 1
                if is_sop_overlay:
                    moved_sop += 1
                else:
                    moved_hf += 1
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            QMessageBox.critical(self, "Copy Failed", f"Could not copy rows to the schedule library:\n{e}")
            return
        finally:
            conn.close()
        self._refresh_schedule_resources(force=True)
        self._highlight_time_conflicts()
        QMessageBox.information(
            self,
            "Copied to Library",
            f"Copied {moved} row(s) to the Daily Row Library. The HF Daily schedule was not changed.",
        )

    def _refresh_schedule_issues(self, *, force: bool = False) -> None:
        # Retained method name for call-site compatibility.
        self._refresh_sop_profiles_panel(force=force)

    def _populate_schedule_issues_table(self) -> None:
        # Retained method name for call-site compatibility.
        self._refresh_sop_profiles_panel(force=True)

    def _dismiss_issue(self, key: str) -> None:
        # Retained for compatibility; schedule conflicts are now represented directly in SOP status rows.
        return

    def _open_sop_profile(self, profile_id: int) -> None:
        pid = int(profile_id or 0)
        if pid <= 0:
            return
        self._navigate_to_tab("SOP")
        win = self.window()
        try:
            sop_tab = getattr(win, "sop_tab", None)
            if sop_tab is None:
                return
            if hasattr(sop_tab, "select_profile") and sop_tab.select_profile(pid):
                return
            if hasattr(sop_tab, "profile_combo"):
                for idx in range(sop_tab.profile_combo.count()):
                    if int(sop_tab.profile_combo.itemData(idx) or 0) != pid:
                        continue
                    sop_tab.profile_combo.setCurrentIndex(idx)
                    return
        except Exception as e:
            log.debug("HF Schedule: failed to open SOP profile %s: %s", pid, e)

    def _focus_daily_row(self, group_name: str, band: str, frequency: str) -> None:
        group_norm = str(group_name or "").strip().upper()
        band_norm = str(band or "").strip().upper()
        freq_norm = self._normalize_freq_text(str(frequency or "").strip())
        for r in range(self.table.rowCount()):
            if self._is_sop_overlay_row(r):
                continue
            group_val = self._get_combo_value(r, self.COL_GROUP, default="").strip().upper()
            band_val = self._get_combo_value(r, self.COL_BAND, default="").strip().upper()
            freq_val = self._normalize_freq_text(self._get_text_value(r, self.COL_FREQ))
            if group_norm and group_val != group_norm:
                continue
            if band_norm and band_val != band_norm:
                continue
            if freq_norm and freq_val and freq_val != freq_norm:
                continue
            self.table.selectRow(r)
            self.table.scrollToItem(self.table.item(r, self.COL_FREQ))
            self.table.setFocus(Qt.TabFocusReason)
            return
        if self.table.rowCount() > 0:
            self.table.selectRow(0)
            self.table.setFocus(Qt.TabFocusReason)

    def focus_source_segment(self, segment: Any) -> bool:
        raw = getattr(segment, "raw", {}) if segment is not None else {}
        try:
            target_row_id = int(raw.get("source_row_id") or 0)
        except Exception:
            target_row_id = 0
        target_key = str(raw.get("source_key") or "").strip()
        for r in range(self.table.rowCount()):
            if self._is_sop_overlay_row(r):
                continue
            select_widget = self.table.cellWidget(r, self.COL_SELECT)
            if isinstance(select_widget, QWidget):
                try:
                    row_id = int(select_widget.property("source_row_id") or 0)
                except Exception:
                    row_id = 0
                row_key = str(select_widget.property("source_key") or "").strip()
                if (target_row_id > 0 and row_id == target_row_id) or (target_key and row_key == target_key):
                    self.table.selectRow(r)
                    item = self.table.item(r, self.COL_FREQ)
                    if item is not None:
                        self.table.scrollToItem(item)
                        self.table.setCurrentItem(item)
                        self.table.editItem(item)
                    self.table.setFocus(Qt.TabFocusReason)
                    return True
        self._focus_daily_row(
            str(getattr(segment, "group_name", "") or ""),
            str(getattr(segment, "band", "") or ""),
            str(getattr(segment, "frequency", "") or ""),
        )
        return False

    def _navigate_to_tab(self, tab_label: str) -> None:
        label_target = str(tab_label or "").strip().upper()
        if not label_target:
            return
        win = self.window()
        try:
            if hasattr(win, "_screens") and hasattr(win, "_set_screen"):
                for idx, (lbl, _w) in enumerate(win._screens):
                    if str(lbl or "").strip().upper() == label_target:
                        win._set_screen(idx)
                        return
        except Exception:
            pass

    def _handle_issue_action(self, issue: Dict[str, Any]) -> None:
        action_type = str(issue.get("action_type") or "").strip().lower()
        if action_type == "open_sop":
            self._navigate_to_tab("SOP")
            return
        if action_type == "deactivate_profile":
            profile_id = int(issue.get("profile_id") or 0)
            if profile_id > 0:
                self._on_toggle_sop_profile_active(profile_id, False)
            return
        if action_type == "adjust_daily":
            self._focus_daily_row(
                str(issue.get("group_name") or ""),
                str(issue.get("band") or ""),
                str(issue.get("frequency") or ""),
            )
            self.save_btn.setStyleSheet(button_style("eligible_warning", resolve_theme(self.settings)))
            return

    def _set_headers(self):
        mode_label = "Local" if self._show_local else "UTC"
        headers = [
            "Selected",
            f"Day ({mode_label})",
            "Source",
            "Group Name",
            "Mode",
            "Band",
            "Freq (MHz)",
            f"Start ({mode_label})",
            f"End ({mode_label})",
            "Auto-Tune",
            "Target Scope",
            "Target",
        ]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)

    def _apply_compact_schedule_view(self, show_all: bool | None = None) -> None:
        if not hasattr(self, "table"):
            return
        if show_all is None:
            show_all = bool(getattr(self, "view_edit_btn", None) and self.view_edit_btn.isChecked())
        for col in range(self.table.columnCount()):
            self.table.setColumnHidden(col, not show_all and col not in self.COMPACT_VISIBLE_COLUMNS)
        self._apply_daily_compact_table_sizing(
            compact=self._daily_responsive_mode_for_width(int(self.width() or 0)) == "compact"
        )
        if hasattr(self, "view_edit_btn"):
            self.view_edit_btn.setToolTip(
                "Hide advanced HF schedule fields for normal scanning."
                if show_all
                else "Show all editable fields for the active HF schedule rows."
            )
            try:
                self.view_edit_btn.setStyleSheet(
                    button_style("info" if show_all else "muted", resolve_theme(self.settings))
                )
            except Exception:
                pass

    def _day_offset(self, day_name: str) -> int:
        """
        Return 0-6 offset for canonical day names (Sunday=0). Defaults to 0 on unknown.
        """
        try:
            return DAY_CANON.index(day_name)
        except Exception:
            return 0

    def _anchor_utc_sunday(self) -> datetime.datetime:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        delta = (now_utc.weekday() + 1) % 7  # Sunday=0, Monday=1, ...
        sunday = now_utc - datetime.timedelta(days=delta)
        return sunday.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=datetime.timezone.utc)

    def _anchor_local_sunday(self) -> datetime.datetime:
        """
        Sunday 00:00 in the configured local timezone.
        """
        _, tz = self._current_timezone()
        now_local = datetime.datetime.now(tz)
        delta = (now_local.weekday() + 1) % 7
        sunday = now_local - datetime.timedelta(days=delta)
        return sunday.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)

    def _convert_day_time(self, day: str, hhmm: str, to_local: bool) -> Tuple[str, str]:
        """
        Convert (day, HH:MM) between UTC and local time using current timezone.
        Returns (day_name, hh:mm) in target zone. 'ALL' keeps day as ALL but still converts time.
        """
        day = (day or "ALL").strip()
        hhmm = self._normalize_hhmm(str(hhmm or ""))
        if not hhmm:
            return day, hhmm
        try:
            hour, minute = hhmm.split(":")
            hour = int(hour)
            minute = int(minute)
        except Exception:
            return day, hhmm
        # Map day to canonical offset (Sunday=0)
        day_upper = day.upper()
        day_idx = 0 if day_upper == "ALL" else self._day_offset(day)
        if to_local:
            anchor = self._anchor_utc_sunday()
            _, tz = self._current_timezone()
            dt_utc = anchor + datetime.timedelta(days=day_idx, hours=hour, minutes=minute)
            dt_loc = dt_utc.astimezone(tz)
            return ("ALL" if day_upper == "ALL" else dt_loc.strftime("%A")), dt_loc.strftime("%H:%M")
        else:
            anchor_loc = self._anchor_local_sunday()
            dt_loc = anchor_loc + datetime.timedelta(days=day_idx, hours=hour, minutes=minute)
            dt_utc = dt_loc.astimezone(datetime.timezone.utc)
            return ("ALL" if day_upper == "ALL" else dt_utc.strftime("%A")), dt_utc.strftime("%H:%M")

    # ---------------- Data load/save ---------------- #

    def _db_path(self) -> Path:
        """
        Location of the primary settings DB (freqinout.db).
        """
        from freqinout.core.config_paths import get_config_dir

        cfg_path = getattr(self.settings, "_config_path", None)
        if cfg_path:
            try:
                return Path(cfg_path)
            except Exception:
                pass
        return get_config_dir() / "config" / "freqinout.db"

    def _load_schedule_from_db(self) -> List[Dict]:
        """
        Load HF schedule rows from SQLite table daily_schedule_tab, if present.
        """
        db_path = self._db_path()
        if not db_path.exists():
            return []

        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_schedule_tab'"
            )
            if not cur.fetchone():
                return []
            table_cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(daily_schedule_tab)").fetchall()
            }
            has_target_cols = {
                "target_scope",
                "target_device_profile_id",
                "target_operating_profile_id",
            }.issubset(table_cols)

            # Try new schema; if fails, fall back to legacy (we'll map)
            try:
                cur = conn.execute(
                    (
                        """
                        SELECT
                            id,
                            day_utc,
                            band,
                            mode,
                            vfo,
                            frequency,
                            start_utc,
                            end_utc,
                            group_name,
                            auto_tune,
                            target_scope,
                            target_device_profile_id,
                            target_operating_profile_id
                        FROM daily_schedule_tab
                        """
                        if has_target_cols
                        else """
                        SELECT
                            id,
                            day_utc,
                            band,
                            mode,
                            vfo,
                            frequency,
                            start_utc,
                            end_utc,
                            group_name,
                            auto_tune
                        FROM daily_schedule_tab
                        """
                    )
                )
                rows: List[Dict] = []
                for fetched in cur.fetchall():
                    (
                        row_id,
                        day_utc,
                        band,
                        mode,
                        vfo,
                        freq,
                        start_utc,
                        end_utc,
                        group_name,
                        auto_tune,
                        *target_meta,
                    ) = fetched
                    rows.append(
                        normalize_schedule_target_fields(
                            {
                                "day_utc": (day_utc or "ALL").strip(),
                                "band": (band or "").strip(),
                                "mode": (mode or "Digi").strip(),
                                "vfo": (vfo or "A").strip().upper(),
                                "frequency": str(freq or ""),
                                "start_utc": start_utc or "",
                                "end_utc": end_utc or "",
                                "group_name": (group_name or "").strip(),
                                "auto_tune": bool(auto_tune),
                                "fldigi_offset": "",
                                "js8_offset": "",
                                "primary_js8call_group": "",
                                "comment": "",
                                "source_table": "daily_schedule_tab",
                                "source_row_id": int(row_id or 0),
                                "source_key": f"HF:{int(row_id or 0)}" if int(row_id or 0) > 0 else "",
                                "target_scope": target_meta[0] if len(target_meta) > 0 else TARGET_SCOPE_STATION,
                                "target_device_profile_id": target_meta[1] if len(target_meta) > 1 else None,
                                "target_operating_profile_id": target_meta[2] if len(target_meta) > 2 else None,
                            }
                        )
                    )
                return rows
            except Exception:
                pass

            # Legacy schema fallback
            cur = conn.execute(
                """
                SELECT
                    id,
                    day_utc,
                    band,
                    mode,
                    vfo,
                    frequency,
                    fldigi_offset,
                    js8_offset,
                    start_utc,
                    end_utc,
                    primary_js8call_group,
                    group_name,
                    comment,
                    auto_tune
                FROM daily_schedule_tab
                """
            )
            rows: List[Dict] = []
            for (
                row_id,
                day_utc,
                band,
                mode,
                vfo,
                freq,
                fldigi_offset,
                js8_offset,
                start_utc,
                end_utc,
                primary_group,
                group_name,
                comment,
                auto_tune,
            ) in cur.fetchall():
                rows.append(
                    normalize_schedule_target_fields(
                        {
                            "day_utc": (day_utc or "ALL").strip(),
                            "band": (band or "").strip(),
                            "mode": (mode or "Digi").strip(),
                            "vfo": (vfo or "A").strip().upper(),
                            "frequency": str(freq or ""),
                            "fldigi_offset": "",
                            "js8_offset": "",
                            "start_utc": start_utc or "",
                            "end_utc": end_utc or "",
                            "primary_js8call_group": "",
                            "group_name": group_name or "",
                            "comment": "",
                            "auto_tune": bool(auto_tune),
                            "source_table": "daily_schedule_tab",
                            "source_row_id": int(row_id or 0),
                            "source_key": f"HF:{int(row_id or 0)}" if int(row_id or 0) > 0 else "",
                        }
                    )
                )
            return rows
        except Exception as e:
            log.error("HF Frequency Schedule: failed to load from DB %s: %s", db_path, e)
            return []
        finally:
            conn.close()

    def _save_schedule_to_db(self, rows: List[Dict]) -> None:
        """
        Persist HF schedule rows to SQLite table daily_schedule_tab.
        """
        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DROP TABLE IF EXISTS daily_schedule_tab")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_schedule_tab (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day_utc TEXT NOT NULL,
                    band TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    vfo TEXT,
                    frequency TEXT NOT NULL,
                    start_utc TEXT NOT NULL,
                    end_utc TEXT NOT NULL,
                    group_name TEXT,
                    auto_tune INTEGER DEFAULT 0,
                    target_scope TEXT NOT NULL DEFAULT 'station',
                    target_device_profile_id INTEGER,
                    target_operating_profile_id INTEGER
                )
                """
            )
            conn.execute("DELETE FROM daily_schedule_tab")
            for row in rows:
                normalized = normalize_schedule_target_fields(row)
                conn.execute(
                    """
                    INSERT INTO daily_schedule_tab
                        (day_utc, band, mode, vfo, frequency,
                         start_utc, end_utc, group_name, auto_tune,
                         target_scope, target_device_profile_id, target_operating_profile_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized.get("day_utc"),
                        normalized.get("band"),
                        normalized.get("mode"),
                        normalized.get("vfo"),
                        normalized.get("frequency"),
                        normalized.get("start_utc"),
                        normalized.get("end_utc"),
                        normalized.get("group_name"),
                        1 if normalized.get("auto_tune") else 0,
                        normalized.get("target_scope"),
                        normalized.get("target_device_profile_id"),
                        normalized.get("target_operating_profile_id"),
                    ),
                )
            conn.commit()
            log.info("HF schedule mirrored to DB at %s (%d entries).", db_path, len(rows))
        finally:
            conn.close()

    def _persist_hf_schedule_rows(
        self,
        hf_rows: List[Dict[str, Any]],
        *,
        prompt_after_save: bool = False,
    ) -> Tuple[bool, str]:
        try:
            if hasattr(self.settings, "set"):
                self.settings.set("hf_schedule", hf_rows)
                self.settings.set("daily_schedule", hf_rows)
                if hasattr(self.settings, "save"):
                    self.settings.save()
            else:
                data = self.settings.all()
                data["hf_schedule"] = hf_rows
                data["daily_schedule"] = hf_rows
                if hasattr(self.settings, "_data"):
                    self.settings._data = data  # type: ignore[attr-defined]
                if hasattr(self.settings, "save"):
                    self.settings.save()
        except Exception as e:
            log.error("HF Frequency Schedule save failed: %s", e)
            return False, f"Could not save HF schedule:\n{e}"

        try:
            self._save_schedule_to_db(hf_rows)
        except Exception as e:
            log.error("HF Frequency Schedule DB save failed: %s", e)
            return False, f"HF schedule saved to settings, but DB save failed:\n{e}"

        self._raw_schedule = [dict(row) for row in hf_rows]
        self._saved_rows_signature = self._rows_signature(self._collect_rows_for_signature())
        self._set_dirty(False)
        self._effective_projection_cache_token = None
        self._effective_projection_cache_rows = []
        self._refresh_freq_planner()
        self._refresh_schedule_resources(force=True)
        self._refresh_schedule_issues(force=True)
        self._refresh_sop_overlay_rows_in_table()
        self._update_effective_source_label()
        try:
            win = self.window()
            if hasattr(win, "scheduler"):
                win.scheduler.force_refresh()
        except Exception:
            pass
        if prompt_after_save:
            self._prompt_active_sop_conflicts_after_schedule_change()
        return True, f"HF rows saved: {len(hf_rows)}."

    def _load_schedule(self):
        self._refresh_schedule_target_catalogs()
        hf_sched = self._load_schedule_from_db()
        loaded_from_db = bool(hf_sched)
        if loaded_from_db:
            try:
                data = self.settings.all()
                stored_rows = data.get("hf_schedule") or data.get("daily_schedule", [])
            except Exception:
                stored_rows = []
            hf_sched = reattach_schedule_frequency_keys(hf_sched, stored_rows if isinstance(stored_rows, list) else [])

        if not hf_sched:
            data = self.settings.all()
            hf_sched = data.get("hf_schedule")

            # Backwards compatibility: if hf_schedule not present, try daily_schedule
            if hf_sched is None:
                hf_sched = data.get("daily_schedule", [])

            if not isinstance(hf_sched, list):
                hf_sched = []
        hf_sched = [normalize_schedule_target_fields(entry) for entry in hf_sched if isinstance(entry, dict)]

        self._suspend_dirty_tracking = True
        try:
            self.table.setRowCount(0)
            self._raw_schedule = hf_sched

            for entry in hf_sched:
                self._append_entry_row(self._entry_for_display(entry))

            if self.table.rowCount() == 0:
                # Add a single empty row to start with
                self._add_row()
        finally:
            self._suspend_dirty_tracking = False

        src = "DB" if loaded_from_db else "settings"
        log.info("HF Frequency Schedule loaded from %s: %d rows", src, self.table.rowCount())
        self._set_headers()
        self._apply_compact_schedule_view()
        self._update_clock_labels()
        self._saved_rows_signature = self._rows_signature(self._raw_schedule)
        self._set_dirty(False)
        self._saved_rows_signature = self._rows_signature(self._collect_rows_for_signature())
        self.table.clearSelection()
        self._set_dirty(False)
        self._invalidate_active_schedule_views()
        if self.table.rowCount() > 1:
            self._sort_active_schedule_by_time(refresh_post_sort=False)
        self._highlight_time_conflicts()
        self._refresh_schedule_resources(force=True)
        self._refresh_schedule_issues(force=True)
        self._refresh_sop_overlay_rows_in_table()
        self._apply_schedule_table_height_hints()

    def _load_active_sop_overlay_rows(self) -> List[Dict[str, Any]]:
        """
        Return active HF SOP layer rows rendered as read-only overlays in Active Schedule.
        """
        profiles = [p for p in self._load_sop_profile_catalog() if self._is_hf_sop_profile(p) and bool(p.get("active"))]
        if not profiles:
            return []
        rows: List[Dict[str, Any]] = []
        try:
            sop_rows = self._load_sop_schedule_resource_rows(profiles=profiles, apply_condition_filter=True)
            active_keys = {str(r.get("source_key") or "").strip() for r in sop_rows if str(r.get("source_key") or "").strip()}
            if active_keys:
                self._hidden_sop_overlay_keys.intersection_update(active_keys)
            for row in sop_rows:
                source_key = str(row.get("source_key") or "").strip()
                if source_key and source_key in self._hidden_sop_overlay_keys:
                    continue
                layer_id = 0
                if source_key.startswith("sop:"):
                    parts = source_key.split(":")
                    if len(parts) >= 3 and str(parts[2]).isdigit():
                        layer_id = int(parts[2])
                entry = {
                    "day_utc": self._normalize_day(str(row.get("day_utc") or "ALL")),
                    "group_name": str(row.get("group_name") or "").strip(),
                    "mode": str(row.get("mode") or "").strip().upper(),
                    "band": str(row.get("band") or "").strip().upper(),
                    "frequency": self._normalize_freq_text(str(row.get("frequency") or "")),
                    "start_utc": self._normalize_hhmm(str(row.get("start_utc") or "")),
                    "end_utc": self._normalize_hhmm(str(row.get("end_utc") or "")),
                    "auto_tune": False,
                    "_sop_overlay": True,
                    "_sop_profile_name": str(row.get("sop_profile_name") or "").strip(),
                    "_source_key": source_key,
                    "_sop_profile_id": int(row.get("sop_profile_id") or 0),
                    "_sop_layer_id": layer_id,
                    "_sop_recurrence": str(row.get("recurrence") or "Weekly"),
                    "_sop_biweekly_offset_weeks": int(row.get("biweekly_offset_weeks") or 0),
                    "_sop_month_weeks": str(row.get("month_weeks") or "").strip(),
                    "_sop_vfo": str(row.get("vfo") or "A").strip().upper() or "A",
                }
                if not (
                    entry["group_name"]
                    and entry["mode"]
                    and entry["band"]
                    and entry["frequency"]
                    and entry["start_utc"]
                    and entry["end_utc"]
                ):
                    continue
                rows.append(entry)
        except Exception as e:
            log.debug("HF Schedule: failed loading active SOP overlay rows: %s", e)
            rows = []
        rows.sort(
            key=lambda r: (
                str(r.get("_sop_profile_name") or "").upper(),
                str(r.get("group_name") or "").upper(),
                self._normalize_day(str(r.get("day_utc") or "ALL")),
                self._normalize_hhmm(str(r.get("start_utc") or "")),
                self._normalize_hhmm(str(r.get("end_utc") or "")),
            )
        )
        return rows

    def _on_toggle_sop_overlay_visibility(self, checked: bool) -> None:
        self._refresh_sop_overlay_rows_in_table()

    def _invalidate_active_schedule_views(self, *, invalidate_resource_table: bool = True) -> None:
        self._active_conflict_pairs_cache = None
        self._active_conflict_rows_cache = None
        if invalidate_resource_table:
            self._schedule_resource_view_token = None

    def _cache_active_conflict_state(
        self,
        conflict_pairs: List[Tuple[int, int, str]],
        conflict_rows: Set[int],
    ) -> None:
        self._active_conflict_pairs_cache = [tuple(row) for row in (conflict_pairs or [])]
        self._active_conflict_rows_cache = {int(r) for r in (conflict_rows or set())}

    def _get_active_conflict_state(self, *, force_refresh: bool = False) -> Tuple[List[Tuple[int, int, str]], Set[int]]:
        if (
            not force_refresh
            and self._active_conflict_pairs_cache is not None
            and self._active_conflict_rows_cache is not None
        ):
            return [tuple(row) for row in self._active_conflict_pairs_cache], set(self._active_conflict_rows_cache)
        conflict_pairs, conflict_rows = self._compute_active_conflict_state()
        self._cache_active_conflict_state(conflict_pairs, conflict_rows)
        return [tuple(row) for row in conflict_pairs], set(conflict_rows)

    def _update_sop_overlay_control_state(self, has_active_hf_sop: bool) -> None:
        self._has_active_hf_sop_profiles = bool(has_active_hf_sop)
        chk = getattr(self, "show_sop_overlay_chk", None)
        if not isinstance(chk, QCheckBox):
            return
        chk.setEnabled(self._has_active_hf_sop_profiles)
        if self._has_active_hf_sop_profiles:
            chk.setToolTip(
                "Show a read-only HF/SOP runtime projection for the next 7 days. This does not change scheduler behavior."
            )
            return
        chk.setToolTip("Available only while an HF SOP profile is active.")
        if chk.isChecked():
            chk.setChecked(False)
        else:
            self._refresh_sop_overlay_rows_in_table()

    def _resolve_projection_operating_group(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        group_name = str(entry.get("group_name") or entry.get("group") or "").strip().upper()
        band = str(entry.get("band") or "").strip().upper()
        mode = str(entry.get("mode") or "").strip()
        freq = self._normalize_freq_text(str(entry.get("frequency") or ""))
        if not group_name:
            return None

        candidates: List[Tuple[str, str, str, Dict[str, Any]]] = []
        for group in self.operating_groups:
            g_name = str(group.get("group") or "").strip().upper()
            if g_name != group_name:
                continue
            g_band = str(group.get("band") or "").strip().upper()
            g_mode = str(group.get("mode") or "").strip()
            g_freq = self._normalize_freq_text(str(group.get("frequency") or ""))
            candidates.append((g_band, g_mode, g_freq, group))
        if not candidates:
            return None

        for g_band, g_mode, _g_freq, group in candidates:
            if g_band == band and g_mode == mode and g_mode:
                return group
        for g_band, _g_mode, g_freq, group in candidates:
            if g_band == band and g_freq == freq and g_freq:
                return group
        for g_band, _g_mode, _g_freq, group in candidates:
            if g_band == band:
                return group
        for _g_band, g_mode, _g_freq, group in candidates:
            if g_mode == mode and g_mode:
                return group
        for _g_band, _g_mode, g_freq, group in candidates:
            if g_freq == freq and g_freq:
                return group
        return candidates[0][3]

    def _apply_projection_runtime_overrides(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        effective = dict(entry or {})
        group = self._resolve_projection_operating_group(effective)
        if not isinstance(group, dict):
            return effective

        band = str(group.get("band") or "").strip().upper()
        mode = str(group.get("mode") or "").strip()
        freq = self._normalize_freq_text(str(group.get("frequency") or ""))
        if band:
            effective["band"] = band
        if mode:
            effective["mode"] = mode
        if freq:
            effective["frequency"] = freq
        if "auto_tune" in group:
            effective["auto_tune"] = bool(group.get("auto_tune"))
        return effective

    @staticmethod
    def _projection_windows_overlap(
        window: Dict[str, Any],
        start_utc: datetime.datetime,
        end_utc: datetime.datetime,
    ) -> bool:
        begin = window.get("start_dt_utc")
        finish = window.get("end_dt_utc")
        if not isinstance(begin, datetime.datetime) or not isinstance(finish, datetime.datetime):
            return False
        return begin < end_utc and start_utc < finish

    @staticmethod
    def _projection_sop_rank(window: Dict[str, Any]) -> Tuple[Any, ...]:
        try:
            priority = int(window.get("sop_priority") or 100)
        except Exception:
            priority = 100
        try:
            updated_text = str(window.get("sop_profile_updated_utc") or "").strip().replace("Z", "+00:00")
            if updated_text:
                updated_dt = datetime.datetime.fromisoformat(updated_text)
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=datetime.timezone.utc)
                updated_epoch = float(updated_dt.astimezone(datetime.timezone.utc).timestamp())
            else:
                updated_epoch = 0.0
        except Exception:
            updated_epoch = 0.0
        start_dt = window.get("start_dt_utc")
        start_epoch = (
            float(start_dt.astimezone(datetime.timezone.utc).timestamp())
            if isinstance(start_dt, datetime.datetime)
            else 0.0
        )
        try:
            profile_id = int(window.get("sop_profile_id") or 0)
        except Exception:
            profile_id = 0
        try:
            sort_order = int(window.get("sort_order") or 0)
        except Exception:
            sort_order = 0
        try:
            layer_id = int(window.get("id") or window.get("sop_layer_id") or 0)
        except Exception:
            layer_id = 0
        return (priority, -updated_epoch, -start_epoch, profile_id, sort_order, layer_id)

    def _effective_projection_token(self, profiles: List[Dict[str, Any]]) -> Tuple[Any, ...]:
        profile_sig = tuple(
            sorted(
                (
                    int(p.get("id") or 0),
                    bool(p.get("active")),
                    int(p.get("priority") or 100),
                    str(p.get("updated_utc") or ""),
                )
                for p in profiles
                if int(p.get("id") or 0) > 0
            )
        )
        return (
            self._safe_mtime(self._db_path()),
            self._rows_signature(self._collect_rows_for_signature()),
            self._operating_groups_sig,
            profile_sig,
        )

    def _build_effective_schedule_projection_rows(self) -> List[Dict[str, Any]]:
        profiles = [p for p in self._load_sop_profile_catalog() if self._is_hf_sop_profile(p) and bool(p.get("active"))]
        token = self._effective_projection_token(profiles)
        if self._effective_projection_cache_token == token:
            return [dict(row) for row in self._effective_projection_cache_rows]

        window_start = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + datetime.timedelta(days=7)
        profile_lookup = {int(p.get("id") or 0): dict(p) for p in profiles if int(p.get("id") or 0) > 0}
        hf_rows = self._collect_current_hf_rows_utc()
        sop_rows = self._load_sop_schedule_resource_rows(profiles=profiles, apply_condition_filter=True)

        for idx, row in enumerate(sop_rows):
            profile = profile_lookup.get(int(row.get("sop_profile_id") or 0), {})
            try:
                row["sop_priority"] = int(profile.get("priority") or 100)
            except Exception:
                row["sop_priority"] = 100
            row["sop_profile_updated_utc"] = str(profile.get("updated_utc") or "").strip()
            row["sort_order"] = int(row.get("sort_order") or idx)

        try:
            hf_windows = self._sop_manager._expand_schedule_rows_windows(
                hf_rows,
                window_start_utc=window_start,
                window_end_utc=window_end,
            )
            sop_windows = self._sop_manager._expand_schedule_rows_windows(
                sop_rows,
                window_start_utc=window_start,
                window_end_utc=window_end,
            )
        except Exception as e:
            log.debug("HF Schedule: failed building effective schedule projection: %s", e)
            self._effective_projection_cache_token = token
            self._effective_projection_cache_rows = []
            return []

        projection: List[Dict[str, Any]] = []
        horizon_days = max(0, int((window_end - window_start).days))
        for day_offset in range(horizon_days):
            day_start = window_start + datetime.timedelta(days=day_offset)
            day_end = day_start + datetime.timedelta(days=1)
            boundaries: Set[datetime.datetime] = {day_start, day_end}
            day_hf_windows = [w for w in hf_windows if self._projection_windows_overlap(w, day_start, day_end)]
            day_sop_windows = [w for w in sop_windows if self._projection_windows_overlap(w, day_start, day_end)]
            if not day_hf_windows and not day_sop_windows:
                continue

            for window in day_hf_windows + day_sop_windows:
                begin = window.get("start_dt_utc")
                finish = window.get("end_dt_utc")
                if not isinstance(begin, datetime.datetime) or not isinstance(finish, datetime.datetime):
                    continue
                boundaries.add(max(begin, day_start))
                boundaries.add(min(finish, day_end))

            ordered = sorted(boundaries)
            for idx in range(len(ordered) - 1):
                seg_start = ordered[idx]
                seg_end = ordered[idx + 1]
                if seg_end <= seg_start:
                    continue
                probe_utc = seg_start + ((seg_end - seg_start) / 2)
                active_hf = [
                    w
                    for w in day_hf_windows
                    if self._projection_windows_overlap(w, seg_start, seg_end)
                    and isinstance(w.get("start_dt_utc"), datetime.datetime)
                    and isinstance(w.get("end_dt_utc"), datetime.datetime)
                    and w["start_dt_utc"] <= probe_utc < w["end_dt_utc"]
                ]
                active_sop = [
                    w
                    for w in day_sop_windows
                    if self._projection_windows_overlap(w, seg_start, seg_end)
                    and isinstance(w.get("start_dt_utc"), datetime.datetime)
                    and isinstance(w.get("end_dt_utc"), datetime.datetime)
                    and w["start_dt_utc"] <= probe_utc < w["end_dt_utc"]
                ]
                chosen_hf = None
                if active_hf:
                    chosen_hf = max(active_hf, key=lambda w: w["start_dt_utc"])
                chosen_sop = None
                if active_sop:
                    chosen_sop = min(active_sop, key=self._projection_sop_rank)

                source = ""
                chosen = None
                detail = ""
                if isinstance(chosen_sop, dict):
                    source = "SOP"
                    chosen = chosen_sop
                    detail = str(chosen_sop.get("sop_profile_name") or "").strip() or "Active HF SOP"
                elif isinstance(chosen_hf, dict):
                    source = "HF"
                    chosen = chosen_hf
                    prev = projection[-1] if projection else None
                    same_day_resume = bool(
                        isinstance(prev, dict)
                        and str(prev.get("day_utc") or "") == day_start.strftime("%A")
                        and str(prev.get("source") or "") == "SOP"
                    )
                    detail = "HF resume" if same_day_resume else "Baseline HF"
                if not isinstance(chosen, dict):
                    continue

                effective = self._apply_projection_runtime_overrides(chosen)
                entry = {
                    "source": source,
                    "day_utc": day_start.strftime("%A"),
                    "group_name": str(effective.get("group_name") or chosen.get("group_name") or "").strip().upper(),
                    "band": str(effective.get("band") or chosen.get("band") or "").strip().upper(),
                    "frequency": self._normalize_freq_text(str(effective.get("frequency") or chosen.get("frequency") or "")),
                    "start_utc": seg_start.astimezone(datetime.timezone.utc).strftime("%H:%M"),
                    "end_utc": seg_end.astimezone(datetime.timezone.utc).strftime("%H:%M"),
                    "detail": detail,
                }
                if not (entry["group_name"] and entry["band"] and entry["frequency"]):
                    continue

                if projection:
                    prev = projection[-1]
                    if (
                        str(prev.get("day_utc") or "") == str(entry.get("day_utc") or "")
                        and str(prev.get("source") or "") == str(entry.get("source") or "")
                        and str(prev.get("group_name") or "") == str(entry.get("group_name") or "")
                        and str(prev.get("band") or "") == str(entry.get("band") or "")
                        and str(prev.get("frequency") or "") == str(entry.get("frequency") or "")
                        and str(prev.get("end_utc") or "") == str(entry.get("start_utc") or "")
                        and (
                            str(prev.get("source") or "") != "SOP"
                            or str(prev.get("detail") or "") == str(entry.get("detail") or "")
                        )
                    ):
                        prev["end_utc"] = str(entry.get("end_utc") or "")
                        continue
                projection.append(entry)

        self._effective_projection_cache_token = token
        self._effective_projection_cache_rows = [dict(row) for row in projection]
        return [dict(row) for row in projection]

    def _populate_sop_overlay_panel(self) -> None:
        has_active_hf_sop = bool(getattr(self, "_has_active_hf_sop_profiles", False))
        if (
            not has_active_hf_sop
            or not getattr(self, "show_sop_overlay_chk", None)
            or not self.show_sop_overlay_chk.isChecked()
        ):
            self.sop_overlay_box.setVisible(False)
            if hasattr(self, "sop_overlay_table"):
                self.sop_overlay_table.setRowCount(0)
            if hasattr(self, "sop_overlay_summary_label"):
                self.sop_overlay_summary_label.setText(
                    "Projection unavailable until an HF SOP is active."
                    if not has_active_hf_sop
                    else "Projection hidden."
                )
            return

        self.sop_overlay_box.setVisible(True)
        rows = self._build_effective_schedule_projection_rows()
        if not rows:
            self.sop_overlay_summary_label.setText("No projected HF/SOP runtime segments for the next 7 days.")
            self.sop_overlay_table.setRowCount(0)
            return

        plural = "s" if len(rows) != 1 else ""
        self.sop_overlay_summary_label.setText(
            f"Showing {len(rows)} projected HF/SOP runtime segment{plural} for the next 7 days. Read-only."
        )
        sort_header = self.sop_overlay_table.horizontalHeader()
        sort_section = int(sort_header.sortIndicatorSection())
        if not (0 <= sort_section < self.sop_overlay_table.columnCount()):
            sort_section = 1
        sort_order = sort_header.sortIndicatorOrder()
        self.sop_overlay_table.setSortingEnabled(False)
        self.sop_overlay_table.setRowCount(0)
        for row in rows:
            r = self.sop_overlay_table.rowCount()
            self.sop_overlay_table.insertRow(r)
            day_txt, start_txt, end_txt = self._display_resource_day_time(
                str(row.get("day_utc") or "ALL"),
                str(row.get("start_utc") or ""),
                str(row.get("end_utc") or ""),
            )
            values = [
                str(row.get("source") or ""),
                day_txt,
                str(row.get("group_name") or ""),
                str(row.get("band") or ""),
                str(row.get("frequency") or ""),
                start_txt,
                end_txt,
                str(row.get("detail") or ""),
            ]
            day_sort = -1 if day_txt == "ALL" else int(DAY_INDEX.get(day_txt, 99))
            start_sort = self._time_to_minutes(start_txt)
            end_sort = self._time_to_minutes(end_txt)
            freq_sort = self._parse_frequency_sort_key(str(row.get("frequency") or ""))
            source_txt = str(row.get("source") or "")
            group_txt = str(row.get("group_name") or "")
            sort_keys = [
                (str(source_txt).upper(), day_sort, start_sort if start_sort is not None else -1, group_txt.upper(), freq_sort),
                (day_sort, start_sort if start_sort is not None else -1, end_sort if end_sort is not None else -1, str(source_txt).upper(), group_txt.upper(), freq_sort),
                (group_txt.upper(), day_sort, start_sort if start_sort is not None else -1, freq_sort),
                (str(row.get("band") or "").upper(), freq_sort, day_sort, start_sort if start_sort is not None else -1),
                (freq_sort, str(row.get("frequency") or ""), day_sort, start_sort if start_sort is not None else -1),
                (start_sort if start_sort is not None else -1, day_sort, end_sort if end_sort is not None else -1, str(source_txt).upper(), group_txt.upper()),
                (end_sort if end_sort is not None else -1, day_sort, start_sort if start_sort is not None else -1, str(source_txt).upper(), group_txt.upper()),
                (str(row.get("detail") or "").upper(), day_sort, start_sort if start_sort is not None else -1, group_txt.upper()),
            ]
            for col, val in enumerate(values):
                item = _SortKeyTableWidgetItem(val, sort_key=sort_keys[col])
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.sop_overlay_table.setItem(r, col, item)
        self.sop_overlay_table.setSortingEnabled(True)
        self.sop_overlay_table.sortItems(sort_section, sort_order)

    def _append_sop_overlay_rows(self) -> None:
        # Retained as a no-op. Effective Schedule is now shown in a read-only panel
        # instead of adding synthetic rows into the editable HF table.
        return

    def _refresh_sop_overlay_rows_in_table(self) -> None:
        self._populate_sop_overlay_panel()

    def _is_sop_overlay_row(self, row_index: int) -> bool:
        wrap = self.table.cellWidget(row_index, self.COL_SELECT)
        if not isinstance(wrap, QWidget):
            return False
        return bool(wrap.property("sop_overlay"))

    def _sop_overlay_source_key(self, row_index: int) -> str:
        wrap = self.table.cellWidget(row_index, self.COL_SELECT)
        if not isinstance(wrap, QWidget):
            return ""
        return str(wrap.property("sop_overlay_source_key") or "").strip()

    def _schedule_day_names(self, day_utc: str) -> List[str]:
        day = self._normalize_day(day_utc)
        if day == "ALL":
            return list(DAY_CANON)
        return [day]

    @staticmethod
    def _time_to_minutes(hhmm: str) -> Optional[int]:
        try:
            hh, mm = str(hhmm or "").split(":")
            h = int(hh)
            m = int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h * 60 + m
        except Exception:
            return None
        return None

    @staticmethod
    def _parse_frequency_sort_key(value: str) -> float:
        try:
            return float(str(value or "").strip())
        except Exception:
            return -1.0

    @staticmethod
    def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
        return max(a_start, b_start) < min(a_end, b_end)

    def _highlight_time_conflicts(
        self,
        *,
        conflict_rows_override: Optional[Set[int]] = None,
    ) -> None:
        with perf_span(
            "daily_schedule.highlight_time_conflicts",
            settings=self.settings,
            meta={"rows": int(self.table.rowCount()), "reuse": bool(conflict_rows_override is not None)},
            min_ms=1.0,
        ):
            prev_block = self.table.blockSignals(True)
            try:
                if conflict_rows_override is None:
                    _pairs, conflict_rows = self._get_active_conflict_state()
                else:
                    conflict_rows = set(conflict_rows_override)

                theme = resolve_theme(self.settings)
                warn = QColor(str(theme.get("warning", "#C99700")))
                warn.setAlpha(64)
                for r in range(self.table.rowCount()):
                    for col in (self.COL_START, self.COL_END):
                        item = self.table.item(r, col)
                        if item is None:
                            continue
                        base_tip = str(item.data(Qt.UserRole) or "")
                        if r in conflict_rows:
                            item.setBackground(warn)
                            item.setToolTip((base_tip + "\n" if base_tip else "") + "Time conflict with another active row.")
                        else:
                            item.setData(Qt.BackgroundRole, None)
                            item.setToolTip(base_tip)
            finally:
                self.table.blockSignals(prev_block)

    def _save_schedule(self):
        # Ensure in-progress cell edits are committed
        self._commit_active_table_editor()

        hf_rows: List[Dict[str, Any]] = []
        sop_updates: Dict[int, List[Dict[str, Any]]] = {}
        format_errors: List[str] = []
        save_warnings: List[str] = []

        for r in range(self.table.rowCount()):
            is_sop = self._is_sop_overlay_row(r)
            if is_sop:
                # Inline SOP rows are display-only and never persisted from Daily.
                continue
            day = self._get_combo_value(r, self.COL_DAY, default="ALL")
            group_name = self._get_combo_value(r, self.COL_GROUP, default="")
            mode = self._get_combo_value(r, self.COL_MODE, default="Digi")
            band = self._get_combo_value(r, self.COL_BAND, default="")
            freq_text = self._frequency_value(r)
            start_val = self._get_text_value(r, self.COL_START)
            end_val = self._get_text_value(r, self.COL_END)
            auto_tune = self._get_checkbox_value(r, self.COL_AUTOTUNE)
            target_scope, target_device_profile_id, target_operating_profile_id = self._selected_schedule_target(r)

            if group_name and band and mode and not freq_text:
                resolution = resolve_operating_group_frequency(
                    self.operating_groups, group=group_name, band=band, mode=mode
                )
                if resolution.state == "ambiguous":
                    format_errors.append(f"Row {r+1}: Select a frequency for {group_name} {band}.")
                    continue
            if not group_name or not band or not freq_text or not start_val or not end_val:
                continue
            if target_scope == TARGET_SCOPE_DEVICE_PROFILE and target_device_profile_id is None:
                format_errors.append(f"Row {r+1}: Device-targeted rows require a device profile.")
                continue
            if target_scope == TARGET_SCOPE_OPERATING_PROFILE and target_operating_profile_id is None:
                format_errors.append(f"Row {r+1}: Frequency Plan-targeted rows require a Frequency Plan.")
                continue

            # Enforce frequency validity for band/mode
            if not self._validate_frequency(band, mode, freq_text):
                return  # validation already warned the user
            freq_text = str(freq_text).strip()

            # Validate times
            start_val = self._normalize_hhmm(start_val)
            end_val = self._normalize_hhmm(end_val)
            if not self._validate_time(start_val) or not self._validate_time(end_val):
                format_errors.append(f"Row {r+1}: Start/End must be HH:MM (24h)")
                continue
            self._set_text_value(r, self.COL_START, start_val)
            self._set_text_value(r, self.COL_END, end_val)

            if self._show_local:
                day_utc, start_utc = self._convert_day_time(day, start_val, to_local=False)
                _, end_utc = self._convert_day_time(day, end_val, to_local=False)
            else:
                day_utc = day
                start_utc = start_val
                end_utc = end_val

            hf_rows.append(
                normalize_schedule_target_fields(
                    {
                        "day_utc": day_utc,
                        "band": band,
                        "mode": mode,
                        "vfo": "A",
                        "frequency": freq_text,
                        "operating_group_frequency_key": self._frequency_key(r),
                        "start_utc": start_utc,
                        "end_utc": end_utc,
                        "group_name": group_name,
                        "fldigi_offset": "",
                        "js8_offset": "",
                        "primary_js8call_group": "",
                        "comment": "",
                        "auto_tune": bool(auto_tune),
                        "target_scope": target_scope,
                        "target_device_profile_id": target_device_profile_id,
                        "target_operating_profile_id": target_operating_profile_id,
                    }
                )
            )

        if format_errors:
            QMessageBox.warning(
                self,
                "Save Blocked",
                "Fix formatting issues before saving:\n" + "\n".join(format_errors),
            )
            return

        if save_warnings:
            QMessageBox.warning(
                self,
                "Partial Save",
                "Some rows were skipped:\n" + "\n".join(save_warnings),
            )

        saved, save_detail = self._persist_hf_schedule_rows(hf_rows, prompt_after_save=False)
        if not saved:
            QMessageBox.critical(self, "Save Failed", save_detail)
            return

        sop_changed = 0
        sop_failures: List[str] = []
        for profile_id, layer_rows in sop_updates.items():
            try:
                sop_changed += int(self._sop_manager.upsert_schedule_layer_rows(profile_id, layer_rows) or 0)
            except Exception as e:
                sop_failures.append(f"Profile {profile_id}: {e}")

        if sop_updates:
            self._dispatch_sop_schedule_change()

        if sop_failures:
            QMessageBox.warning(
                self,
                "SOP Save Warning",
                "Some SOP rows could not be updated:\n" + "\n".join(sop_failures[:12]),
            )

        self._load_schedule()
        log.info("HF Frequency Schedule saved: %d HF rows, %d SOP rows updated", len(hf_rows), sop_changed)
        QMessageBox.information(
            self,
            "Saved",
            f"HF Schedule saved. HF rows: {len(hf_rows)} | SOP rows updated: {sop_changed}.",
        )
        self._prompt_active_sop_conflicts_after_schedule_change()

    def _prompt_active_sop_conflicts_after_schedule_change(self) -> None:
        try:
            conflicts = self._sop_manager.collect_active_hf_conflicts(force_refresh=True)
        except Exception as e:
            log.debug("HF Schedule: active SOP conflict scan failed: %s", e)
            return
        if not conflicts:
            return
        lines: List[str] = []
        for row in conflicts[:8]:
            profile_name = str(row.get("profile_name") or "").strip() or "HF SOP"
            action_label = str(row.get("action_label") or "").strip() or "Action"
            band = str(row.get("band") or "").strip().upper()
            freq = str(row.get("frequency") or "").strip()
            daily_summary = str(row.get("daily_summary") or "").strip()
            net_summary = str(row.get("net_summary") or "").strip()
            sop_summary = str(row.get("sop_summary") or "").strip()
            detail = ", ".join([txt for txt in [daily_summary, net_summary, sop_summary] if txt])
            lines.append(f"{profile_name}: {action_label} {band} {freq}".strip())
            if detail:
                lines.append(f"  - {detail}")
        if len(conflicts) > 8:
            lines.append(f"...and {len(conflicts) - 8} more conflict item(s).")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("SOP Conflict After HF Schedule Change")
        box.setText(
            "Active HF SOP actions now conflict with HF Daily/Net windows.\n"
            "Resolve in SOP Builder, or deactivate active HF SOP profiles."
        )
        box.setInformativeText("\n".join(lines))
        keep_btn = box.addButton("Keep Active (SOP Priority)", QMessageBox.AcceptRole)
        deactivate_btn = box.addButton("Deactivate Active HF SOPs", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is not deactivate_btn:
            return
        self.deactivate_hf_sops_with_return_to_normal(origin_label="Daily Schedule")

    def _export_schedule(self):
        """
        Export HF schedule (no nets) to JSON with callsign in filename.
        """
        data = self.settings.all()
        callsign = (data.get("operator_callsign") or "").strip().upper() or "UNKNOWN"
        default_name = f"{callsign}-hf-schedule-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')}.json"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export HF Schedule",
            default_name,
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        rows = self._raw_schedule if hasattr(self, "_raw_schedule") and self._raw_schedule else data.get("hf_schedule", [])
        if not rows:
            QMessageBox.warning(self, "Export", "No HF schedule rows to export.")
            return
        try:
            payload = {
                "callsign": callsign,
                "created_utc": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
                "rows": [],
            }
            for r in rows:
                payload["rows"].append(
                    {
                        "source": "HF",
                        "group_name": r.get("group_name", ""),
                        "day_utc": r.get("day_utc", "ALL"),
                        "start_utc": r.get("start_utc", ""),
                        "end_utc": r.get("end_utc", ""),
                        "band": r.get("band", ""),
                        "mode": r.get("mode", ""),
                        "frequency": r.get("frequency", ""),
                        "operating_group_frequency_key": r.get("operating_group_frequency_key", ""),
                        "target_scope": r.get("target_scope", TARGET_SCOPE_STATION),
                        "target_device_profile_id": r.get("target_device_profile_id"),
                        "target_operating_profile_id": r.get("target_operating_profile_id"),
                    }
                )
            Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            QMessageBox.information(self, "Exported", f"HF schedule exported to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"Could not export:\n{e}")
            log.error("HF schedule export failed: %s", e)

    def _infer_group_for_import(self, row: Dict[str, Any]) -> str:
        band = str(row.get("band") or "").strip().upper()
        mode = str(row.get("mode") or "").strip().upper()
        freq = self._normalize_freq_text(str(row.get("frequency") or ""))
        if not (band and mode and freq):
            return ""
        matches: List[str] = []
        for og in self.operating_groups:
            group_name = str(og.get("group") or "").strip()
            og_band = str(og.get("band") or "").strip().upper()
            og_mode = str(og.get("mode") or "").strip().upper()
            og_freq = self._normalize_freq_text(str(og.get("frequency") or ""))
            if og_band != band or og_mode != mode or og_freq != freq:
                continue
            if group_name:
                matches.append(group_name)
        if len(matches) == 1:
            return matches[0]
        return ""

    def _import_schedule(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import HF Schedule",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.warning(self, "Import HF Schedule", f"Could not parse JSON:\n{e}")
            return

        rows_raw: Any
        if isinstance(payload, dict):
            rows_raw = payload.get("rows", [])
        elif isinstance(payload, list):
            rows_raw = payload
        else:
            rows_raw = []
        if not isinstance(rows_raw, list):
            QMessageBox.warning(self, "Import HF Schedule", "Import file has no valid 'rows' array.")
            return

        imported_rows: List[Dict[str, Any]] = []
        skipped_rows = 0
        for idx, raw in enumerate(rows_raw):
            if not isinstance(raw, dict):
                skipped_rows += 1
                continue
            source = str(raw.get("source") or "HF").strip().upper()
            if source not in {"", "HF"}:
                skipped_rows += 1
                continue
            day = self._normalize_day(str(raw.get("day_utc") or "ALL"))
            group = str(raw.get("group_name") or "").strip()
            mode = str(raw.get("mode") or "").strip().upper()
            band = str(raw.get("band") or "").strip().upper()
            freq = str(raw.get("frequency") or "").strip()
            start = self._normalize_hhmm(str(raw.get("start_utc") or ""))
            end = self._normalize_hhmm(str(raw.get("end_utc") or ""))
            if not group:
                group = self._infer_group_for_import(raw)
            if not (group and mode and band and freq and start and end):
                skipped_rows += 1
                continue
            if not self._validate_time(start) or not self._validate_time(end):
                skipped_rows += 1
                continue
            target_scope, target_device_profile_id, target_operating_profile_id = normalize_schedule_target(
                raw.get("target_scope"),
                target_device_profile_id=raw.get("target_device_profile_id"),
                target_operating_profile_id=raw.get("target_operating_profile_id"),
            )
            imported_rows.append(
                normalize_schedule_target_fields(
                    {
                        "day_utc": day,
                        "group_name": group,
                        "mode": mode,
                        "band": band,
                        "frequency": freq,
                        "operating_group_frequency_key": str(raw.get("operating_group_frequency_key") or "").strip(),
                        "start_utc": start,
                        "end_utc": end,
                        "auto_tune": False,
                        "target_scope": target_scope,
                        "target_device_profile_id": target_device_profile_id,
                        "target_operating_profile_id": target_operating_profile_id,
                    }
                )
            )

        if not imported_rows:
            QMessageBox.warning(self, "Import HF Schedule", "No valid HF rows found to import.")
            return

        choice = QMessageBox.question(
            self,
            "Import HF Schedule",
            (
                f"Imported {len(imported_rows)} row(s)"
                + (f"; skipped {skipped_rows} row(s)." if skipped_rows else ".")
                + "\n\nReplace current HF rows?"
            ),
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if choice == QMessageBox.Cancel:
            return

        prev_suppress = self._suppress_autostart
        self._suppress_autostart = True
        try:
            if choice == QMessageBox.Yes:
                self._raw_schedule = [dict(r) for r in imported_rows]
                self._rebuild_from_raw()
            else:
                current_hf_rows: List[Dict[str, Any]] = []
                for row in self._collect_rows_for_signature():
                    if str(row.get("source") or "").strip().upper() != "HF":
                        continue
                    current_hf_rows.append(
                        normalize_schedule_target_fields(
                            {
                                "day_utc": str(row.get("day_utc") or "ALL"),
                                "group_name": str(row.get("group_name") or ""),
                                "mode": str(row.get("mode") or ""),
                                "band": str(row.get("band") or ""),
                                "frequency": str(row.get("frequency") or ""),
                                "start_utc": str(row.get("start_utc") or ""),
                                "end_utc": str(row.get("end_utc") or ""),
                                "auto_tune": bool(row.get("auto_tune", False)),
                                "target_scope": row.get("target_scope"),
                                "target_device_profile_id": row.get("target_device_profile_id"),
                                "target_operating_profile_id": row.get("target_operating_profile_id"),
                            }
                        )
                    )
                current_hf_rows.extend(dict(r) for r in imported_rows)
                self._raw_schedule = current_hf_rows
                self._rebuild_from_raw()
        finally:
            self._suppress_autostart = prev_suppress
        self._mark_dirty()
        QMessageBox.information(
            self,
            "Import HF Schedule",
            (
                f"Imported {len(imported_rows)} HF row(s)."
                + (f"\nSkipped {skipped_rows} row(s)." if skipped_rows else "")
            ),
        )

    # ---------------- Row helpers ---------------- #

    def _entry_for_display(self, entry: Dict) -> Dict:
        d = dict(entry)
        if self._show_local:
            day_loc, start_loc = self._convert_day_time(d.get("day_utc", ""), d.get("start_utc", ""), to_local=True)
            _, end_loc = self._convert_day_time(d.get("day_utc", ""), d.get("end_utc", ""), to_local=True)
            d["day_utc"] = day_loc  # reuse column but reflects view
            d["start_utc"] = start_loc
            d["end_utc"] = end_loc
        return d

    def _rebuild_from_raw(self):
        self._suspend_dirty_tracking = True
        try:
            self.table.setRowCount(0)
            for entry in self._raw_schedule:
                self._append_entry_row(self._entry_for_display(entry))
            if self.table.rowCount() == 0:
                self._add_row()
        finally:
            self._suspend_dirty_tracking = False
        self._set_headers()
        self._apply_compact_schedule_view()
        self._update_clock_labels()
        self.table.clearSelection()
        self._invalidate_active_schedule_views()
        if self.table.rowCount() > 1:
            self._sort_active_schedule_by_time(refresh_post_sort=False)
        self._mark_dirty()
        self._highlight_time_conflicts()
        self._update_resource_action_state()
        self._refresh_sop_overlay_rows_in_table()

    def _toggle_time_view(self):
        was_dirty = bool(self._dirty)
        if was_dirty:
            rows_utc = self._collect_current_hf_rows_utc()
            if len(rows_utc) < self._editable_active_row_count():
                self._publish_time_toggle_blocked_feedback()
                return
            self._raw_schedule = rows_utc
        self._show_local = not self._show_local
        self._rebuild_from_raw()
        self._set_dirty(was_dirty)
        self._update_suspend_state()
        self._populate_schedule_resources_table()
        self._populate_schedule_issues_table()

    def _editable_active_row_count(self) -> int:
        count = 0
        for row in range(self.table.rowCount()):
            if self._is_sop_overlay_row(row):
                continue
            if self._active_row_is_empty(row):
                continue
            count += 1
        return count

    def _publish_time_toggle_blocked_feedback(self, detail: str = "") -> None:
        summary = "Finish the current HF Schedule row before changing the time view."
        win = self.window()
        service = getattr(win, "action_feedback_service", None) if win is not None else None
        if service is not None and hasattr(service, "publish"):
            try:
                service.publish(
                    scope="scheduler",
                    action_type="time_view",
                    status="blocked",
                    summary=summary,
                    detail=str(detail or "").strip(),
                    source_surface="daily_schedule_tab",
                )
                return
            except Exception as e:
                log.debug("HF Schedule: failed publishing time toggle feedback: %s", e)
        try:
            status_bar = win.statusBar() if win is not None and hasattr(win, "statusBar") else None
            if status_bar is not None:
                status_bar.showMessage(summary, 6000)
                return
        except Exception:
            pass
        log.info("HF Schedule: time view change blocked; %s", detail)

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

    def _refresh_qsy_options(self):
        """
        Build a unique frequency list from Operating Groups (auto-tune wins on duplicates).
        """
        ops = self._load_operating_groups()
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
        win = self.window()
        return perform_qsy(win, meta)

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

    def _refresh_freq_planner(self) -> None:
        """
        Ask the main window to refresh the Frequency Planner after schedule changes.
        """
        try:
            self.schedule_saved.emit()
        except Exception:
            pass

    def on_settings_saved(self):
        """
        Refresh operating groups/QSY options when settings are saved.
        """
        try:
            self.plan_context_label.invalidate_context()
            self.plan_context_label.refresh_context(refresh=True)
        except Exception:
            pass
        try:
            self.settings.reload()
        except Exception:
            pass
        self._refresh_freqplanner_source_combo()
        self._sync_selected_freqplanner_source_table()
        latest = self._load_operating_groups()
        self.operating_groups = latest
        self._operating_groups_sig = self._snapshot_operating_groups(latest)
        self._refresh_schedule_target_catalogs()
        prev_suppress = self._suppress_autostart
        self._suppress_autostart = True
        prev_dirty = self._suspend_dirty_tracking
        self._suspend_dirty_tracking = True
        try:
            self._refresh_group_band_cells()
            self._refresh_schedule_target_widgets()
        finally:
            self._suppress_autostart = prev_suppress
            self._suspend_dirty_tracking = prev_dirty
        self._refresh_sop_overlay_rows_in_table()
        self._refresh_qsy_options()
        self._apply_theme()
        self._refresh_sop_profiles_panel(force=True)
        self._refresh_schedule_resources(force=True)

    def on_sop_data_changed(self) -> None:
        if not self.isVisible():
            self._schedule_resource_token = None
            self._last_sop_panel_refresh_ts = 0.0
            return
        self._refresh_sop_overlay_rows_in_table()
        self._refresh_sop_profiles_panel(force=True)
        self._update_effective_source_label()
        self._refresh_schedule_resources(force=True)

    def on_tab_activated(self) -> None:
        with perf_span(
            "daily_schedule.on_tab_activated",
            settings=self.settings,
            meta={"rows": int(self.table.rowCount())},
            min_ms=5.0,
        ):
            now_ts = time.time()
            if (now_ts - float(self._last_tab_activation_refresh_ts or 0.0)) < float(
                self._tab_activation_refresh_interval_sec
            ):
                self._update_header_title()
                self._update_suspend_state()
                return
            self._last_tab_activation_refresh_ts = now_ts
            activation_token = self._schedule_state_token()
            if self._last_activation_schedule_token == activation_token:
                self._update_header_title()
                self._update_effective_source_label()
                self._update_suspend_state()
                self._refresh_schedule_resources(force=False)
                self._sync_selected_freqplanner_source_table()
                return
            self._last_activation_schedule_token = activation_token
            self._sync_selected_freqplanner_source_table()
            self._update_header_title()
            self._refresh_schedule_target_widgets()
            self._refresh_sop_overlay_rows_in_table()
            self._refresh_sop_profiles_panel(force=False)
            self._update_effective_source_label()
            self._update_suspend_state()
            self._refresh_schedule_resources(force=False)

    def set_tab_active(self, active: bool) -> None:
        self._active = bool(active)
        if self._active:
            if self._clock_timer and not self._clock_timer.isActive():
                self._clock_timer.start(1000)
            if self._sop_panel_timer and not self._sop_panel_timer.isActive():
                self._sop_panel_timer.start(30_000)
            QTimer.singleShot(0, self.on_tab_activated)
            return
        if self._clock_timer and self._clock_timer.isActive():
            self._clock_timer.stop()
        if self._sop_panel_timer and self._sop_panel_timer.isActive():
            self._sop_panel_timer.stop()

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

    def _apply_theme(self, *, refresh_dynamic: bool = True) -> None:
        theme = resolve_theme(self.settings)
        self.help_btn.setStyleSheet(button_style("secondary", theme))
        self._update_time_toggle_style(theme)
        self._update_effective_source_label(theme)
        self.sop_runtime_box.setStyleSheet(
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 4px; }"
        )
        self.add_row_btn.setStyleSheet(button_style("muted", theme))
        if hasattr(self, "new_source_btn"):
            self.new_source_btn.setStyleSheet(button_style("muted", theme))
        if hasattr(self, "rename_source_btn"):
            self.rename_source_btn.setStyleSheet(button_style("muted", theme))
        if hasattr(self, "save_source_btn"):
            self.save_source_btn.setStyleSheet(button_style("info", theme))
        if hasattr(self, "delete_source_btn"):
            self.delete_source_btn.setStyleSheet(button_style("danger", theme))
        if hasattr(self, "view_edit_btn"):
            self.view_edit_btn.setStyleSheet(button_style("info" if self.view_edit_btn.isChecked() else "muted", theme))
        self._refresh_save_button_state(theme)
        menu_font_css = font_css(self.add_row_btn.font())
        self.import_export_btn.setStyleSheet(button_style("muted", theme) + menu_font_css)
        self.import_export_btn.setFont(self.add_row_btn.font())
        self.add_to_schedule_btn.setStyleSheet(button_style("muted", theme) + menu_font_css)
        self.add_to_schedule_btn.setFont(self.add_row_btn.font())
        self.resources_refresh_btn.setStyleSheet(button_style("muted", theme))
        self._update_suspend_state()
        self._update_delete_button_state()
        if refresh_dynamic:
            self._refresh_sop_profiles_panel(force=True, theme=theme)
            self._populate_schedule_resources_table()

    def apply_theme(self) -> None:
        self._apply_theme()
        self._queue_daily_geometry_refresh()

    def _rows_signature(self, rows: List[Dict]) -> str:
        normalized: List[Dict[str, object]] = []
        for row in rows:
            target_scope, target_device_profile_id, target_operating_profile_id = normalize_schedule_target(
                row.get("target_scope"),
                target_device_profile_id=row.get("target_device_profile_id"),
                target_operating_profile_id=row.get("target_operating_profile_id"),
            )
            normalized.append(
                {
                    "source": str(row.get("source", "HF")),
                    "source_key": str(row.get("source_key", "")),
                    "day_utc": str(row.get("day_utc", "")),
                    "group_name": str(row.get("group_name", "")),
                    "mode": str(row.get("mode", "")),
                    "band": str(row.get("band", "")),
                    "frequency": str(row.get("frequency", "")),
                    "operating_group_frequency_key": str(row.get("operating_group_frequency_key", "")),
                    "start_utc": str(row.get("start_utc", "")),
                    "end_utc": str(row.get("end_utc", "")),
                    "auto_tune": bool(row.get("auto_tune", False)),
                    "target_scope": target_scope,
                    "target_device_profile_id": target_device_profile_id,
                    "target_operating_profile_id": target_operating_profile_id,
                }
            )
        return json.dumps(normalized, sort_keys=True)

    def _collect_rows_for_signature(self) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        for r in range(self.table.rowCount()):
            is_sop = self._is_sop_overlay_row(r)
            if is_sop:
                continue
            row = self._active_row_to_utc(r, include_sop_overlay=True)
            if row is None:
                continue
            rows.append(
                {
                    "source": "HF",
                    "source_key": "",
                    "day_utc": str(row.get("day_utc") or ""),
                    "group_name": str(row.get("group_name") or ""),
                    "mode": str(row.get("mode") or ""),
                    "band": str(row.get("band") or ""),
                    "frequency": str(row.get("frequency") or ""),
                    "start_utc": str(row.get("start_utc") or ""),
                    "end_utc": str(row.get("end_utc") or ""),
                    "auto_tune": bool(row.get("auto_tune", False)),
                    "target_scope": str(row.get("target_scope") or TARGET_SCOPE_STATION),
                    "target_device_profile_id": row.get("target_device_profile_id"),
                    "target_operating_profile_id": row.get("target_operating_profile_id"),
                }
            )
        return rows

    def _refresh_save_button_state(self, theme: Optional[Dict[str, str]] = None) -> None:
        if theme is None:
            theme = resolve_theme(self.settings)
        self.save_btn.setStyleSheet(button_style("muted", theme))
        self.save_btn.setToolTip(
            "Save or update this named HF Daily schedule, then select it in Plan Builder with Nets and SOP layers for RF Guard assignment."
        )

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = bool(dirty)
        self._refresh_save_button_state()

    def _mark_dirty(self, *_args) -> None:
        if self._suspend_dirty_tracking:
            return
        self._invalidate_active_schedule_views()
        try:
            current_sig = self._rows_signature(self._collect_rows_for_signature())
            self._set_dirty(current_sig != self._saved_rows_signature)
        except Exception:
            self._set_dirty(True)

    def _queue_table_conflict_refresh(self) -> None:
        if self._suspend_dirty_tracking:
            return
        self._pending_table_conflict_refresh = True
        if self._table_conflict_refresh_timer is not None:
            self._table_conflict_refresh_timer.start(25)
        else:
            self._flush_table_conflict_refresh()

    def _flush_table_conflict_refresh(self) -> None:
        if self._suspend_dirty_tracking or not self._pending_table_conflict_refresh:
            return
        self._pending_table_conflict_refresh = False
        conflict_pairs, conflict_rows = self._compute_active_conflict_state()
        self._cache_active_conflict_state(conflict_pairs, conflict_rows)
        self._highlight_time_conflicts(conflict_rows_override=conflict_rows)
        self._update_resource_action_state(active_conflicts_override=conflict_pairs)

    def _on_table_item_changed(self, _item: QTableWidgetItem) -> None:
        if self._suspend_dirty_tracking:
            return
        with perf_span(
            "daily_schedule.item_changed_conflicts",
            settings=self.settings,
            meta={"rows": int(self.table.rowCount())},
            min_ms=5.0,
        ):
            self._mark_dirty()
            self._queue_table_conflict_refresh()

    def _has_delete_selection(self) -> bool:
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, self.COL_SELECT)
            if isinstance(w, QCheckBox) and w.isChecked():
                return True
            if isinstance(w, QWidget):
                chk = w.findChild(QCheckBox)
                if chk is not None and chk.isChecked():
                    return True
        try:
            sel_model = self.table.selectionModel()
            if not sel_model:
                return False
            return any(not self._is_sop_overlay_row(int(idx.row())) for idx in sel_model.selectedRows())
        except Exception:
            return False

    def _update_delete_button_state(self) -> None:
        theme = resolve_theme(self.settings)
        has_selection = self._has_delete_selection()
        self.del_row_btn.setEnabled(has_selection)
        role = "eligible_danger" if has_selection else "muted"
        self.del_row_btn.setStyleSheet(button_style(role, theme))
        self.move_to_resources_btn.setEnabled(has_selection)
        self.move_to_resources_btn.setStyleSheet(
            button_style("eligible_warning" if has_selection else "muted", theme)
        )

    def _append_entry_row(self, entry: Dict):
        row = self.table.rowCount()
        self.table.insertRow(row)
        is_sop_overlay = bool(entry.get("_sop_overlay"))
        overlay_profile = str(entry.get("_sop_profile_name") or "").strip()
        overlay_tip = (
            "Active SOP overlay row. Read-only for timeline visibility; scheduler still uses the SOP layer directly."
            if is_sop_overlay
            else ""
        )

        # Select checkbox
        sel_chk = QCheckBox()
        sel_chk.stateChanged.connect(self._update_delete_button_state)
        sel_chk.setEnabled(not is_sop_overlay)
        sel_wrap = QWidget()
        sel_layout = QHBoxLayout(sel_wrap)
        sel_layout.setContentsMargins(0, 0, 0, 0)
        sel_layout.setAlignment(Qt.AlignCenter)
        sel_layout.addWidget(sel_chk)
        try:
            sel_wrap.setProperty("resource_id", int(entry.get("_resource_id") or 0))
        except Exception:
            sel_wrap.setProperty("resource_id", 0)
        try:
            sel_wrap.setProperty("source_row_id", int(entry.get("source_row_id") or entry.get("_source_row_id") or 0))
        except Exception:
            sel_wrap.setProperty("source_row_id", 0)
        sel_wrap.setProperty("source_key", str(entry.get("source_key") or entry.get("_source_key") or "").strip())
        sel_wrap.setProperty("source_table", str(entry.get("source_table") or "daily_schedule_tab"))
        sel_wrap.setProperty("resource_set", str(entry.get("_resource_set") or ""))
        sel_wrap.setProperty("operating_group_frequency_key", str(entry.get("operating_group_frequency_key") or ""))
        sel_wrap.setProperty("sop_overlay", is_sop_overlay)
        sel_wrap.setProperty("sop_profile_name", overlay_profile)
        sel_wrap.setProperty("sop_overlay_source_key", str(entry.get("_source_key") or "").strip())
        sel_wrap.setProperty("sop_overlay_group_name", str(entry.get("group_name") or "").strip())
        sel_wrap.setProperty("sop_overlay_profile_id", int(entry.get("_sop_profile_id") or 0))
        sel_wrap.setProperty("sop_overlay_layer_id", int(entry.get("_sop_layer_id") or 0))
        sel_wrap.setProperty("sop_overlay_recurrence", str(entry.get("_sop_recurrence") or "Weekly"))
        sel_wrap.setProperty("sop_overlay_biweekly", int(entry.get("_sop_biweekly_offset_weeks") or 0))
        sel_wrap.setProperty("sop_overlay_month_weeks", str(entry.get("_sop_month_weeks") or ""))
        sel_wrap.setProperty("sop_overlay_vfo", str(entry.get("_sop_vfo") or "A"))
        self.table.setCellWidget(row, self.COL_SELECT, sel_wrap)

        # Source
        source_item = QTableWidgetItem("SOP" if is_sop_overlay else "HF")
        source_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if overlay_tip:
            source_item.setToolTip(overlay_tip)
        self.table.setItem(row, self.COL_SOURCE, source_item)

        # Day
        day_combo = QComboBox()
        day_combo.addItems(DAY_OPTIONS)
        day_val = (entry.get("day_utc") or "ALL").strip()
        if day_val not in DAY_OPTIONS:
            day_val = "ALL"
        day_combo.setCurrentText(day_val)
        day_combo.setEnabled(not is_sop_overlay)
        if not is_sop_overlay:
            day_combo.currentTextChanged.connect(self._mark_dirty)
        if overlay_tip:
            day_combo.setToolTip(overlay_tip)
        self.table.setCellWidget(row, self.COL_DAY, day_combo)

        # Group (from operating groups)
        group_combo = QComboBox()
        group_val = (entry.get("group_name") or "").strip()
        if is_sop_overlay:
            group_display = f"SOP:{group_val}" if group_val else "SOP"
            group_combo.addItem(group_display)
            group_combo.setCurrentText(group_display)
            group_combo.setEnabled(False)
            if overlay_tip:
                group_combo.setToolTip(overlay_tip)
        else:
            group_names = sorted({g.get("group", "") for g in self.operating_groups if g.get("group")})
            group_combo.addItems(group_names)
            if group_val and group_val in group_names:
                group_combo.setCurrentText(group_val)
            group_combo.currentTextChanged.connect(self._mark_dirty)
        self.table.setCellWidget(row, self.COL_GROUP, group_combo)

        # Band
        band_combo = QComboBox()
        band_val = (entry.get("band") or "").strip()
        if is_sop_overlay:
            band_combo.addItem(band_val)
            band_combo.setCurrentText(band_val)
            band_combo.setEnabled(False)
            if overlay_tip:
                band_combo.setToolTip(overlay_tip)
        else:
            self._populate_band_combo(band_combo, group_combo.currentText())
            if band_val and band_combo.findText(band_val) >= 0:
                band_combo.setCurrentText(band_val)
            band_combo.currentTextChanged.connect(self._mark_dirty)
        self.table.setCellWidget(row, self.COL_BAND, band_combo)

        # Mode + Frequency
        if is_sop_overlay:
            mode_combo = QComboBox()
            mode_combo.addItem(str(entry.get("mode") or "").strip().upper())
            mode_combo.setEnabled(False)
            if overlay_tip:
                mode_combo.setToolTip(overlay_tip)
            self.table.setCellWidget(row, self.COL_MODE, mode_combo)

        freq_item = QTableWidgetItem()
        freq_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        freq_item.setText(str(entry.get("frequency") or ""))
        if overlay_tip:
            freq_item.setToolTip(overlay_tip)
        self.table.setItem(row, self.COL_FREQ, freq_item)

        # Start / End
        st_item = QTableWidgetItem(entry.get("start_utc", ""))
        if is_sop_overlay:
            st_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        else:
            self._make_editable(st_item)
        if overlay_tip:
            st_item.setToolTip(overlay_tip)
        st_item.setData(Qt.UserRole, st_item.toolTip())
        self.table.setItem(row, self.COL_START, st_item)

        en_item = QTableWidgetItem(entry.get("end_utc", ""))
        if is_sop_overlay:
            en_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        else:
            self._make_editable(en_item)
        if overlay_tip:
            en_item.setToolTip(overlay_tip)
        en_item.setData(Qt.UserRole, en_item.toolTip())
        self.table.setItem(row, self.COL_END, en_item)

        # Auto-Tune
        chk = QCheckBox()
        chk.setChecked(bool(entry.get("auto_tune", False)))
        chk.setTristate(False)
        chk.setEnabled(not is_sop_overlay)
        if not is_sop_overlay:
            chk.stateChanged.connect(self._mark_dirty)
        auto_wrap = QWidget()
        auto_layout = QHBoxLayout(auto_wrap)
        auto_layout.setContentsMargins(0, 0, 0, 0)
        auto_layout.setAlignment(Qt.AlignCenter)
        auto_layout.addWidget(chk)
        self.table.setCellWidget(row, self.COL_AUTOTUNE, auto_wrap)

        target_scope_combo = QComboBox()
        target_scope_combo.setToolTip(self._target_scope_tooltip())
        target_value_combo = QComboBox()
        target_value_combo.setToolTip(self._target_scope_tooltip())
        if is_sop_overlay:
            target_scope_combo.addItem("SOP Layer", "sop_layer")
            target_scope_combo.setEnabled(False)
            target_scope_combo.setToolTip("SOP layer target scope is not editable in this slice.")
            self._populate_target_value_combo(
                target_value_combo,
                TARGET_SCOPE_STATION,
                editable=False,
                fixed_label=overlay_profile or "SOP Layer",
            )
        else:
            target_scope, target_device_profile_id, target_operating_profile_id = normalize_schedule_target(
                entry.get("target_scope"),
                target_device_profile_id=entry.get("target_device_profile_id"),
                target_operating_profile_id=entry.get("target_operating_profile_id"),
            )
            for label, value in SCHEDULE_TARGET_SCOPE_ITEMS:
                target_scope_combo.addItem(label, value)
            idx = target_scope_combo.findData(target_scope)
            if idx >= 0:
                target_scope_combo.setCurrentIndex(idx)
            self._populate_target_value_combo(
                target_value_combo,
                target_scope,
                target_device_profile_id=target_device_profile_id,
                target_operating_profile_id=target_operating_profile_id,
            )
            target_scope_combo.currentIndexChanged.connect(
                lambda _idx, scope_combo=target_scope_combo, value_combo=target_value_combo: self._populate_target_value_combo(
                    value_combo,
                    normalize_target_scope(scope_combo.currentData()),
                )
            )
            target_scope_combo.currentTextChanged.connect(self._mark_dirty)
            target_scope_combo.currentTextChanged.connect(lambda _text: self._queue_table_conflict_refresh())
            target_value_combo.currentTextChanged.connect(self._mark_dirty)
            target_value_combo.currentTextChanged.connect(lambda _text: self._queue_table_conflict_refresh())
        self.table.setCellWidget(row, self.COL_TARGET_SCOPE, target_scope_combo)
        self.table.setCellWidget(row, self.COL_TARGET, target_value_combo)

        if is_sop_overlay:
            self._update_delete_button_state()
            return

        # wiring for group/band changes
        def on_group_changed(text: str, self=self, row=row, band_combo=band_combo):
            self._populate_band_combo(band_combo, text)
            # auto-select first band
            if band_combo.count() > 0:
                band_combo.setCurrentIndex(0)
            self._update_mode_freq(row)

        def on_band_changed(text: str, self=self, row=row):
            self._update_mode_freq(row)

        group_combo.currentTextChanged.connect(on_group_changed)
        band_combo.currentTextChanged.connect(on_band_changed)
        # Ensure initial mode/freq selection is synced to operating group data
        self._update_mode_freq(row)
        self._update_delete_button_state()

    def _add_row(self):
        self._append_entry_row({})
        self.table.scrollToBottom()
        self._mark_dirty()
        self._highlight_time_conflicts()
        self._update_resource_action_state()
        self._apply_schedule_table_height_hints()

    def _delete_selected_rows(self):
        selected = set()
        removed_hf = 0
        # Prefer checkbox selection
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, self.COL_SELECT)
            if isinstance(w, QCheckBox) and w.isChecked():
                selected.add(r)
            if isinstance(w, QWidget):
                chk = w.findChild(QCheckBox)
                if chk is not None and chk.isChecked():
                    selected.add(r)
        # Fallback to selected cells if no checkboxes are ticked
        if not selected:
            for idx in self.table.selectedIndexes():
                row_idx = int(idx.row())
                if self._is_sop_overlay_row(row_idx):
                    continue
                selected.add(row_idx)
        for r in sorted(selected, reverse=True):
            if self._is_sop_overlay_row(r):
                continue
            removed_hf += 1
            self.table.removeRow(r)
        self._update_delete_button_state()
        if removed_hf > 0:
            self._mark_dirty()
        self._highlight_time_conflicts()
        self._update_resource_action_state()
        self._apply_schedule_table_height_hints()

    @staticmethod
    def _sort_day_rank(day_value: str) -> int:
        day_txt = str(day_value or "").strip().upper()
        if day_txt == "ALL":
            return 0
        for idx, day_name in enumerate(DAY_CANON, start=1):
            if day_txt == day_name.upper() or day_txt.startswith(day_name[:3].upper()):
                return idx
        return 99

    @staticmethod
    def _sort_hhmm_to_minutes(value: str) -> int:
        txt = str(value or "").strip()
        try:
            hh, mm = txt.split(":", 1)
            h = int(hh)
            m = int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return (h * 60) + m
        except Exception:
            pass
        return 9999

    def _snapshot_active_entry_for_sort(self, row_index: int) -> Tuple[Dict[str, Any], Tuple[Any, ...]]:
        is_sop = self._is_sop_overlay_row(row_index)
        row_utc = self._active_row_to_utc(row_index, include_sop_overlay=True)
        if row_utc:
            entry: Dict[str, Any] = dict(self._entry_for_display(dict(row_utc)))
        else:
            entry = {
                "day_utc": self._get_combo_value(row_index, self.COL_DAY, "ALL"),
                "group_name": self._get_combo_value(row_index, self.COL_GROUP, ""),
                "mode": self._get_combo_value(row_index, self.COL_MODE, ""),
                "band": self._get_combo_value(row_index, self.COL_BAND, ""),
                "frequency": self._get_text_value(row_index, self.COL_FREQ),
                "start_utc": self._get_text_value(row_index, self.COL_START),
                "end_utc": self._get_text_value(row_index, self.COL_END),
                "auto_tune": self._get_checkbox_value(row_index, self.COL_AUTOTUNE),
            }

        sel_wrap = self.table.cellWidget(row_index, self.COL_SELECT)
        if isinstance(sel_wrap, QWidget):
            try:
                entry["_resource_id"] = int(sel_wrap.property("resource_id") or 0)
            except Exception:
                entry["_resource_id"] = 0
            entry["_resource_set"] = str(sel_wrap.property("resource_set") or "")
            entry["operating_group_frequency_key"] = str(sel_wrap.property("operating_group_frequency_key") or "")
            if is_sop:
                entry["_sop_overlay"] = True
                entry["_source_key"] = str(sel_wrap.property("sop_overlay_source_key") or "").strip()
                entry["_sop_profile_name"] = str(sel_wrap.property("sop_profile_name") or "").strip()
                entry["_sop_profile_id"] = int(sel_wrap.property("sop_overlay_profile_id") or 0)
                entry["_sop_layer_id"] = int(sel_wrap.property("sop_overlay_layer_id") or 0)
                entry["_sop_recurrence"] = str(sel_wrap.property("sop_overlay_recurrence") or "Weekly")
                entry["_sop_biweekly_offset_weeks"] = int(sel_wrap.property("sop_overlay_biweekly") or 0)
                entry["_sop_month_weeks"] = str(sel_wrap.property("sop_overlay_month_weeks") or "")
                entry["_sop_vfo"] = str(sel_wrap.property("sop_overlay_vfo") or "A")

        day_txt = str(entry.get("day_utc") or "ALL")
        start_txt = str(entry.get("start_utc") or "")
        end_txt = str(entry.get("end_utc") or "")
        group_txt = str(entry.get("group_name") or "").strip().upper()
        band_txt = str(entry.get("band") or "").strip().upper()
        freq_txt = self._normalize_freq_text(str(entry.get("frequency") or ""))
        source_rank = 1 if is_sop else 0
        sort_key = (
            self._sort_day_rank(day_txt),
            self._sort_hhmm_to_minutes(start_txt),
            source_rank,
            group_txt,
            band_txt,
            freq_txt,
            self._sort_hhmm_to_minutes(end_txt),
            int(row_index),
        )
        return entry, sort_key

    def _sort_active_schedule_by_time(self, *, refresh_post_sort: bool = True) -> None:
        if self.table.rowCount() <= 1:
            return
        rows_for_sort: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []
        for r in range(self.table.rowCount()):
            entry, key = self._snapshot_active_entry_for_sort(r)
            rows_for_sort.append((key, entry))
        rows_for_sort.sort(key=lambda pair: pair[0])

        was_dirty = bool(self._dirty)
        self._suspend_dirty_tracking = True
        try:
            self.table.setRowCount(0)
            for _key, entry in rows_for_sort:
                self._append_entry_row(entry)
        finally:
            self._suspend_dirty_tracking = False

        self.table.clearSelection()
        self._update_delete_button_state()
        self._invalidate_active_schedule_views(invalidate_resource_table=False)
        if refresh_post_sort:
            self._highlight_time_conflicts()
            self._update_resource_action_state()
        self._set_dirty(was_dirty)

    # ---------------- Cell access helpers ---------------- #

    def _get_combo_value(self, row: int, col: int, default: str = "") -> str:
        w = self.table.cellWidget(row, col)
        if isinstance(w, QComboBox):
            return w.currentText().strip()
        item = self.table.item(row, col)
        if item is not None:
            return item.text().strip()
        return default

    def _get_checkbox_value(self, row: int, col: int) -> bool:
        w = self.table.cellWidget(row, col)
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QWidget):
            chk = w.findChild(QCheckBox)
            if chk is not None:
                return chk.isChecked()
        return False

    def _get_text_value(self, row: int, col: int) -> str:
        w = self.table.cellWidget(row, col)
        if isinstance(w, QComboBox):
            if col == self.COL_FREQ:
                value = w.currentData(Qt.UserRole + 1)
                return str(value if value is not None else w.currentText()).strip()
            return w.currentText().strip()
        item = self.table.item(row, col)
        if item is None:
            return ""
        return item.text().strip()

    def _set_text_value(self, row: int, col: int, value: str) -> None:
        w = self.table.cellWidget(row, col)
        if isinstance(w, QComboBox):
            idx = w.findData(value, Qt.UserRole + 1)
            if idx >= 0:
                w.setCurrentIndex(idx)
            return
        item = self.table.item(row, col)
        if item is not None and item.text() != value:
            item.setText(value)

    def _make_editable(self, item: QTableWidgetItem):
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)

    def _populate_band_combo(self, band_combo: QComboBox, group_name: str):
        band_combo.blockSignals(True)
        band_combo.clear()
        bands = sorted(
            {g.get("band") for g in self.operating_groups if g.get("group") == group_name and g.get("band")}
        )
        for b in bands:
            band_combo.addItem(b)
        band_combo.blockSignals(False)

    def _matching_operating_groups(self, group: str, band: str) -> List[Dict]:
        return [
            g
            for g in self.operating_groups
            if g.get("group") == group and g.get("band") == band
        ]

    def _frequency_value(self, row: int) -> str:
        widget = self.table.cellWidget(row, self.COL_FREQ)
        if isinstance(widget, QComboBox):
            value = widget.currentData(Qt.UserRole + 1)
            return str(value if value is not None else widget.currentText()).strip()
        return self._get_text_value(row, self.COL_FREQ)

    def _frequency_key(self, row: int) -> str:
        widget = self.table.cellWidget(row, self.COL_FREQ)
        if isinstance(widget, QComboBox):
            return str(widget.currentData(Qt.UserRole) or "").strip()
        select = self.table.cellWidget(row, self.COL_SELECT)
        return str(select.property("operating_group_frequency_key") or "").strip() if isinstance(select, QWidget) else ""

    def _set_frequency_choice(self, row: int, group: str, band: str, mode: str, frequency: str = "", frequency_key: str = "") -> None:
        result = resolve_operating_group_frequency(
            self.operating_groups, group=group, band=band, mode=mode,
            frequency_key=frequency_key, frequency=frequency or None,
        )
        candidates = [g for g in result.candidates if normalize_frequency_hz(g.get("frequency")) is not None]
        if not candidates and not frequency_key:
            # Preserve legacy free-text schedule rows when no valid configured
            # frequency variant exists for this group/mode.
            self.table.removeCellWidget(row, self.COL_FREQ)
            item = self.table.item(row, self.COL_FREQ)
            if item is None:
                item = QTableWidgetItem()
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.table.setItem(row, self.COL_FREQ, item)
            item.setText(str(frequency or "").strip())
            return
        combo = QComboBox()
        combo.setMinimumContentsLength(7)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.addItem("Select frequency…", "")
        combo.setItemData(0, "", Qt.UserRole + 1)
        for candidate in candidates:
            value = str(candidate.get("frequency") or "").strip()
            key = str(candidate.get("operating_group_frequency_key") or "").strip()
            combo.addItem(format_frequency_mhz(value), key)
            combo.setItemData(combo.count() - 1, value, Qt.UserRole + 1)
        selected = -1
        if result.resolved and result.row is not None:
            selected_key = str(result.row.get("operating_group_frequency_key") or "")
            selected = combo.findData(selected_key, Qt.UserRole)
        elif frequency or frequency_key:
            # Keep stale or incompatible references visible and intact until the
            # operator explicitly selects a configured alternative.
            combo.addItem(f"Unresolved — {frequency}" if frequency else "Unresolved frequency reference", frequency_key)
            combo.setItemData(combo.count() - 1, frequency, Qt.UserRole + 1)
            selected = combo.count() - 1
        if selected >= 0:
            combo.setCurrentIndex(selected)
        combo.setToolTip(result.reason if result.state != "resolved" else "Choose the configured frequency for this schedule row.")
        combo.currentIndexChanged.connect(lambda _idx, r=row: self._on_frequency_choice_changed(r))
        combo.currentIndexChanged.connect(self._mark_dirty)
        self.table.setCellWidget(row, self.COL_FREQ, combo)
        select = self.table.cellWidget(row, self.COL_SELECT)
        if isinstance(select, QWidget):
            select.setProperty("operating_group_frequency_key", self._frequency_key(row))

    def _on_frequency_choice_changed(self, row: int) -> None:
        select = self.table.cellWidget(row, self.COL_SELECT)
        if isinstance(select, QWidget):
            select.setProperty("operating_group_frequency_key", self._frequency_key(row))
        mode = self._get_combo_value(row, self.COL_MODE, "")
        if mode:
            self._auto_start_for_mode(mode)

    def _set_mode_widget(
        self, row: int, group: str, band: str, preferred_mode: str = "", entries: Optional[List[Dict]] = None
    ) -> QComboBox:
        if entries is None:
            entries = self._matching_operating_groups(group, band)
        modes = sorted({(e.get("mode") or "").strip() for e in entries if e.get("mode")})
        combo = QComboBox()
        if modes:
            combo.addItems(modes)
        else:
            combo.addItem("")
        if preferred_mode and preferred_mode in modes:
            combo.setCurrentText(preferred_mode)
        elif modes:
            combo.setCurrentIndex(0)
        combo.setEnabled(len(modes) > 1)
        combo.currentTextChanged.connect(lambda _m, r=row: self._update_mode_freq(r))
        combo.currentTextChanged.connect(self._mark_dirty)
        self.table.setCellWidget(row, self.COL_MODE, combo)
        return combo

    def _update_mode_freq(self, row: int):
        group = self._get_combo_value(row, self.COL_GROUP, "")
        band = self._get_combo_value(row, self.COL_BAND, "")
        entries = self._matching_operating_groups(group, band)
        preferred_mode = self._get_combo_value(row, self.COL_MODE, "")
        mode_combo = self._set_mode_widget(row, group, band, preferred_mode, entries)
        mode_val = mode_combo.currentText().strip() if isinstance(mode_combo, QComboBox) else preferred_mode
        old_frequency = self._frequency_value(row)
        old_key = self._frequency_key(row)
        self._set_frequency_choice(row, group, band, mode_val, old_frequency, old_key)
        # trigger autostart if mode changes
        if mode_val:
            self._auto_start_for_mode(mode_val)

    # ---------------- Auto-start radio software ---------------- #

    def _auto_start_for_mode(self, mode: str):
        """
        Start radio programs according to mode, if their Auto-Start flags
        are enabled in Settings and the programs are not already running.

        - JS8: JS8Call
        - Digi: FLDigi, FLMsg, FLAmp
        - Tri: all radio programs
        - SSB: no auto-start
        """
        if getattr(self, "_suppress_autostart", False):
            return
        if self._is_truthy(self.settings.get("launch_control_enabled", True)):
            return

        mode = (mode or "").strip().upper()
        if not mode:
            return

        if mode == "DIGI":
            programs = ["FLDigi", "FLMsg", "FLAmp"]
        else:
            # SSB (or anything else): no auto-start
            return

        allowed_autostart = {"FLDigi", "FLMsg", "FLAmp"}
        programs = [p for p in programs if p in allowed_autostart]
        if not programs:
            return

        for prog in programs:
            self._launch_program_if_autostart_enabled(prog)

    def _launch_program_if_autostart_enabled(self, prog_name: str):
        if prog_name not in {"FLDigi", "FLMsg", "FLAmp"}:
            return
        meta = PROGRAMS.get(prog_name)
        if not meta:
            return

        autostart_key = meta["autostart_key"]
        autostart = self._is_truthy(self.settings.get(autostart_key, False))
        if not autostart:
            return

        if self._program_is_running(prog_name, meta):
            return

        path_str = self.settings.get(meta["path_key"], "") or ""
        if path_str:
            exe_path = Path(path_str)
            cmd = [str(exe_path)]
        else:
            cmd = [meta["default_cmd"]]

        try:
            if platform.system() == "Windows":
                subprocess.Popen(
                    cmd,
                    shell=False,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                )
            else:
                subprocess.Popen(cmd)
            log.info("DailyScheduleTab: auto-started %s via %r", prog_name, cmd)
        except Exception as e:
            log.error("DailyScheduleTab: failed to auto-start %s via %r: %s", prog_name, cmd, e)

    def _program_is_running(self, prog_name: str, meta: Dict) -> bool:
        """
        Check if a program is already running using the shared status service.
        """
        _ = meta  # Kept for signature compatibility with existing call sites.
        try:
            return bool(self._status_service.program_is_running(prog_name))
        except Exception:
            return False

    @staticmethod
    def _is_truthy(val) -> bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return val != 0
        if isinstance(val, str):
            return val.strip().lower() in {"true", "1", "yes", "on"}
        return False

    # ---------------- Validation ---------------- #

    def _validate_time(self, text: str) -> bool:
        text = self._normalize_hhmm(str(text or ""))
        if not text:
            return False
        try:
            h, m = text.split(":")
            h = int(h)
            m = int(m)
            return 0 <= h <= 23 and 0 <= m <= 59
        except Exception:
            return False

    def _validate_frequency(self, band: str, mode: str, freq_text: str) -> bool:
        """
        Validate frequency based on band/mode constraints.

        Modes are now JS8, Digi, Tri, SSB.

        For band/mode limits, we treat:
          - JS8 as Digi
          - Tri as Digi
        """
        band = (band or "").strip().upper()
        mode_raw = (mode or "").strip().title()
        # Map JS8 and Tri to Digi for band-plan limits
        if mode_raw in ("Js8", "Tri"):
            eff_mode = "Digi"
        elif mode_raw in ("Usb", "Lsb"):
            eff_mode = "SSB"
        else:
            eff_mode = mode_raw

        freq_text = (freq_text or "").strip()
        if not freq_text:
            QMessageBox.warning(self, "Missing Frequency", "Frequency is required for all HF schedule rows.")
            return False

        # Parse frequency; handle "5.358.500" style if user types with extra dot
        try:
            normalized = freq_text.replace(",", ".").replace(" ", "")
            parts = normalized.split(".")
            if len(parts) > 2:
                normalized = parts[0] + "." + "".join(parts[1:])
            freq = float(normalized)
        except Exception:
            QMessageBox.warning(
                self,
                "Invalid Frequency",
                f"Frequency '{freq_text}' is not a valid number.",
            )
            return False

        # Special 60M handling
        if band == "60M":
            allowed = [5.332, 5.348, 5.3585, 5.373, 5.405]
            for a in allowed:
                if abs(freq - a) < 0.0005:
                    return True
            QMessageBox.warning(
                self,
                "Invalid 60M Frequency",
                "On 60M the only allowed channels are:\n"
                " 5.332, 5.348, 5.358.500, 5.373, 5.405 MHz",
            )
            return False

        # Range table
        ranges = {
            ("20M", "Digi"): (14.000, 14.150),
            ("20M", "SSB"): (14.150, 14.350),
            ("40M", "Digi"): (7.000, 7.125),
            ("40M", "SSB"): (7.125, 7.300),
            ("80M", "Digi"): (3.500, 3.600),
            ("80M", "SSB"): (3.600, 4.000),
            ("2M", "Digi"): (144.000, 148.000),
            ("2M", "SSB"): (144.100, 148.000),
            ("6M", "Digi"): (50.000, 54.000),
            ("6M", "SSB"): (50.100, 54.000),
            ("10M", "Digi"): (28.000, 28.300),
            ("10M", "SSB"): (28.300, 29.700),
            ("12M", "Digi"): (24.890, 24.930),
            ("12M", "SSB"): (24.930, 24.990),
            ("15M", "Digi"): (21.000, 21.200),
            ("15M", "SSB"): (21.200, 21.450),
            ("17M", "Digi"): (18.068, 18.110),
            ("17M", "SSB"): (18.110, 18.168),
            ("30M", "Any"): (10.100, 10.150),
        }

        key = (band, eff_mode)
        any_key = (band, "Any")

        if key in ranges:
            lo, hi = ranges[key]
        elif any_key in ranges:
            lo, hi = ranges[any_key]
        else:
            # If band not in our table, accept anything
            return True

        if not (lo <= freq <= hi):
            QMessageBox.warning(
                self,
                "Frequency out of range",
                f"{band} {mode_raw}: {freq:.3f} MHz is outside allowed range "
                f"{lo:.3f} - {hi:.3f} MHz.",
            )
            return False

        return True
