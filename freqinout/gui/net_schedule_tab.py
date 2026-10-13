from __future__ import annotations

from collections import OrderedDict
from typing import List, Dict, Optional, Any, Tuple, Set

import platform
import sqlite3
import subprocess
import datetime
import json
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QRegularExpression, QItemSelectionModel, QEvent
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
    QLineEdit,
    QCheckBox,
    QFileDialog,
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QCompleter,
    QToolButton,
    QMenu,
    QScrollArea,
    QSizePolicy,
)
from PySide6.QtGui import QRegularExpressionValidator, QAction, QColor

from freqinout.core.multi_radio_store import MultiRadioStore, settings_db_path
from freqinout.core.schedule_targeting import (
    TARGET_SCOPE_DEVICE_PROFILE,
    TARGET_SCOPE_OPERATING_PROFILE,
    TARGET_SCOPE_STATION,
    normalize_schedule_target,
    normalize_schedule_target_fields,
    normalize_target_scope,
    schedule_target_identity_parts,
)
from freqinout.core.operating_group_frequency import (
    ensure_operating_group_frequency_keys,
    format_frequency_mhz,
    normalize_frequency_hz,
    reattach_schedule_frequency_keys,
    resolve_operating_group_frequency,
)
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.resource_catalog_store import ResourceCatalogStore
from freqinout.core.schedule_source_sets import (
    LIVE_SOURCE_SET_ID,
    HF_NET_SOURCE_CATEGORY,
    HF_NET_SOURCE_SETS_KEY,
    SELECTED_HF_NET_SOURCE_SET_KEY,
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
from freqinout.core.plan_context_service import PlanContextService
from freqinout.core.software_status_service import SoftwareStatusService
from freqinout.core.sop_manager import SOPManager
from freqinout.core.perf_metrics import span as perf_span
from freqinout.core.logger import log
from freqinout.core.legacy_resource_projection import (
    dedupe_legacy_resources,
    delete_legacy_resources,
    finalize_legacy_resource_projection,
    upsert_legacy_resource,
)
from freqinout.utils.timezones import get_timezone
from freqinout.gui.help_registry import resolve_help_host
from freqinout.gui.plan_context_label import PlanContextLabel
from freqinout.gui.theme import (
    resolve_theme, button_style, font_css, item_view_height_for_rows,
    font_derived_widget_height, control_height_for_font, fit_combo_box_to_contents,
)


# ---- Band / Mode metadata (keep in sync with HF tab) ----

BAND_ORDER = [
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

# Updated mode list
MODES = ["Digi", "SSB", "USB", "LSB"]

BUILTIN_NET_RESOURCES_SYNC_VERSION = 2

# For band limits, JS8 and Tri behave like Digi ranges
BAND_MODE_LIMITS = {
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
    ("30M", "Digi"): (10.100, 10.150),
    ("30M", "SSB"): (10.100, 10.150),
}

SIXTY_M_CHANNELS = {
    "5.332",
    "5.348",
    "5.3585",
    "5.373",
    "5.405",
}

DAY_NAMES = [
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]
DAY_OPTIONS = ["ALL"] + DAY_NAMES
SCHEDULE_TARGET_SCOPE_ITEMS = [
    ("Station", TARGET_SCOPE_STATION),
    ("Radio Profile", TARGET_SCOPE_DEVICE_PROFILE),
    ("Frequency Plan", TARGET_SCOPE_OPERATING_PROFILE),
]

_FLDIGI_MODE_OPTIONS_FALLBACK = [
    "Cont-4/250",
    "MFSK32",
    "SSB",
    "FSQ",
    "CW",
]
_FLDIGI_MODE_OPTIONS_CACHE: Optional[List[str]] = None


def _fldigi_mode_options() -> List[str]:
    global _FLDIGI_MODE_OPTIONS_CACHE
    if _FLDIGI_MODE_OPTIONS_CACHE is not None:
        return list(_FLDIGI_MODE_OPTIONS_CACHE)
    try:
        from freqinout.gui.settings_tab import FLDIGI_MODE_OPTIONS  # lazy import to avoid tab load coupling

        vals = [str(v).strip() for v in FLDIGI_MODE_OPTIONS if str(v).strip()]
    except Exception:
        vals = list(_FLDIGI_MODE_OPTIONS_FALLBACK)
    seen = set()
    ordered: List[str] = []
    for v in vals:
        key = v.upper()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(v)
    _FLDIGI_MODE_OPTIONS_CACHE = ordered
    return list(ordered)

# Program metadata matching SettingsTab keys
PROGRAM_META: Dict[str, Dict[str, str]] = {
    "FLRig": {"path_key": "path_flrig", "autostart_key": "autostart_flrig"},
    "FLDigi": {"path_key": "path_fldigi", "autostart_key": "autostart_fldigi"},
    "FLMsg": {"path_key": "path_flmsg", "autostart_key": "autostart_flmsg"},
    "FLAmp": {"path_key": "path_flamp", "autostart_key": "autostart_flamp"},
    "JS8Call": {"path_key": "path_js8call", "autostart_key": "autostart_js8call"},
}


class NetScheduleTab(QWidget):
    schedule_saved = Signal()
    """
    Net Schedules GUI.

    Columns:
      0: Select (checkbox for delete)
      1: Day (UTC)
      2: Recurrence (Weekly / Daily / Periodic)
      3: Month Weeks (1/2/3/4/5)
      4: Group Name (from Operating Groups)
      5: Mode (JS8 / Digi / Tri / SSB)
      6: Band
      7: Frequency (MHz)
      8: Start UTC (HH:MM)
      9: End UTC (HH:MM)
      10: Early Check-in (minutes: 0/5/10/15)
      11: FLDigi Starting Mode
      12: FLDigi Starting Offset (Hz)
      13: Net Name
      14: Auto-Tune

    Data is saved to:
      - config/config.json under key "net_schedule"
      - SQLite DB freqinout_nets.db tables "net_schedule_tab"
        and legacy "net_schedule" (without VFO for backward compatibility)
    """

    COL_SELECT = 0
    COL_DAY = 1
    COL_RECURRENCE = 2
    COL_MONTH_WEEKS = 3
    COL_GROUP = 4
    COL_MODE = 5
    COL_BAND = 6
    COL_FREQ = 7
    COL_START = 8
    COL_END = 9
    COL_EARLY = 10
    COL_FLDIGI_MODE = 11
    COL_FLDIGI_OFFSET = 12
    COL_NETNAME = 13
    COL_AUTOTUNE = 14
    COL_TARGET_SCOPE = 15
    COL_TARGET = 16
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
            COL_NETNAME,
        }
    )

    RES_COL_SOURCE = 0
    RES_COL_SET = 1
    RES_COL_DAY = 2
    RES_COL_RECURRENCE = 3
    RES_COL_MONTH_WEEKS = 4
    RES_COL_GROUP = 5
    RES_COL_MODE = 6
    RES_COL_BAND = 7
    RES_COL_FREQ = 8
    RES_COL_START = 9
    RES_COL_END = 10
    RES_COL_EARLY = 11
    RES_COL_FLDIGI_MODE = 12
    RES_COL_FLDIGI_OFFSET = 13
    RES_COL_NETNAME = 14
    RES_COL_COVERAGE = 15
    RES_COL_COMMENT = 16
    RES_COL_UPDATED = 17

    def __init__(self, parent=None, *, plan_context_service: Optional[PlanContextService] = None):
        super().__init__(parent)
        self.settings = SettingsManager()
        self.plan_context_service = plan_context_service or PlanContextService()
        self._status_service = SoftwareStatusService(self.settings)
        self._sop_manager = SOPManager()
        self._net_name_history: List[str] = []
        self.operating_groups: List[Dict[str, str]] = []
        self._clock_timer: QTimer | None = None
        self._suppress_autostart: bool = True  # avoid auto-start during initial load
        default_mode = (self.settings.get("display_time_mode", "LOCAL") or "LOCAL").upper()
        self._show_local: bool = default_mode != "UTC"
        self._raw_rows: List[Dict] = []
        self.device_profiles: List[Dict[str, Any]] = []
        self.operating_profiles: List[Dict[str, Any]] = []
        self._refresh_schedule_target_catalogs()
        self._resource_rows: List[Dict[str, Any]] = []
        self._resource_view_rows: List[Dict[str, Any]] = []
        self._dirty: bool = False
        self._suspend_dirty_tracking: bool = False
        self._saved_rows_signature: str = ""
        self._blocking_net_sop_conflict_count: int = 0
        self._blocking_net_sop_conflict_signatures: Set[str] = set()
        self._conflict_refresh_timer: QTimer | None = None
        self._pending_sop_conflict_refresh: bool = False
        self._net_sop_conflict_cache: OrderedDict[
            Tuple[str, float, int, int],
            Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Set[str]],
        ] = OrderedDict()
        self._net_sop_conflict_cache_limit: int = 6
        self._net_sop_conflict_cache_epoch: int = 0
        self._responsive_layout_mode = "wide"
        self._net_geometry_pending = False
        self._net_geometry_timer = QTimer(self)
        self._net_geometry_timer.setSingleShot(True)
        self._net_geometry_timer.timeout.connect(self._refresh_net_geometry)

        self._build_ui()
        self._load()
        self._setup_clock_timer()
        self._suppress_autostart = False

    def _open_context_help(self, context_key: str) -> None:
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_context_help"):
            try:
                host.open_context_help(context_key)
            except Exception:
                pass

    # --------- UI --------- #

    def _build_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        self.net_schedule_scroll_area = QScrollArea()
        self.net_schedule_scroll_area.setObjectName("netScheduleScrollArea")
        self.net_schedule_scroll_area.setWidgetResizable(True)
        self.net_schedule_scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.net_schedule_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer_layout.addWidget(self.net_schedule_scroll_area)

        content = QWidget()
        content.setObjectName("netScheduleScrollContent")
        self.net_schedule_scroll_area.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setSpacing(10)

        header = QHBoxLayout()
        self.header_title_label = QLabel("<h3>Net Source Schedule</h3>")
        header.addWidget(self.header_title_label)
        self.help_btn = QPushButton("Help")
        self.help_btn.setToolTip("Open Net Schedules help.")
        self.help_btn.clicked.connect(lambda: self._open_context_help("tab.hf-nets"))
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
        layout.addLayout(header)

        self.plan_context_label = PlanContextLabel(
            "net_schedule",
            service=self.plan_context_service,
            fallback_text="Net schedule workspace context is available from Help.",
        )
        self.plan_context_label.setToolTip(
            "Use this context to confirm which radio and assigned Frequency Plan net schedule changes apply to."
        )
        self.plan_context_label.setVisible(False)
        self.plan_context_label.refresh_context(refresh=True)
        self.source_usage_label = QLabel("")
        self.source_usage_label.setObjectName("netScheduleSourceUsage")
        self.source_usage_label.setWordWrap(True)
        self.source_usage_label.setToolTip(
            "Shows which linked Frequency Plan(s) and assigned radio(s) use the selected Net schedule."
        )
        self.source_usage_label.setVisible(False)

        # table
        self.table = QTableWidget()
        self.table.setColumnCount(self.COL_TARGET + 1)
        self._set_headers()
        self.table.setSortingEnabled(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(self.COL_NETNAME, QHeaderView.Stretch)
        hv.setStretchLastSection(False)
        hv.setMinimumSectionSize(50)

        self.add_btn = QPushButton("Add Row")
        self.add_hf_net_btn = QPushButton("Add HF Net")
        self.add_hf_net_btn.setToolTip("Choose published Net Directory meetings and review them before adding drafts to a named HF Net schedule.")
        self.del_btn = QPushButton("Delete Selected")
        self.view_edit_btn = QPushButton("View/Edit")
        self.view_edit_btn.setCheckable(True)
        self.view_edit_btn.setToolTip("Show or hide the full editable net schedule fields.")
        self.move_to_resources_btn = QPushButton("Save Selected to Library")
        self.move_to_resources_btn.setToolTip(
            "Copy selected net schedule rows into the reusable Net Row Library. The schedule rows stay in place."
        )
        self.export_btn = QPushButton("Export Net Schedule")
        self.manage_net_sop_policies_btn = QPushButton("Manage Net/SOP Policies")
        self.schedule_source_label = QLabel("Net Schedule:")
        self.schedule_source_combo = QComboBox()
        self.schedule_source_combo.setObjectName("netScheduleSourceCombo")
        self.schedule_source_combo.setEditable(True)
        self.schedule_source_combo.setInsertPolicy(QComboBox.NoInsert)
        self.schedule_source_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.schedule_source_combo.setMinimumContentsLength(24)
        if self.schedule_source_combo.lineEdit() is not None:
            self.schedule_source_combo.lineEdit().setPlaceholderText("Name or select a net schedule")
        self.schedule_source_combo.setToolTip(
            "Select a saved HF Net schedule, or type a clear name here before Save Schedule."
        )
        self.new_source_btn = QPushButton("New Schedule")
        self.new_source_btn.setToolTip("Start a blank HF Net schedule. A name typed before or after New Schedule is retained until Save.")
        self.save_btn = QPushButton("Save Schedule")
        self.save_btn.setToolTip(
            "Save the visible rows as the selected named HF Net schedule, or create a new named schedule."
        )
        self.rename_source_btn = QPushButton("Rename Schedule")
        self.rename_source_btn.setToolTip("Rename the selected HF Net schedule without changing its rows.")
        self.save_source_btn = QPushButton("Save / Update Schedule")
        self.save_source_btn.setToolTip("Save the visible HF Net rows as the selected named schedule, or create a new named schedule.")
        self.save_source_btn.setVisible(False)
        self.delete_source_btn = QPushButton("Delete Schedule")
        self.delete_source_btn.setToolTip("Delete the selected named HF Net schedule.")
        self._net_action_layout = QGridLayout()
        self._net_action_layout.setContentsMargins(0, 0, 0, 0)
        self._net_action_layout.setSpacing(8)
        self._net_toolbar_rows: list[QHBoxLayout] = []
        self._net_toolbar_pool = [QHBoxLayout() for _ in range(4)]
        layout.addLayout(self._net_action_layout)
        self.source_usage_label.setVisible(True)
        layout.addWidget(self.source_usage_label)
        self.subscription_status_label = QLabel("")
        self.subscription_status_label.setObjectName("netScheduleDirectorySubscriptionStatus")
        self.subscription_status_label.setWordWrap(True)
        self.subscription_status_label.setVisible(False)
        layout.addWidget(self.subscription_status_label)
        layout.addWidget(self.table)

        # Net resources section
        res_header = QHBoxLayout()
        res_header.addWidget(QLabel("<h3>Net Row Library</h3>"))
        res_header.addStretch()
        self.resources_count_label = QLabel("")
        self.resources_count_label.setObjectName("netScheduleResourcesCount")
        res_header.addWidget(self.resources_count_label)
        self.net_resources_hint = QLabel("Visit SitRepNet.com for more information.")
        self.net_resources_hint.setTextFormat(Qt.PlainText)
        res_header.addWidget(self.net_resources_hint)
        layout.addLayout(res_header)

        self.resource_set_label = QLabel("Library:")
        self.resource_set_combo = QComboBox()
        self.resource_set_combo.addItem("All resources", "All")
        self.resource_set_combo.setMinimumWidth(260)
        self.resource_search_label = QLabel("Search:")
        self.resource_search = QLineEdit()
        self.resource_search.setPlaceholderText("Search set, group, net, band, time...")
        self.add_to_schedule_btn = QToolButton()
        self.add_to_schedule_btn.setPopupMode(QToolButton.MenuButtonPopup)
        add_menu = QMenu(self.add_to_schedule_btn)
        self.add_selected_resource_action = QAction("Add Selected Rows", self)
        self.add_filtered_resource_action = QAction("Add Filtered Rows", self)
        add_menu.addAction(self.add_selected_resource_action)
        add_menu.addAction(self.add_filtered_resource_action)
        self.add_to_schedule_btn.setMenu(add_menu)
        self.add_to_schedule_default_action = QAction("Add Selected Rows", self)
        self.add_to_schedule_btn.setDefaultAction(self.add_to_schedule_default_action)
        self.add_to_schedule_btn.setFont(self.add_btn.font())
        self.add_to_schedule_btn.setToolTip(
            "Copy reusable library rows into the HF Net schedule being edited. Library rows stay saved."
        )

        self.manage_resources_btn = QToolButton()
        self.manage_resources_btn.setPopupMode(QToolButton.MenuButtonPopup)
        manage_menu = QMenu(self.manage_resources_btn)
        self.manage_import_json_action = QAction("Import JSON...", self)
        self.manage_export_new_action = QAction("Export New Resource File", self)
        manage_menu.addAction(self.manage_import_json_action)
        manage_menu.addSeparator()
        manage_menu.addAction(self.manage_export_new_action)
        self.manage_resources_btn.setMenu(manage_menu)
        self.manage_resources_default_action = QAction("Import/Export", self)
        self.manage_resources_btn.setDefaultAction(self.manage_resources_default_action)
        self.manage_resources_btn.setFont(self.add_btn.font())

        self.edit_resource_btn = QPushButton("Edit Library Row")
        self.delete_resource_btn = QPushButton("Delete Library Rows")
        self._net_resource_filter_layout = QGridLayout()
        self._net_resource_filter_layout.setContentsMargins(0, 0, 0, 0)
        self._net_resource_filter_layout.setSpacing(8)
        layout.addLayout(self._net_resource_filter_layout)
        self._arrange_net_action_rows(compact=False)

        self.resources_table = QTableWidget()
        self.resources_table.setColumnCount(18)
        self.resources_table.setHorizontalHeaderLabels(
            [
                "From",
                "Library",
                "Day",
                "Recurrence",
                "Weeks",
                "Group",
                "Mode",
                "Band",
                "Freq (MHz)",
                "Start",
                "End",
                "Early",
                "FLDigi",
                "Offset",
                "Net Name",
                "Coverage",
                "Comment",
                "Age",
            ]
        )
        self.resources_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.resources_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.resources_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.resources_table.setSortingEnabled(True)
        self.resources_table.verticalHeader().setVisible(False)
        self.resources_table.setMinimumHeight(220)
        res_hv = self.resources_table.horizontalHeader()
        res_hv.setSectionResizeMode(QHeaderView.ResizeToContents)
        res_hv.setStretchLastSection(False)
        self.resources_table.setColumnWidth(self.RES_COL_SET, 180)
        self.resources_table.setColumnWidth(self.RES_COL_NETNAME, 220)
        self.resources_table.setColumnWidth(self.RES_COL_UPDATED, 90)
        layout.addWidget(self.resources_table)

        # signals
        self.add_btn.clicked.connect(self._add_row)
        self.add_hf_net_btn.clicked.connect(self._open_add_hf_net_workflow)
        self.del_btn.clicked.connect(self._delete_rows)
        self.view_edit_btn.toggled.connect(self._apply_compact_schedule_view)
        self.move_to_resources_btn.clicked.connect(self._save_selected_schedule_rows_as_resources)
        self.export_btn.clicked.connect(self._export_schedule)
        self.schedule_source_combo.currentIndexChanged.connect(self._on_freqplanner_source_selected)
        self.new_source_btn.clicked.connect(self._on_new_freqplanner_source_clicked)
        self.save_btn.clicked.connect(self._on_save_freqplanner_source_clicked)
        self.save_source_btn.clicked.connect(self._on_save_freqplanner_source_clicked)
        self.rename_source_btn.clicked.connect(self._on_rename_freqplanner_source_clicked)
        self.delete_source_btn.clicked.connect(self._on_delete_freqplanner_source_clicked)
        self.manage_net_sop_policies_btn.clicked.connect(self._open_net_sop_policy_manager)
        self.table.itemSelectionChanged.connect(self._update_delete_button_state)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.resource_set_combo.currentIndexChanged.connect(self._on_resource_set_changed)
        self.resource_search.textChanged.connect(self._refresh_resources_table)
        self.resources_table.itemSelectionChanged.connect(self._update_resource_action_state)
        self.add_to_schedule_default_action.triggered.connect(self._add_resources_default)
        self.add_selected_resource_action.triggered.connect(self._add_selected_resources_to_schedule)
        self.add_filtered_resource_action.triggered.connect(self._add_filtered_resources_to_schedule)
        self.manage_resources_default_action.triggered.connect(self._import_resources_with_mode_prompt)
        self.manage_import_json_action.triggered.connect(self._import_resources_with_mode_prompt)
        self.manage_export_new_action.triggered.connect(self._export_new_resource_file)
        self.edit_resource_btn.clicked.connect(self._edit_selected_resource)
        self.delete_resource_btn.clicked.connect(self._delete_selected_resources)

        self._update_clock_labels()
        self._setup_clock_timer()
        self._apply_theme()
        self._apply_compact_schedule_view(False)
        self._resize_table_columns()
        self._update_delete_button_state()
        self._update_resource_action_state()
        self._refresh_freqplanner_source_combo()
        self._conflict_refresh_timer = QTimer(self)
        self._conflict_refresh_timer.setSingleShot(True)
        self._conflict_refresh_timer.setInterval(180)
        self._conflict_refresh_timer.timeout.connect(self._refresh_net_sop_conflict_highlighting)
        self._update_net_responsive_layout()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_net_responsive_layout()

    def _net_responsive_mode_for_width(self, width: int) -> str:
        # Natural action widths remain independent of grid columns and viewport.
        controls = [self.time_toggle_btn, self.schedule_source_label,
                    self.new_source_btn, self.save_btn, self.rename_source_btn,
                    self.delete_source_btn]
        spacing = self._net_action_layout.spacing()
        natural = sum(w.sizeHint().width() for w in controls)
        natural += self.schedule_source_combo.fontMetrics().horizontalAdvance("M" * 24)
        natural += spacing * len(controls) + self.fontMetrics().horizontalAdvance("MM")
        return "compact" if int(width) < natural else "wide"

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange):
            self._queue_net_geometry_refresh()

    def _queue_net_geometry_refresh(self) -> None:
        if not hasattr(self, "_net_action_layout") or self._net_geometry_pending:
            return
        self._net_geometry_pending = True
        self._net_geometry_timer.start(0)

    def _refresh_net_geometry(self) -> None:
        self._net_geometry_pending = False
        self._responsive_layout_mode = ""
        self._update_net_responsive_layout()
        self._apply_schedule_table_height_hints()

    def _update_net_responsive_layout(self) -> None:
        if not hasattr(self, "_net_resource_filter_layout"):
            return
        mode = self._net_responsive_mode_for_width(int(self.width() or 0))
        if mode == self._responsive_layout_mode and self._net_resource_filter_layout.count() > 0:
            return
        self._responsive_layout_mode = mode
        self._arrange_net_action_rows(compact=(mode == "compact"))
        self._apply_net_compact_table_sizing(compact=(mode == "compact"))

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

    def _arrange_net_action_rows(self, *, compact: bool) -> None:
        for grid in (self._net_action_layout, self._net_resource_filter_layout):
            self._clear_grid_layout(grid)

        # Independent rows keep buttons at natural width, with trailing space.
        for row in self._net_toolbar_pool:
            while row.count():
                row.takeAt(0)
        self._net_toolbar_rows.clear()

        def add_row(widgets):
            row = self._net_toolbar_pool[len(self._net_toolbar_rows)]
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(self._net_action_layout.spacing())
            for widget in widgets:
                if widget is self.schedule_source_combo:
                    row.addWidget(widget, 1)
                else:
                    row.addWidget(widget, 0, Qt.AlignLeft)
            row.addStretch()
            self._net_action_layout.addLayout(row, len(self._net_toolbar_rows), 0)
            self._net_toolbar_rows.append(row)

        self.schedule_source_combo.setMinimumWidth(0)
        self.schedule_source_combo.setMaximumWidth(
            self.schedule_source_combo.fontMetrics().horizontalAdvance("M" * 40)
        )
        context = [self.time_toggle_btn, self.schedule_source_label, self.schedule_source_combo]
        management = [self.new_source_btn, self.rename_source_btn, self.delete_source_btn]
        editing = [self.add_btn, self.add_hf_net_btn, self.del_btn, self.view_edit_btn]
        utilities = [self.move_to_resources_btn, self.export_btn, self.manage_net_sop_policies_btn]
        if compact:
            add_row(context + [self.save_btn])
            add_row(management)
            add_row(editing)
            add_row(utilities)
            self.resource_set_combo.setMinimumWidth(0)
            filter_placements = [
                (self.resource_set_label, 0, 0),
                (self.resource_set_combo, 0, 1),
                (self.resource_search_label, 0, 2),
                (self.resource_search, 0, 3, 1, 2),
                (self.add_to_schedule_btn, 1, 0),
                (self.manage_resources_btn, 1, 1),
                (self.edit_resource_btn, 1, 2),
                (self.delete_resource_btn, 1, 3),
            ]
        else:
            add_row(context + [self.save_btn] + management)
            add_row(editing + utilities)
            self.resource_set_combo.setMinimumWidth(260)
            filter_placements = [
                (self.resource_set_label, 0, 0),
                (self.resource_set_combo, 0, 1),
                (self.resource_search_label, 0, 2),
                (self.resource_search, 0, 3),
                (self.add_to_schedule_btn, 0, 4),
                (self.manage_resources_btn, 0, 5),
                (self.edit_resource_btn, 0, 6),
                (self.delete_resource_btn, 0, 7),
            ]
        self._place_grid_widgets(self._net_resource_filter_layout, filter_placements)
        self._net_resource_filter_layout.setColumnStretch(3 if not compact else 4, 1)
        self._apply_schedule_table_height_hints()

    def _apply_schedule_table_height_hints(self) -> None:
        if not hasattr(self, "table") or not hasattr(self, "resources_table"):
            return
        try:
            row_height = control_height_for_font(self.table)
            for row in range(self.table.rowCount()):
                for col in range(self.table.columnCount()):
                    editor = self.table.cellWidget(row, col)
                    if editor is not None:
                        if isinstance(editor, QComboBox):
                            fit_combo_box_to_contents(editor)
                        editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                        row_height = max(row_height, font_derived_widget_height(editor))
            # Republish persistent-editor rectangles after late editor polish.
            self.table.doItemsLayout()
            self.table.verticalHeader().setMinimumSectionSize(row_height)
            self.table.verticalHeader().setDefaultSectionSize(row_height)
            self.table.horizontalHeader().setMinimumHeight(
                font_derived_widget_height(self.table.horizontalHeader())
            )
            row_count = max(1, int(self.table.rowCount()))
            visible_rows = max(4, min(row_count, 10))
            self.table.setMaximumHeight(
                item_view_height_for_rows(self.table, visible_rows=visible_rows)
            )
            self.resources_table.setMinimumHeight(
                item_view_height_for_rows(self.resources_table, visible_rows=4)
            )
        except Exception:
            pass

    def _refresh_freqplanner_source_combo(self) -> None:
        if not hasattr(self, "schedule_source_combo"):
            return
        selected = selected_source_set_id(self.settings, SELECTED_HF_NET_SOURCE_SET_KEY)
        self.schedule_source_combo.blockSignals(True)
        self.schedule_source_combo.clear()
        self.schedule_source_combo.addItem("Active Net Schedule", LIVE_SOURCE_SET_ID)
        for row in source_sets_for_category(self.settings, HF_NET_SOURCE_SETS_KEY, HF_NET_SOURCE_CATEGORY):
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
        name = str(self.schedule_source_combo.currentText() or "Active Net Schedule").strip()
        try:
            usage = plan_source_usage_summary(
                self.plan_context_service.store,
                category=HF_NET_SOURCE_CATEGORY,
                set_id=set_id,
                live_label="hf_nets",
            )
            usage_text = str(usage.get("text") or "").strip()
        except Exception as exc:
            log.debug("HF Nets: source usage summary skipped: %s", exc)
            usage_text = "Usage: --"
        self.source_usage_label.setText(f"<b>Editing:</b> {name or 'Net schedule'} | {usage_text}")
        self.source_usage_label.setToolTip(
            "Named net schedules stay linked to plans by default. Updating this source refreshes dependent plans after RF Guard review."
        )

    def _refresh_directory_subscription_status(self) -> None:
        """Surface directory changes without applying them to HF schedule rows."""
        if not hasattr(self, "subscription_status_label") or not hasattr(self, "table"):
            return
        from freqinout.core.hf_net_subscription import subscription_update_status

        store = ResourceCatalogStore(self._db_path())
        notices = []
        for row_index in range(min(self.table.rowCount(), 200)):
            widget = self.table.cellWidget(row_index, self.COL_SELECT)
            if not isinstance(widget, QWidget):
                continue
            key = str(widget.property("net_session_key") or "").strip()
            if not key:
                continue
            row = {name: widget.property(name) for name in ("net_session_key", "accepted_session_version_hash", "accepted_snapshot_json")}
            status = subscription_update_status(store, row)
            if status.state not in {"current", "unlinked"}:
                fields = ", ".join(str(getattr(diff, "field_name", "change")) for diff in status.diffs[:4])
                notices.append(f"{key}: {status.warning}{' Fields: ' + fields if fields else ''}")
        if notices:
            self.subscription_status_label.setText("Directory review required — " + " | ".join(notices[:4]) + ". Apply changes only through normal HF Save Schedule review.")
            self.subscription_status_label.setVisible(True)
        else:
            self.subscription_status_label.setVisible(False)

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
        self.settings.set(SELECTED_HF_NET_SOURCE_SET_KEY, set_id)
        self._update_source_action_state()
        self._update_source_usage_label()
        try:
            self.settings.save()
        except Exception:
            pass
        self._load_selected_freqplanner_source_now()
        self._refresh_freq_planner()

    def _update_source_action_state(self) -> None:
        set_id = self._selected_freqplanner_source_id()
        is_saved = bool(set_id and set_id != LIVE_SOURCE_SET_ID)
        if hasattr(self, "delete_source_btn"):
            self.delete_source_btn.setEnabled(is_saved)
        if hasattr(self, "rename_source_btn"):
            self.rename_source_btn.setEnabled(is_saved)

    def _selected_freqplanner_source_id(self) -> str:
        if not hasattr(self, "schedule_source_combo"):
            return str(getattr(self, "_editing_freqplanner_source_id", "") or LIVE_SOURCE_SET_ID)
        set_id = str(self.schedule_source_combo.currentData() or "").strip()
        return set_id or str(getattr(self, "_editing_freqplanner_source_id", "") or LIVE_SOURCE_SET_ID)

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
        draft_name = current_name if current_index < 0 or current_name != selected_label else ""
        self._editing_freqplanner_source_id = LIVE_SOURCE_SET_ID
        self.schedule_source_combo.blockSignals(True)
        self.schedule_source_combo.setCurrentIndex(-1)
        self.schedule_source_combo.setEditText(draft_name)
        self.schedule_source_combo.blockSignals(False)
        self.settings.set(SELECTED_HF_NET_SOURCE_SET_KEY, LIVE_SOURCE_SET_ID)
        try:
            self.settings.save()
        except Exception:
            pass
        self._update_source_action_state()
        self._update_source_usage_label()
        line_edit = self.schedule_source_combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("New HF Net schedule name")
            line_edit.setFocus(Qt.OtherFocusReason)
        self._load_source_rows_into_table([])
        self._refresh_freq_planner()

    def open_hf_net_subscription(self, session_keys: Tuple[str, ...] | List[str] = ()) -> None:
        """Open the source-first directory workflow without saving or commanding.

        The dialog selects a named destination.  The canonical service creates
        reviewed schedule rows; this tab then uses its existing editor and Save
        Schedule/RF Guard/reprojection path for the actual subscription write.
        """
        from freqinout.gui.hf_net_subscription_dialog import HfNetSubscriptionDialog

        store = ResourceCatalogStore(self._db_path())
        dialog = HfNetSubscriptionDialog(store, self.settings, self, initial_session_keys=session_keys)
        if dialog.exec() != QDialog.Accepted or dialog.selection is None:
            return
        self._add_hf_subscription_draft(store, dialog.selection.destination_id, dialog.selection.destination_name, dialog.selection.session_keys)

    def _open_add_hf_net_workflow(self) -> None:
        self.open_hf_net_subscription()

    def open_directory_subscription(self, session_keys: Tuple[str, ...] | List[str]) -> None:
        """Stable Net Directory handoff target for canonical session keys."""
        self.open_hf_net_subscription(session_keys)

    def open_directory_schedule(self, net_session_key: str) -> None:
        """Open the named HF source that already follows a directory session."""
        from freqinout.core.hf_net_subscription import hf_net_destination_for_session

        destination = hf_net_destination_for_session(self.settings, net_session_key)
        if destination is None:
            self.net_resources_hint.setText("This directory session is not present in a named HF Net schedule.")
            return
        destination_id, destination_name = destination
        index = self.schedule_source_combo.findData(destination_id)
        if index < 0:
            self._refresh_freqplanner_source_combo()
            index = self.schedule_source_combo.findData(destination_id)
        if index >= 0:
            self.schedule_source_combo.setCurrentIndex(index)
            self.net_resources_hint.setText(f"Opened '{destination_name}' for the selected directory session.")

    def _add_hf_subscription_draft(self, store: ResourceCatalogStore, destination_id: str, destination_name: str, session_keys: Tuple[str, ...]) -> None:
        """Place service-built drafts in the existing named-source editor only."""
        from freqinout.core.hf_net_subscription import build_hf_subscription_drafts

        destination_id = str(destination_id or "").strip()
        if not destination_id or destination_id == LIVE_SOURCE_SET_ID:
            QMessageBox.warning(self, "Add HF Net", "Choose a named HF Net schedule destination before adding sessions.")
            return
        index = self.schedule_source_combo.findData(destination_id)
        if index < 0:
            QMessageBox.warning(self, "Add HF Net", "The selected named HF Net schedule is no longer available. Refresh and choose it again.")
            return
        # Selecting the target preserves the established unsaved-change prompt
        # and source loading behavior before any canonical draft is inserted.
        self.schedule_source_combo.setCurrentIndex(index)
        if self._selected_freqplanner_source_id() != destination_id:
            return
        try:
            drafts = build_hf_subscription_drafts(store, session_keys)
        except Exception as exc:
            QMessageBox.warning(self, "Add HF Net", f"Could not prepare published sessions:\n{exc}")
            return
        existing_keys = set()
        for row_index in range(self.table.rowCount()):
            widget = self.table.cellWidget(row_index, self.COL_SELECT)
            if isinstance(widget, QWidget):
                key = str(widget.property("net_session_key") or "").strip()
                if key:
                    existing_keys.add(key)
        added, duplicates, warnings = 0, 0, []
        for draft in drafts:
            if draft.net_session_key in existing_keys:
                duplicates += 1
                continue
            self._add_row(self._to_view_row(dict(draft.schedule_row)))
            existing_keys.add(draft.net_session_key)
            added += 1
            warnings.extend(draft.warnings)
        if added:
            self._set_dirty(True)
            self._apply_schedule_table_height_hints()
            self._schedule_net_sop_conflict_refresh(force=True)
            self._refresh_directory_subscription_status()
        review = " Review the highlighted recurrence/time, target/radio, early check-in, mode, frequency, and conflict policy before Save Schedule."
        warning_text = f" Warnings: {'; '.join(dict.fromkeys(warnings))}" if warnings else ""
        self.net_resources_hint.setText(f"Added {added} canonical session draft(s) to '{destination_name}'. Skipped {duplicates} duplicate(s).{review}{warning_text}")

    def _selected_freqplanner_source_row(self) -> Optional[Dict[str, Any]]:
        if not hasattr(self, "schedule_source_combo"):
            return None
        set_id = str(self.schedule_source_combo.currentData() or "").strip()
        if not set_id:
            set_id = str(getattr(self, "_editing_freqplanner_source_id", "") or LIVE_SOURCE_SET_ID)
        return source_set_row_by_id_for_category(
            self.settings,
            HF_NET_SOURCE_SETS_KEY,
            HF_NET_SOURCE_CATEGORY,
            set_id,
        )

    def _current_freqplanner_source_name(self) -> str:
        if not hasattr(self, "schedule_source_combo"):
            return ""
        text = str(self.schedule_source_combo.currentText() or "").strip()
        if text == "Active Net Schedule":
            return ""
        return text

    @staticmethod
    def _default_net_schedule_name() -> str:
        return "HF Net Schedule"

    @staticmethod
    def _format_age_label(value: Any, *, now: Optional[datetime.datetime] = None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "--"
        try:
            dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            dt = dt.astimezone(datetime.timezone.utc)
            current = now or datetime.datetime.now(datetime.timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=datetime.timezone.utc)
            seconds = max(0, int((current.astimezone(datetime.timezone.utc) - dt).total_seconds()))
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
            self._raw_rows = rows
            for row in self._raw_rows:
                self._add_row(self._to_view_row(row))
            self._net_name_history = sorted(
                {r.get("net_name", "") for r in rows if isinstance(r, dict) and r.get("net_name")}
            )
            self._update_clock_labels()
            self._resize_table_columns()
            self._saved_rows_signature = self._rows_signature(self._raw_rows)
            self._set_dirty(False)
            self._schedule_net_sop_conflict_refresh(force=True)
            self._refresh_directory_subscription_status()
        finally:
            self._suspend_dirty_tracking = False
        self._apply_schedule_table_height_hints()

    def _load_selected_freqplanner_source_now(self) -> None:
        row = self._selected_freqplanner_source_row()
        if row is None:
            self._load()
            return
        self._load_source_rows_into_table([dict(item) for item in row.get("rows", []) if isinstance(item, dict)])

    def _sync_selected_freqplanner_source_table(self) -> None:
        if bool(getattr(self, "_dirty", False)):
            return
        row = self._selected_freqplanner_source_row()
        if row is None:
            return
        source_rows = [dict(item) for item in row.get("rows", []) if isinstance(item, dict)]
        try:
            current_rows = [self._strip_internal_row(dict(item)) for item in self._collect_rows()]
        except Exception:
            current_rows = []
        if self._rows_signature(source_rows) == self._rows_signature(current_rows):
            return
        self._load_source_rows_into_table(source_rows)

    def _on_load_freqplanner_source_clicked(self) -> None:
        if not self._confirm_discard_unsaved_source_load():
            return
        self._load_selected_freqplanner_source_now()

    def _confirm_discard_unsaved_source_load(self) -> bool:
        if not bool(getattr(self, "_dirty", False)):
            return True
        response = QMessageBox.question(
            self,
            "Load Schedule",
            "Load the selected HF Net schedule? Unsaved edits in the current table will be discarded.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return response == QMessageBox.Yes

    def _prompt_for_freqplanner_source_name(self, title: str, label: str, default_name: str) -> Tuple[str, bool]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        prompt = QLabel(label)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        name_edit = QLineEdit(str(default_name or "").strip())
        name_edit.setObjectName("netScheduleFreqPlannerSourceNameEdit")
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

    def _source_rows_for_freqplanner_snapshot(self) -> List[Dict[str, Any]]:
        return [self._strip_internal_row(dict(row)) for row in self._collect_rows()]

    def _on_save_freqplanner_source_clicked(self) -> None:
        try:
            rows = self._source_rows_for_freqplanner_snapshot()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Net Schedule", str(exc))
            return
        if not rows:
            QMessageBox.warning(
                self,
                "No HF Net Rows",
                "Add at least one HF Net row before saving this schedule.",
            )
            return
        selected = self._selected_freqplanner_source_row()
        existing_id = int(selected.get("db_id", 0) or 0) if selected else 0
        name = self._current_freqplanner_source_name()
        if not name:
            name = self._default_net_schedule_name()
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
                    "Use Rename Schedule to change the HF Net schedule name without updating row data.",
                )
                return
        if not selected:
            existing_id = 0
        if existing_id and not self._confirm_rf_guard_source_update(
            HF_NET_SOURCE_CATEGORY,
            f"plan:{existing_id}",
            rows,
            name,
        ):
            return
        try:
            saved = save_source_schedule(
                self.settings,
                HF_NET_SOURCE_CATEGORY,
                SELECTED_HF_NET_SOURCE_SET_KEY,
                name,
                rows,
                existing_plan_id=existing_id or None,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", f"Could not save HF Net schedule:\n{exc}")
            return
        updated_plans: List[Dict[str, Any]] = []
        saved_id = str(saved.get("id") or "").strip()
        if saved_id:
            try:
                updated_plans = reproject_frequency_plans_for_source_update(
                    self.settings,
                    HF_NET_SOURCE_CATEGORY,
                    saved_id,
                    rows,
                )
            except Exception as exc:
                log.exception("HF Nets: failed refreshing dependent Frequency Plans.")
                QMessageBox.warning(
                    self,
                    "Plan Refresh Warning",
                    "The HF Net schedule was saved, but CommsPlan could not refresh dependent Frequency Plans.\n\n"
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
            try:
                self._load_source_rows_into_table(rows)
            except Exception:
                pass
        self._refresh_freq_planner()
        verb = "Updated" if existing_id else "Saved"
        plan_note = f" Refreshed {len(updated_plans)} dependent Frequency Plan(s)." if updated_plans else ""
        QMessageBox.information(
            self,
            f"HF Net Schedule {verb}",
            f"{verb} '{saved['name']}' with {len(rows)} HF Net row(s).{plan_note} Select it in Plan Builder.",
        )

    def _on_rename_freqplanner_source_clicked(self) -> None:
        row = self._selected_freqplanner_source_row()
        if row is None:
            QMessageBox.information(self, "Rename Schedule", "Select a saved HF Net schedule before renaming.")
            return
        new_name = self._current_freqplanner_source_name()
        old_name = str(row.get("name") or "").strip()
        if not new_name:
            QMessageBox.warning(self, "Rename Schedule", "Type a clear HF Net schedule name before renaming.")
            return
        if new_name == old_name:
            QMessageBox.information(self, "Rename Schedule", f"'{new_name}' is already the selected schedule name.")
            return
        try:
            saved = rename_source_schedule(
                self.settings,
                HF_NET_SOURCE_CATEGORY,
                SELECTED_HF_NET_SOURCE_SET_KEY,
                str(row.get("id") or ""),
                new_name,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Rename Failed", f"Could not rename HF Net schedule:\n{exc}")
            return
        self._refresh_freqplanner_source_combo()
        self._refresh_freq_planner()
        QMessageBox.information(self, "HF Net Schedule Renamed", f"Renamed schedule to '{saved['name']}'.")

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
            log.exception("Net Schedule: RF Guard impact scan failed.")
            response = QMessageBox.question(
                self,
                "RF Guard Check Unavailable",
                "RF Guard could not check assigned master schedules before updating this HF Net schedule.\n\n"
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
            body + "\n\nSave this HF Net schedule update anyway?",
            QMessageBox.Save | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return response == QMessageBox.Save

    def _on_delete_freqplanner_source_clicked(self) -> None:
        row = self._selected_freqplanner_source_row()
        if row is None:
            return
        name = str(row.get("name") or "selected HF Net schedule")
        response = QMessageBox.question(
            self,
            "Delete HF Net Schedule",
            f"Delete '{name}'? This removes the named schedule but does not change the live HF Net schedule.",
            QMessageBox.Delete | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if response != QMessageBox.Delete:
            return
        try:
            delete_source_schedule(
                self.settings,
                HF_NET_SOURCE_SETS_KEY,
                SELECTED_HF_NET_SOURCE_SET_KEY,
                str(row.get("id") or ""),
            )
        except Exception as exc:
            QMessageBox.critical(self, "Delete Failed", f"Could not delete HF Net schedule:\n{exc}")
            return
        if hasattr(self, "schedule_source_combo"):
            self._refresh_freqplanner_source_combo()
        self._refresh_freq_planner()

    def _resize_table_columns(self) -> None:
        try:
            self.table.resizeColumnsToContents()
            self.resources_table.resizeColumnsToContents()
            self._apply_net_compact_table_sizing(
                compact=self._net_responsive_mode_for_width(int(self.width() or 0)) == "compact"
            )
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

    def _apply_net_compact_table_sizing(self, *, compact: bool) -> None:
        if not hasattr(self, "table") or not hasattr(self, "resources_table"):
            return
        show_all = bool(getattr(self, "view_edit_btn", None) and self.view_edit_btn.isChecked())
        if compact and not show_all:
            self._set_table_resize_modes(
                self.table,
                {
                    self.COL_SELECT,
                    self.COL_DAY,
                    self.COL_GROUP,
                    self.COL_MODE,
                    self.COL_BAND,
                    self.COL_FREQ,
                    self.COL_START,
                    self.COL_END,
                },
                set(),
            )
            self.table.setColumnWidth(
                self.COL_NETNAME, self.table.fontMetrics().horizontalAdvance("M" * 20)
            )
        else:
            self._set_table_resize_modes(
                self.table,
                {
                    self.COL_SELECT,
                    self.COL_DAY,
                    self.COL_GROUP,
                    self.COL_RECURRENCE,
                    self.COL_MONTH_WEEKS,
                    self.COL_MODE,
                    self.COL_BAND,
                    self.COL_FREQ,
                    self.COL_START,
                    self.COL_END,
                    self.COL_EARLY,
                    self.COL_FLDIGI_MODE,
                    self.COL_FLDIGI_OFFSET,
                    self.COL_AUTOTUNE,
                    self.COL_TARGET_SCOPE,
                },
                {self.COL_NETNAME, self.COL_TARGET},
            )

        self._set_table_resize_modes(
            self.resources_table,
            {
                self.RES_COL_SOURCE,
                self.RES_COL_SET,
                self.RES_COL_DAY,
                self.RES_COL_RECURRENCE,
                self.RES_COL_MONTH_WEEKS,
                self.RES_COL_MODE,
                self.RES_COL_BAND,
                self.RES_COL_FREQ,
                self.RES_COL_START,
                self.RES_COL_END,
                self.RES_COL_EARLY,
                self.RES_COL_FLDIGI_MODE,
                self.RES_COL_FLDIGI_OFFSET,
                self.RES_COL_UPDATED,
            },
            {self.RES_COL_GROUP, self.RES_COL_NETNAME, self.RES_COL_COVERAGE, self.RES_COL_COMMENT},
        )

    def _apply_compact_schedule_view(self, show_all: bool | None = None) -> None:
        if not hasattr(self, "table"):
            return
        if show_all is None:
            show_all = bool(getattr(self, "view_edit_btn", None) and self.view_edit_btn.isChecked())
        for col in range(self.table.columnCount()):
            self.table.setColumnHidden(col, not show_all and col not in self.COMPACT_VISIBLE_COLUMNS)
        self._apply_net_compact_table_sizing(
            compact=self._net_responsive_mode_for_width(int(self.width() or 0)) == "compact"
        )
        if hasattr(self, "view_edit_btn"):
            self.view_edit_btn.setToolTip(
                "Hide advanced schedule fields for normal net scanning."
                if show_all
                else "Show all editable fields for the selected net schedule rows."
            )
            try:
                self.view_edit_btn.setStyleSheet(
                    button_style("info" if show_all else "muted", resolve_theme(self.settings))
                )
            except Exception:
                pass

    def _refresh_schedule_target_catalogs(self) -> None:
        try:
            store = MultiRadioStore(settings_db_path())
            self.device_profiles = [dict(row) for row in store.list_device_profiles()]
            self.operating_profiles = [dict(row) for row in store.list_operating_profiles()]
        except Exception as e:
            log.debug("Net Schedule: failed loading target catalogs: %s", e)
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
            "Frequency Plan rows apply only when the station-default radio carries that assigned plan. "
            "Full radio-owned schedule orchestration is a later-phase feature and is not modeled by these compatibility targets."
        )

    def _populate_target_value_combo(
        self,
        combo: QComboBox,
        scope: str,
        *,
        target_device_profile_id: Optional[int] = None,
        target_operating_profile_id: Optional[int] = None,
    ) -> None:
        prev_block = combo.blockSignals(True)
        try:
            combo.clear()
            combo.setToolTip(self._target_scope_tooltip())
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
            scope, target_device_profile_id, target_operating_profile_id = self._selected_schedule_target(row_index)
            self._populate_target_value_combo(
                target_widget,
                scope,
                target_device_profile_id=target_device_profile_id,
                target_operating_profile_id=target_operating_profile_id,
            )

    def on_settings_saved(self) -> None:
        """
        Reload settings and operating groups after Save Settings.
        Refresh group/band/mode combos in existing rows.
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
        self._load_operating_groups()
        self._refresh_schedule_target_catalogs()
        prev_suppress = self._suppress_autostart
        self._suppress_autostart = True
        prev_dirty = self._suspend_dirty_tracking
        self._suspend_dirty_tracking = True
        try:
            for r in range(self.table.rowCount()):
                group_combo: QComboBox = self.table.cellWidget(r, self.COL_GROUP)  # type: ignore
                band_combo: QComboBox = self.table.cellWidget(r, self.COL_BAND)  # type: ignore
                current_group = group_combo.currentText().strip() if group_combo else ""
                if group_combo:
                    group_names = sorted({g.get("group", "") for g in self.operating_groups if g.get("group")})
                    group_combo.blockSignals(True)
                    group_combo.clear()
                    group_combo.addItems(group_names)
                    if current_group and current_group in group_names:
                        group_combo.setCurrentText(current_group)
                    group_combo.blockSignals(False)
                if band_combo:
                    self._populate_band_combo(band_combo, current_group)
                    # keep current band if still valid
                    current_band = band_combo.currentText()
                    if current_band and band_combo.findText(current_band) >= 0:
                        band_combo.setCurrentText(current_band)
                self._update_mode_freq(r, preserve_frequency=True)
            self._refresh_schedule_target_widgets()
        finally:
            self._suppress_autostart = prev_suppress
            self._suspend_dirty_tracking = prev_dirty
        # keep net name history intact
        self._update_clock_labels()
        self._refresh_resource_set_combo()
        self._refresh_resources_table()
        self._apply_theme()
        self._resize_table_columns()
        self._refresh_freqplanner_source_combo()
        self._sync_selected_freqplanner_source_table()

    def _refresh_freq_planner(self) -> None:
        try:
            self.schedule_saved.emit()
        except Exception:
            pass

    def on_sop_data_changed(self) -> None:
        self._bump_net_sop_conflict_scan_epoch()
        if not self.isVisible():
            self._pending_sop_conflict_refresh = True
            return
        self._schedule_net_sop_conflict_refresh(force=True)

    def on_tab_activated(self) -> None:
        with perf_span(
            "net_schedule.on_tab_activated",
            settings=self.settings,
            meta={"rows": int(self.table.rowCount())},
            min_ms=0.0,
        ):
            self._refresh_freqplanner_source_combo()
            self._sync_selected_freqplanner_source_table()
            self._refresh_schedule_target_widgets()
            if self._pending_sop_conflict_refresh:
                self._pending_sop_conflict_refresh = False
                self._schedule_net_sop_conflict_refresh(force=True)

    def _apply_theme(self) -> None:
        theme = resolve_theme(self.settings)
        menu_font_css = font_css(self.add_btn.font())
        self._update_time_toggle_style(theme)
        self.help_btn.setStyleSheet(button_style("secondary", theme))
        self.add_btn.setStyleSheet(button_style("muted", theme))
        self.del_btn.setStyleSheet(button_style("muted", theme))
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
        self.move_to_resources_btn.setStyleSheet(button_style("muted", theme))
        self.export_btn.setStyleSheet(button_style("muted", theme))
        self.manage_net_sop_policies_btn.setStyleSheet(button_style("muted", theme))
        self._refresh_save_button_state(theme)
        self.add_to_schedule_btn.setStyleSheet(button_style("muted", theme) + menu_font_css)
        self.add_to_schedule_btn.setFont(self.add_btn.font())
        self.manage_resources_btn.setStyleSheet(button_style("muted", theme) + menu_font_css)
        self.manage_resources_btn.setFont(self.add_btn.font())
        self.edit_resource_btn.setStyleSheet(button_style("muted", theme))
        self.delete_resource_btn.setStyleSheet(button_style("muted", theme))
        self._update_delete_button_state()
        self._update_resource_action_state()

    def apply_theme(self) -> None:
        self._apply_theme()
        self._queue_net_geometry_refresh()

    def _rows_signature(self, rows: List[Dict[str, Any]]) -> str:
        normalized: List[Dict[str, Any]] = []
        for raw in rows:
            row = normalize_schedule_target_fields(self._strip_internal_row(raw))
            recurrence = str(row.get("recurrence") or "Weekly").strip()
            if recurrence == "Monthly":
                recurrence = "Periodic"
            if recurrence not in ("Weekly", "Daily", "Periodic"):
                recurrence = "Weekly"
            month_weeks = self._format_month_weeks(str(row.get("month_weeks") or ""))
            if recurrence != "Periodic":
                month_weeks = ""
            normalized.append(
                {
                    "day_utc": self._normalize_day(str(row.get("day_utc") or "")),
                    "recurrence": recurrence,
                    "month_weeks": month_weeks,
                    "group_name": str(row.get("group_name") or ""),
                    "band": str(row.get("band") or "").strip().upper(),
                    "mode": str(row.get("mode") or "").strip(),
                    "frequency": self._normalize_freq_key(row.get("frequency")),
                    "operating_group_frequency_key": str(row.get("operating_group_frequency_key") or ""),
                    "start_utc": self._normalize_hhmm(str(row.get("start_utc") or "")),
                    "end_utc": self._normalize_hhmm(str(row.get("end_utc") or "")),
                    "early_checkin": str(row.get("early_checkin") or ""),
                    "net_name": str(row.get("net_name") or ""),
                    "auto_tune": bool(row.get("auto_tune", False)),
                    "fldigi_mode": str(row.get("fldigi_mode") or "").strip(),
                    "fldigi_offset": str(row.get("fldigi_offset") or "").strip(),
                    "target_scope": str(row.get("target_scope") or TARGET_SCOPE_STATION),
                    "target_device_profile_id": row.get("target_device_profile_id"),
                    "target_operating_profile_id": row.get("target_operating_profile_id"),
                }
            )
        return json.dumps(normalized, sort_keys=True)

    def _refresh_save_button_state(self, theme: Optional[Dict[str, str]] = None) -> None:
        if theme is None:
            theme = resolve_theme(self.settings)
        if self._blocking_net_sop_conflict_count > 0:
            role = "eligible_warning"
            self.save_btn.setToolTip(
                f"{self._blocking_net_sop_conflict_count} Net/SOP conflict(s) require Net Priority before save."
            )
        else:
            role = "eligible_success" if self._dirty else "muted"
            self.save_btn.setToolTip(
                "Save the visible rows as the selected named HF Net schedule, or create a new named schedule for Plan Builder."
            )
        self.save_btn.setStyleSheet(button_style(role, theme))

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = bool(dirty)
        self._refresh_save_button_state()

    def _mark_dirty(self, *_args) -> None:
        if self._suspend_dirty_tracking:
            return
        try:
            current_sig = self._rows_signature(self._collect_rows())
            self._set_dirty(current_sig != self._saved_rows_signature)
        except Exception:
            # While row edits are incomplete (e.g., invalid time), keep save highlighted.
            self._set_dirty(True)
        self._schedule_net_sop_conflict_refresh()

    def _on_table_item_changed(self, _item: QTableWidgetItem) -> None:
        self._mark_dirty()

    def _has_delete_selection(self) -> bool:
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, self.COL_SELECT)
            if isinstance(w, QCheckBox) and w.isChecked():
                return True
            if isinstance(w, QWidget):
                chk = w.findChild(QCheckBox)
                if chk is not None and chk.isChecked():
                    return True
        return False

    def _update_delete_button_state(self) -> None:
        theme = resolve_theme(self.settings)
        has_selection = self._has_delete_selection()
        self.del_btn.setEnabled(has_selection)
        self.move_to_resources_btn.setEnabled(has_selection)
        role = "eligible_danger" if has_selection else "muted"
        self.del_btn.setStyleSheet(button_style(role, theme))
        self.move_to_resources_btn.setStyleSheet(button_style("eligible_info" if has_selection else "muted", theme))

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return float(path.stat().st_mtime) if path.exists() else 0.0
        except Exception:
            return 0.0

    def _bump_net_sop_conflict_scan_epoch(self) -> None:
        self._net_sop_conflict_cache_epoch += 1
        self._net_sop_conflict_cache.clear()

    def _schedule_net_sop_conflict_refresh(self, *, force: bool = False) -> None:
        timer = self._conflict_refresh_timer
        if timer is None:
            return
        if self._suspend_dirty_tracking and not force:
            return
        if not self.isVisible():
            self._pending_sop_conflict_refresh = True
            return
        self._pending_sop_conflict_refresh = False
        timer.start()

    @staticmethod
    def _net_sop_policy_is_net_priority(value: Any) -> bool:
        return str(value or "").strip().upper() == "NET_PRIORITY"

    def _scan_net_sop_conflicts(
        self,
        *,
        net_rows_override: Optional[List[Dict[str, Any]]] = None,
        horizon_days: int = 35,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Set[str]]:
        rows: List[Dict[str, Any]]
        if isinstance(net_rows_override, list):
            rows = [self._strip_internal_row(r) for r in net_rows_override if isinstance(r, dict)]
        else:
            try:
                rows = [self._strip_internal_row(r) for r in self._collect_rows()]
            except Exception:
                return [], [], set()
        if not rows:
            return [], [], set()
        cache_key = (
            self._rows_signature(rows),
            self._safe_mtime(self._db_path()),
            int(self._net_sop_conflict_cache_epoch),
            int(max(1, horizon_days)),
        )
        cached = self._net_sop_conflict_cache.get(cache_key)
        if cached is not None:
            self._net_sop_conflict_cache.move_to_end(cache_key, last=True)
            cached_conflicts, cached_blocking, cached_sigs = cached
            return list(cached_conflicts), list(cached_blocking), set(cached_sigs)
        with perf_span(
            "net_schedule.scan_net_sop_conflicts",
            settings=self.settings,
            meta={"rows": len(rows), "horizon_days": int(max(1, horizon_days)), "cache": "miss"},
            min_ms=5.0,
        ):
            try:
                conflicts = self._sop_manager.collect_active_net_sop_conflicts(
                    horizon_days=max(1, int(horizon_days)),
                    net_rows_override=rows,
                )
            except Exception as e:
                log.debug("Net Schedule: active SOP conflict scan failed: %s", e)
                return [], [], set()
            blocking: List[Dict[str, Any]] = []
            signatures: Set[str] = set()
            for row in conflicts:
                if not isinstance(row, dict):
                    continue
                if self._net_sop_policy_is_net_priority(row.get("resolved_policy")):
                    continue
                blocking.append(row)
                sig = str(row.get("net_row_signature") or "").strip()
                if sig:
                    signatures.add(sig)
            self._net_sop_conflict_cache[cache_key] = (list(conflicts), list(blocking), set(signatures))
            self._net_sop_conflict_cache.move_to_end(cache_key, last=True)
            while len(self._net_sop_conflict_cache) > max(1, int(self._net_sop_conflict_cache_limit)):
                self._net_sop_conflict_cache.popitem(last=False)
            return conflicts, blocking, signatures

    def _refresh_net_sop_conflict_highlighting(self) -> None:
        with perf_span(
            "net_schedule.refresh_conflict_highlighting",
            settings=self.settings,
            meta={"rows": int(self.table.rowCount())},
            min_ms=5.0,
        ):
            rows_utc: Optional[List[Dict[str, Any]]] = None
            try:
                rows_utc = [self._strip_internal_row(r) for r in self._collect_rows()]
            except Exception:
                rows_utc = None

            if rows_utc is None:
                blocking: List[Dict[str, Any]] = []
                conflict_sigs: Set[str] = set()
            else:
                _, blocking, conflict_sigs = self._scan_net_sop_conflicts(net_rows_override=rows_utc)
            self._blocking_net_sop_conflict_count = len(blocking)
            self._blocking_net_sop_conflict_signatures = set(conflict_sigs)
            prev_block = self.table.blockSignals(True)
            try:
                theme = resolve_theme(self.settings)
                warn = QColor(str(theme.get("warning", "#C99700")))
                warn.setAlpha(64)
                try:
                    ui_rows = self._collect_rows_by_ui_index(rows_override=rows_utc)
                except Exception:
                    ui_rows = {}
                for r in range(self.table.rowCount()):
                    for col in (self.COL_FREQ, self.COL_START, self.COL_END):
                        item = self.table.item(r, col)
                        if item is None:
                            continue
                        base_tip = str(item.data(Qt.UserRole) or "")
                        item.setData(Qt.BackgroundRole, None)
                        item.setToolTip(base_tip)
                for ui_row, row in ui_rows.items():
                    sig = str(self._sop_manager._net_row_signature(row))
                    is_blocking = sig in conflict_sigs
                    for col in (self.COL_FREQ, self.COL_START, self.COL_END):
                        item = self.table.item(ui_row, col)
                        if item is None:
                            continue
                        base_tip = str(item.data(Qt.UserRole) or "")
                        if is_blocking:
                            item.setBackground(warn)
                            item.setToolTip(
                                (base_tip + "\n" if base_tip else "")
                                + "Conflicts with active SOP. Set Net Priority to keep this overlap."
                            )
            finally:
                self.table.blockSignals(prev_block)
            self._refresh_save_button_state()

    # --------- helpers: time / primary groups --------- #
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

    def _update_time_toggle_style(self, theme: Optional[Dict[str, str]] = None) -> None:
        if theme is None:
            theme = resolve_theme(self.settings)
        role = "info" if not self._show_local else "muted"
        self.time_toggle_btn.setStyleSheet(button_style(role, theme))

    def _setup_clock_timer(self):
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock_labels)
        self._clock_timer.start(1000)
        self._update_clock_labels()

    def _set_headers(self):
        mode_label = "Local" if self._show_local else "UTC"
        self.table.setHorizontalHeaderLabels(
            [
                "Select",
                f"Day ({mode_label})",
                "Recurrence",
                "Weeks of Month",
                "Group Name",
                "Mode",
                "Band",
                "Freq (MHz)",
                f"Start ({mode_label})",
                f"End ({mode_label})",
                "Early (min)",
                "FLDigi Mode",
                "FLDigi Offset",
                "Net Name",
                "Auto-Tune",
                "Target Scope",
                "Target",
            ]
        )
        self.time_toggle_btn.setText("Times: Local" if self._show_local else "Times: UTC")
        self._update_time_toggle_style()

    # --------- time conversion helpers --------- #
    def _day_offset(self, day_name: str) -> int:
        try:
            return DAY_NAMES.index(day_name)
        except Exception:
            return 0

    def _anchor_utc_sunday(self) -> datetime.datetime:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        delta = (now_utc.weekday() + 1) % 7  # Sunday=0
        sunday = now_utc - datetime.timedelta(days=delta)
        return sunday.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=datetime.timezone.utc)

    def _anchor_local_sunday(self) -> datetime.datetime:
        tz = get_timezone(self.settings.get("timezone", "UTC") or "UTC")
        now_local = datetime.datetime.now(tz)
        delta = (now_local.weekday() + 1) % 7
        sunday = now_local - datetime.timedelta(days=delta)
        return sunday.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)

    def _convert_day_time(self, day: str, hhmm: str, to_local: bool) -> tuple[str, str]:
        """
        Convert (day, HH:MM) between UTC and local using configured timezone.
        Returns day name and hh:mm in target zone.
        """
        day = (day or "").strip()
        if not day or not hhmm:
            return day, hhmm
        try:
            h, m = hhmm.split(":")
            h_i = int(h)
            m_i = int(m)
        except Exception:
            return day, hhmm
        if day == "ALL":
            if to_local:
                anchor = self._anchor_utc_sunday()
                tz = get_timezone(self.settings.get("timezone", "UTC") or "UTC")
                dt_utc = anchor + datetime.timedelta(hours=h_i, minutes=m_i)
                dt_loc = dt_utc.astimezone(tz)
                return "ALL", dt_loc.strftime("%H:%M")
            anchor_loc = self._anchor_local_sunday()
            dt_loc = anchor_loc + datetime.timedelta(hours=h_i, minutes=m_i)
            dt_utc = dt_loc.astimezone(datetime.timezone.utc)
            return "ALL", dt_utc.strftime("%H:%M")
        if day not in DAY_NAMES:
            return day, hhmm
        idx = self._day_offset(day)
        if to_local:
            anchor = self._anchor_utc_sunday()
            tz = get_timezone(self.settings.get("timezone", "UTC") or "UTC")
            dt_utc = anchor + datetime.timedelta(days=idx, hours=h_i, minutes=m_i)
            dt_loc = dt_utc.astimezone(tz)
            return dt_loc.strftime("%A"), dt_loc.strftime("%H:%M")
        anchor_loc = self._anchor_local_sunday()
        dt_loc = anchor_loc + datetime.timedelta(days=idx, hours=h_i, minutes=m_i)
        dt_utc = dt_loc.astimezone(datetime.timezone.utc)
        return dt_utc.strftime("%A"), dt_utc.strftime("%H:%M")

    def _to_view_row(self, row: Dict) -> Dict:
        """
        Convert a UTC row to current view (local if toggled), preserving other fields.
        """
        if not self._show_local:
            return dict(row)
        day, start_local = self._convert_day_time(row.get("day_utc", ""), row.get("start_utc", ""), to_local=True)
        _, end_local = self._convert_day_time(row.get("day_utc", ""), row.get("end_utc", ""), to_local=True)
        out = dict(row)
        out["day_utc"] = day
        out["start_utc"] = start_local
        out["end_utc"] = end_local
        return out

    def _toggle_time_view(self):
        """
        Flip between UTC and Local view without changing canonical schedule semantics.
        """
        was_dirty = bool(self._dirty)
        if was_dirty:
            try:
                # Preserve in-progress user edits by normalizing the current view to UTC before flipping.
                rows_utc = self._collect_rows()
            except Exception as e:
                self._publish_time_toggle_blocked_feedback(str(e))
                return
        else:
            rows_utc = [dict(row) for row in (self._raw_rows or [])]
        self._raw_rows = rows_utc
        self._show_local = not self._show_local
        self._set_headers()
        self._suspend_dirty_tracking = True
        try:
            self.table.setRowCount(0)
            for row in self._raw_rows:
                self._add_row(self._to_view_row(row))
        finally:
            self._suspend_dirty_tracking = False
        self._update_clock_labels()
        self._resize_table_columns()
        self._set_dirty(was_dirty)
        self._schedule_net_sop_conflict_refresh(force=True)

    def _publish_time_toggle_blocked_feedback(self, detail: str = "") -> None:
        summary = "Finish the current Net Schedule row before changing the time view."
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
                    source_surface="net_schedule_tab",
                )
                return
            except Exception as e:
                log.debug("Net Schedule: failed publishing time toggle feedback: %s", e)
        try:
            status_bar = win.statusBar() if win is not None and hasattr(win, "statusBar") else None
            if status_bar is not None:
                status_bar.showMessage(summary, 6000)
                return
        except Exception:
            pass
        log.info("Net Schedule: time view change blocked; %s", detail)


    # --------- row widgets --------- #

    def _add_row(self, row_data: Dict | None = None):
        r = self.table.rowCount()
        self.table.insertRow(r)

        row_data = row_data or {}

        # Select checkbox
        sel_chk = QCheckBox()
        sel_chk.stateChanged.connect(self._update_delete_button_state)
        sel_wrap = QWidget()
        sel_layout = QHBoxLayout(sel_wrap)
        sel_layout.setContentsMargins(0, 0, 0, 0)
        sel_layout.setAlignment(Qt.AlignCenter)
        sel_layout.addWidget(sel_chk)
        if row_data.get("_resource_id") is not None:
            sel_wrap.setProperty("resource_id", int(row_data.get("_resource_id")))
        try:
            sel_wrap.setProperty("source_row_id", int(row_data.get("source_row_id") or row_data.get("_source_row_id") or 0))
        except Exception:
            sel_wrap.setProperty("source_row_id", 0)
        sel_wrap.setProperty("source_key", str(row_data.get("source_key") or row_data.get("_source_key") or "").strip())
        sel_wrap.setProperty("source_table", str(row_data.get("source_table") or "net_schedule_tab"))
        sel_wrap.setProperty("operating_group_frequency_key", str(row_data.get("operating_group_frequency_key") or ""))
        # Canonical directory subscription metadata is carried through the
        # existing HF source-row save path; it never becomes a bare legacy ID.
        for key in (
            "net_session_key",
            "frequency_resource_key",
            "accepted_session_version_hash",
            "accepted_resource_version_hash",
            "accepted_snapshot_json",
        ):
            if row_data.get(key) not in (None, ""):
                sel_wrap.setProperty(key, row_data.get(key))
        if row_data.get("_resource_set"):
            sel_wrap.setProperty("resource_set", str(row_data.get("_resource_set")))
        fld_mode = str(row_data.get("fldigi_mode") or "").strip()
        fld_off = str(row_data.get("fldigi_offset") or "").strip()
        if fld_mode:
            sel_wrap.setProperty("fldigi_mode", fld_mode)
        if fld_off:
            sel_wrap.setProperty("fldigi_offset", fld_off)
        self.table.setCellWidget(r, self.COL_SELECT, sel_wrap)

        # Day combo
        day_combo = QComboBox()
        self._set_day_options(day_combo, include_all=True)
        day_val = row_data.get("day_utc", "")
        if day_val in DAY_OPTIONS:
            day_combo.setCurrentIndex(DAY_OPTIONS.index(day_val))
        day_combo.setEditable(True)
        if day_combo.lineEdit():
            day_combo.lineEdit().setReadOnly(True)
        day_combo.setMinimumContentsLength(6)
        day_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        day_combo.currentIndexChanged.connect(lambda _, c=day_combo: self._update_day_display(c))
        day_combo.currentTextChanged.connect(self._mark_dirty)
        self._update_day_display(day_combo)
        self.table.setCellWidget(r, self.COL_DAY, day_combo)

        # Recurrence combo
        recur_combo = QComboBox()
        recur_combo.addItems(["Weekly", "Daily", "Periodic"])
        recur_val = (row_data.get("recurrence", "Weekly") or "Weekly").strip()
        if recur_val == "Monthly":
            recur_val = "Periodic"
        if recur_val == "Bi-Weekly":
            recur_val = "Weekly"
        if recur_val not in ["Weekly", "Daily", "Periodic"]:
            recur_val = "Weekly"
        recur_combo.setCurrentText(recur_val)
        recur_combo.currentTextChanged.connect(self._mark_dirty)
        self.table.setCellWidget(r, self.COL_RECURRENCE, recur_combo)
        recur_combo.setMinimumContentsLength(7)
        recur_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)

        # Month weeks summary for Periodic recurrence
        weeks_txt = (row_data.get("month_weeks") or "").strip()
        month_weeks_edit = QLineEdit()
        month_weeks_edit.setPlaceholderText("1,3,5")
        month_weeks_edit.setText(weeks_txt)
        month_weeks_edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9,\\s]*")))
        month_weeks_edit.editingFinished.connect(
            lambda w=month_weeks_edit: w.setText(self._format_month_weeks(w.text()))
        )
        month_weeks_edit.textChanged.connect(
            lambda _, w=month_weeks_edit, c=recur_combo: self._validate_month_weeks_field(w, c)
        )
        month_weeks_edit.textChanged.connect(self._mark_dirty)
        self._validate_month_weeks_field(month_weeks_edit, recur_combo)
        self.table.setCellWidget(r, self.COL_MONTH_WEEKS, month_weeks_edit)
        self._set_month_weeks_enabled(month_weeks_edit, recur_val == "Periodic")
        recur_combo.currentTextChanged.connect(
            lambda txt, row=r, w=month_weeks_edit, d=day_combo: self._on_recurrence_changed(row, txt, w, d)
        )
        self._on_recurrence_changed(r, recur_val, month_weeks_edit, day_combo)

        # Group combo
        group_combo = QComboBox()
        group_names = sorted({g.get("group", "") for g in self.operating_groups if g.get("group")})
        group_combo.addItems(group_names)
        group_val = (row_data.get("group_name") or "").strip()
        if group_val:
            if group_combo.findText(group_val) < 0:
                group_combo.addItem(group_val)
            group_combo.setCurrentText(group_val)
        group_combo.currentTextChanged.connect(self._mark_dirty)
        self.table.setCellWidget(r, self.COL_GROUP, group_combo)
        group_combo.setMinimumContentsLength(10)
        group_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)

        # Mode combo (cascades from group+band)
        mode_combo = self._set_mode_widget(r, group_combo.currentText(), "", row_data.get("mode", ""))

        # Band combo (cascades from group; fall back to BAND_ORDER)
        band_combo = QComboBox()
        self._populate_band_combo(band_combo, group_combo.currentText())
        band_val = row_data.get("band", "")
        if band_val and band_combo.findText(band_val) >= 0:
            band_combo.setCurrentText(band_val)
        elif band_combo.count() == 0:
            band_combo.addItems(BAND_ORDER)
            idx = band_combo.findText(band_val)
            if idx >= 0:
                band_combo.setCurrentIndex(idx)
        band_combo.currentTextChanged.connect(self._mark_dirty)
        self.table.setCellWidget(r, self.COL_BAND, band_combo)
        band_combo.setMinimumContentsLength(5)
        band_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)

        # Early check-in
        early_combo = QComboBox()
        early_combo.addItems(["0", "5", "10", "15"])
        early_val = str(row_data.get("early_checkin", "0"))
        idx = early_combo.findText(early_val)
        if idx >= 0:
            early_combo.setCurrentIndex(idx)
        early_combo.currentTextChanged.connect(self._mark_dirty)
        self.table.setCellWidget(r, self.COL_EARLY, early_combo)
        early_combo.setMinimumContentsLength(3)
        early_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)

        # FLDigi mode / offset (explicit per-row schedule values)
        fldigi_mode_combo = self._build_fldigi_mode_combo(str(row_data.get("fldigi_mode") or ""))
        fldigi_mode_combo.currentTextChanged.connect(self._mark_dirty)
        self.table.setCellWidget(r, self.COL_FLDIGI_MODE, fldigi_mode_combo)
        fldigi_mode_combo.setMinimumContentsLength(10)
        fldigi_mode_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        fldigi_offset_edit = QLineEdit(str(row_data.get("fldigi_offset") or ""))
        fldigi_offset_edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9]*")))
        fldigi_offset_edit.textChanged.connect(self._mark_dirty)
        self.table.setCellWidget(r, self.COL_FLDIGI_OFFSET, fldigi_offset_edit)

        # Net name edit
        net_edit = QLineEdit()
        net_val = row_data.get("net_name", "")
        net_edit.setText(net_val)
        net_edit.textChanged.connect(self._mark_dirty)
        self.table.setCellWidget(r, self.COL_NETNAME, net_edit)

        # Auto-Tune checkbox
        auto_chk = QCheckBox()
        auto_chk.setChecked(bool(row_data.get("auto_tune", False)))
        auto_chk.stateChanged.connect(self._mark_dirty)
        auto_wrap = QWidget()
        auto_layout = QHBoxLayout(auto_wrap)
        auto_layout.setContentsMargins(0, 0, 0, 0)
        auto_layout.setAlignment(Qt.AlignCenter)
        auto_layout.addWidget(auto_chk)
        self.table.setCellWidget(r, self.COL_AUTOTUNE, auto_wrap)

        target_scope_combo = QComboBox()
        target_scope_combo.setToolTip(self._target_scope_tooltip())
        target_value_combo = QComboBox()
        target_value_combo.setToolTip(self._target_scope_tooltip())
        target_scope, target_device_profile_id, target_operating_profile_id = normalize_schedule_target(
            row_data.get("target_scope"),
            target_device_profile_id=row_data.get("target_device_profile_id"),
            target_operating_profile_id=row_data.get("target_operating_profile_id"),
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
        target_value_combo.currentTextChanged.connect(self._mark_dirty)
        self.table.setCellWidget(r, self.COL_TARGET_SCOPE, target_scope_combo)
        self.table.setCellWidget(r, self.COL_TARGET, target_value_combo)

        # Freq / times as QTableWidgetItem
        def set_item(col: int, value: str | None):
            item = QTableWidgetItem(str(value) if value is not None else "")
            self.table.setItem(r, col, item)

        set_item(self.COL_FREQ, row_data.get("frequency", ""))
        set_item(self.COL_START, row_data.get("start_utc", ""))
        set_item(self.COL_END, row_data.get("end_utc", ""))

        # wiring for cascades
        def on_group_changed(text: str, self=self, row=r, band_combo=band_combo):
            self._populate_band_combo(band_combo, text)
            if band_combo.count() > 0:
                band_combo.setCurrentIndex(0)
            self._update_mode_freq(row, preserve_frequency=True)

        def on_band_changed(text: str, self=self, row=r):
            self._update_mode_freq(row, preserve_frequency=True)

        group_combo.currentTextChanged.connect(on_group_changed)
        band_combo.currentTextChanged.connect(on_band_changed)
        # Ensure initial mode/freq selection is synced to operating group data
        self._update_mode_freq(r, preserve_frequency=bool(str(row_data.get("frequency") or "").strip()))
        # For rows loaded without explicit FLDigi fields, seed defaults from matching OG.
        if not str(row_data.get("fldigi_mode") or "").strip() and not str(row_data.get("fldigi_offset") or "").strip():
            d_mode, d_offset = self._default_fldigi_for_row(
                group_combo.currentText().strip(),
                band_combo.currentText().strip(),
                self._get_combo_value(r, self.COL_MODE, ""),
            )
            if d_mode:
                fldigi_mode_combo.setCurrentText(d_mode)
            if d_offset:
                fldigi_offset_edit.setText(d_offset)
        self._update_delete_button_state()
        self._mark_dirty()
        self._queue_net_geometry_refresh()

    def _parse_month_weeks(self, txt: str) -> set[int]:
        out: set[int] = set()
        for token in (txt or "").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                val = int(token)
            except Exception:
                continue
            if 1 <= val <= 5:
                out.add(val)
        return out

    def _format_month_weeks(self, txt: str) -> str:
        cleaned = (txt or "").replace(";", ",").replace(" ", ",")
        if cleaned.isdigit() and len(cleaned) > 1:
            cleaned = ",".join(list(cleaned))
        weeks = sorted(self._parse_month_weeks(cleaned))
        return ",".join(str(w) for w in weeks)

    def _validate_month_weeks_field(self, widget: QLineEdit, recur_combo: QComboBox | None = None) -> None:
        if not isinstance(widget, QLineEdit):
            return
        if recur_combo is not None and recur_combo.currentText().strip() != "Periodic":
            widget.setStyleSheet("")
            widget.setToolTip("")
            return
        txt = widget.text().strip()
        if not txt or txt.upper() == "ALL":
            widget.setStyleSheet("")
            widget.setToolTip("")
            return
        weeks = self._parse_month_weeks(txt)
        if not weeks:
            widget.setStyleSheet("QLineEdit { border: 2px solid #C62828; }")
            widget.setToolTip("Weeks of Month must be 1-5 (comma-separated).")
            return
        widget.setStyleSheet("")
        widget.setToolTip("")

    def _get_month_weeks(self, widget: QWidget | None) -> List[int]:
        if not isinstance(widget, QLineEdit):
            return []
        return self._parse_month_weeks(self._format_month_weeks(widget.text()))

    def _set_month_weeks_enabled(self, widget: QWidget, enabled: bool) -> None:
        if not isinstance(widget, QLineEdit):
            return
        widget.setReadOnly(not enabled)
        widget.setEnabled(True)
        if not enabled:
            widget.setText("ALL")
        else:
            if widget.text().strip().upper() == "ALL":
                widget.setText("")

    def _on_recurrence_changed(
        self, row: int, recurrence: str, widget: QWidget, day_combo: QComboBox
    ) -> None:
        is_periodic = recurrence.strip() == "Periodic"
        is_daily = recurrence.strip() == "Daily"
        self._set_month_weeks_enabled(widget, is_periodic)
        if is_periodic:
            self._set_day_options(day_combo, include_all=False)
        else:
            self._set_day_options(day_combo, include_all=True)
        if is_periodic and not self._get_month_weeks(widget):
            if isinstance(widget, QLineEdit):
                widget.setText("1")
        if is_daily:
            day_combo.setCurrentText("ALL")
            day_combo.setEnabled(False)
        else:
            day_combo.setEnabled(True)
        self._update_day_display(day_combo)

    def _get_combo_value(self, row: int, col: int, default: str = "") -> str:
        w = self.table.cellWidget(row, col)
        if isinstance(w, QComboBox):
            if col == self.COL_FREQ:
                value = w.currentData(Qt.UserRole + 1)
                return str(value if value is not None else w.currentText()).strip()
            if col == self.COL_DAY:
                data = w.currentData(Qt.UserRole)
                if data:
                    return str(data).strip()
            return w.currentText().strip()
        if isinstance(w, QLineEdit):
            return w.text().strip() if w.text() else default
        item = self.table.item(row, col)
        if item is not None:
            return item.text().strip()
        return default

    def _update_day_display(self, combo: QComboBox) -> None:
        if combo is None:
            return
        full = combo.currentText()
        short = "ALL" if full == "ALL" else (full[:3] or "").upper()
        combo.setItemData(combo.currentIndex(), full, Qt.UserRole)
        if combo.lineEdit():
            combo.lineEdit().setText(short)

    def _set_day_options(self, combo: QComboBox, *, include_all: bool) -> None:
        if combo is None:
            return
        current_full = combo.currentData(Qt.UserRole) or combo.currentText()
        options = DAY_OPTIONS if include_all else DAY_NAMES
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(options)
        if current_full in options:
            combo.setCurrentText(current_full)
        combo.blockSignals(False)

    def _delete_rows(self):
        selected = set()
        # Prefer checkboxes
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, self.COL_SELECT)
            if isinstance(w, QCheckBox) and w.isChecked():
                selected.add(r)
            if isinstance(w, QWidget):
                chk = w.findChild(QCheckBox)
                if chk is not None and chk.isChecked():
                    selected.add(r)
        # Fallback to selected cells if no checkboxes
        if not selected:
            selected = {i.row() for i in self.table.selectedIndexes()}
        for r in sorted(selected, reverse=True):
            self.table.removeRow(r)
        self._update_delete_button_state()
        if selected:
            self._mark_dirty()
            self._schedule_net_sop_conflict_refresh(force=True)
        self._apply_schedule_table_height_hints()

    # --------- Operating group helpers (cascading selections) --------- #

    def _load_operating_groups(self) -> None:
        data = self.settings.all()
        og = data.get("operating_groups", [])
        if isinstance(og, list):
            self.operating_groups = ensure_operating_group_frequency_keys([g for g in og if isinstance(g, dict)])
        else:
            self.operating_groups = []

    def _populate_band_combo(self, band_combo: QComboBox, group_name: str):
        band_combo.blockSignals(True)
        band_combo.clear()
        bands = sorted(
            {g.get("band") for g in self.operating_groups if g.get("group") == group_name and g.get("band")}
        )
        if bands:
            band_combo.addItems(bands)
        else:
            band_combo.addItems(BAND_ORDER)
        band_combo.blockSignals(False)

    def _matching_operating_groups(self, group: str, band: str) -> List[Dict]:
        return [
            g
            for g in self.operating_groups
            if g.get("group") == group and g.get("band") == band
        ]

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
            combo.addItems(MODES)
        if preferred_mode and preferred_mode in modes:
            combo.setCurrentText(preferred_mode)
        elif modes:
            combo.setCurrentIndex(0)
        combo.setEnabled(len(modes) > 1 or not modes)
        combo.currentTextChanged.connect(lambda _m, r=row: self._update_mode_freq(r, preserve_frequency=True))
        combo.currentTextChanged.connect(self._mark_dirty)
        self.table.setCellWidget(row, self.COL_MODE, combo)
        return combo

    def _build_fldigi_mode_combo(self, value: str = "") -> QComboBox:
        combo = QComboBox()
        options = _fldigi_mode_options()
        combo.setEditable(True)
        combo.addItems(options)
        combo.setInsertPolicy(QComboBox.NoInsert)
        completer = QCompleter(options, combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        combo.setCompleter(completer)
        if value:
            combo.setCurrentText(value)
        return combo

    def _set_fldigi_from_operating_group(self, row: int, entry: Optional[Dict[str, Any]], *, force: bool = False) -> None:
        if not isinstance(entry, dict):
            return
        mode_widget = self.table.cellWidget(row, self.COL_FLDIGI_MODE)
        offset_widget = self.table.cellWidget(row, self.COL_FLDIGI_OFFSET)
        fldigi_mode = str(entry.get("fldigi_mode") or "").strip()
        fldigi_offset = str(entry.get("fldigi_offset") or "").strip()
        if isinstance(mode_widget, QComboBox):
            current = mode_widget.currentText().strip()
            if fldigi_mode and (force or not current):
                mode_widget.setCurrentText(fldigi_mode)
        if isinstance(offset_widget, QLineEdit):
            current = offset_widget.text().strip()
            if fldigi_offset and (force or not current):
                offset_widget.setText(fldigi_offset)

    def _update_mode_freq(self, row: int, *, preserve_frequency: bool = False):
        group = self._get_combo_value(row, self.COL_GROUP, "")
        band = self._get_combo_value(row, self.COL_BAND, "")
        entries = self._matching_operating_groups(group, band)
        preferred_mode = self._get_combo_value(row, self.COL_MODE, "")
        mode_combo = self._set_mode_widget(row, group, band, preferred_mode, entries)
        mode_val = mode_combo.currentText().strip() if isinstance(mode_combo, QComboBox) else preferred_mode
        current_freq = self._frequency_value(row)
        current_key = self._frequency_key(row)
        previous_key = current_key
        previous_frequency = current_freq
        if not preserve_frequency:
            current_freq, current_key = "", ""
        self._set_frequency_choice(row, group, band, mode_val, current_freq, current_key)
        selected = resolve_operating_group_frequency(
            self.operating_groups, group=group, band=band, mode=mode_val,
            frequency_key=self._frequency_key(row), frequency=self._frequency_value(row) or None,
        )
        selection_changed = bool(selected.resolved and selected.row is not None and (
            previous_key != str(selected.row.get("operating_group_frequency_key") or "")
            or previous_frequency != str(selected.row.get("frequency") or "").strip()
        ))
        self._set_fldigi_from_operating_group(row, selected.row if selected.resolved else None, force=selection_changed)
        # trigger autostart if mode changes
        if mode_val:
            self._on_mode_changed(row, mode_val)

    def _frequency_value(self, row: int) -> str:
        widget = self.table.cellWidget(row, self.COL_FREQ)
        if isinstance(widget, QComboBox):
            value = widget.currentData(Qt.UserRole + 1)
            return str(value if value is not None else widget.currentText()).strip()
        item = self.table.item(row, self.COL_FREQ)
        return item.text().strip() if item is not None else ""

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
            # Keep legacy free-text schedule entry behavior if there is no valid
            # configured variant to offer in the selector.
            self.table.removeCellWidget(row, self.COL_FREQ)
            item = self.table.item(row, self.COL_FREQ)
            if item is None:
                item = QTableWidgetItem()
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
            selected = combo.findData(str(result.row.get("operating_group_frequency_key") or ""), Qt.UserRole)
        elif frequency or frequency_key:
            combo.addItem(f"Unresolved — {frequency}" if frequency else "Unresolved frequency reference", frequency_key)
            combo.setItemData(combo.count() - 1, frequency, Qt.UserRole + 1)
            selected = combo.count() - 1
        if selected >= 0:
            combo.setCurrentIndex(selected)
        combo.setToolTip(result.reason if result.state != "resolved" else "Choose the configured frequency for this net row.")
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
        group = self._get_combo_value(row, self.COL_GROUP, "")
        band = self._get_combo_value(row, self.COL_BAND, "")
        mode = self._get_combo_value(row, self.COL_MODE, "")
        result = resolve_operating_group_frequency(
            self.operating_groups, group=group, band=band, mode=mode,
            frequency_key=self._frequency_key(row), frequency=self._frequency_value(row) or None,
        )
        if result.resolved:
            self._set_fldigi_from_operating_group(row, result.row, force=True)

    def _format_freq(self, freq: str | float) -> str:
        try:
            # Keep at least 3 decimal places (e.g., .110 stays .110)
            return f"{float(freq):.3f}"
        except Exception:
            try:
                return str(freq).strip()
            except Exception:
                return ""

    # --------- auto-start behavior --------- #

    def _on_mode_changed(self, row: int, mode: str):
        """
        Called whenever a Mode cell changes. Mode is one of JS8, Digi, Tri, SSB.
        Triggers appropriate auto-start behavior based on Settings autostart flags.
        """
        mode = (mode or "").strip()
        if not mode:
            return
        self._autostart_for_mode(mode)

    def _autostart_for_mode(self, mode: str):
        """
        Mode → programs mapping:

          JS8  → JS8Call
          Digi → FLDigi, FLMsg, FLAmp
          Tri  → FLRig, FLDigi, FLMsg, FLAmp, JS8Call
          SSB  → no auto-launch (RF-only)
        """
        if getattr(self, "_suppress_autostart", False):
            return
        if self._is_truthy(self.settings.get("launch_control_enabled", True)):
            return

        mode = mode.strip()

        if mode == "Digi":
            programs = ["FLDigi", "FLMsg", "FLAmp"]
        else:
            # For SSB (or anything else), do nothing
            return

        allowed_autostart = {"FLDigi", "FLMsg", "FLAmp"}
        programs = [p for p in programs if p in allowed_autostart]
        if not programs:
            return

        for prog in programs:
            if not self._program_autostart_enabled(prog):
                continue
            if self._program_is_running(prog):
                continue
            self._launch_program(prog)

    def _program_autostart_enabled(self, program_name: str) -> bool:
        if program_name not in {"FLDigi", "FLMsg", "FLAmp"}:
            return False
        meta = PROGRAM_META.get(program_name)
        if not meta:
            return False
        key = meta["autostart_key"]
        try:
            val = self.settings.get(key, False)
        except Exception:
            data = self.settings.all()
            val = data.get(key, False)
        return self._is_truthy(val)

    def _get_saved_program_path(self, program_name: str) -> Optional[Path]:
        meta = PROGRAM_META.get(program_name)
        if not meta:
            return None
        key = meta["path_key"]
        try:
            path_str = self.settings.get(key)
        except Exception:
            data = self.settings.all()
            path_str = data.get(key)
        if not path_str:
            return None
        p = Path(path_str)
        return p if p.exists() else None

    def _program_is_running(self, program_name: str) -> bool:
        try:
            return bool(self._status_service.program_is_running(program_name))
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

    def _launch_program(self, program_name: str) -> bool:
        exe_path = self._get_saved_program_path(program_name)

        # Try explicit path first
        if exe_path and exe_path.exists():
            try:
                if platform.system() == "Windows":
                    subprocess.Popen(
                        [str(exe_path)],
                        shell=False,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                    )
                else:
                    subprocess.Popen([str(exe_path)])
                log.info("NetSchedule: launched %s from saved path %s", program_name, exe_path)
                return True
            except Exception as e:
                log.error("NetSchedule: failed launching %s from saved path %s: %s", program_name, exe_path, e)

        # Fallback: rely on PATH
        for cand in [program_name.lower(), program_name]:
            try:
                subprocess.Popen(
                    [cand],
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                    if platform.system() == "Windows"
                    else 0,
                )
                log.info("NetSchedule: launched %s via PATH as '%s'", program_name, cand)
                return True
            except Exception:
                continue

        log.warning("NetSchedule: unable to launch %s; no valid path or PATH command", program_name)
        return False

    # --------- parsing / validation --------- #

    def _parse_hhmm(self, txt: str):
        txt = (txt or "").strip()
        if not txt:
            return None
        try:
            parts = txt.split(":")
            if len(parts) != 2:
                return None
            h = int(parts[0])
            m = int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                return None
            return h * 60 + m
        except Exception:
            return None

    def _collect_rows(self) -> List[Dict]:
        rows: List[Dict] = []
        net_names_seen = set()

        for r in range(self.table.rowCount()):
            def text(col: int) -> str:
                item = self.table.item(r, col)
                return item.text().strip() if item else ""

            day_combo: QComboBox = self.table.cellWidget(r, self.COL_DAY)  # type: ignore
            recur_combo: QComboBox = self.table.cellWidget(r, self.COL_RECURRENCE)  # type: ignore
            month_weeks_widget: QWidget = self.table.cellWidget(r, self.COL_MONTH_WEEKS)  # type: ignore
            group_combo: QComboBox = self.table.cellWidget(r, self.COL_GROUP)  # type: ignore
            band_combo: QComboBox = self.table.cellWidget(r, self.COL_BAND)  # type: ignore
            mode_combo: QComboBox = self.table.cellWidget(r, self.COL_MODE)  # type: ignore
            early_combo: QComboBox = self.table.cellWidget(r, self.COL_EARLY)  # type: ignore
            fldigi_mode_widget = self.table.cellWidget(r, self.COL_FLDIGI_MODE)
            fldigi_offset_widget = self.table.cellWidget(r, self.COL_FLDIGI_OFFSET)
            net_edit: QLineEdit = self.table.cellWidget(r, self.COL_NETNAME)  # type: ignore
            auto_widget = self.table.cellWidget(r, self.COL_AUTOTUNE)
            select_widget = self.table.cellWidget(r, self.COL_SELECT)

            day = self._get_combo_value(r, self.COL_DAY, "")
            group_name = group_combo.currentText().strip() if group_combo else ""
            band = band_combo.currentText().strip() if band_combo else ""
            mode = mode_combo.currentText().strip() if mode_combo else ""
            early = early_combo.currentText().strip() if early_combo else "0"
            fldigi_mode = (
                fldigi_mode_widget.currentText().strip()
                if isinstance(fldigi_mode_widget, QComboBox)
                else ""
            )
            fldigi_offset = (
                fldigi_offset_widget.text().strip()
                if isinstance(fldigi_offset_widget, QLineEdit)
                else ""
            )
            net_name = net_edit.text().strip() if net_edit else ""
            recurrence = recur_combo.currentText().strip() if recur_combo else "Weekly"
            month_weeks = self._get_month_weeks(month_weeks_widget)
            auto_tune = False
            if isinstance(auto_widget, QCheckBox):
                auto_tune = auto_widget.isChecked()
            elif isinstance(auto_widget, QWidget):
                chk = auto_widget.findChild(QCheckBox)
                if chk is not None:
                    auto_tune = chk.isChecked()
            resource_id = None
            resource_set = ""
            if isinstance(select_widget, QWidget):
                rid = select_widget.property("resource_id")
                if rid not in (None, ""):
                    try:
                        resource_id = int(rid)
                    except Exception:
                        resource_id = None
                rs = select_widget.property("resource_set")
                if rs not in (None, ""):
                    resource_set = str(rs).strip()
                fm = select_widget.property("fldigi_mode")
                if not fldigi_mode and fm not in (None, ""):
                    fldigi_mode = str(fm).strip()
                fo = select_widget.property("fldigi_offset")
                if not fldigi_offset and fo not in (None, ""):
                    fldigi_offset = str(fo).strip()
                subscription_values = {
                    key: select_widget.property(key)
                    for key in (
                        "net_session_key",
                        "frequency_resource_key",
                        "accepted_session_version_hash",
                        "accepted_resource_version_hash",
                        "accepted_snapshot_json",
                    )
                }
            else:
                subscription_values = {}

            freq = self._frequency_value(r)
            start_txt = text(self.COL_START)
            end_txt = text(self.COL_END)

            # Skip completely empty rows
            if not (day or band or freq or start_txt or end_txt or net_name):
                continue

            if not day:
                raise ValueError(f"Row {r+1}: Day is required.")
            if recurrence == "Daily":
                day = "ALL"
            if recurrence == "Periodic" and day == "ALL":
                raise ValueError(f"Row {r+1}: Periodic nets require a specific day.")
            if band == "--":
                raise ValueError(f"Row {r+1}: '--' is not a valid band.")
            if band and band not in BAND_ORDER:
                raise ValueError(f"Row {r+1}: Unknown band '{band}'.")
            if mode and mode not in MODES:
                raise ValueError(f"Row {r+1}: Unknown mode '{mode}'.")

            # Frequency validation
            if not freq:
                raise ValueError(f"Row {r+1}: Frequency is required.")
            if not net_name:
                raise ValueError(f"Row {r+1}: Net Name is required.")
            try:
                freq_norm = freq.replace(" ", "")
                if freq_norm.count(".") > 1:
                    parts = freq_norm.split(".")
                    freq_norm = parts[0] + "." + "".join(parts[1:])
                freq_mhz = float(freq_norm)
            except ValueError:
                raise ValueError(f"Row {r+1}: Invalid frequency '{freq}'.")

            # Normalize band like "40" -> "40M"
            if band and band not in BAND_ORDER:
                if not band.endswith("M"):
                    band = f"{band}M"

            if band == "60M":
                key = f"{freq_mhz:.4f}".rstrip("0").rstrip(".")
                allowed = {c.rstrip("0").rstrip(".") for c in SIXTY_M_CHANNELS}
                if key not in allowed:
                    raise ValueError(
                        "Row %d: 60M must be one of 5.332, 5.348, 5.3585, 5.373, 5.405 MHz."
                        % (r + 1)
                    )
            else:
                # Treat JS8 and Tri as Digi for band limits.
                # USB/LSB voice modes share SSB ranges in this table.
                mode_for_limits = mode
                if mode_for_limits in ("JS8", "Tri"):
                    mode_for_limits = "Digi"
                elif mode_for_limits in ("USB", "LSB"):
                    mode_for_limits = "SSB"

                limits = BAND_MODE_LIMITS.get((band, mode_for_limits))
                if limits:
                    lo, hi = limits
                    if not (lo <= freq_mhz <= hi):
                        raise ValueError(
                            "Row %d: %s %s frequency must be between %.3f and %.3f MHz."
                            % (r + 1, band, mode, lo, hi)
                        )

            smin = self._parse_hhmm(start_txt)
            emin = self._parse_hhmm(end_txt)
            if smin is None or emin is None:
                raise ValueError(f"Row {r+1}: Invalid time (use HH:MM).")

            try:
                early_int = int(early)
            except ValueError:
                raise ValueError(f"Row {r+1}: Early check-in must be 0, 5, 10, or 15.")

            if early_int not in (0, 5, 10, 15):
                raise ValueError(f"Row {r+1}: Early check-in must be 0, 5, 10, or 15.")

            recurrence = recurrence if recurrence in ("Weekly", "Daily", "Periodic") else "Weekly"
            if recurrence != "Periodic":
                month_weeks = []
            biweekly_offset = 0
            if recurrence == "Periodic":
                if not month_weeks:
                    month_weeks = [1]
                if isinstance(month_weeks_widget, QLineEdit):
                    month_weeks_widget.setText(",".join(str(w) for w in month_weeks))
                if not month_weeks:
                    raise ValueError(f"Row {r+1}: Weeks of Month must be 1-5.")

            # If viewing local, convert back to UTC before storing
            if self._show_local:
                orig_day = day
                day, start_txt = self._convert_day_time(orig_day, start_txt, to_local=False)
                _, end_txt = self._convert_day_time(orig_day, end_txt, to_local=False)

            target_scope, target_device_profile_id, target_operating_profile_id = self._selected_schedule_target(r)
            if target_scope == TARGET_SCOPE_DEVICE_PROFILE and target_device_profile_id is None:
                raise ValueError(f"Row {r+1}: Device-targeted rows require a device profile.")
            if target_scope == TARGET_SCOPE_OPERATING_PROFILE and target_operating_profile_id is None:
                raise ValueError(f"Row {r+1}: Frequency Plan-targeted rows require a Frequency Plan.")

            row = normalize_schedule_target_fields(
                {
                    "day_utc": day,
                    "recurrence": recurrence,
                    "biweekly_offset_weeks": biweekly_offset,
                    "month_weeks": ",".join(str(w) for w in month_weeks) if month_weeks else "",
                    "group_name": group_name,
                    "band": band,
                    "mode": mode,
                    "vfo": "A",
                    "frequency": str(freq).strip(),
                    "operating_group_frequency_key": self._frequency_key(r),
                    "start_utc": start_txt,
                    "end_utc": end_txt,
                    "early_checkin": str(early_int),
                    "net_name": net_name,
                    "auto_tune": bool(auto_tune),
                    "fldigi_mode": fldigi_mode,
                    "fldigi_offset": fldigi_offset,
                    "target_scope": target_scope,
                    "target_device_profile_id": target_device_profile_id,
                    "target_operating_profile_id": target_operating_profile_id,
                }
            )
            if resource_id is not None:
                row["_resource_id"] = resource_id
            if resource_set:
                row["_resource_set"] = resource_set
            for key, value in subscription_values.items():
                if value not in (None, ""):
                    row[key] = value
            rows.append(row)

            if net_name:
                net_names_seen.add(net_name)

        self._net_name_history = sorted(net_names_seen)
        return rows

    @staticmethod
    def _strip_internal_row(row: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in dict(row).items() if not str(k).startswith("_")}

    # --------- load/save --------- #

    def _db_path(self) -> Path:
        cfg_path = getattr(self.settings, "_config_path", None)
        if cfg_path:
            try:
                cfg = Path(cfg_path)
                return cfg.parent / "freqinout_nets.db"
            except Exception:
                pass
        from freqinout.core.config_paths import get_config_dir

        return get_config_dir() / "config" / "freqinout_nets.db"

    def _load_from_db(self) -> List[Dict]:
        db_path = self._db_path()
        if not db_path.exists():
            return []

        conn = sqlite3.connect(db_path)
        try:
            has_new = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='net_schedule_tab'"
            ).fetchone()
            has_legacy = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='net_schedule'"
            ).fetchone()
            if not has_new and not has_legacy:
                return []

            rows: List[Dict] = []
            if has_new:
                try:
                    cur = conn.execute(
                        """
                        SELECT
                            id,
                            day_utc,
                            recurrence,
                            biweekly_offset_weeks,
                            month_weeks,
                            band,
                            mode,
                            vfo,
                            frequency,
                            start_utc,
                            end_utc,
                            early_checkin,
                            auto_tune,
                            primary_js8call_group,
                            comment,
                            net_name,
                            group_name,
                            fldigi_mode,
                            fldigi_offset,
                            resource_id,
                            net_session_key,
                            accepted_session_version_hash,
                            accepted_resource_version_hash,
                            accepted_snapshot_json,
                            target_scope,
                            target_device_profile_id,
                            target_operating_profile_id
                        FROM net_schedule_tab
                        """
                    )
                    for (
                        row_id,
                        day_utc,
                        recurrence,
                        biweekly_offset_weeks,
                        month_weeks,
                        band,
                        mode,
                        vfo,
                        freq,
                        start_utc,
                        end_utc,
                        early,
                        auto_tune,
                        group,
                        comment,
                        net_name,
                        group_name,
                        fldigi_mode,
                        fldigi_offset,
                        resource_id,
                        net_session_key,
                        accepted_session_version_hash,
                        accepted_resource_version_hash,
                        accepted_snapshot_json,
                        target_scope,
                        target_device_profile_id,
                        target_operating_profile_id,
                    ) in cur.fetchall():
                        rows.append(
                            normalize_schedule_target_fields(
                                {
                                    "day_utc": day_utc or "",
                                    "recurrence": "Periodic" if (recurrence or "Weekly") == "Monthly" else recurrence or "Weekly",
                                    "biweekly_offset_weeks": int(biweekly_offset_weeks or 0),
                                    "month_weeks": month_weeks or "",
                                    "band": band or "",
                                    "mode": mode or "",
                                    "vfo": (vfo or "A").strip().upper(),
                                    "frequency": str(freq or ""),
                                    "start_utc": start_utc or "",
                                    "end_utc": end_utc or "",
                                    "early_checkin": str(early if early is not None else 0),
                                    "auto_tune": bool(auto_tune),
                                    "primary_js8call_group": group or "",
                                    "comment": comment or "",
                                    "net_name": net_name or "",
                                    "group_name": group_name or "",
                                    "fldigi_mode": fldigi_mode or "",
                                    "fldigi_offset": fldigi_offset or "",
                                    "source_table": "net_schedule_tab",
                                    "source_row_id": int(row_id or 0),
                                    "source_key": f"NET:{int(row_id or 0)}" if int(row_id or 0) > 0 else "",
                                    "_resource_id": int(resource_id) if resource_id not in (None, "") else None,
                                    "net_session_key": net_session_key or "",
                                    "accepted_session_version_hash": accepted_session_version_hash or "",
                                    "accepted_resource_version_hash": accepted_resource_version_hash or "",
                                    "accepted_snapshot_json": accepted_snapshot_json or "",
                                    "target_scope": target_scope,
                                    "target_device_profile_id": target_device_profile_id,
                                    "target_operating_profile_id": target_operating_profile_id,
                                }
                            )
                        )
                    return rows
                except Exception:
                    try:
                        cur = conn.execute(
                            """
                            SELECT
                                day_utc,
                                band,
                                mode,
                                vfo,
                                frequency,
                                start_utc,
                                end_utc,
                                early_checkin,
                                primary_js8call_group,
                                comment,
                                net_name,
                                fldigi_mode,
                                fldigi_offset
                            FROM net_schedule_tab
                            """
                        )
                        fetched = cur.fetchall()
                        parse_mode = "with_fldigi"
                    except Exception:
                        cur = conn.execute(
                            """
                            SELECT
                                day_utc,
                                band,
                                mode,
                                vfo,
                                frequency,
                                start_utc,
                                end_utc,
                                early_checkin,
                                primary_js8call_group,
                                comment,
                                net_name
                            FROM net_schedule_tab
                            """
                        )
                        fetched = cur.fetchall()
                        parse_mode = "legacy"
                    for row in fetched:
                        if parse_mode == "with_fldigi":
                            (
                                day_utc,
                                band,
                                mode,
                                vfo,
                                freq,
                                start_utc,
                                end_utc,
                                early,
                                group,
                                comment,
                                net_name,
                                fldigi_mode,
                                fldigi_offset,
                            ) = row
                        else:
                            (
                                day_utc,
                                band,
                                mode,
                                vfo,
                                freq,
                                start_utc,
                                end_utc,
                                early,
                                group,
                                comment,
                                net_name,
                            ) = row
                            fldigi_mode = ""
                            fldigi_offset = ""
                        rows.append(
                            normalize_schedule_target_fields(
                                {
                                    "day_utc": day_utc or "",
                                    "recurrence": "Weekly",
                                    "biweekly_offset_weeks": 0,
                                    "month_weeks": "",
                                    "band": band or "",
                                    "mode": mode or "",
                                    "vfo": (vfo or "A").strip().upper(),
                                    "frequency": str(freq or ""),
                                    "start_utc": start_utc or "",
                                    "end_utc": end_utc or "",
                                    "early_checkin": str(early if early is not None else 0),
                                    "auto_tune": False,
                                    "primary_js8call_group": group or "",
                                    "comment": comment or "",
                                    "net_name": net_name or "",
                                    "group_name": "",
                                    "fldigi_mode": fldigi_mode or "",
                                    "fldigi_offset": fldigi_offset or "",
                                }
                            )
                        )
                    return rows

            if has_legacy:
                try:
                    cur = conn.execute(
                        """
                        SELECT
                            day_utc,
                            recurrence,
                            biweekly_offset_weeks,
                            month_weeks,
                            band,
                            mode,
                            frequency,
                            start_utc,
                            end_utc,
                            early_checkin,
                            auto_tune,
                            primary_js8call_group,
                            comment,
                            net_name,
                            group_name,
                            fldigi_mode,
                            fldigi_offset,
                            target_scope,
                            target_device_profile_id,
                            target_operating_profile_id
                        FROM net_schedule
                        """
                    )
                    for (
                        day_utc,
                        recurrence,
                        biweekly_offset_weeks,
                        month_weeks,
                        band,
                        mode,
                        freq,
                        start_utc,
                        end_utc,
                        early,
                        auto_tune,
                        group,
                        comment,
                        net_name,
                        group_name,
                        fldigi_mode,
                        fldigi_offset,
                        target_scope,
                        target_device_profile_id,
                        target_operating_profile_id,
                    ) in cur.fetchall():
                        rows.append(
                            normalize_schedule_target_fields(
                                {
                                    "day_utc": day_utc or "",
                                    "recurrence": "Periodic" if (recurrence or "Weekly") == "Monthly" else recurrence or "Weekly",
                                    "biweekly_offset_weeks": int(biweekly_offset_weeks or 0),
                                    "month_weeks": month_weeks or "",
                                    "band": band or "",
                                    "mode": mode or "",
                                    "vfo": "A",
                                    "frequency": str(freq or ""),
                                    "start_utc": start_utc or "",
                                    "end_utc": end_utc or "",
                                    "early_checkin": str(early if early is not None else 0),
                                    "auto_tune": bool(auto_tune),
                                    "primary_js8call_group": group or "",
                                    "comment": comment or "",
                                    "net_name": net_name or "",
                                    "group_name": group_name or "",
                                    "fldigi_mode": fldigi_mode or "",
                                    "fldigi_offset": fldigi_offset or "",
                                    "target_scope": target_scope,
                                    "target_device_profile_id": target_device_profile_id,
                                    "target_operating_profile_id": target_operating_profile_id,
                                }
                            )
                        )
                    return rows
                except Exception:
                    try:
                        cur = conn.execute(
                            """
                            SELECT
                                day_utc,
                                band,
                                mode,
                                frequency,
                                start_utc,
                                end_utc,
                                early_checkin,
                                primary_js8call_group,
                                comment,
                                net_name,
                                fldigi_mode,
                                fldigi_offset
                            FROM net_schedule
                            """
                        )
                        fetched = cur.fetchall()
                        parse_mode = "with_fldigi"
                    except Exception:
                        cur = conn.execute(
                            """
                            SELECT
                                day_utc,
                                band,
                                mode,
                                frequency,
                                start_utc,
                                end_utc,
                                early_checkin,
                                primary_js8call_group,
                                comment,
                                net_name
                            FROM net_schedule
                            """
                        )
                        fetched = cur.fetchall()
                        parse_mode = "legacy"
                    for row in fetched:
                        if parse_mode == "with_fldigi":
                            (
                                day_utc,
                                band,
                                mode,
                                freq,
                                start_utc,
                                end_utc,
                                early,
                                group,
                                comment,
                                net_name,
                                fldigi_mode,
                                fldigi_offset,
                            ) = row
                        else:
                            (
                                day_utc,
                                band,
                                mode,
                                freq,
                                start_utc,
                                end_utc,
                                early,
                                group,
                                comment,
                                net_name,
                            ) = row
                            fldigi_mode = ""
                            fldigi_offset = ""
                        rows.append(
                            normalize_schedule_target_fields(
                                {
                                    "day_utc": day_utc or "",
                                    "recurrence": "Weekly",
                                    "biweekly_offset_weeks": 0,
                                    "month_weeks": "",
                                    "band": band or "",
                                    "mode": mode or "",
                                    "vfo": "A",
                                    "frequency": str(freq or ""),
                                    "start_utc": start_utc or "",
                                    "end_utc": end_utc or "",
                                    "early_checkin": str(early if early is not None else 0),
                                    "auto_tune": False,
                                    "primary_js8call_group": group or "",
                                    "comment": comment or "",
                                    "net_name": net_name or "",
                                    "group_name": "",
                                    "fldigi_mode": fldigi_mode or "",
                                    "fldigi_offset": fldigi_offset or "",
                                }
                            )
                        )
                    return rows
            return rows
        except Exception as e:
            log.error("NetScheduleTab: failed to load schedule from DB %s: %s", db_path, e)
            return []
        finally:
            conn.close()
        # Should not reach here
        return []

    def _load(self):
        self._suspend_dirty_tracking = True
        try:
            self.table.setRowCount(0)
            try:
                self.settings.reload()
            except Exception:
                pass
            self._load_operating_groups()
            data = self._load_from_db()
            loaded_from_db = bool(data)
            if loaded_from_db:
                try:
                    stored_rows = self.settings.get("net_schedule", [])
                except Exception:
                    stored_rows = []
                data = reattach_schedule_frequency_keys(data, stored_rows if isinstance(stored_rows, list) else [])
            if not data:
                data = self.settings.get("net_schedule", [])
                if not isinstance(data, list):
                    data = []
                data = [normalize_schedule_target_fields(row) for row in data if isinstance(row, dict)]
            self._raw_rows = data
            for row in self._raw_rows:
                self._add_row(self._to_view_row(row))
            self._net_name_history = sorted(
                {r.get("net_name", "") for r in data if isinstance(r, dict) and r.get("net_name")}
            )
            self._bootstrap_net_resources()
            self._load_resources_from_db()
            self._refresh_resource_set_combo()
            self._refresh_resources_table()
            self._update_clock_labels()
            self._resize_table_columns()
            src = "DB" if loaded_from_db else "settings"
            log.info("Net schedule loaded from %s: %d rows", src, len(data))
            self._saved_rows_signature = self._rows_signature(self._raw_rows)
            self._set_dirty(False)
            self._schedule_net_sop_conflict_refresh(force=True)
        finally:
            self._suspend_dirty_tracking = False

    def _save(self):
        try:
            rows = self._collect_rows()
        except ValueError as e:
            QMessageBox.warning(self, "Invalid Net Schedule", str(e))
            return
        clean_rows = [self._strip_internal_row(r) for r in rows]
        self._raw_rows = rows
        if not self._enforce_net_priority_for_conflicts(rows):
            self._schedule_net_sop_conflict_refresh(force=True)
            return

        # Save to JSON config
        self.settings.set("net_schedule", clean_rows)
        self.settings.save()
        log.info("Net schedule saved to config: %d entries", len(clean_rows))

        # Also mirror to SQLite DB (new table plus legacy table)
        try:
            self._save_to_db(clean_rows)
        except Exception as e:
            log.error("Failed to save net schedule to DB: %s", e)
            QMessageBox.warning(
                self,
                "DB Save Error",
                f"Net schedule saved to config.json, but DB save failed:\n{e}",
            )
            return
        self._bump_net_sop_conflict_scan_epoch()

        try:
            self.schedule_saved.emit()
        except Exception:
            pass

        self._load()
        self._schedule_net_sop_conflict_refresh(force=True)
        QMessageBox.information(self, "Saved", "Net Schedule saved.")

    @staticmethod
    def _format_conflict_span(start_utc: Any, end_utc: Any) -> str:
        start_txt = str(start_utc or "").strip()
        end_txt = str(end_utc or "").strip()
        if not start_txt or not end_txt:
            return f"{start_txt} - {end_txt}".strip(" -")
        try:
            start_dt = datetime.datetime.fromisoformat(start_txt.replace("Z", "+00:00"))
            end_dt = datetime.datetime.fromisoformat(end_txt.replace("Z", "+00:00"))
            return f"{start_dt.strftime('%a %H:%M')} - {end_dt.strftime('%H:%M')} UTC"
        except Exception:
            return f"{start_txt} - {end_txt}"

    def _coalesce_net_sop_conflicts(self, conflicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for row in conflicts:
            if not isinstance(row, dict):
                continue
            net_sig = str(row.get("net_row_signature") or "").strip()
            sop_sig = str(row.get("sop_row_signature") or "").strip()
            key = (net_sig, sop_sig)
            grouped.setdefault(key, []).append(row)
        out: List[Dict[str, Any]] = []
        for (_net_sig, _sop_sig), rows in grouped.items():
            rows_sorted = sorted(rows, key=lambda r: str(r.get("window_start_utc") or ""))
            sample = rows_sorted[0] if rows_sorted else {}
            out.append(
                {
                    "sample": sample,
                    "rows": rows_sorted,
                    "count": len(rows_sorted),
                }
            )
        out.sort(key=lambda g: str((g.get("sample") or {}).get("window_start_utc") or ""))
        return out

    def _conflict_summary_line(self, grouped: Dict[str, Any]) -> str:
        sample = grouped.get("sample") or {}
        count = int(grouped.get("count") or 0)
        sop_name = str(sample.get("sop_profile_name") or "").strip() or "HF SOP"
        net_name = str(sample.get("net_name") or sample.get("net_group_name") or "Net").strip()
        span = self._format_conflict_span(sample.get("window_start_utc"), sample.get("window_end_utc"))
        extra = f" (+{count - 1} more)" if count > 1 else ""
        return f"{sop_name} vs {net_name}: {span}{extra}"

    def _enforce_net_priority_for_conflicts(
        self,
        rows: List[Dict[str, Any]],
        *,
        operation_label: str = "save",
    ) -> bool:
        _, blocking, _ = self._scan_net_sop_conflicts(net_rows_override=rows)
        if not blocking:
            return True

        grouped = self._coalesce_net_sop_conflicts(blocking)
        lines: List[str] = []
        for group in grouped[:10]:
            lines.append(self._conflict_summary_line(group))
        if len(grouped) > 10:
            lines.append(f"...and {len(grouped) - 10} more blocking conflict pair(s).")
        op = str(operation_label or "save").strip().lower()
        op_title = "Add Blocked" if op == "add" else "Save Blocked"
        op_btn = "Cancel Add" if op == "add" else "Cancel Save"

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(f"Net/SOP Conflicts Block {op.title()}")
        box.setText(
            f"Conflicting Net rows are blocked until overlap windows are set to Net Priority before {op}.\n"
            "Saved overlap decisions from this dialog are temporary for the current SOP session by default."
        )
        box.setInformativeText("\n".join(lines))
        net_all_btn = box.addButton("Set Net Priority for All", QMessageBox.AcceptRole)
        review_btn = box.addButton("Review Each Conflict", QMessageBox.ActionRole)
        deactivate_btn = box.addButton("Deactivate Active HF SOPs", QMessageBox.DestructiveRole)
        box.addButton(op_btn, QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None:
            return False

        decisions: List[Dict[str, Any]] = []
        if clicked is deactivate_btn:
            self._deactivate_active_hf_sops()
        elif clicked is net_all_btn:
            decisions = self._build_net_sop_policy_decisions(blocking, "NET_PRIORITY")
        elif clicked is review_btn:
            decisions = self._review_each_net_sop_conflict(blocking, allow_sop_priority=False)

        if decisions:
            saved = 0
            try:
                saved = int(
                    self._save_net_sop_policy_decisions_session_aware(
                        decisions,
                        origin="Net Schedule save-blocking conflict resolution",
                    )
                    or 0
                )
            except Exception as e:
                log.debug("Net Schedule: failed saving Net/SOP policy decisions: %s", e)
            if saved > 0:
                self._notify_sop_data_changed()

        _, still_blocking, _ = self._scan_net_sop_conflicts(net_rows_override=rows)
        if still_blocking:
            QMessageBox.warning(
                self,
                op_title,
                f"{len(still_blocking)} conflict window(s) remain unresolved for Net Priority.\n"
                f"Set Net Priority, edit times, or remove conflicting rows before {op}.",
            )
            return False
        return True

    def _prompt_active_sop_conflicts_after_net_change(
        self,
        *,
        net_rows_override: Optional[List[Dict[str, Any]]] = None,
        show_resolved_hint: bool = False,
        require_net_priority: bool = False,
    ) -> None:
        try:
            conflicts = self._sop_manager.collect_active_net_sop_conflicts(
                horizon_days=35,
                net_rows_override=net_rows_override,
            )
        except Exception as e:
            log.debug("Net Schedule: active SOP conflict scan failed: %s", e)
            return
        if not conflicts:
            return
        if require_net_priority:
            pending_conflicts = [
                c for c in conflicts if not self._net_sop_policy_is_net_priority(c.get("resolved_policy"))
            ]
        else:
            pending_conflicts = [c for c in conflicts if not bool(c.get("has_policy"))]
        if not pending_conflicts:
            if show_resolved_hint:
                QMessageBox.information(
                    self,
                    "Net/SOP Conflicts Already Resolved" if not require_net_priority else "Net/SOP Conflicts",
                    "Conflicts were detected, but all matching windows already have saved Net/SOP policies."
                    if not require_net_priority
                    else "Conflicts were detected, but all blocking windows are already set to Net Priority.",
                )
            return
        conflicts = pending_conflicts
        grouped = self._coalesce_net_sop_conflicts(conflicts)
        lines: List[str] = []
        for group in grouped[:10]:
            lines.append(self._conflict_summary_line(group))
        if len(grouped) > 10:
            lines.append(f"...and {len(grouped) - 10} more conflict pair(s).")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Net vs Active SOP Conflicts")
        if require_net_priority:
            box.setText(
                "Active HF SOP and Net schedule windows conflict. Net Priority is required to keep overlaps.\n"
                "Saved overlap decisions from this dialog are temporary for the current SOP session by default."
            )
        else:
            box.setText(
                "Active HF SOP and Net schedule windows conflict. Choose resolution policy.\n"
                "Saved overlap decisions from this dialog are temporary for the current SOP session by default."
            )
        box.setInformativeText("\n".join(lines))
        net_all_btn = box.addButton("Net Priority for All", QMessageBox.AcceptRole)
        sop_all_btn = box.addButton("SOP Priority for All", QMessageBox.AcceptRole) if not require_net_priority else None
        review_btn = box.addButton("Review Each Conflict", QMessageBox.ActionRole)
        deactivate_btn = box.addButton("Deactivate Active HF SOPs", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None:
            return
        if clicked is deactivate_btn:
            self._deactivate_active_hf_sops()
            return

        decisions: List[Dict[str, Any]] = []
        if sop_all_btn is not None and clicked is sop_all_btn:
            decisions = self._build_net_sop_policy_decisions(conflicts, "SOP_PRIORITY")
        elif clicked is net_all_btn:
            decisions = self._build_net_sop_policy_decisions(conflicts, "NET_PRIORITY")
        elif clicked is review_btn:
            decisions = self._review_each_net_sop_conflict(conflicts, allow_sop_priority=not require_net_priority)
        if not decisions:
            return
        saved = 0
        try:
            saved = int(
                self._save_net_sop_policy_decisions_session_aware(
                    decisions,
                    origin="Net Schedule active conflict resolution",
                )
                or 0
            )
        except Exception as e:
            log.debug("Net Schedule: failed saving Net/SOP policy decisions: %s", e)
            saved = 0
        self._notify_sop_data_changed()
        if saved > 0:
            QMessageBox.information(
                self,
                "Conflict Policies Saved",
                f"Saved {saved} Net/SOP conflict policy decision(s).",
            )
        self._schedule_net_sop_conflict_refresh(force=True)

    def _build_net_sop_policy_decisions(
        self,
        conflicts: List[Dict[str, Any]],
        policy: str,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        chosen = "SOP_PRIORITY" if str(policy).strip().upper() == "SOP_PRIORITY" else "NET_PRIORITY"
        for row in conflicts:
            if not isinstance(row, dict):
                continue
            net_sig = str(row.get("net_row_signature") or "").strip()
            sop_sig = str(row.get("sop_row_signature") or "").strip()
            start_utc = str(row.get("window_start_utc") or "").strip()
            end_utc = str(row.get("window_end_utc") or "").strip()
            if not net_sig or not sop_sig or not start_utc or not end_utc:
                continue
            out.append(
                {
                    "sop_profile_id": int(row.get("sop_profile_id") or 0),
                    "sop_layer_id": int(row.get("sop_layer_id") or 0),
                    "net_row_signature": net_sig,
                    "sop_row_signature": sop_sig,
                    "window_start_utc": start_utc,
                    "window_end_utc": end_utc,
                    "policy": chosen,
                    "resolution_note": "Net schedule conflict resolution",
                }
            )
        return out

    def _review_each_net_sop_conflict(
        self,
        conflicts: List[Dict[str, Any]],
        *,
        allow_sop_priority: bool = True,
    ) -> List[Dict[str, Any]]:
        decisions: List[Dict[str, Any]] = []
        grouped = self._coalesce_net_sop_conflicts(conflicts)
        for idx, group in enumerate(grouped, start=1):
            rows = list(group.get("rows") or [])
            if not rows:
                continue
            row = rows[0]
            sop_summary = str(row.get("sop_summary") or "").strip()
            net_summary = str(row.get("net_summary") or "").strip()
            existing_vals = {str(r.get("resolved_policy") or "").strip().upper() for r in rows}
            existing_vals.discard("")
            existing = existing_vals.pop() if len(existing_vals) == 1 else ""
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle(f"Resolve Net/SOP Conflict ({idx}/{len(grouped)})")
            if allow_sop_priority:
                box.setText("Choose which schedule wins for this overlap window.")
            else:
                box.setText("Net Priority is required to keep this overlap in Net Schedule.")
            info_lines = [sop_summary, net_summary]
            if len(rows) > 1:
                info_lines.append(f"Applies to {len(rows)} occurrence windows.")
            if existing in {"SOP_PRIORITY", "NET_PRIORITY"}:
                info_lines.append(f"Current policy: {'SOP Priority' if existing == 'SOP_PRIORITY' else 'Net Priority'}")
            box.setInformativeText("\n".join([ln for ln in info_lines if ln]))
            net_btn = box.addButton("Net Priority", QMessageBox.AcceptRole)
            sop_btn = box.addButton("SOP Priority", QMessageBox.AcceptRole) if allow_sop_priority else None
            skip_btn = box.addButton("Skip", QMessageBox.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is skip_btn or clicked is None:
                continue
            policy = "SOP_PRIORITY" if (sop_btn is not None and clicked is sop_btn) else "NET_PRIORITY"
            built = self._build_net_sop_policy_decisions(rows, policy)
            if built:
                decisions.extend(built)
        return decisions

    def _save_net_sop_policy_decisions_session_aware(self, decisions: List[Dict[str, Any]], *, origin: str) -> int:
        win = self.window()
        try:
            daily_tab = getattr(win, "daily_tab", None)
            if daily_tab is not None and hasattr(daily_tab, "save_net_sop_conflict_policies_with_session_tracking"):
                return int(
                    daily_tab.save_net_sop_conflict_policies_with_session_tracking(
                        decisions,
                        origin=origin,
                    )
                    or 0
                )
        except Exception as e:
            log.debug("Net Schedule: session-aware Net/SOP policy save fallback due to error: %s", e)
        return int(self._sop_manager.save_net_sop_conflict_policies(decisions) or 0)

    def _deactivate_active_hf_sops(self) -> None:
        win = self.window()
        try:
            daily_tab = getattr(win, "daily_tab", None)
            if daily_tab is not None and hasattr(daily_tab, "deactivate_hf_sops_with_return_to_normal"):
                daily_tab.deactivate_hf_sops_with_return_to_normal(origin_label="Net Schedule")
                return
        except Exception as e:
            log.debug("Net Schedule: unified SOP deactivation flow unavailable: %s", e)
        changed = 0
        for profile in self._sop_manager.list_profiles():
            if not bool(profile.get("active")):
                continue
            category = str(profile.get("category") or "HF").strip().upper()
            if category != "HF":
                continue
            try:
                if self._sop_manager.set_profile_active(int(profile.get("id") or 0), False):
                    changed += 1
            except Exception:
                continue
        if changed > 0:
            self._notify_sop_data_changed()
            QMessageBox.information(self, "SOP", f"Deactivated {changed} active HF SOP profile(s).")

    def _notify_sop_data_changed(self) -> None:
        self._bump_net_sop_conflict_scan_epoch()
        try:
            win = self.window()
            if hasattr(win, "daily_tab") and hasattr(win.daily_tab, "on_sop_data_changed"):
                win.daily_tab.on_sop_data_changed()
            if hasattr(win, "sop_tab") and hasattr(win.sop_tab, "on_sop_profiles_updated"):
                win.sop_tab.on_sop_profiles_updated()
            if hasattr(win, "_on_sop_data_changed"):
                win._on_sop_data_changed()
        except Exception:
            pass

    def _open_net_sop_policy_manager(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Manage Net/SOP Policies")
        dlg.setModal(True)
        dlg.resize(980, 520)
        layout = QVBoxLayout(dlg)
        hint = QLabel("Review or adjust saved Net/SOP conflict policies. Stale rows no longer match active conflicts.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        table = QTableWidget(dlg)
        table.setColumnCount(7)
        table.setHorizontalHeaderLabels(
            [
                "Start (UTC)",
                "End (UTC)",
                "SOP",
                "Net",
                "Policy",
                "State",
                "Updated",
            ]
        )
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        hv = table.horizontalHeader()
        hv.setSectionResizeMode(QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(2, QHeaderView.Stretch)
        hv.setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(table)

        controls = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        set_sop_btn = QPushButton("Set Selected: SOP Priority")
        set_net_btn = QPushButton("Set Selected: Net Priority")
        clear_sel_btn = QPushButton("Clear Selected")
        clear_all_btn = QPushButton("Clear All")
        close_btn = QPushButton("Close")
        controls.addWidget(refresh_btn)
        controls.addWidget(set_sop_btn)
        controls.addWidget(set_net_btn)
        controls.addWidget(clear_sel_btn)
        controls.addWidget(clear_all_btn)
        controls.addStretch()
        controls.addWidget(close_btn)
        layout.addLayout(controls)

        theme = resolve_theme(self.settings)
        refresh_btn.setStyleSheet(button_style("muted", theme))
        set_sop_btn.setStyleSheet(button_style("eligible_info", theme))
        set_net_btn.setStyleSheet(button_style("eligible_primary", theme))
        clear_sel_btn.setStyleSheet(button_style("eligible_danger", theme))
        clear_all_btn.setStyleSheet(button_style("danger", theme))
        close_btn.setStyleSheet(button_style("muted", theme))

        policy_rows: List[Dict[str, Any]] = []

        def _selected_policy_ids() -> List[int]:
            indexes = {idx.row() for idx in table.selectionModel().selectedRows()} if table.selectionModel() else set()
            ids: List[int] = []
            for row_idx in sorted(indexes):
                item = table.item(row_idx, 0)
                if item is None:
                    continue
                try:
                    pid = int(item.data(Qt.UserRole) or 0)
                except Exception:
                    pid = 0
                if pid > 0:
                    ids.append(pid)
            return ids

        def _refresh_state() -> None:
            has_rows = bool(policy_rows)
            selected_ids = _selected_policy_ids()
            has_selected = bool(selected_ids)
            set_sop_btn.setEnabled(has_selected)
            set_net_btn.setEnabled(has_selected)
            clear_sel_btn.setEnabled(has_selected)
            clear_all_btn.setEnabled(has_rows)
            set_sop_btn.setStyleSheet(button_style("eligible_info" if has_selected else "muted", theme))
            set_net_btn.setStyleSheet(button_style("eligible_primary" if has_selected else "muted", theme))
            clear_sel_btn.setStyleSheet(button_style("eligible_danger" if has_selected else "muted", theme))
            clear_all_btn.setStyleSheet(button_style("danger" if has_rows else "muted", theme))

        def _load_rows() -> None:
            nonlocal policy_rows
            try:
                policy_rows = self._sop_manager.list_net_sop_policy_review_rows(horizon_days=7)
            except Exception as e:
                policy_rows = []
                log.debug("Net Schedule: failed loading policy review rows: %s", e)
            table.setSortingEnabled(False)
            table.setRowCount(0)
            for row in policy_rows:
                r = table.rowCount()
                table.insertRow(r)
                policy_val = str(row.get("policy") or "NET_PRIORITY").strip().upper()
                policy_txt = "SOP Priority" if policy_val == "SOP_PRIORITY" else "Net Priority"
                state_txt = str(row.get("state") or "Current").strip()
                values = [
                    str(row.get("window_start_utc") or ""),
                    str(row.get("window_end_utc") or ""),
                    str(row.get("sop_summary") or ""),
                    str(row.get("net_summary") or ""),
                    policy_txt,
                    state_txt,
                    str(row.get("updated_utc") or ""),
                ]
                for c, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    if c == 0:
                        item.setData(Qt.UserRole, int(row.get("id") or 0))
                    if c == 5 and state_txt.upper() == "STALE":
                        item.setForeground(QColor(theme.get("text_muted", "#888888")))
                    table.setItem(r, c, item)
            table.setSortingEnabled(True)
            table.resizeColumnsToContents()
            _refresh_state()

        def _apply_policy(policy: str) -> None:
            ids = _selected_policy_ids()
            if not ids:
                return
            changed = 0
            for pid in ids:
                try:
                    if self._sop_manager.update_net_sop_conflict_policy(pid, policy):
                        changed += 1
                except Exception:
                    continue
            if changed > 0:
                self._notify_sop_data_changed()
                self._schedule_net_sop_conflict_refresh(force=True)
            _load_rows()

        def _clear_selected() -> None:
            ids = _selected_policy_ids()
            if not ids:
                return
            confirm = QMessageBox.question(
                dlg,
                "Clear Selected Policies",
                f"Clear {len(ids)} selected Net/SOP policy row(s)?",
            )
            if confirm != QMessageBox.Yes:
                return
            cleared = int(self._sop_manager.clear_net_sop_conflict_policies(ids) or 0)
            if cleared > 0:
                self._notify_sop_data_changed()
                self._schedule_net_sop_conflict_refresh(force=True)
            _load_rows()

        def _clear_all() -> None:
            if not policy_rows:
                return
            confirm = QMessageBox.question(
                dlg,
                "Clear All Policies",
                "Clear all active Net/SOP conflict policy rows?",
            )
            if confirm != QMessageBox.Yes:
                return
            cleared = int(self._sop_manager.clear_net_sop_conflict_policies(None) or 0)
            if cleared > 0:
                self._notify_sop_data_changed()
                self._schedule_net_sop_conflict_refresh(force=True)
            _load_rows()

        refresh_btn.clicked.connect(_load_rows)
        set_sop_btn.clicked.connect(lambda: _apply_policy("SOP_PRIORITY"))
        set_net_btn.clicked.connect(lambda: _apply_policy("NET_PRIORITY"))
        clear_sel_btn.clicked.connect(_clear_selected)
        clear_all_btn.clicked.connect(_clear_all)
        close_btn.clicked.connect(dlg.accept)
        table.itemSelectionChanged.connect(_refresh_state)

        _load_rows()
        dlg.exec()

    def _export_schedule(self) -> None:
        """
        Export net schedule to JSON in UTC. Auto-Tune is nulled.
        """
        data = self.settings.all()
        callsign = (data.get("operator_callsign") or "").strip().upper() or "UNKNOWN"
        default_name = f"{callsign}-net-schedule-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')}.json"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Net Schedule",
            default_name,
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            rows = self._collect_rows()
        except ValueError as e:
            QMessageBox.warning(self, "Invalid Net Schedule", str(e))
            return
        if not rows:
            QMessageBox.warning(self, "Export", "No net schedule rows to export.")
            return
        try:
            payload = {
                "callsign": callsign,
                "created_utc": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
                "rows": [],
            }
            for r in rows:
                normalized = normalize_schedule_target_fields(r)
                payload["rows"].append(
                    {
                        "day_utc": normalized.get("day_utc", ""),
                        "recurrence": "Periodic"
                        if normalized.get("recurrence", "Weekly") == "Monthly"
                        else normalized.get("recurrence", "Weekly"),
                        "biweekly_offset_weeks": int(normalized.get("biweekly_offset_weeks", 0) or 0),
                        "month_weeks": normalized.get("month_weeks", ""),
                        "group_name": normalized.get("group_name", ""),
                        "band": normalized.get("band", ""),
                        "mode": normalized.get("mode", ""),
                        "frequency": normalized.get("frequency", ""),
                        "operating_group_frequency_key": normalized.get("operating_group_frequency_key", ""),
                        "start_utc": normalized.get("start_utc", ""),
                        "end_utc": normalized.get("end_utc", ""),
                        "early_checkin": normalized.get("early_checkin", 0),
                        "auto_tune": None,
                        "primary_js8call_group": normalized.get("primary_js8call_group", ""),
                        "comment": normalized.get("comment", ""),
                        "net_name": normalized.get("net_name", ""),
                        "fldigi_mode": normalized.get("fldigi_mode", ""),
                        "fldigi_offset": normalized.get("fldigi_offset", ""),
                        "target_scope": normalized.get("target_scope", TARGET_SCOPE_STATION),
                        "target_device_profile_id": normalized.get("target_device_profile_id"),
                        "target_operating_profile_id": normalized.get("target_operating_profile_id"),
                    }
                )
            Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            QMessageBox.information(self, "Exported", f"Net schedule exported to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"Could not export:\n{e}")
            log.error("Net schedule export failed: %s", e)

    def _import_schedule(self) -> None:
        """Backward-compatible wrapper: imports now target Net Resources."""
        self._import_schedule_to_resources()

    # --------- SQLite mirror --------- #

    def _save_to_db(self, rows: List[Dict]):
        """
        Persist net schedule rows into SQLite tables in config/freqinout_nets.db.
        Writes both net_schedule_tab and the legacy net_schedule table for backwards compatibility.
        """
        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        try:
            linked_rows, created_resources, linked_resources = self._ensure_manual_schedule_resources(conn, rows)
            conn.execute("DELETE FROM net_schedule_tab")
            conn.execute("DELETE FROM net_schedule")
            self._insert_rows(conn, linked_rows)
            finalize_legacy_resource_projection(conn)
            conn.commit()
            if created_resources or linked_resources:
                log.info(
                    "NetSchedule: linked %d net schedule row(s) to resources; created %d manual resource(s).",
                    linked_resources,
                    created_resources,
                )
            log.info("Net schedule mirrored to DB at %s (%d entries).", db_path, len(rows))
        finally:
            conn.close()

    def _insert_rows(self, conn: sqlite3.Connection, rows: List[Dict]) -> None:
        """Insert rows into startup-owned schedule tables."""
        self._insert_rows_inner(conn, rows)

    def _insert_rows_inner(self, conn: sqlite3.Connection, rows: List[Dict]) -> None:
        for row in rows:
            normalized = normalize_schedule_target_fields(row)
            conn.execute(
                """
                INSERT INTO net_schedule_tab
                  (day_utc, recurrence, biweekly_offset_weeks, month_weeks, band, mode, vfo, frequency, start_utc, end_utc,
                   early_checkin, auto_tune, primary_js8call_group, comment, net_name, group_name, fldigi_mode, fldigi_offset,
                   resource_id, net_session_key, accepted_session_version_hash, accepted_resource_version_hash,
                   accepted_snapshot_json, target_scope, target_device_profile_id, target_operating_profile_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.get("day_utc"),
                    normalized.get("recurrence", "Weekly"),
                    int(normalized.get("biweekly_offset_weeks", 0) or 0),
                    normalized.get("month_weeks", ""),
                    normalized.get("band"),
                    normalized.get("mode"),
                    normalized.get("vfo"),
                    normalized.get("frequency"),
                    normalized.get("start_utc"),
                    normalized.get("end_utc"),
                    int(normalized.get("early_checkin", "0") or 0),
                    1 if normalized.get("auto_tune") else 0,
                    normalized.get("primary_js8call_group"),
                    normalized.get("comment"),
                    normalized.get("net_name"),
                    normalized.get("group_name"),
                    normalized.get("fldigi_mode", ""),
                    normalized.get("fldigi_offset", ""),
                    normalized.get("_resource_id"),
                    normalized.get("net_session_key"),
                    normalized.get("accepted_session_version_hash"),
                    normalized.get("accepted_resource_version_hash"),
                    normalized.get("accepted_snapshot_json"),
                    normalized.get("target_scope"),
                    normalized.get("target_device_profile_id"),
                    normalized.get("target_operating_profile_id"),
                ),
            )
            conn.execute(
                """
                INSERT INTO net_schedule
                  (day_utc, recurrence, biweekly_offset_weeks, month_weeks, band, mode, frequency, start_utc, end_utc,
                   early_checkin, auto_tune, primary_js8call_group, comment, net_name, group_name, fldigi_mode, fldigi_offset,
                   net_session_key, accepted_session_version_hash, accepted_resource_version_hash,
                   accepted_snapshot_json, target_scope, target_device_profile_id, target_operating_profile_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.get("day_utc"),
                    normalized.get("recurrence", "Weekly"),
                    int(normalized.get("biweekly_offset_weeks", 0) or 0),
                    normalized.get("month_weeks", ""),
                    normalized.get("band"),
                    normalized.get("mode"),
                    normalized.get("frequency"),
                    normalized.get("start_utc"),
                    normalized.get("end_utc"),
                    int(normalized.get("early_checkin", "0") or 0),
                    1 if normalized.get("auto_tune") else 0,
                    normalized.get("primary_js8call_group"),
                    normalized.get("comment"),
                    normalized.get("net_name"),
                    normalized.get("group_name"),
                    normalized.get("fldigi_mode", ""),
                    normalized.get("fldigi_offset", ""),
                    normalized.get("net_session_key"),
                    normalized.get("accepted_session_version_hash"),
                    normalized.get("accepted_resource_version_hash"),
                    normalized.get("accepted_snapshot_json"),
                    normalized.get("target_scope"),
                    normalized.get("target_device_profile_id"),
                    normalized.get("target_operating_profile_id"),
                ),
            )

    # --------- Net resources --------- #

    def _find_resource_match_for_schedule_row(
        self,
        conn: sqlite3.Connection,
        row: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        normalized = self._strip_internal_row(row)
        recurrence = str(normalized.get("recurrence") or "Weekly").strip()
        if recurrence == "Monthly":
            recurrence = "Periodic"
        if recurrence == "Bi-Weekly":
            recurrence = "Weekly"
        if recurrence not in ("Weekly", "Daily", "Periodic"):
            recurrence = "Weekly"
        month_weeks = self._format_month_weeks(str(normalized.get("month_weeks") or ""))
        if recurrence != "Periodic":
            month_weeks = ""
        freq_key = self._normalize_freq_key(normalized.get("frequency"))
        try:
            freq_num = float(freq_key)
        except Exception:
            freq_num = None
        found = conn.execute(
            """
            SELECT id, resource_set
            FROM net_resources
            WHERE TRIM(day_utc)=TRIM(?)
              AND TRIM(COALESCE(recurrence,''))=TRIM(?)
              AND TRIM(COALESCE(month_weeks,''))=TRIM(?)
              AND UPPER(TRIM(COALESCE(group_name,'')))=UPPER(TRIM(?))
              AND UPPER(TRIM(COALESCE(band,'')))=UPPER(TRIM(?))
              AND UPPER(TRIM(COALESCE(mode,'')))=UPPER(TRIM(?))
              AND TRIM(start_utc)=TRIM(?)
              AND TRIM(end_utc)=TRIM(?)
              AND UPPER(TRIM(COALESCE(net_name,'')))=UPPER(TRIM(?))
              AND UPPER(TRIM(COALESCE(fldigi_mode,'')))=UPPER(TRIM(?))
              AND TRIM(COALESCE(fldigi_offset,''))=TRIM(?)
              AND (
                    (CAST(? AS REAL) IS NOT NULL AND ABS(CAST(COALESCE(frequency,'0') AS REAL) - CAST(? AS REAL)) < 0.000001)
                    OR TRIM(COALESCE(frequency,''))=TRIM(?)
                  )
            ORDER BY
              CASE LOWER(TRIM(COALESCE(source_type,'')))
                WHEN 'builtin' THEN 0
                WHEN 'imported' THEN 1
                WHEN 'manual' THEN 2
                ELSE 3
              END,
              id ASC
            LIMIT 1
            """,
            (
                self._normalize_day(str(normalized.get("day_utc") or "")),
                recurrence,
                month_weeks,
                str(normalized.get("group_name") or "").strip(),
                str(normalized.get("band") or "").strip(),
                str(normalized.get("mode") or "").strip(),
                self._normalize_hhmm(str(normalized.get("start_utc") or "")),
                self._normalize_hhmm(str(normalized.get("end_utc") or "")),
                str(normalized.get("net_name") or "").strip(),
                str(normalized.get("fldigi_mode") or "").strip(),
                str(normalized.get("fldigi_offset") or "").strip(),
                freq_num if freq_num is not None else None,
                freq_num if freq_num is not None else None,
                freq_key,
            ),
        ).fetchone()
        if not found:
            return None
        try:
            rid = int(found[0] or 0)
        except Exception:
            rid = 0
        if rid <= 0:
            return None
        return {
            "id": rid,
            "resource_set": str(found[1] or "").strip() or "Custom",
        }

    def _ensure_manual_schedule_resources(
        self,
        conn: sqlite3.Connection,
        rows: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], int, int]:
        linked_rows: List[Dict[str, Any]] = []
        created = 0
        linked = 0
        for row in rows:
            normalized = normalize_schedule_target_fields(dict(row))
            resource_id = normalized.get("_resource_id")
            if resource_id not in (None, ""):
                existing_resource = None
                try:
                    existing_resource = self._load_resource_row_by_id(conn, int(resource_id))
                except Exception:
                    existing_resource = None
                if existing_resource and self._schedule_row_matches_resource_row(normalized, existing_resource):
                    linked_rows.append(normalized)
                    continue
                normalized.pop("_resource_id", None)
                normalized.pop("_resource_set", None)
                if existing_resource:
                    linked_rows.append(normalized)
                    continue
            match = self._find_resource_match_for_schedule_row(conn, normalized)
            if match:
                normalized["_resource_id"] = int(match["id"])
                normalized["_resource_set"] = str(match.get("resource_set") or "Custom")
                linked += 1
                linked_rows.append(normalized)
                continue
            rid = self._upsert_resource_row(
                conn,
                normalized,
                resource_set="Custom",
                source_type="manual",
                source_ref="auto_from_schedule",
                readonly=1,
                resource_id=None,
                update_existing=False,
            )
            if rid:
                normalized["_resource_id"] = int(rid)
                normalized["_resource_set"] = "Custom"
                created += 1
                linked += 1
            linked_rows.append(normalized)
        return linked_rows, created, linked

    @staticmethod
    def _resource_source_label(source_type: str) -> str:
        key = (source_type or "").strip().lower()
        mapping = {
            "builtin": "Built-in",
            "imported": "Imported",
            "manual": "Manual",
            "migrated": "Migrated",
        }
        return mapping.get(key, "Manual")

    @staticmethod
    def _normalize_day(day: str) -> str:
        d = (day or "").strip()
        if not d:
            return ""
        for opt in DAY_OPTIONS:
            if d.lower() == opt.lower():
                return opt
        return d

    @staticmethod
    def _normalize_hhmm(text: str) -> str:
        txt = (text or "").strip()
        if not txt:
            return ""
        try:
            h, m = [int(x) for x in txt.split(":")]
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}"
        except Exception:
            pass
        return txt

    def _normalize_freq_key(self, freq: Any) -> str:
        return format_frequency_mhz(freq)

    def _default_fldigi_for_row(self, group_name: str, band: str, mode: str) -> Tuple[str, str]:
        g = (group_name or "").strip()
        b = (band or "").strip()
        m = (mode or "").strip()
        if not (g and b and m):
            return "", ""
        result = resolve_operating_group_frequency(self.operating_groups, group=g, band=b, mode=m)
        if not result.resolved:
            return "", ""
        chosen = result.row
        fld_mode = (chosen.get("fldigi_mode") or "").strip()
        fld_offset = (chosen.get("fldigi_offset") or "").strip()
        return fld_mode, fld_offset

    def _schedule_dup_key(self, row: Dict[str, Any]) -> Tuple[str, str, str, str, str, str, str, str, str]:
        target_scope, target_device_profile_id, target_operating_profile_id = schedule_target_identity_parts(row)
        return (
            self._normalize_day(str(row.get("day_utc") or "")),
            self._normalize_hhmm(str(row.get("start_utc") or "")),
            self._normalize_hhmm(str(row.get("end_utc") or "")),
            str(row.get("band") or "").strip().upper(),
            self._normalize_freq_key(row.get("frequency")),
            str(row.get("mode") or "").strip().upper(),
            target_scope,
            target_device_profile_id,
            target_operating_profile_id,
        )

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _builtin_resource_files(self) -> List[Tuple[Path, str]]:
        root = Path(__file__).resolve().parents[2]
        base = root / "config" / "net_resources"
        winter = base / "sitrepnets-winter.json"
        if not winter.exists():
            winter = base / "sitrepnets-fall.json"
        return [
            (winter, "Winter"),
            (base / "sitrepnets-summer.json", "Summer"),
        ]

    @staticmethod
    def _guess_resource_set_from_path(path: Path) -> str:
        stem = path.stem.lower()
        if "winter" in stem or "fall" in stem:
            return "Winter"
        if "summer" in stem:
            return "Summer"
        return "Imported"

    def _normalize_import_row(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        day = (row.get("day_utc") or "").strip()
        band = (row.get("band") or "").strip()
        mode = (row.get("mode") or "").strip()
        freq = str(row.get("frequency") or "").strip()
        start = (row.get("start_utc") or "").strip()
        end = (row.get("end_utc") or "").strip()
        if not (day and band and freq and start and end and mode):
            return None
        recurrence = (row.get("recurrence") or "Weekly").strip()
        if recurrence == "Monthly":
            recurrence = "Periodic"
        if recurrence == "Bi-Weekly":
            recurrence = "Weekly"
        if recurrence not in ("Weekly", "Daily", "Periodic"):
            recurrence = "Weekly"
        try:
            biweekly_offset = int(row.get("biweekly_offset_weeks", 0) or 0)
        except Exception:
            biweekly_offset = 0
        try:
            early_checkin = int(row.get("early_checkin", 0) or 0)
        except Exception:
            early_checkin = 0
        raw_month_weeks = (
            row.get("month_weeks")
            or row.get("week_of_month")
            or row.get("Week of Month")
            or ""
        )
        month_weeks = self._format_month_weeks(str(raw_month_weeks))
        if recurrence != "Periodic":
            month_weeks = ""
        elif not month_weeks:
            month_weeks = "1"
        fldigi_mode = (row.get("fldigi_mode") or "").strip()
        fldigi_offset = (row.get("fldigi_offset") or "").strip()
        coverage = (row.get("coverage") or row.get("Coverage") or "").strip()
        if not fldigi_mode or not fldigi_offset:
            d_mode, d_offset = self._default_fldigi_for_row(
                (row.get("group_name") or "").strip(),
                band,
                mode,
            )
            if not fldigi_mode:
                fldigi_mode = d_mode
            if not fldigi_offset:
                fldigi_offset = d_offset
        normalized = {
            "day_utc": self._normalize_day(day),
            "recurrence": recurrence,
            "biweekly_offset_weeks": biweekly_offset,
            "month_weeks": month_weeks,
            "group_name": (row.get("group_name") or "").strip(),
            "band": band,
            "mode": mode,
            "frequency": self._format_freq(freq),
            "start_utc": self._normalize_hhmm(start),
            "end_utc": self._normalize_hhmm(end),
            "early_checkin": str(early_checkin),
            "primary_js8call_group": (row.get("primary_js8call_group") or "").strip(),
            "coverage": coverage,
            "comment": (row.get("comment") or "").strip(),
            "net_name": (row.get("net_name") or "").strip(),
            "fldigi_mode": fldigi_mode,
            "fldigi_offset": fldigi_offset,
        }
        return normalized

    def _parse_schedule_json(self, path: Path) -> List[Dict[str, Any]]:
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except Exception as e:
            log.error("Net resources import failed for %s: %s", path, e)
            return []
        rows = payload.get("rows", [])
        if not isinstance(rows, list):
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized = self._normalize_import_row(row)
            if normalized is not None:
                out.append(normalized)
        return out

    def _upsert_resource_row(
        self,
        conn: sqlite3.Connection,
        row: Dict[str, Any],
        *,
        resource_set: str,
        source_type: str,
        source_ref: str,
        readonly: int = 1,
        resource_id: Optional[int] = None,
        update_existing: bool = True,
    ) -> int:
        """Write through the single legacy/canonical compatibility boundary."""
        normalized = self._strip_internal_row(row)
        normalized["frequency"] = self._normalize_freq_key(normalized.get("frequency"))
        return upsert_legacy_resource(
            conn,
            normalized,
            resource_set=resource_set,
            source_type=source_type,
            source_ref=source_ref,
            readonly=readonly,
            resource_id=resource_id,
            update_existing=update_existing,
            synchronize=False,
        )

    def _dedupe_net_resources(self, conn: sqlite3.Connection) -> int:
        """Delegate dedupe to the single compatibility transaction owner."""
        return dedupe_legacy_resources(conn, synchronize=False)

    def _sync_builtin_resource_sets(
        self,
        conn: sqlite3.Connection,
        *,
        force: bool = False,
    ) -> int:
        try:
            current_version = int(self.settings.get("net_resources_builtin_sync_version", 0) or 0)
        except Exception:
            current_version = 0
        if not force and current_version >= BUILTIN_NET_RESOURCES_SYNC_VERSION:
            return 0

        updated_sets = 0
        for path, resource_set in self._builtin_resource_files():
            if not path.exists():
                continue
            rows = self._parse_schedule_json(path)
            if not rows:
                continue
            existing_ids = conn.execute(
                """SELECT id FROM net_resources
                     WHERE LOWER(TRIM(COALESCE(source_type, ''))) = 'builtin'
                       AND TRIM(COALESCE(resource_set, '')) = TRIM(?)""",
                (resource_set,),
            ).fetchall()
            delete_legacy_resources(conn, (item[0] for item in existing_ids), synchronize=False)
            for row in rows:
                self._upsert_resource_row(
                    conn,
                    row,
                    resource_set=resource_set,
                    source_type="builtin",
                    source_ref=path.name,
                    readonly=1,
                    resource_id=None,
                    update_existing=False,
                )
            updated_sets += 1

        if updated_sets > 0:
            self.settings.set("net_resources_builtin_sync_version", BUILTIN_NET_RESOURCES_SYNC_VERSION)
        return updated_sets

    def _bootstrap_net_resources(self) -> None:
        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        try:
            current_count = int(conn.execute("SELECT COUNT(*) FROM net_resources").fetchone()[0] or 0)
            updated_builtin_sets = self._sync_builtin_resource_sets(conn, force=(current_count == 0))
            if updated_builtin_sets:
                log.info("NetSchedule: refreshed %d builtin net resource set(s)", updated_builtin_sets)

            migrated = bool(self.settings.get("net_resources_migrated_v1", False))
            if not migrated:
                active_rows = self._load_from_db()
                if not active_rows:
                    raw = self.settings.get("net_schedule", [])
                    if isinstance(raw, list):
                        for r in raw:
                            if isinstance(r, dict):
                                active_rows.append(r)
                for row in active_rows:
                    self._upsert_resource_row(
                        conn,
                        row,
                        resource_set="Custom",
                        source_type="migrated",
                        source_ref="upgrade",
                        readonly=1,
                        resource_id=None,
                    )
                self.settings.set("net_resources_migrated_v1", True)
            removed = self._dedupe_net_resources(conn)
            if removed:
                log.info("NetSchedule: deduped %d net resource rows during bootstrap", removed)
            finalize_legacy_resource_projection(conn)
            conn.commit()
        except Exception as e:
            log.error("NetSchedule: failed bootstrapping net resources: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()

    def _load_resources_from_db(self) -> None:
        db_path = self._db_path()
        self._resource_rows = []
        if not db_path.exists():
            return
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                """
                SELECT
                    id, resource_set, source_type, source_ref, readonly, day_utc, recurrence,
                    biweekly_offset_weeks, month_weeks, group_name, band, mode, frequency,
                    start_utc, end_utc, early_checkin, primary_js8call_group, coverage, comment, net_name,
                    fldigi_mode, fldigi_offset, updated_utc
                  FROM net_resources
                """
            )
            out: List[Dict[str, Any]] = []
            for row in cur.fetchall():
                out.append(
                    {
                        "id": int(row[0]),
                        "resource_set": row[1] or "Custom",
                        "source_type": row[2] or "manual",
                        "source_ref": row[3] or "",
                        "readonly": int(row[4] or 0),
                        "day_utc": row[5] or "",
                        "recurrence": row[6] or "Weekly",
                        "biweekly_offset_weeks": int(row[7] or 0),
                        "month_weeks": row[8] or "",
                        "group_name": row[9] or "",
                        "band": row[10] or "",
                        "mode": row[11] or "",
                        "frequency": str(row[12] or ""),
                        "start_utc": row[13] or "",
                        "end_utc": row[14] or "",
                        "early_checkin": str(row[15] if row[15] is not None else 0),
                        "primary_js8call_group": row[16] or "",
                        "coverage": row[17] or "",
                        "comment": row[18] or "",
                        "net_name": row[19] or "",
                        "fldigi_mode": row[20] or "",
                        "fldigi_offset": row[21] or "",
                        "updated_utc": row[22] or "",
                    }
                )
            out.sort(
                key=lambda r: (
                    str(r.get("resource_set") or ""),
                    str(r.get("day_utc") or ""),
                    str(r.get("start_utc") or ""),
                    str(r.get("net_name") or ""),
                )
            )
            self._resource_rows = out
        except Exception as e:
            log.error("NetSchedule: failed loading net resources: %s", e)
            self._resource_rows = []
        finally:
            conn.close()

    def _refresh_resource_set_combo(self) -> None:
        current_saved = str(self.settings.get("net_resources_selected_set", "All") or "All")
        current = current_saved
        if current_saved == "Fall":
            current_saved = "Winter"
        if current == "Fall":
            current = "Winter"
        if hasattr(self, "resource_set_combo"):
            selected = self.resource_set_combo.currentData()
            if selected not in (None, ""):
                current = str(selected)
        if current == "Fall":
            current = "Winter"
        sets = sorted({str(r.get("resource_set") or "Custom") for r in self._resource_rows})
        preferred: List[str] = ["All"]
        for key in ("Winter", "Summer", "Custom", "Imported", "Fall"):
            if key in sets:
                preferred.append(key)
                sets.remove(key)
        preferred.extend(sets)
        self.resource_set_combo.blockSignals(True)
        self.resource_set_combo.clear()
        for name in preferred:
            self.resource_set_combo.addItem(name, name)
        idx = self.resource_set_combo.findData(current)
        if idx < 0:
            idx = self.resource_set_combo.findData(current_saved)
        if idx < 0:
            idx = self.resource_set_combo.findData("All")
        self.resource_set_combo.setCurrentIndex(max(0, idx))
        self.resource_set_combo.blockSignals(False)

    def _on_resource_set_changed(self) -> None:
        selected = str(self.resource_set_combo.currentData() or "All")
        self.settings.set("net_resources_selected_set", selected)
        self._refresh_resources_table()

    def _resource_matches_search(self, row: Dict[str, Any], term: str) -> bool:
        if not term:
            return True
        blob = " ".join(
            [
                self._resource_source_label(str(row.get("source_type") or "")),
                str(row.get("resource_set") or ""),
                str(row.get("day_utc") or ""),
                str(row.get("recurrence") or ""),
                str(row.get("month_weeks") or ""),
                str(row.get("group_name") or ""),
                str(row.get("mode") or ""),
                str(row.get("band") or ""),
                str(row.get("frequency") or ""),
                str(row.get("start_utc") or ""),
                str(row.get("end_utc") or ""),
                str(row.get("early_checkin") or ""),
                str(row.get("fldigi_mode") or ""),
                str(row.get("fldigi_offset") or ""),
                str(row.get("net_name") or ""),
                str(row.get("coverage") or ""),
                str(row.get("comment") or ""),
                str(row.get("updated_utc") or ""),
            ]
        ).lower()
        return term in blob

    def _refresh_resources_table(self) -> None:
        selected_set = str(self.resource_set_combo.currentData() or "All")
        term = (self.resource_search.text() or "").strip().lower()
        rows = [
            r
            for r in self._resource_rows
            if (selected_set == "All" or str(r.get("resource_set") or "") == selected_set)
        ]
        if term:
            rows = [r for r in rows if self._resource_matches_search(r, term)]
        self._resource_view_rows = rows
        self.resources_table.setSortingEnabled(False)
        self.resources_table.setRowCount(0)
        for row in rows:
            r = self.resources_table.rowCount()
            self.resources_table.insertRow(r)
            values = [
                self._resource_source_label(str(row.get("source_type") or "")),
                str(row.get("resource_set") or ""),
                str(row.get("day_utc") or ""),
                str(row.get("recurrence") or ""),
                str(row.get("month_weeks") or ""),
                str(row.get("group_name") or ""),
                str(row.get("mode") or ""),
                str(row.get("band") or ""),
                str(row.get("frequency") or ""),
                str(row.get("start_utc") or ""),
                str(row.get("end_utc") or ""),
                str(row.get("early_checkin") or ""),
                str(row.get("fldigi_mode") or ""),
                str(row.get("fldigi_offset") or ""),
                str(row.get("net_name") or ""),
                str(row.get("coverage") or ""),
                str(row.get("comment") or ""),
                self._format_age_label(row.get("updated_utc")),
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if c == self.RES_COL_SOURCE:
                    item.setData(Qt.UserRole, int(row.get("id") or 0))
                if c == self.RES_COL_UPDATED:
                    raw_updated = str(row.get("updated_utc") or "").strip()
                    item.setToolTip(raw_updated if raw_updated else "No saved timestamp available.")
                self.resources_table.setItem(r, c, item)
        self.resources_table.setSortingEnabled(True)
        if hasattr(self, "resources_count_label"):
            total = len(getattr(self, "_resource_rows", []) or [])
            shown = len(rows)
            self.resources_count_label.setText(f"{shown} shown / {total} total" if total else "0 resources")
        self._update_resource_action_state()
        self._resize_table_columns()
        self._apply_schedule_table_height_hints()

    def _update_resource_action_state(self) -> None:
        selected_rows = {idx.row() for idx in self.resources_table.selectionModel().selectedRows()} if self.resources_table.selectionModel() else set()
        has_selected = bool(selected_rows)
        has_single = len(selected_rows) == 1
        has_filtered = bool(self._resource_view_rows)
        set_name = str(self.resource_set_combo.currentData() or "All")
        has_set_rows = bool([r for r in self._resource_rows if set_name == "All" or str(r.get("resource_set") or "") == set_name])
        self.add_to_schedule_btn.setEnabled(has_selected or has_filtered)
        self.add_selected_resource_action.setEnabled(has_selected)
        self.add_filtered_resource_action.setEnabled(has_filtered)
        self.add_to_schedule_default_action.setEnabled(has_selected or has_filtered)
        self.add_to_schedule_default_action.setText("Add Selected Rows" if has_selected else "Add Filtered Rows")
        self.add_selected_resource_action.setText("Add Selected Rows")
        self.add_filtered_resource_action.setText("Add Filtered Rows")
        self.add_to_schedule_btn.setText("Add Selected Rows" if has_selected else "Add Filtered Rows")
        self.add_to_schedule_btn.setToolTip(
            "Copy selected library rows into the HF Net schedule being edited. Library rows stay saved."
            if has_selected
            else "Copy all currently filtered library rows into the HF Net schedule being edited."
        )
        self.manage_resources_btn.setEnabled(True)
        self.manage_resources_default_action.setEnabled(True)
        self.manage_import_json_action.setEnabled(True)
        self.manage_export_new_action.setEnabled(has_set_rows)
        self.edit_resource_btn.setEnabled(has_single)
        self.delete_resource_btn.setEnabled(has_selected)
        theme = resolve_theme(self.settings)
        self.add_to_schedule_btn.setStyleSheet(
            button_style("eligible_primary" if has_selected else "muted", theme)
        )
        self.manage_resources_btn.setStyleSheet(button_style("muted", theme))
        self.edit_resource_btn.setStyleSheet(button_style("eligible_info" if has_single else "muted", theme))
        self.delete_resource_btn.setStyleSheet(button_style("eligible_danger" if has_selected else "muted", theme))

    def _selected_resource_rows(self) -> List[Dict[str, Any]]:
        rows_idx = (
            {idx.row() for idx in self.resources_table.selectionModel().selectedRows()}
            if self.resources_table.selectionModel()
            else set()
        )
        if not rows_idx:
            return []
        id_map: Dict[int, Dict[str, Any]] = {}
        for row in self._resource_rows:
            try:
                rid = int(row.get("id") or 0)
            except Exception:
                rid = 0
            if rid > 0:
                id_map[rid] = row
        out: List[Dict[str, Any]] = []
        for r in sorted(rows_idx):
            item = self.resources_table.item(r, self.RES_COL_SOURCE)
            rid = int(item.data(Qt.UserRole) or 0) if item is not None else 0
            if rid > 0 and rid in id_map:
                out.append(dict(id_map[rid]))
                continue
            if 0 <= r < len(self._resource_view_rows):
                out.append(dict(self._resource_view_rows[r]))
        return out

    def _resource_rows_for_set(self, set_name: str) -> List[Dict[str, Any]]:
        key = (set_name or "All").strip()
        if key == "All":
            return [dict(r) for r in self._resource_rows]
        return [dict(r) for r in self._resource_rows if str(r.get("resource_set") or "") == key]

    @staticmethod
    def _resource_file_slug(name: str) -> str:
        txt = (name or "").strip().lower()
        txt = re.sub(r"[^a-z0-9]+", "-", txt).strip("-")
        return txt or "resources"

    def _resource_export_payload(self, set_name: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        data = self.settings.all()
        callsign = (data.get("operator_callsign") or "").strip().upper() or "UNKNOWN"
        payload_rows: List[Dict[str, Any]] = []
        for r in rows:
            payload_rows.append(
                {
                    "day_utc": r.get("day_utc", ""),
                    "recurrence": r.get("recurrence", "Weekly"),
                    "biweekly_offset_weeks": int(r.get("biweekly_offset_weeks", 0) or 0),
                    "month_weeks": r.get("month_weeks", ""),
                    "group_name": r.get("group_name", ""),
                    "band": r.get("band", ""),
                    "mode": r.get("mode", ""),
                    "frequency": self._normalize_freq_key(r.get("frequency")),
                    "start_utc": r.get("start_utc", ""),
                    "end_utc": r.get("end_utc", ""),
                    "early_checkin": int(r.get("early_checkin", 0) or 0),
                    "auto_tune": None,
                    "primary_js8call_group": r.get("primary_js8call_group", ""),
                    "coverage": r.get("coverage", ""),
                    "comment": r.get("comment", ""),
                    "net_name": r.get("net_name", ""),
                    "fldigi_mode": r.get("fldigi_mode", ""),
                    "fldigi_offset": r.get("fldigi_offset", ""),
                }
            )
        payload_rows.sort(
            key=lambda r: (
                str(r.get("day_utc") or ""),
                str(r.get("start_utc") or ""),
                str(r.get("net_name") or ""),
            )
        )
        return {
            "callsign": callsign,
            "resource_set": set_name,
            "created_utc": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
            "rows": payload_rows,
        }

    def _non_overwrite_path(self, target: Path) -> Path:
        """
        Ensure exports never overwrite an existing file by suffixing _N.
        """
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix or ".json"
        parent = target.parent
        n = 1
        while True:
            candidate = parent / f"{stem}_{n}{suffix}"
            if not candidate.exists():
                return candidate
            n += 1

    def _export_new_resource_file(self) -> None:
        set_name = str(self.resource_set_combo.currentData() or "All")
        rows = self._resource_rows_for_set(set_name)
        if not rows:
            QMessageBox.warning(self, "Export Net Resources", f"No rows found for set '{set_name}'.")
            return
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        slug = self._resource_file_slug(set_name if set_name != "All" else "all")
        default_name = f"net_resources_{slug}_{timestamp}.json"
        # Intentionally use the same chooser behavior/location as Export Net Schedule.
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Net Resources",
            default_name,
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != ".json":
            target = target.with_suffix(".json")
        target = self._non_overwrite_path(target)
        payload = self._resource_export_payload(set_name, rows)
        try:
            target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            QMessageBox.information(self, "Exported", f"Exported resources to:\n{target}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"Could not export resource file:\n{e}")

    def _export_resource_set_json(self) -> None:
        """
        Backward-compatible wrapper for prior action wiring.
        """
        self._export_new_resource_file()

    def _edit_resource_dialog(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Net Resource")
        form = QFormLayout(dlg)

        set_combo = QComboBox(dlg)
        known_sets = sorted({str(r.get("resource_set") or "") for r in self._resource_rows if str(r.get("resource_set") or "").strip()})
        ordered_sets: List[str] = []
        for key in ("Winter", "Summer", "Custom", "Imported", "Fall"):
            if key in known_sets:
                ordered_sets.append(key)
                known_sets.remove(key)
        ordered_sets.extend(known_sets)
        if (row.get("resource_set") or "") and str(row.get("resource_set")) not in ordered_sets:
            ordered_sets.append(str(row.get("resource_set")))
        if not ordered_sets:
            ordered_sets = ["Custom"]
        set_combo.addItems(ordered_sets)
        set_combo.setCurrentText(str(row.get("resource_set") or "Custom"))
        form.addRow("Set:", set_combo)

        day_combo = QComboBox(dlg)
        day_combo.addItems(DAY_OPTIONS)
        day_combo.setCurrentText(str(row.get("day_utc") or "ALL"))
        form.addRow("Day (UTC):", day_combo)

        recurrence_combo = QComboBox(dlg)
        recurrence_combo.addItems(["Weekly", "Daily", "Periodic"])
        rec_val = str(row.get("recurrence") or "Weekly")
        if rec_val == "Monthly":
            rec_val = "Periodic"
        recurrence_combo.setCurrentText(rec_val if rec_val in {"Weekly", "Daily", "Periodic"} else "Weekly")
        form.addRow("Recurrence:", recurrence_combo)

        month_weeks_edit = QLineEdit(str(row.get("month_weeks") or ""), dlg)
        month_weeks_edit.setPlaceholderText("1,3,5 (for Periodic)")
        form.addRow("Weeks of Month:", month_weeks_edit)

        group_edit = QLineEdit(str(row.get("group_name") or ""), dlg)
        form.addRow("Group:", group_edit)

        mode_edit = QLineEdit(str(row.get("mode") or ""), dlg)
        form.addRow("Mode:", mode_edit)

        band_edit = QLineEdit(str(row.get("band") or ""), dlg)
        form.addRow("Band:", band_edit)

        freq_edit = QLineEdit(self._normalize_freq_key(row.get("frequency")), dlg)
        form.addRow("Frequency (MHz):", freq_edit)

        start_edit = QLineEdit(str(row.get("start_utc") or ""), dlg)
        end_edit = QLineEdit(str(row.get("end_utc") or ""), dlg)
        form.addRow("Start (UTC HH:MM):", start_edit)
        form.addRow("End (UTC HH:MM):", end_edit)

        early_combo = QComboBox(dlg)
        early_combo.addItems(["0", "5", "10", "15"])
        early_combo.setCurrentText(str(row.get("early_checkin") or "0"))
        form.addRow("Early Check-in (min):", early_combo)

        fldigi_mode_edit = self._build_fldigi_mode_combo(str(row.get("fldigi_mode") or ""))
        fldigi_offset_edit = QLineEdit(str(row.get("fldigi_offset") or ""), dlg)
        fldigi_offset_edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9]*")))
        form.addRow("FLDigi Starting Mode:", fldigi_mode_edit)
        form.addRow("FLDigi Starting Offset (Hz):", fldigi_offset_edit)

        net_name_edit = QLineEdit(str(row.get("net_name") or ""), dlg)
        coverage_edit = QLineEdit(str(row.get("coverage") or ""), dlg)
        comment_edit = QLineEdit(str(row.get("comment") or ""), dlg)
        form.addRow("Net Name:", net_name_edit)
        form.addRow("Coverage:", coverage_edit)
        form.addRow("Comment:", comment_edit)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dlg)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() != QDialog.Accepted:
            return None

        candidate = {
            "day_utc": day_combo.currentText().strip(),
            "recurrence": recurrence_combo.currentText().strip(),
            "biweekly_offset_weeks": int(row.get("biweekly_offset_weeks", 0) or 0),
            "month_weeks": month_weeks_edit.text().strip(),
            "group_name": group_edit.text().strip(),
            "band": band_edit.text().strip(),
            "mode": mode_edit.text().strip(),
            "frequency": freq_edit.text().strip(),
            "start_utc": start_edit.text().strip(),
            "end_utc": end_edit.text().strip(),
            "early_checkin": early_combo.currentText().strip() or "0",
            "primary_js8call_group": str(row.get("primary_js8call_group") or "").strip(),
            "coverage": coverage_edit.text().strip(),
            "comment": comment_edit.text().strip(),
            "net_name": net_name_edit.text().strip(),
            "fldigi_mode": fldigi_mode_edit.currentText().strip(),
            "fldigi_offset": fldigi_offset_edit.text().strip(),
        }
        normalized = self._normalize_import_row(candidate)
        if normalized is None:
            QMessageBox.warning(
                self,
                "Invalid Resource",
                "Required fields are missing or invalid (day/band/mode/frequency/start/end).",
            )
            return None
        normalized["resource_set"] = set_combo.currentText().strip() or "Custom"
        return normalized

    def _edit_selected_resource(self) -> None:
        rows = self._selected_resource_rows()
        if len(rows) != 1:
            QMessageBox.information(self, "Edit Resource", "Select exactly one resource row to edit.")
            return
        original = rows[0]
        edited = self._edit_resource_dialog(original)
        if edited is None:
            return
        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        try:
            self._upsert_resource_row(
                conn,
                edited,
                resource_set=str(edited.get("resource_set") or "Custom"),
                source_type="manual",
                source_ref="ui_edit",
                readonly=1,
                resource_id=int(original.get("id") or 0),
            )
            finalize_legacy_resource_projection(conn)
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            QMessageBox.critical(self, "Edit Failed", f"Could not save resource edits:\n{e}")
            return
        finally:
            conn.close()
        self._load_resources_from_db()
        self._refresh_resource_set_combo()
        idx = self.resource_set_combo.findData(str(edited.get("resource_set") or "Custom"))
        if idx >= 0:
            self.resource_set_combo.setCurrentIndex(idx)
        self._refresh_resources_table()

    def _delete_selected_resources(self) -> None:
        rows = self._selected_resource_rows()
        if not rows:
            QMessageBox.information(self, "Delete Resources", "No resources selected.")
            return
        ids = sorted({int(r.get("id") or 0) for r in rows if int(r.get("id") or 0) > 0})
        if not ids:
            QMessageBox.warning(self, "Delete Resources", "Selected rows have no persistent IDs.")
            return
        confirm = QMessageBox.question(
            self,
            "Delete Resources",
            f"Delete {len(ids)} selected resource row(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        try:
            delete_legacy_resources(conn, ids)
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            QMessageBox.critical(self, "Delete Failed", f"Could not delete resources:\n{e}")
            return
        finally:
            conn.close()
        self._load_resources_from_db()
        self._refresh_resource_set_combo()
        self._refresh_resources_table()

    def _resource_rows_to_schedule_rows(self, resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in resources:
            out.append(
                {
                    "day_utc": row.get("day_utc", ""),
                    "recurrence": row.get("recurrence", "Weekly"),
                    "biweekly_offset_weeks": int(row.get("biweekly_offset_weeks", 0) or 0),
                    "month_weeks": row.get("month_weeks", ""),
                    "group_name": row.get("group_name", ""),
                    "band": row.get("band", ""),
                    "mode": row.get("mode", ""),
                    "vfo": "A",
                    "frequency": self._normalize_freq_key(row.get("frequency")),
                    "start_utc": row.get("start_utc", ""),
                    "end_utc": row.get("end_utc", ""),
                    "early_checkin": str(row.get("early_checkin", "0")),
                    "auto_tune": False,
                    "primary_js8call_group": row.get("primary_js8call_group", ""),
                    "comment": row.get("comment", ""),
                    "net_name": row.get("net_name", ""),
                    "fldigi_mode": row.get("fldigi_mode", ""),
                    "fldigi_offset": row.get("fldigi_offset", ""),
                    "_resource_id": int(row.get("id") or 0),
                    "_resource_set": str(row.get("resource_set") or ""),
                }
            )
        return out

    def _duplicate_conflicts(
        self,
        active_rows: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
    ) -> List[str]:
        active_map: Dict[Tuple[str, str, str, str, str, str, str, str, str], Dict[str, Any]] = {}
        for row in active_rows:
            active_map[self._schedule_dup_key(row)] = row
        conflicts: List[str] = []
        for row in candidates:
            key = self._schedule_dup_key(row)
            if key not in active_map:
                continue
            existing = active_map[key]
            label = (
                f"{key[0]} {key[1]}-{key[2]} {key[3]} {key[4]} {key[5]} "
                f"(existing: {existing.get('net_name','') or '<unnamed>'}, incoming: {row.get('net_name','') or '<unnamed>'})"
            )
            conflicts.append(label)
        return conflicts

    def _add_resources_to_schedule(self, resources: List[Dict[str, Any]], *, origin: str) -> None:
        if not resources:
            if hasattr(self, "net_resources_hint"):
                self.net_resources_hint.setText("Select library rows before adding them to the schedule.")
            return
        try:
            active_rows = self._collect_rows()
        except ValueError as e:
            QMessageBox.warning(self, "Invalid Net Schedule", str(e))
            return
        candidates = self._resource_rows_to_schedule_rows(resources)
        conflicts = self._duplicate_conflicts(active_rows, candidates)
        if conflicts:
            details = "\n".join(conflicts[:20])
            if len(conflicts) > 20:
                details += f"\n... and {len(conflicts) - 20} more."
            QMessageBox.warning(
                self,
                "Duplicate Net Schedule Entries",
                "Add blocked due to duplicate day/time/band/frequency/mode entries.\n\n"
                f"{details}",
            )
            return
        prospective_rows = [self._strip_internal_row(r) for r in active_rows]
        prospective_rows.extend(self._strip_internal_row(r) for r in candidates)
        if not self._enforce_net_priority_for_conflicts(prospective_rows, operation_label="add"):
            self._schedule_net_sop_conflict_refresh(force=True)
            return
        for row in candidates:
            self._add_row(self._to_view_row(row))
        self._raw_rows = self._collect_rows()
        self._update_delete_button_state()
        self._schedule_net_sop_conflict_refresh(force=True)
        if hasattr(self, "net_resources_hint"):
            self.net_resources_hint.setText(f"Added {len(candidates)} library row(s) from {origin}. Save Schedule when ready.")

    def _add_resources_default(self) -> None:
        selected = self._selected_resource_rows()
        if selected:
            self._add_resources_to_schedule(selected, origin="selected")
            return
        rows = [dict(r) for r in self._resource_view_rows]
        self._add_resources_to_schedule(rows, origin="filtered")

    def _highlight_resource_candidates(self, resources: List[Dict[str, Any]]) -> None:
        ids = {int(r.get("id") or 0) for r in resources if int(r.get("id") or 0) > 0}
        if not ids:
            return
        self.resources_table.clearSelection()
        model = self.resources_table.selectionModel()
        if model is None:
            return
        for row_idx in range(self.resources_table.rowCount()):
            item = self.resources_table.item(row_idx, self.RES_COL_SOURCE)
            rid = int(item.data(Qt.UserRole) or 0) if item is not None else 0
            if rid <= 0 or rid not in ids:
                continue
            idx = self.resources_table.model().index(row_idx, self.RES_COL_SOURCE)
            model.select(idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)

    def _add_selected_resources_to_schedule(self) -> None:
        rows = self._selected_resource_rows()
        self._add_resources_to_schedule(rows, origin="selected")

    def _add_filtered_resources_to_schedule(self) -> None:
        rows = [dict(r) for r in self._resource_view_rows]
        self._add_resources_to_schedule(rows, origin="filtered resources")

    def _checked_schedule_row_indexes(self) -> List[int]:
        selected: List[int] = []
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, self.COL_SELECT)
            chk = None
            if isinstance(w, QCheckBox):
                chk = w
            elif isinstance(w, QWidget):
                chk = w.findChild(QCheckBox)
            if chk is not None and chk.isChecked():
                selected.append(r)
        return selected

    def _ui_row_is_empty(self, row_index: int) -> bool:
        day = self._get_combo_value(row_index, self.COL_DAY, "")
        band = self._get_combo_value(row_index, self.COL_BAND, "")
        start_item = self.table.item(row_index, self.COL_START)
        end_item = self.table.item(row_index, self.COL_END)
        net_edit = self.table.cellWidget(row_index, self.COL_NETNAME)
        freq = self._frequency_value(row_index)
        start = start_item.text().strip() if start_item else ""
        end = end_item.text().strip() if end_item else ""
        net_name = net_edit.text().strip() if isinstance(net_edit, QLineEdit) else ""
        return not (day or band or freq or start or end or net_name)

    def focus_source_segment(self, segment: Any) -> bool:
        raw = getattr(segment, "raw", {}) if segment is not None else {}
        try:
            target_row_id = int(raw.get("source_row_id") or 0)
        except Exception:
            target_row_id = 0
        target_key = str(raw.get("source_key") or "").strip()
        target_resource_id = raw.get("resource_id")
        try:
            target_resource_id_int = int(target_resource_id or 0)
        except Exception:
            target_resource_id_int = 0
        for r in range(self.table.rowCount()):
            select_widget = self.table.cellWidget(r, self.COL_SELECT)
            if isinstance(select_widget, QWidget):
                try:
                    row_id = int(select_widget.property("source_row_id") or 0)
                except Exception:
                    row_id = 0
                row_key = str(select_widget.property("source_key") or "").strip()
                try:
                    resource_id = int(select_widget.property("resource_id") or 0)
                except Exception:
                    resource_id = 0
                if (
                    (target_row_id > 0 and row_id == target_row_id)
                    or (target_key and row_key == target_key)
                    or (target_resource_id_int > 0 and resource_id == target_resource_id_int)
                ):
                    self.table.selectRow(r)
                    widget = self.table.cellWidget(r, self.COL_NETNAME)
                    item = self.table.item(r, self.COL_FREQ)
                    frequency_widget = self.table.cellWidget(r, self.COL_FREQ)
                    if item is not None:
                        self.table.scrollToItem(item)
                        self.table.setCurrentItem(item)
                    elif isinstance(frequency_widget, QWidget):
                        frequency_widget.setFocus(Qt.TabFocusReason)
                    if isinstance(widget, QLineEdit):
                        widget.setFocus(Qt.TabFocusReason)
                        widget.selectAll()
                    else:
                        self.table.setFocus(Qt.TabFocusReason)
                    return True
        return False

    def _collect_rows_by_ui_index(
        self,
        rows_override: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[int, Dict[str, Any]]:
        if isinstance(rows_override, list):
            rows = [self._strip_internal_row(r) for r in rows_override if isinstance(r, dict)]
        else:
            rows = self._collect_rows()
        mapped: Dict[int, Dict[str, Any]] = {}
        idx = 0
        for r in range(self.table.rowCount()):
            if self._ui_row_is_empty(r):
                continue
            if idx >= len(rows):
                break
            mapped[r] = rows[idx]
            idx += 1
        return mapped

    def _load_resource_row_by_id(self, conn: sqlite3.Connection, resource_id: int) -> Optional[Dict[str, Any]]:
        rid = int(resource_id or 0)
        if rid <= 0:
            return None
        row = conn.execute(
            """
            SELECT
                id, resource_set, source_type, day_utc, recurrence, biweekly_offset_weeks, month_weeks,
                group_name, band, mode, frequency, start_utc, end_utc, early_checkin,
                primary_js8call_group, comment, net_name, fldigi_mode, fldigi_offset
            FROM net_resources
            WHERE id = ?
            """,
            (rid,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": int(row[0] or 0),
            "resource_set": str(row[1] or "").strip(),
            "source_type": str(row[2] or "").strip().lower(),
            "day_utc": str(row[3] or "").strip(),
            "recurrence": str(row[4] or "Weekly").strip(),
            "biweekly_offset_weeks": int(row[5] or 0),
            "month_weeks": str(row[6] or "").strip(),
            "group_name": str(row[7] or "").strip(),
            "band": str(row[8] or "").strip(),
            "mode": str(row[9] or "").strip(),
            "frequency": str(row[10] or "").strip(),
            "start_utc": str(row[11] or "").strip(),
            "end_utc": str(row[12] or "").strip(),
            "early_checkin": int(row[13] or 0),
            "primary_js8call_group": str(row[14] or "").strip(),
            "comment": str(row[15] or "").strip(),
            "net_name": str(row[16] or "").strip(),
            "fldigi_mode": str(row[17] or "").strip(),
            "fldigi_offset": str(row[18] or "").strip(),
        }

    def _find_builtin_resource_id_for_schedule_row(
        self,
        conn: sqlite3.Connection,
        row: Dict[str, Any],
    ) -> Optional[int]:
        normalized = self._strip_internal_row(row)
        recurrence = str(normalized.get("recurrence") or "Weekly").strip()
        if recurrence == "Monthly":
            recurrence = "Periodic"
        if recurrence == "Bi-Weekly":
            recurrence = "Weekly"
        if recurrence not in ("Weekly", "Daily", "Periodic"):
            recurrence = "Weekly"
        month_weeks = self._format_month_weeks(str(normalized.get("month_weeks") or ""))
        if recurrence != "Periodic":
            month_weeks = ""
        freq_key = self._normalize_freq_key(normalized.get("frequency"))
        try:
            freq_num = float(freq_key)
        except Exception:
            freq_num = None
        found = conn.execute(
            """
            SELECT id
            FROM net_resources
            WHERE LOWER(TRIM(COALESCE(source_type,'')))='builtin'
              AND TRIM(day_utc)=TRIM(?)
              AND TRIM(COALESCE(recurrence,''))=TRIM(?)
              AND TRIM(COALESCE(month_weeks,''))=TRIM(?)
              AND UPPER(TRIM(COALESCE(group_name,'')))=UPPER(TRIM(?))
              AND UPPER(TRIM(COALESCE(band,'')))=UPPER(TRIM(?))
              AND UPPER(TRIM(COALESCE(mode,'')))=UPPER(TRIM(?))
              AND TRIM(start_utc)=TRIM(?)
              AND TRIM(end_utc)=TRIM(?)
              AND UPPER(TRIM(COALESCE(net_name,'')))=UPPER(TRIM(?))
              AND UPPER(TRIM(COALESCE(fldigi_mode,'')))=UPPER(TRIM(?))
              AND (
                    (CAST(? AS REAL) IS NOT NULL AND ABS(CAST(COALESCE(frequency,'0') AS REAL) - CAST(? AS REAL)) < 0.000001)
                    OR TRIM(COALESCE(frequency,''))=TRIM(?)
                  )
            LIMIT 1
            """,
            (
                self._normalize_day(str(normalized.get("day_utc") or "")),
                recurrence,
                month_weeks,
                str(normalized.get("group_name") or "").strip(),
                str(normalized.get("band") or "").strip(),
                str(normalized.get("mode") or "").strip(),
                self._normalize_hhmm(str(normalized.get("start_utc") or "")),
                self._normalize_hhmm(str(normalized.get("end_utc") or "")),
                str(normalized.get("net_name") or "").strip(),
                str(normalized.get("fldigi_mode") or "").strip(),
                freq_num if freq_num is not None else None,
                freq_num if freq_num is not None else None,
                freq_key,
            ),
        ).fetchone()
        if not found:
            return None
        try:
            rid = int(found[0] or 0)
        except Exception:
            rid = 0
        return rid if rid > 0 else None

    def _schedule_row_matches_resource_row(self, row: Dict[str, Any], resource_row: Dict[str, Any]) -> bool:
        schedule = self._strip_internal_row(row)
        recurrence = str(schedule.get("recurrence") or "Weekly").strip()
        if recurrence == "Monthly":
            recurrence = "Periodic"
        if recurrence == "Bi-Weekly":
            recurrence = "Weekly"
        if recurrence not in ("Weekly", "Daily", "Periodic"):
            recurrence = "Weekly"
        month_weeks = self._format_month_weeks(str(schedule.get("month_weeks") or ""))
        if recurrence != "Periodic":
            month_weeks = ""
        resource_recurrence = str(resource_row.get("recurrence") or "Weekly").strip()
        if resource_recurrence == "Monthly":
            resource_recurrence = "Periodic"
        if resource_recurrence == "Bi-Weekly":
            resource_recurrence = "Weekly"
        if resource_recurrence not in ("Weekly", "Daily", "Periodic"):
            resource_recurrence = "Weekly"
        resource_month_weeks = self._format_month_weeks(str(resource_row.get("month_weeks") or ""))
        if resource_recurrence != "Periodic":
            resource_month_weeks = ""
        # Compare only fields represented in Net Schedule UI/edit flow.
        # Fields like primary_js8call_group/comment/coverage are not present on the
        # schedule grid and should not force built-in rows into Manual source.
        return (
            self._normalize_day(str(schedule.get("day_utc") or "")) == self._normalize_day(str(resource_row.get("day_utc") or ""))
            and recurrence == resource_recurrence
            and month_weeks == resource_month_weeks
            and int(schedule.get("biweekly_offset_weeks") or 0) == int(resource_row.get("biweekly_offset_weeks") or 0)
            and str(schedule.get("group_name") or "").strip().upper() == str(resource_row.get("group_name") or "").strip().upper()
            and str(schedule.get("band") or "").strip().upper() == str(resource_row.get("band") or "").strip().upper()
            and str(schedule.get("mode") or "").strip().upper() == str(resource_row.get("mode") or "").strip().upper()
            and self._normalize_freq_key(schedule.get("frequency")) == self._normalize_freq_key(resource_row.get("frequency"))
            and self._normalize_hhmm(str(schedule.get("start_utc") or "")) == self._normalize_hhmm(str(resource_row.get("start_utc") or ""))
            and self._normalize_hhmm(str(schedule.get("end_utc") or "")) == self._normalize_hhmm(str(resource_row.get("end_utc") or ""))
            and int(schedule.get("early_checkin") or 0) == int(resource_row.get("early_checkin") or 0)
            and str(schedule.get("net_name") or "").strip().upper() == str(resource_row.get("net_name") or "").strip().upper()
            and str(schedule.get("fldigi_mode") or "").strip().upper() == str(resource_row.get("fldigi_mode") or "").strip().upper()
            and str(schedule.get("fldigi_offset") or "").strip() == str(resource_row.get("fldigi_offset") or "").strip()
        )

    def _save_selected_schedule_rows_as_resources(self) -> None:
        selected = self._checked_schedule_row_indexes()
        if not selected:
            QMessageBox.information(self, "Save as Resources", "No Net Schedule rows selected.")
            return
        try:
            by_ui = self._collect_rows_by_ui_index()
        except ValueError as e:
            QMessageBox.warning(self, "Invalid Net Schedule", str(e))
            return
        target_set = str(self.resource_set_combo.currentData() or "All")
        if target_set == "All":
            target_set = "Custom"
        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        moved = 0
        try:
            for r in selected:
                row = by_ui.get(r)
                if not row:
                    continue
                sw = self.table.cellWidget(r, self.COL_SELECT)
                existing_id = None
                existing_set = target_set
                existing_source_type = ""
                if isinstance(sw, QWidget):
                    rid = sw.property("resource_id")
                    if rid not in (None, ""):
                        try:
                            existing_id = int(rid)
                        except Exception:
                            existing_id = None
                    rset = sw.property("resource_set")
                    if rset not in (None, ""):
                        existing_set = str(rset)
                if not existing_id:
                    existing_id = self._find_builtin_resource_id_for_schedule_row(conn, row)
                existing_resource = self._load_resource_row_by_id(conn, int(existing_id or 0)) if existing_id else None
                if existing_resource:
                    existing_source_type = str(existing_resource.get("source_type") or "").strip().lower()
                    if existing_set in ("", "All"):
                        existing_set = str(existing_resource.get("resource_set") or "").strip() or target_set
                if existing_resource and existing_source_type == "builtin":
                    if self._schedule_row_matches_resource_row(row, existing_resource):
                        # Unchanged built-in row is already available as a resource.
                        moved += 1
                        continue
                self._upsert_resource_row(
                    conn,
                    row,
                    resource_set=existing_set or "Custom",
                    source_type="manual",
                    source_ref="saved_from_schedule",
                    readonly=1,
                    resource_id=existing_id,
                )
                moved += 1
            finalize_legacy_resource_projection(conn)
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            QMessageBox.critical(self, "Save Failed", f"Could not save selected rows to the Net Row Library:\n{e}")
            return
        finally:
            conn.close()
        self._load_resources_from_db()
        self._refresh_resource_set_combo()
        self._refresh_resources_table()
        self._update_delete_button_state()
        QMessageBox.information(
            self,
            "Saved to Net Row Library",
            f"Saved {moved} selected row(s) as reusable library rows. The HF Net schedule was not changed.",
        )

    def _resource_import_key(
        self, row: Dict[str, Any]
    ) -> Tuple[str, str, str, str, str, str, str, str, str]:
        recurrence = str(row.get("recurrence") or "Weekly").strip()
        if recurrence == "Monthly":
            recurrence = "Periodic"
        if recurrence == "Bi-Weekly":
            recurrence = "Weekly"
        if recurrence not in ("Weekly", "Daily", "Periodic"):
            recurrence = "Weekly"
        month_weeks = self._format_month_weeks(str(row.get("month_weeks") or ""))
        if recurrence != "Periodic":
            month_weeks = ""
        elif not month_weeks:
            month_weeks = "1"
        return (
            self._normalize_day(str(row.get("day_utc") or "")),
            recurrence,
            month_weeks,
            str(row.get("band") or "").strip().upper(),
            str(row.get("mode") or "").strip().upper(),
            self._normalize_freq_key(row.get("frequency")),
            self._normalize_hhmm(str(row.get("start_utc") or "")),
            self._normalize_hhmm(str(row.get("end_utc") or "")),
            str(row.get("fldigi_mode") or "").strip().upper(),
        )

    def _upsert_resource_row_by_import_key(
        self,
        conn: sqlite3.Connection,
        row: Dict[str, Any],
        *,
        resource_set: str,
        source_type: str,
        source_ref: str,
        readonly: int = 1,
    ) -> Tuple[bool, int]:
        """Match the legacy import key, then write through the compatibility owner."""
        normalized = self._strip_internal_row(row)
        (day_key, recurrence_key, month_weeks_key, band_key, mode_key, freq_key,
         start_key, end_key, fld_mode_key) = self._resource_import_key(normalized)
        existing = conn.execute(
            """SELECT id FROM net_resources
                 WHERE TRIM(resource_set)=TRIM(?)
                   AND TRIM(day_utc)=TRIM(?)
                   AND TRIM(COALESCE(recurrence,''))=TRIM(?)
                   AND TRIM(COALESCE(month_weeks,''))=TRIM(?)
                   AND UPPER(TRIM(COALESCE(band,'')))=UPPER(TRIM(?))
                   AND UPPER(TRIM(COALESCE(mode,'')))=UPPER(TRIM(?))
                   AND TRIM(start_utc)=TRIM(?) AND TRIM(end_utc)=TRIM(?)
                   AND UPPER(TRIM(COALESCE(fldigi_mode,'')))=UPPER(TRIM(?))
                   AND TRIM(COALESCE(frequency,''))=TRIM(?)
                 ORDER BY id DESC LIMIT 1""",
            (resource_set, day_key, recurrence_key, month_weeks_key, band_key, mode_key,
             start_key, end_key, fld_mode_key, freq_key),
        ).fetchone()
        resource_id = int(existing[0]) if existing else None
        rid = self._upsert_resource_row(
            conn, normalized, resource_set=resource_set, source_type=source_type,
            source_ref=source_ref, readonly=readonly, resource_id=resource_id,
        )
        return existing is None, rid

    def _import_source_type(self, path: Path) -> str:
        name = path.name.lower()
        if name.startswith("sitrepnets-"):
            return "builtin"
        return "imported"

    def _resolve_import_resource_set(self, path: Path, selected_set: str, source_type: str) -> str:
        # Seasonal built-in deliveries should always import into their canonical set
        # (Winter/Summer) regardless of the current combo selection.
        if source_type == "builtin":
            guessed = self._guess_resource_set_from_path(path)
            if guessed and guessed != "Imported":
                return guessed
        if selected_set != "All":
            return selected_set
        return self._guess_resource_set_from_path(path)

    def _resource_import_dir_default(self) -> Path:
        saved = str(self.settings.get("net_resources_last_import_dir", "") or "").strip()
        if saved:
            p = Path(saved)
            if p.exists() and p.is_dir():
                return p
        downloads = Path.home() / "Downloads"
        if downloads.exists() and downloads.is_dir():
            return downloads
        root = Path(__file__).resolve().parents[2]
        bundled = root / "config" / "net_resources"
        if bundled.exists() and bundled.is_dir():
            return bundled
        return Path.home()

    def _choose_resource_import_file(self) -> Optional[Path]:
        start_dir = self._resource_import_dir_default()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Net Resources",
            str(start_dir),
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return None
        p = Path(path)
        try:
            self.settings.set("net_resources_last_import_dir", str(p.parent))
        except Exception:
            pass
        return p

    def _choose_resource_import_mode(self) -> Optional[str]:
        box = QMessageBox(self)
        box.setWindowTitle("Import Mode")
        box.setIcon(QMessageBox.Question)
        box.setText("Choose how to import this Net Resources file.")
        box.setInformativeText("Merge updates matching rows. Replace also removes missing built-in rows for that set.")
        merge_btn = box.addButton("Merge/Update", QMessageBox.AcceptRole)
        replace_btn = box.addButton("Replace Built-in Set", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(merge_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked == merge_btn:
            return "merge"
        if clicked == replace_btn:
            return "replace"
        return None

    def _import_resources_with_mode_prompt(self) -> None:
        selected = self._choose_resource_import_file()
        if selected is None:
            return
        mode = self._choose_resource_import_mode()
        if mode is None:
            return
        self._import_schedule_to_resources(import_mode=mode, selected_path=selected)

    def _import_schedule_to_resources(
        self, *, import_mode: str = "merge", selected_path: Optional[Path] = None
    ) -> None:
        p = selected_path
        if p is None:
            p = self._choose_resource_import_file()
        if p is None:
            return
        rows = self._parse_schedule_json(p)
        if not rows:
            QMessageBox.warning(self, "Import", "No valid rows found to import.")
            return
        selected_set = str(self.resource_set_combo.currentData() or "All")
        source_type = self._import_source_type(p)
        resource_set = self._resolve_import_resource_set(p, selected_set, source_type)
        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        inserted = 0
        updated = 0
        removed = 0
        try:
            incoming_keys = {self._resource_import_key(r) for r in rows}
            if import_mode == "replace":
                existing = conn.execute(
                    """
                    SELECT
                        id, day_utc, recurrence, month_weeks, band, mode, frequency, start_utc, end_utc,
                        fldigi_mode, source_type
                      FROM net_resources
                     WHERE TRIM(resource_set)=TRIM(?)
                    """,
                    (resource_set,),
                ).fetchall()
                delete_ids: List[int] = []
                for (
                    rid,
                    day_utc,
                    recurrence,
                    month_weeks,
                    band,
                    mode,
                    frequency,
                    start_utc,
                    end_utc,
                    fldigi_mode,
                    row_source_type,
                ) in existing:
                    src = str(row_source_type or "").strip().lower()
                    if src == "manual":
                        continue
                    if src != "builtin":
                        continue
                    key = self._resource_import_key(
                        {
                            "day_utc": day_utc or "",
                            "recurrence": recurrence or "Weekly",
                            "month_weeks": month_weeks or "",
                            "band": band or "",
                            "mode": mode or "",
                            "frequency": frequency or "",
                            "start_utc": start_utc or "",
                            "end_utc": end_utc or "",
                            "fldigi_mode": fldigi_mode or "",
                        }
                    )
                    if key not in incoming_keys:
                        delete_ids.append(int(rid))
                if delete_ids:
                    removed = delete_legacy_resources(conn, delete_ids, synchronize=False)

            for row in rows:
                was_insert, _ = self._upsert_resource_row_by_import_key(
                    conn,
                    row,
                    resource_set=resource_set,
                    source_type=source_type,
                    source_ref=p.name,
                    readonly=1,
                )
                if was_insert:
                    inserted += 1
                else:
                    updated += 1
            finalize_legacy_resource_projection(conn)
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            QMessageBox.critical(self, "Import Failed", f"Could not import resources:\n{e}")
            return
        finally:
            conn.close()
        self._load_resources_from_db()
        self._refresh_resource_set_combo()
        idx = self.resource_set_combo.findData(resource_set)
        if idx >= 0:
            self.resource_set_combo.setCurrentIndex(idx)
        self.settings.set("net_resources_selected_set", resource_set)
        self._refresh_resources_table()
        if import_mode == "replace":
            QMessageBox.information(
                self,
                "Imported",
                (
                    f"Imported into Net Resources ({resource_set}).\n"
                    f"Inserted: {inserted}\nUpdated: {updated}\nRemoved (Built-in only): {removed}"
                ),
            )
        else:
            QMessageBox.information(
                self,
                "Imported",
                f"Imported into Net Resources ({resource_set}).\nInserted: {inserted}\nUpdated: {updated}",
            )
