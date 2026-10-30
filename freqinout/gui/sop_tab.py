
from __future__ import annotations

import datetime
import html
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPageLayout, QPageSize, QTextDocument, QStandardItem, QStandardItemModel, QPdfWriter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QBoxLayout,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QCompleter,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.logger import log
from freqinout.core.navigation_intent import NavigationIntent
from freqinout.core.perf_metrics import span as perf_span
from freqinout.core.plan_context_service import PlanContextService
from freqinout.core.condition_sop_policy import (
    AUTO_SOP_INVOCATION_SETTING_KEY,
    evaluate_condition_sop_invocations,
)
from freqinout.core.condition_sop_invocation import schedule_layer_rows_for_condition_decision
from freqinout.core.condition_sop_audit import (
    append_condition_sop_invocation_audit,
    condition_sop_audit_display,
    condition_sop_audit_summary,
    list_condition_sop_invocation_audit,
)
from freqinout.core.condition_level_update import apply_operating_group_condition_level
from freqinout.core.operating_group_frequency import format_frequency_mhz, normalize_frequency_hz, resolve_operating_group_frequency
from freqinout.core.condition_sop_revert import revert_condition_sop_audit_row
from freqinout.core.observation_queries import ObservationQuery, operational_activity_snapshot
from freqinout.core.schedule_source_sets import assigned_plan_rf_guard_impacts_for_sop_update
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.js8_spotter_forms import discover_spotter_forms, resolve_spotter_forms_dir
from freqinout.core.sop_manager import SOPManager
from freqinout.core.sop_action_model import SopActionDraftCollection
from freqinout.gui.sop_action_editor import SopActionEditorDialog
from freqinout.gui.freq_planner_tab import FreqPlannerTab
from freqinout.gui.help_registry import resolve_help_host
from freqinout.gui.plan_context_label import PlanContextLabel
from freqinout.gui.theme import apply_text_size_accessibility_guards, resolve_theme, button_style, label_style
from freqinout.utils.timezones import get_timezone


def _contrast_text_hex(bg_hex: str) -> str:
    h = (bg_hex or "").strip().lstrip("#")
    if len(h) != 6:
        return "#111111"
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
    except Exception:
        return "#111111"
    yiq = ((r * 299) + (g * 587) + (b * 114)) / 1000
    return "#111111" if yiq >= 140 else "#FFFFFF"


class _ConditionLevelsMultiCombo(QComboBox):
    selectionChanged = Signal()

    _OPTIONS = ("ALL", "1", "2", "3", "4", "5")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        if self.lineEdit() is not None:
            self.lineEdit().setReadOnly(True)
            self.lineEdit().setPlaceholderText("ALL")
        model = QStandardItemModel(self)
        self.setModel(model)
        self._syncing = False
        self._build_items()
        try:
            self.view().viewport().installEventFilter(self)
        except Exception:
            pass
        self.set_normalized_value("ALL", emit=False)

    def _build_items(self) -> None:
        model = self.model()
        if not isinstance(model, QStandardItemModel):
            return
        model.clear()
        for label in self._OPTIONS:
            item = QStandardItem(label)
            item.setEditable(False)
            item.setCheckable(True)
            item.setSelectable(True)
            item.setData(label, Qt.UserRole)
            item.setCheckState(Qt.Unchecked)
            model.appendRow(item)

    def _item(self, row: int) -> QStandardItem | None:
        model = self.model()
        if not isinstance(model, QStandardItemModel):
            return None
        item = model.item(row)
        return item if isinstance(item, QStandardItem) else None

    def _find_row(self, value: str) -> int:
        target = str(value or "").strip().upper()
        model = self.model()
        if not isinstance(model, QStandardItemModel):
            return -1
        for row in range(model.rowCount()):
            item = model.item(row)
            if not isinstance(item, QStandardItem):
                continue
            if str(item.data(Qt.UserRole) or item.text() or "").strip().upper() == target:
                return row
        return -1

    def _set_checked(self, value: str, checked: bool) -> None:
        row = self._find_row(value)
        item = self._item(row)
        if item is None:
            return
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _is_checked(self, value: str) -> bool:
        row = self._find_row(value)
        item = self._item(row)
        if item is None:
            return False
        return item.checkState() == Qt.Checked

    def _checked_levels(self) -> List[int]:
        out: List[int] = []
        for val in ("1", "2", "3", "4", "5"):
            if self._is_checked(val):
                try:
                    out.append(int(val))
                except Exception:
                    continue
        return out

    def normalized_value(self) -> str:
        if self._is_checked("ALL"):
            return "ALL"
        levels = sorted(set(self._checked_levels()))
        if not levels:
            return "ALL"
        if levels == [1, 2, 3, 4, 5]:
            return "ALL"
        return ",".join(str(v) for v in levels)

    def set_normalized_value(self, value: str, *, emit: bool = False) -> None:
        raw = str(value or "").strip().upper()
        use_all = (not raw) or (raw == "ALL")
        selected: Set[int] = set()
        if not use_all:
            for token in raw.replace(";", ",").replace("|", ",").split(","):
                token = token.strip()
                if not token:
                    continue
                if token == "ALL":
                    use_all = True
                    selected.clear()
                    break
                try:
                    lvl = int(token)
                except Exception:
                    continue
                if 1 <= lvl <= 5:
                    selected.add(lvl)
            if not selected and not use_all:
                use_all = True
            if selected == {1, 2, 3, 4, 5}:
                use_all = True
                selected.clear()

        self._syncing = True
        try:
            self._set_checked("ALL", use_all)
            for lvl in ("1", "2", "3", "4", "5"):
                self._set_checked(lvl, (not use_all) and int(lvl) in selected)
            self._update_display_text()
        finally:
            self._syncing = False
        if emit:
            self.selectionChanged.emit()

    def _update_display_text(self) -> None:
        text = self.normalized_value()
        if self.lineEdit() is not None:
            self.lineEdit().setText(text)
        else:
            self.setCurrentText(text)

    def _normalize_after_toggle(self, toggled_label: str) -> None:
        label = str(toggled_label or "").strip().upper()
        if label == "ALL":
            if self._is_checked("ALL"):
                for lvl in ("1", "2", "3", "4", "5"):
                    self._set_checked(lvl, False)
            else:
                if not self._checked_levels():
                    self._set_checked("ALL", True)
        else:
            if self._is_checked(label):
                self._set_checked("ALL", False)
            levels = self._checked_levels()
            if not levels:
                self._set_checked("ALL", True)
            elif sorted(set(levels)) == [1, 2, 3, 4, 5]:
                self._set_checked("ALL", True)
                for lvl in ("1", "2", "3", "4", "5"):
                    self._set_checked(lvl, False)
        self._update_display_text()

    def _toggle_row(self, row: int) -> None:
        item = self._item(row)
        if item is None:
            return
        label = str(item.data(Qt.UserRole) or item.text() or "").strip().upper()
        next_checked = item.checkState() != Qt.Checked
        self._syncing = True
        try:
            item.setCheckState(Qt.Checked if next_checked else Qt.Unchecked)
            self._normalize_after_toggle(label)
        finally:
            self._syncing = False
        self.selectionChanged.emit()

    def eventFilter(self, obj, event) -> bool:
        try:
            if obj is self.view().viewport() and event is not None:
                event_type = event.type()
                if event_type == QEvent.MouseButtonPress:
                    idx = self.view().indexAt(event.pos())
                    if idx.isValid():
                        self._toggle_row(int(idx.row()))
                        return True
                if event_type == QEvent.MouseButtonRelease:
                    idx = self.view().indexAt(event.pos())
                    if idx.isValid():
                        # Consume release so default combo selection handling does not
                        # re-toggle/close unexpectedly after our manual press toggle.
                        return True
        except Exception:
            pass
        return super().eventFilter(obj, event)


class _LegacySOPTab(QWidget):
    """
    SOP reminders tab.
    Reminder-only workflow with manual completion and UTC-driven cadence.
    Terminology:
      - Group Name = HF Operating Group mapping
      - SOP Group = optional SOP profile grouping label
      - SOP Category = HF or Local Net action context
    """

    CONTACT_RULE_OPTIONS = [
        ("none", "None"),
        ("group", "GROUP"),
        ("hub_or_hub_alt", "HUB OR HUB-ALT"),
        ("ncs_or_ancs", "NCS OR ANCS"),
        ("peer", "PEER"),
        ("callsign", "CallSign"),
    ]
    ANY_ROLE_TOKEN = "__any_role__"
    INTERVAL_PRESETS = ["00:30", "01:00", "03:00", "06:00", "12:00", "Daily"]
    SOFTWARES = ["JS8Call", "VarAC", "FLDigi"]
    LOCAL_NET_SOFTWARE = "Local Net"
    LOCAL_NET_ACTION_NCS_KEY = "local_ncs"
    LOCAL_NET_ACTION_CHECKIN_KEY = "local_checkin"
    LOCAL_NET_ACTION_MESSAGE_KEY = "local_message"
    # Legacy key retained for backward compatibility with existing profiles.
    LOCAL_NET_ACTION_KEY = "local_open_net"
    LOCAL_NET_ACTION_KEYS = (
        LOCAL_NET_ACTION_NCS_KEY,
        LOCAL_NET_ACTION_CHECKIN_KEY,
        LOCAL_NET_ACTION_MESSAGE_KEY,
        LOCAL_NET_ACTION_KEY,
    )
    BAND_CHOICES = ["160M", "80M", "60M", "40M", "30M", "20M", "17M", "15M", "12M", "10M", "6M"]
    sop_data_changed = Signal()

    COL_BAND = 0
    COL_FREQ = 1
    COL_SOFTWARE = 2
    COL_ACTION = 3
    COL_INTERVAL = 4
    COL_CONTACT = 5
    COL_CONTACT_TARGET = 6
    COL_DESC = 7
    COL_REMOVE = 8

    LAYER_COL_DAY = 0
    LAYER_COL_RECURRENCE = 1
    LAYER_COL_MONTH_WEEKS = 2
    LAYER_COL_START = 3
    LAYER_COL_END = 4
    LAYER_COL_BAND = 5
    LAYER_COL_FREQ = 6
    LAYER_COL_MODE = 7
    LAYER_COL_REMOVE = 8

    def __init__(
        self,
        parent=None,
        *,
        plan_context_service: Optional[PlanContextService] = None,
        defer_initial_load: bool = False,
        settings_manager: SettingsManager | None = None,
    ):
        super().__init__(parent)
        # SOP is not part of the first visible shell.  MainWindow opts into a
        # deferred data projection so profile/schedule queries cannot hold up
        # the first paint.  Standalone/test construction remains eager.
        self._defer_initial_load = bool(defer_initial_load)
        self._initial_data_loaded = False
        self._initial_data_load_pending = False
        self.settings = settings_manager if settings_manager is not None else SettingsManager()
        self._summary_timezone = get_timezone(str(self.settings.all().get("timezone") or "UTC"))
        self.plan_context_service = plan_context_service or PlanContextService()
        self.manager = SOPManager(settings=SettingsManager(initialized_source=self.settings))
        self._profiles: List[Dict[str, Any]] = []
        self._selected_profile_id: int | None = None
        self._upcoming_rows: List[Dict[str, Any]] = []
        self._loading_ui = False
        self._operating_groups: List[Dict[str, Any]] = []
        self._local_net_profiles: List[Dict[str, str]] = []
        self._hidden_actions: List[Dict[str, Any]] = []
        default_mode = (self.settings.get("display_time_mode", "LOCAL") or "LOCAL").upper()
        self._show_local = default_mode != "UTC"
        self._dirty = False
        self._layer_time_header_tag = ""
        self._layer_sync_out_of_sync = False
        self._layer_sync_has_basis = False
        self._layer_sync_cache_key: Tuple[Any, ...] | None = None
        self._layer_sync_cache_value: Tuple[List[Dict[str, Any]], List[str], int] | None = None
        self._hf_group_names_cache: List[str] | None = None
        self._hf_group_condition_meta_cache: Dict[str, Tuple[bool, int | None]] | None = None
        self._condition_level_selector_values_cache: List[str] | None = None
        self._local_group_names_cache: List[str] | None = None
        self._local_resource_cache: Dict[str, List[str]] = {}
        self._local_mode_cache: Dict[Tuple[str, str], List[str]] = {}
        self._hf_bandfreq_cache: Dict[str, List[str]] = {}
        self._spotter_forms_cache_key: Tuple[str, int] | None = None
        self._spotter_forms_cache_value: List[Tuple[str, str]] = []
        self._action_catalog_cache_key: Tuple[Tuple[str, str], ...] | None = None
        self._action_catalog_cache_value: Dict[str, List[Tuple[str, str]]] | None = None
        self._contact_lookup_cache: Dict[Tuple[str, str], Tuple[float, List[str]]] = {}
        self._contact_lookup_cache_ttl_sec = 2.0
        self._hf_schedule_slots_cache_token: Tuple[str, float] | None = None
        self._hf_schedule_slots_index: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
        self._row_dynamic_refresh_timers: Dict[int, QTimer] = {}
        self._active = False
        self._sop_data_changed_emit_pending = False
        self._sop_data_changed_emit_timer = QTimer(self)
        self._sop_data_changed_emit_timer.setSingleShot(True)
        self._sop_data_changed_emit_timer.setInterval(75)
        self._sop_data_changed_emit_timer.timeout.connect(self._flush_sop_data_changed_emit)

        self._build_ui()
        self._set_save_dirty(False)
        if not self._defer_initial_load:
            self._load_initial_data_projection()

        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.refresh_upcoming)

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._update_clock_labels)
        self._update_clock_labels()

        self._layer_sync_timer = QTimer(self)
        self._layer_sync_timer.setSingleShot(True)
        self._layer_sync_timer.setInterval(220)
        self._layer_sync_timer.timeout.connect(self._refresh_layer_sync_hint)
        if not self._defer_initial_load:
            self._schedule_layer_sync_refresh()

    def _load_initial_data_projection(self) -> None:
        """Populate DB-backed SOP state only once the workspace is needed."""

        if self._initial_data_loaded:
            return
        self._initial_data_loaded = True
        self._initial_data_load_pending = False
        self._refresh_reference_data()
        self._reload_profiles(select_id=None)
        self.refresh_upcoming()
        refresh_workbench = getattr(self, "_refresh_sop_workbench_contracts", None)
        if callable(refresh_workbench):
            refresh_workbench()

    def _ensure_initial_data_projection(self) -> None:
        if not self._defer_initial_load or self._initial_data_loaded:
            return
        if not self._active:
            self._initial_data_load_pending = False
            return
        try:
            self._load_initial_data_projection()
            self._schedule_layer_sync_refresh()
            self.on_tab_activated()
        except Exception as exc:
            self._initial_data_load_pending = False
            log.debug("SOP: initial deferred data projection failed: %s", exc)

    def _open_context_help(self, context_key: str) -> None:
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_context_help"):
            try:
                host.open_context_help(context_key)
            except Exception:
                pass

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("<h3>SOP Builder</h3>"))
        self.help_btn = QPushButton("Help")
        self.help_btn.setToolTip("Open SOP Builder help.")
        self.help_btn.clicked.connect(lambda: self._open_context_help("tab.sop-builder"))
        title_row.addWidget(self.help_btn)
        title_row.addStretch()
        self.utc_label = QLabel()
        self.local_label = QLabel()
        self.utc_label.setVisible(False)
        self.local_label.setVisible(False)
        title_row.addWidget(self.utc_label)
        title_row.addWidget(self.local_label)
        self.time_toggle_btn = QPushButton("Times: Local")
        self.time_toggle_btn.clicked.connect(self._toggle_time_view)
        outer.addLayout(title_row)

        self.sop_scroll = QScrollArea(self)
        self.sop_scroll.setObjectName("sopBuilderScroll")
        self.sop_scroll.setWidgetResizable(True)
        # The SOP page is a vertical owner.  Wide data tables retain their
        # own local surface policy; the normal builder form must reflow.
        self.sop_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sop_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.sop_scroll_content = QWidget()
        root = QVBoxLayout(self.sop_scroll_content)
        root.setContentsMargins(0, 0, 0, 0)
        self.sop_scroll.setWidget(self.sop_scroll_content)
        outer.addWidget(self.sop_scroll, stretch=1)

        self.plan_context_label = PlanContextLabel(
            "sop",
            service=self.plan_context_service,
            fallback_text="SOP Builder uses the current Frequency Plan and radio context when reviewing HF and Local procedures.",
        )
        self.plan_context_label.setToolTip(
            "Use this context to confirm which radio and assigned Frequency Plan SOP work should be reviewed against."
        )
        self.plan_context_label.setVisible(False)
        root.addWidget(self.plan_context_label)
        if not self._defer_initial_load:
            self.plan_context_label.refresh_context(refresh=True)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.profile_combo = QComboBox()
        self.profile_combo.setPlaceholderText("Select existing or add new...")
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        header.addWidget(QLabel("SOP:"))
        header.addWidget(self.profile_combo, stretch=1)
        header.addWidget(self.time_toggle_btn)

        self.new_btn = QPushButton("New")
        self.save_btn = QPushButton("Save")
        self.delete_btn = QPushButton("Delete")
        self.export_pdf_btn = QPushButton("Export PDF")
        self.export_import_btn = QToolButton()
        self.export_import_btn.setText("Export/Import")
        self.export_import_btn.setPopupMode(QToolButton.InstantPopup)
        self.export_import_menu = QMenu(self.export_import_btn)
        self.export_json_action = self.export_import_menu.addAction("Export JSON")
        self.import_json_action = self.export_import_menu.addAction("Import JSON")
        self.export_import_btn.setMenu(self.export_import_menu)
        self.new_btn.clicked.connect(self._new_profile)
        self.save_btn.clicked.connect(self._save_profile)
        self.delete_btn.clicked.connect(self._delete_profile)
        self.export_pdf_btn.clicked.connect(self._export_pdf)
        self.export_json_action.triggered.connect(self._export_profile)
        self.import_json_action.triggered.connect(self._import_profile)
        for btn in (self.new_btn, self.save_btn, self.delete_btn, self.export_pdf_btn, self.export_import_btn):
            header.addWidget(btn)
        root.addLayout(header)

        cfg_box = QGroupBox("SOP Configuration")
        cfg_layout = QVBoxLayout(cfg_box)

        row1 = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.group_combo = QComboBox()
        self.secondary_combo = QComboBox()
        self.group_combo.setToolTip("Group Name (HF Operating Group) drives HF band/frequency mapping.")
        self.secondary_combo.setToolTip("SOP Group is optional and can be used for SOP profile organization.")
        self.start_edit = QLineEdit()
        self.start_edit.setPlaceholderText("HH:MM")
        self.active_cb = QCheckBox("Active")
        row1.addWidget(QLabel("Name:"))
        row1.addWidget(self.name_edit, stretch=2)
        row1.addWidget(QLabel("Group Name (HF):"))
        row1.addWidget(self.group_combo, stretch=1)
        row1.addWidget(QLabel("SOP Group:"))
        row1.addWidget(self.secondary_combo, stretch=1)
        cfg_layout.addLayout(row1)
        self.terms_hint_label = QLabel(
            "SOP Category = HF or Local Net | SOP Group = optional profile grouping | "
            "Group Name = HF Operating Group frequency mapping"
        )
        self.terms_hint_label.setWordWrap(True)
        cfg_layout.addWidget(self.terms_hint_label)

        row2 = QHBoxLayout()
        self.start_label = QLabel("SOP Daily Start (UTC):")
        row2.addWidget(self.start_label)
        self.start_edit.setMinimumWidth(100)
        self.start_edit.setMaximumWidth(150)
        row2.addWidget(self.start_edit)
        row2.addWidget(QLabel("Priority:"))
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(1, 999)
        self.priority_spin.setValue(100)
        self.priority_spin.setToolTip("Lower number wins when multiple active SOP profiles overlap.")
        row2.addWidget(self.priority_spin)
        row2.addWidget(self.active_cb)
        row2.addStretch()
        cfg_layout.addLayout(row2)

        self.contact_label = QLabel("Primary Contacts: --")
        self.contact_label.setWordWrap(True)
        cfg_layout.addWidget(self.contact_label)

        rows_head = QHBoxLayout()
        rows_head.addWidget(QLabel("Action Rows (each row = one SOP reminder action)"))
        rows_head.addStretch()
        self.hidden_rows_label = QLabel("")
        rows_head.addWidget(self.hidden_rows_label)
        self.add_row_btn = QPushButton("Add Action Row")
        self.add_row_btn.clicked.connect(lambda: self._add_action_row(existing=None))
        rows_head.addWidget(self.add_row_btn)
        cfg_layout.addLayout(rows_head)

        self.actions_table = QTableWidget(0, 9)
        self.actions_table.setHorizontalHeaderLabels(
            [
                "Band",
                "Frequency",
                "Resource",
                "Action",
                "Interval",
                "Contact Rule",
                "Contact Target",
                "Description",
                "Remove",
            ]
        )
        self.actions_table.verticalHeader().setVisible(False)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_BAND, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_FREQ, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_SOFTWARE, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_ACTION, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_INTERVAL, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_CONTACT, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_CONTACT_TARGET, QHeaderView.Fixed)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_DESC, QHeaderView.Stretch)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_REMOVE, QHeaderView.ResizeToContents)
        self.actions_table.setColumnWidth(self.COL_CONTACT_TARGET, 170)
        cfg_layout.addWidget(self.actions_table)
        root.addWidget(cfg_box)

        layer_box = QGroupBox("SOP Schedule Layer (Overrides HF While Active)")
        layer_layout = QVBoxLayout(layer_box)
        layer_hint = QLabel(
            "Optional frequency-plan layer for this SOP. While SOP is Active, these rows supersede HF schedule. "
            "Net schedule remains highest priority."
        )
        layer_hint.setWordWrap(True)
        layer_layout.addWidget(layer_hint)

        layer_head = QHBoxLayout()
        layer_head.addWidget(QLabel("Layer Rows"))
        layer_head.addStretch()
        self.populate_layer_btn = QPushButton("Populate Layer from Actions")
        self.populate_layer_btn.clicked.connect(self._on_populate_layer_from_actions)
        layer_head.addWidget(self.populate_layer_btn)
        self.rebuild_layer_btn = QPushButton("Rebuild Layer Preview")
        self.rebuild_layer_btn.clicked.connect(self._on_rebuild_layer_preview)
        layer_head.addWidget(self.rebuild_layer_btn)
        self.add_layer_row_btn = QPushButton("Add Layer Row")
        self.add_layer_row_btn.clicked.connect(lambda: self._add_layer_row(existing=None))
        layer_head.addWidget(self.add_layer_row_btn)
        layer_layout.addLayout(layer_head)
        self.layer_sync_label = QLabel("")
        self.layer_sync_label.setWordWrap(True)
        self.layer_sync_label.setVisible(False)
        layer_layout.addWidget(self.layer_sync_label)

        self.layer_table = QTableWidget(0, 9)
        self.layer_table.setHorizontalHeaderLabels(
            ["Day", "Recurrence", "Weeks", "Start (UTC)", "End (UTC)", "Band", "Frequency", "Mode", "Remove"]
        )
        self.layer_table.verticalHeader().setVisible(False)
        self.layer_table.horizontalHeader().setSectionResizeMode(self.LAYER_COL_DAY, QHeaderView.ResizeToContents)
        self.layer_table.horizontalHeader().setSectionResizeMode(self.LAYER_COL_RECURRENCE, QHeaderView.ResizeToContents)
        self.layer_table.horizontalHeader().setSectionResizeMode(self.LAYER_COL_MONTH_WEEKS, QHeaderView.ResizeToContents)
        self.layer_table.horizontalHeader().setSectionResizeMode(self.LAYER_COL_START, QHeaderView.Stretch)
        self.layer_table.horizontalHeader().setSectionResizeMode(self.LAYER_COL_END, QHeaderView.Stretch)
        self.layer_table.horizontalHeader().setSectionResizeMode(self.LAYER_COL_BAND, QHeaderView.Stretch)
        self.layer_table.horizontalHeader().setSectionResizeMode(self.LAYER_COL_FREQ, QHeaderView.Stretch)
        self.layer_table.horizontalHeader().setSectionResizeMode(self.LAYER_COL_MODE, QHeaderView.Stretch)
        self.layer_table.horizontalHeader().setSectionResizeMode(self.LAYER_COL_REMOVE, QHeaderView.ResizeToContents)
        self.layer_table.horizontalHeader().setStretchLastSection(False)
        layer_layout.addWidget(self.layer_table)
        self.layer_validation_label = QLabel("")
        self.layer_validation_label.setWordWrap(True)
        self.layer_validation_label.setVisible(False)
        layer_layout.addWidget(self.layer_validation_label)
        self.layer_runtime_label = QLabel("")
        self.layer_runtime_label.setWordWrap(True)
        self.layer_runtime_label.setVisible(False)
        layer_layout.addWidget(self.layer_runtime_label)
        root.addWidget(layer_box)

        upcoming_box = QGroupBox("Upcoming SOP Actions")
        upcoming_layout = QVBoxLayout(upcoming_box)
        top = QHBoxLayout()
        self.horizon_spin = QSpinBox()
        self.horizon_spin.setRange(1, 48)
        self.horizon_spin.setValue(12)
        self.horizon_spin.valueChanged.connect(self.refresh_upcoming)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_upcoming)
        top.addWidget(QLabel("Show next N hours:"))
        top.addWidget(self.horizon_spin)
        top.addStretch()
        top.addWidget(self.refresh_btn)
        upcoming_layout.addLayout(top)

        self.alignment_label = QLabel("")
        self.alignment_label.setWordWrap(True)
        self.alignment_label.setVisible(False)
        upcoming_layout.addWidget(self.alignment_label)

        self.upcoming_table = QTableWidget(0, 8)
        self.upcoming_table.setHorizontalHeaderLabels(
            ["Profile", "Band/Freq", "Resource", "Action", "Description", "Next", "Contact", "Status"]
        )
        self.upcoming_table.verticalHeader().setVisible(False)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeToContents)
        upcoming_layout.addWidget(self.upcoming_table)
        root.addWidget(upcoming_box)

        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        self.secondary_combo.currentIndexChanged.connect(self._on_secondary_group_changed)
        self._wire_dirty_tracking()
        self._apply_accessibility_width_guards()

    def _apply_accessibility_width_guards(self) -> None:
        # Keep action labels readable when UI text size is increased.
        try:
            time_w = int(self.start_edit.fontMetrics().horizontalAdvance("23:59") + 26)
            base = self.start_edit.property("_fio_base_min_width")
            try:
                base_w = int(base)
            except Exception:
                base_w = 100
                self.start_edit.setProperty("_fio_base_min_width", base_w)
            self.start_edit.setFixedWidth(max(base_w, min(150, time_w)))
        except Exception:
            pass
        buttons = [
            self.time_toggle_btn,
            self.new_btn,
            self.save_btn,
            self.delete_btn,
            getattr(self, "versions_btn", None),
            self.export_pdf_btn,
            self.export_import_btn,
            self.add_row_btn,
            self.populate_layer_btn,
            self.rebuild_layer_btn,
            self.add_layer_row_btn,
            self.refresh_btn,
        ]
        for btn in buttons:
            if btn is None:
                continue
            try:
                txt = str(btn.text() or "").strip()
            except Exception:
                txt = ""
            if not txt:
                continue
            try:
                needed = int(btn.fontMetrics().horizontalAdvance(txt.replace("&", "")) + 30)
            except Exception:
                continue
            try:
                current_min = int(btn.minimumWidth() or 0)
            except Exception:
                current_min = 0
            base = btn.property("_fio_base_min_width")
            try:
                base_w = int(base)
            except Exception:
                base_w = current_min
                btn.setProperty("_fio_base_min_width", base_w)
            target = max(base_w, min(360, needed))
            try:
                btn.setMinimumWidth(target)
            except Exception:
                pass
        apply_text_size_accessibility_guards(self, include_widths=False)

    def _refresh_reference_data(self) -> None:
        data = self.settings.all()
        self._summary_timezone = get_timezone(str(data.get("timezone") or "UTC"))
        og = data.get("operating_groups", [])
        self._operating_groups = [g for g in og if isinstance(g, dict)]
        self._load_local_net_profiles_from_data(data)
        self._invalidate_dynamic_option_caches()

        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        group_names = sorted({(g.get("group") or "").strip().upper() for g in self._operating_groups if g.get("group")})
        self.group_combo.addItem("")
        for name in group_names:
            self.group_combo.addItem(name)
        self.group_combo.blockSignals(False)

        self.secondary_combo.clear()
        self.secondary_combo.addItem("")
        for g in self.manager.load_secondary_groups():
            self.secondary_combo.addItem(g)

    def _invalidate_dynamic_option_caches(self) -> None:
        self._hf_group_names_cache = None
        self._hf_group_condition_meta_cache = None
        self._local_group_names_cache = None
        self._local_resource_cache.clear()
        self._local_mode_cache.clear()
        self._hf_bandfreq_cache.clear()
        self._spotter_forms_cache_key = None
        self._spotter_forms_cache_value = []
        self._action_catalog_cache_key = None
        self._action_catalog_cache_value = None
        self._contact_lookup_cache.clear()
        self._hf_schedule_slots_cache_token = None
        self._hf_schedule_slots_index = {}

    def _load_local_net_profiles_from_data(self, data: Dict[str, Any]) -> None:
        from freqinout.core.local_group_frequency import normalize_local_profile

        local_profiles = data.get("local_net_profiles", [])
        self._local_net_profiles = []
        if isinstance(local_profiles, list):
            for raw in local_profiles:
                if not isinstance(raw, dict):
                    continue
                group = str(raw.get("group", raw.get("name", "")) or "").strip()
                if not group:
                    continue
                self._local_net_profiles.append(normalize_local_profile(raw, default_resource=""))
        self._local_net_profiles = sorted(
            self._local_net_profiles,
            key=lambda x: (
                str(x.get("group", "")).upper(),
                str(x.get("resource", "")).upper(),
                str(x.get("mode", "")).upper(),
                str(x.get("target", "")).upper(),
            ),
        )
        self._invalidate_dynamic_option_caches()

    def _local_profile_names(self) -> List[str]:
        names = {
            str(p.get("group", "")).strip()
            for p in self._local_net_profiles
            if str(p.get("group", "")).strip()
        }
        return sorted(names, key=lambda x: x.upper())

    def _local_profile_lookup(self) -> Dict[str, List[Dict[str, str]]]:
        out: Dict[str, List[Dict[str, str]]] = {}
        for row in self._local_net_profiles:
            name = str(row.get("group", "")).strip()
            if not name:
                continue
            out.setdefault(name.upper(), []).append(dict(row))
        for rows in out.values():
            rows.sort(
                key=lambda r: (
                    str(r.get("resource", "")).upper(),
                    str(r.get("mode", "")).upper(),
                    str(r.get("target", "")).upper(),
                )
            )
        return out

    def _wire_dirty_tracking(self) -> None:
        self.name_edit.textChanged.connect(self._mark_dirty)
        self.group_combo.currentIndexChanged.connect(self._mark_dirty)
        self.secondary_combo.currentIndexChanged.connect(self._mark_dirty)
        self.start_edit.textChanged.connect(self._mark_dirty)
        self.priority_spin.valueChanged.connect(self._mark_dirty)
        self.active_cb.toggled.connect(self._mark_dirty)

    def _set_save_dirty(self, dirty: bool) -> None:
        self._dirty = bool(dirty)
        self._update_profile_action_styles()
        # Only the current workbench adds the summary surface; the retained
        # legacy editor still supports dirty tracking without that surface.
        update_summary = getattr(self, '_update_summary_status', None)
        if callable(update_summary):
            update_summary()

    def _mark_dirty(self, *_args) -> None:
        if self._loading_ui:
            return
        self._set_save_dirty(True)
        self._schedule_layer_sync_refresh()
        self._refresh_sop_workbench_contracts()

    def _refresh_sop_workbench_contracts(self) -> None:
        if not hasattr(self, "sop_profile_selector_label"):
            return
        self._refresh_sop_profile_selector_summary()
        self._refresh_sop_context_summary()
        self._refresh_sop_action_cards()
        self._refresh_sop_controlfreq_preview()

    def _refresh_sop_profile_selector_summary(self) -> None:
        label = getattr(self, "sop_profile_selector_label", None)
        if label is None:
            return
        name = self.name_edit.text().strip() if hasattr(self, "name_edit") else ""
        if not name and hasattr(self, "profile_combo"):
            name = str(self.profile_combo.currentText() or "").strip()
        category = str(self.category_combo.currentText() or "SOP") if hasattr(self, "category_combo") else "SOP"
        active = "active" if hasattr(self, "active_cb") and self.active_cb.isChecked() else "inactive"
        dirty = "unsaved changes" if bool(getattr(self, "_dirty", False)) else "saved"
        label.setText(
            f"<b>{html.escape(name or 'New SOP')}</b><br>"
            f"{html.escape(category)} | {active} | {dirty}<br>"
            "Versions and export/import protect recovery while card editing is introduced."
        )

    def _refresh_sop_context_summary(self) -> None:
        label = getattr(self, "sop_context_summary_label", None)
        if label is None:
            return
        try:
            context_text = self._operating_plan_inputs_summary_text()
        except Exception:
            context_text = "Operating Plan Inputs: unavailable."
        workflow = ""
        workflow_label = getattr(self, "sop_workflow_status_label", None)
        if isinstance(workflow_label, QLabel):
            workflow = str(workflow_label.text() or "").replace("<b>", "").replace("</b>", "").strip()
        label.setText(
            html.escape(context_text)
            + (f"<br><b>Impact:</b> {html.escape(workflow)}" if workflow else "")
        )

    def _action_card_line_for_row(self, row: int) -> str:
        def combo_text(col: int) -> str:
            widget = self.actions_table.cellWidget(row, col)
            if isinstance(widget, QComboBox):
                return widget.currentText().strip()
            return ""

        def line_text(col: int) -> str:
            widget = self.actions_table.cellWidget(row, col)
            if isinstance(widget, QLineEdit):
                return widget.text().strip()
            return ""

        group = combo_text(self.COL_GROUP) or "Any group"
        resource = combo_text(self.COL_RESOURCE) or "Resource"
        action = combo_text(self.COL_ACTION) or "Action"
        bandfreq = combo_text(self.COL_BANDFREQ)
        start_edit = self._action_row_start_edit(row) if hasattr(self, "_action_row_start_edit") else None
        start_text = start_edit.text().strip() if isinstance(start_edit, QLineEdit) else ""
        end_text = line_text(self.COL_END)
        desc = line_text(self.COL_DESC)
        conflict_widget = self.actions_table.cellWidget(row, self.COL_CONFLICT)
        conflict = conflict_widget.text().strip() if isinstance(conflict_widget, QToolButton) else "Pending"
        timing = " - ".join(part for part in (start_text, end_text) if part)
        route = " | ".join(part for part in (resource, bandfreq) if part)
        details = " | ".join(part for part in (route, timing, conflict) if part)
        theme = resolve_theme(self.settings)
        return (
            f"<div style='border:1px solid {theme['border']}; border-radius:6px; padding:6px; "
            f"margin:4px 0; background:{theme['surface_alt']}; color:{theme['text']}'>"
            f"<b>{html.escape(group)}</b>: {html.escape(action)}"
            + (f"<br><span style='color:{theme['text_muted']}'>{html.escape(details)}</span>" if details else "")
            + (f"<br>{html.escape(desc)}" if desc else "")
            + "</div>"
        )

    def _refresh_sop_action_cards(self) -> None:
        label = getattr(self, "sop_action_cards_label", None)
        if label is None or not hasattr(self, "actions_table"):
            return
        cards = [self._action_card_line_for_row(row) for row in range(self.actions_table.rowCount())]
        label.setText("".join(cards) if cards else "No SOP actions yet.")

    def _refresh_sop_controlfreq_preview(self) -> None:
        label = getattr(self, "sop_preview_label", None)
        if label is None:
            return
        profile_name = self.name_edit.text().strip() if hasattr(self, "name_edit") else ""
        category = str(self.category_combo.currentText() or "SOP") if hasattr(self, "category_combo") else "SOP"
        action_count = self.actions_table.rowCount() if hasattr(self, "actions_table") else 0
        if action_count <= 0:
            label.setText("No actions will appear in Ops Center yet.")
            return
        label.setText(
            f"<b>{html.escape(profile_name or 'Selected SOP')}</b><br>"
            f"{html.escape(category)} | {action_count} action{'s' if action_count != 1 else ''}<br>"
            "Ops Center will match actions by schedule window, group, condition level, source, and assigned radio."
        )

    def _schedule_layer_sync_refresh(self) -> None:
        if getattr(self, "_loading_ui", False):
            return
        timer = getattr(self, "_layer_sync_timer", None)
        if timer is None:
            return
        timer.start()

    def _update_profile_action_styles(self, theme: Dict[str, str] | None = None) -> None:
        try:
            if theme is None:
                theme = resolve_theme(self.settings)
            has_profile = self._selected_profile_id is not None
            has_active_profile = any(bool(p.get("active")) for p in (self._profiles or []))
            self.new_btn.setStyleSheet(button_style("eligible_info" if has_profile else "muted", theme))
            self.save_btn.setStyleSheet(button_style("eligible_success" if self._dirty else "muted", theme))
            self.delete_btn.setEnabled(has_profile)
            self.delete_btn.setStyleSheet(button_style("eligible_danger" if has_profile else "muted", theme))
            export_pdf_eligible = has_profile or has_active_profile
            self.export_pdf_btn.setEnabled(export_pdf_eligible)
            self.export_pdf_btn.setStyleSheet(button_style("eligible_info" if export_pdf_eligible else "muted", theme))
            self.export_import_btn.setEnabled(True)
            self.export_import_btn.setStyleSheet(button_style("muted", theme))
            self.refresh_btn.setStyleSheet(button_style("muted", theme))
            add_row_eligible = has_profile and self.add_row_btn.isEnabled()
            self.add_row_btn.setStyleSheet(
                button_style("eligible_primary" if add_row_eligible else "muted", theme)
            )
            add_layer_eligible = has_profile and self.add_layer_row_btn.isEnabled()
            self.add_layer_row_btn.setStyleSheet(
                button_style("eligible_primary" if add_layer_eligible else "muted", theme)
            )
            has_non_local = len(self._collect_action_rows_for_layer_seed()) > 0
            populate_eligible = bool(self.group_combo.currentText().strip().upper()) and has_non_local
            rebuild_role = "muted"
            if populate_eligible:
                if self._layer_sync_has_basis and self._layer_sync_out_of_sync:
                    rebuild_role = "eligible_warning"
                else:
                    rebuild_role = "eligible_info"
            self.populate_layer_btn.setEnabled(populate_eligible)
            self.rebuild_layer_btn.setEnabled(populate_eligible)
            self.populate_layer_btn.setStyleSheet(
                button_style("eligible_info" if populate_eligible else "muted", theme)
            )
            self.rebuild_layer_btn.setStyleSheet(
                button_style(rebuild_role, theme)
            )
        except Exception:
            pass

    def _update_time_toggle_style(self, theme: Dict[str, str] | None = None) -> None:
        try:
            if theme is None:
                theme = resolve_theme(self.settings)
            # Local is default display mode; only emphasize UTC override.
            role = "info" if not self._show_local else "muted"
            self.time_toggle_btn.setStyleSheet(button_style(role, theme))
        except Exception:
            pass

    def _configured_softwares(self) -> List[str]:
        data = self.settings.all()
        configured: List[str] = []
        if (data.get("js8_directed_path") or "").strip() or (data.get("js8_forms_path") or "").strip():
            configured.append("JS8Call")
        msg_paths = data.get("message_paths", {}) or {}
        if (data.get("varac_path") or "").strip() or str(msg_paths.get("varac") or "").strip():
            configured.append("VarAC")
        if (
            (data.get("path_fldigi") or "").strip()
            or (data.get("fldigi_log_path") or "").strip()
            or (data.get("fldigi_checkin_dir") or "").strip()
        ):
            configured.append("FLDigi")
        if self.LOCAL_NET_SOFTWARE not in configured:
            configured.append(self.LOCAL_NET_SOFTWARE)
        return configured

    def _frequency_options_for_group(self, group: str) -> List[str]:
        return self._frequency_options_for_group_band(group, "")

    def _frequency_options_for_group_band(self, group: str, band: str) -> List[str]:
        grp = (group or "").strip().upper()
        band_uc = (band or "").strip().upper()
        values = []
        for row in self._operating_groups:
            if (row.get("group") or "").strip().upper() != grp:
                continue
            row_band = (row.get("band") or "").strip().upper()
            if band_uc and row_band != band_uc:
                continue
            frequency = str(row.get("frequency") or "").strip()
            if normalize_frequency_hz(frequency) is not None:
                values.append(frequency)
        return sorted(set(values), key=lambda value: normalize_frequency_hz(value) or 0) if values else []

    def _band_options_for_group(self, group: str) -> List[str]:
        grp = (group or "").strip().upper()
        values = set()
        for row in self._operating_groups:
            if (row.get("group") or "").strip().upper() != grp:
                continue
            band = (row.get("band") or "").strip().upper()
            if band:
                values.add(band)
        return sorted(values, key=lambda x: (len(x), x))

    def _load_spotter_forms(self) -> List[Tuple[str, str]]:
        forms_dir = resolve_spotter_forms_dir(self.settings.get("js8_forms_path", ""))
        try:
            forms_path = str(forms_dir.resolve())
        except Exception:
            forms_path = str(forms_dir)
        try:
            forms_mtime = int(forms_dir.stat().st_mtime_ns) if forms_dir.exists() else -1
        except Exception:
            forms_mtime = 0
        cache_key = (forms_path, forms_mtime)
        if self._spotter_forms_cache_key == cache_key:
            return list(self._spotter_forms_cache_value)

        out: List[Tuple[str, str]] = []
        for definition in discover_spotter_forms(forms_dir):
            code = definition.form_code
            out.append((f"js8_spotter_{code}", code))
        self._spotter_forms_cache_key = cache_key
        self._spotter_forms_cache_value = list(out)
        self._action_catalog_cache_key = None
        self._action_catalog_cache_value = None
        return out

    def _action_catalog(self) -> Dict[str, List[Tuple[str, str]]]:
        catalog: Dict[str, List[Tuple[str, str]]] = {
            "JS8Call": [("js8_send_status", "Status"), ("js8_commstat", "CommStat")],
            "VarAC": [
                ("varac_send_broadcast", "Broadcast"),
                ("varac_direct_contact", "Direct Contact"),
                ("varac_send_sitrep", "SitRep"),
                ("varac_send_statrep", "StatRep"),
                ("varac_send_report", "General"),
            ],
            "FLDigi": [
                ("fldigi_send_sitrep", "SitRep"),
                ("fldigi_send_statrep", "StatRep"),
                ("fldigi_send_report", "General"),
            ],
            self.LOCAL_NET_SOFTWARE: [
                (self.LOCAL_NET_ACTION_NCS_KEY, "NCS"),
                (self.LOCAL_NET_ACTION_CHECKIN_KEY, "Check-in"),
                (self.LOCAL_NET_ACTION_MESSAGE_KEY, "Message"),
            ],
        }
        for key, label in self._load_spotter_forms():
            catalog.setdefault("JS8Call", []).append((key, label))
        return catalog

    def _reload_profiles(self, select_id: int | None) -> None:
        self._profiles = self.manager.list_profiles()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("New SOP", None)
        for p in self._profiles:
            self.profile_combo.addItem(p.get("name", ""), int(p.get("id")))
        self.profile_combo.blockSignals(False)

        if select_id:
            for i in range(self.profile_combo.count()):
                if self.profile_combo.itemData(i) == select_id:
                    self.profile_combo.setCurrentIndex(i)
                    self._on_profile_selected(i)
                    return
        self.profile_combo.setCurrentIndex(-1)
        self._new_profile()

    def _on_profile_selected(self, idx: int) -> None:
        if self._loading_ui:
            return
        if idx < 0:
            return
        profile_id = self.profile_combo.itemData(idx)
        if not profile_id:
            self._new_profile()
            return
        profile = self.manager.get_profile(int(profile_id))
        if not profile:
            self._new_profile()
            return
        self._selected_profile_id = int(profile["id"])
        self._loading_ui = True
        try:
            self.name_edit.setText(profile.get("name", ""))
            self.group_combo.setCurrentText(profile.get("operating_group", ""))
            self.secondary_combo.setCurrentText(profile.get("secondary_group", ""))
            self.start_edit.setText(self._display_start_hhmm_from_utc(profile.get("sop_start_utc", "00:00")))
            self.priority_spin.setValue(int(profile.get("priority") or 100))
            self.active_cb.setChecked(bool(profile.get("active")))
            self._populate_actions(profile.get("actions", []))
            self._populate_schedule_layer(profile.get("schedule_layer", []))
            self._refresh_contact_label()
        finally:
            self._loading_ui = False
        self._invalidate_layer_sync_cache()
        self._set_save_dirty(False)
        self._update_profile_action_styles()
        self._schedule_layer_sync_refresh()
        self.refresh_upcoming()

    def _new_profile(self) -> None:
        self._selected_profile_id = None
        self._loading_ui = True
        try:
            self.name_edit.setText("")
            if self.group_combo.count() > 0:
                self.group_combo.setCurrentIndex(0)
            if self.secondary_combo.count() > 0:
                self.secondary_combo.setCurrentIndex(0)
            self.start_edit.setText(self._display_start_hhmm_from_utc("00:00"))
            self.priority_spin.setValue(100)
            self.active_cb.setChecked(False)
            self._populate_actions([])
            self._populate_schedule_layer([])
            self._refresh_contact_label()
        finally:
            self._loading_ui = False
        self._invalidate_layer_sync_cache()
        self._set_save_dirty(False)
        self._update_profile_action_styles()
        self._schedule_layer_sync_refresh()
        self.refresh_upcoming()

    def _on_group_changed(self) -> None:
        if self._loading_ui:
            return
        self._invalidate_layer_sync_cache()
        self._refresh_all_row_group_options()
        self._refresh_contact_label()
        self._schedule_layer_sync_refresh()

    def _on_secondary_group_changed(self) -> None:
        if self._loading_ui:
            return
        self._refresh_all_contact_target_options()
        self._refresh_contact_label()

    def _refresh_all_row_group_options(self) -> None:
        group = self.group_combo.currentText().strip().upper()
        band_opts = self._band_options_for_group(group)
        for r in range(self.actions_table.rowCount()):
            if self._is_local_net_action_row(r):
                continue
            band_combo = self.actions_table.cellWidget(r, self.COL_BAND)
            freq_combo = self.actions_table.cellWidget(r, self.COL_FREQ)
            if isinstance(band_combo, QComboBox):
                current = band_combo.currentText().strip()
                band_combo.blockSignals(True)
                band_combo.clear()
                band_combo.addItem("")
                for v in band_opts:
                    band_combo.addItem(v)
                band_combo.setCurrentText(current)
                band_combo.blockSignals(False)
            if isinstance(freq_combo, QComboBox):
                selected_band = band_combo.currentText().strip() if isinstance(band_combo, QComboBox) else ""
                freq_opts = self._frequency_options_for_group_band(group, selected_band)
                current = freq_combo.currentText().strip()
                freq_combo.blockSignals(True)
                freq_combo.clear()
                freq_combo.addItem("")
                for v in freq_opts:
                    freq_combo.addItem(v)
                freq_combo.setCurrentText(current)
                freq_combo.blockSignals(False)
        self._refresh_all_layer_group_options()
        self._refresh_all_contact_target_options()

    def _available_callsign_targets(self) -> List[str]:
        group = self.group_combo.currentText().strip().upper()
        subgroup = self.secondary_combo.currentText().strip().upper()
        return self.manager.resolve_group_callsigns(group, subgroup)

    def _contact_rule_options_for_current_filter(self) -> List[Tuple[str, str]]:
        group = self.group_combo.currentText().strip().upper()
        subgroup = self.secondary_combo.currentText().strip().upper()
        contacts = self.manager.resolve_primary_contacts(group, subgroup)
        out: List[Tuple[str, str]] = [("none", "None")]
        if contacts.get("hub"):
            out.append(("hub_or_hub_alt", "HUB OR HUB-ALT"))
        if contacts.get("ncs"):
            out.append(("ncs_or_ancs", "NCS OR ANCS"))
        if contacts.get("peer"):
            out.append(("peer", "PEER"))
        out.append(("callsign", "CallSign"))
        return out

    def _is_local_net_action_row(self, row: int) -> bool:
        if row < 0 or row >= self.actions_table.rowCount():
            return False
        sw_combo = self.actions_table.cellWidget(row, self.COL_SOFTWARE)
        action_combo = self.actions_table.cellWidget(row, self.COL_ACTION)
        if not isinstance(sw_combo, QComboBox) or not isinstance(action_combo, QComboBox):
            return False
        software = sw_combo.currentText().strip()
        action_key = str(action_combo.currentData() or "").strip()
        return software == self.LOCAL_NET_SOFTWARE and action_key in self.LOCAL_NET_ACTION_KEYS

    def _contact_rule_options_for_row(self, row: int) -> List[Tuple[str, str]]:
        if self._is_local_net_action_row(row):
            return [("local_group", "Local Group")]
        return self._contact_rule_options_for_current_filter()

    def _local_profile_display(self, profile_name: str) -> str:
        key = (profile_name or "").strip().upper()
        if not key:
            return "--"
        group_rows = self._local_profile_lookup().get(key) or []
        if not group_rows:
            return profile_name
        group_name = str(group_rows[0].get("group", "")).strip() or profile_name
        details: List[str] = []
        for row in group_rows[:3]:
            resource = str(row.get("resource", "")).strip()
            mode = str(row.get("mode", "")).strip()
            target = str(row.get("target", "")).strip()
            chunk = " ".join([p for p in [resource, mode] if p]).strip()
            if target:
                chunk = f"{chunk} {target}".strip() if chunk else target
            if chunk:
                details.append(chunk)
        suffix = ""
        extra = max(0, len(group_rows) - len(details))
        if extra:
            suffix = f" (+{extra} more)"
        if details:
            return f"{group_name} | {'; '.join(details)}{suffix}"
        return group_name

    def _refresh_all_contact_target_options(self) -> None:
        for r in range(self.actions_table.rowCount()):
            self._refresh_contact_rule_options_for_row(r)
            self._on_contact_rule_changed(r)

    def _refresh_contact_rule_options_for_row(self, row: int) -> None:
        if row < 0 or row >= self.actions_table.rowCount():
            return
        rule_combo = self.actions_table.cellWidget(row, self.COL_CONTACT)
        if not isinstance(rule_combo, QComboBox):
            return
        current = str(rule_combo.currentData() or "none").strip()
        if self._is_local_net_action_row(row) and current == "local_profile":
            current = "local_group"
        opts = self._contact_rule_options_for_row(row)
        rule_combo.blockSignals(True)
        rule_combo.clear()
        for code, txt in opts:
            rule_combo.addItem(txt, code)
        idx = rule_combo.findData(current)
        if idx >= 0:
            rule_combo.setCurrentIndex(idx)
        else:
            idx_none = rule_combo.findData("none")
            rule_combo.setCurrentIndex(idx_none if idx_none >= 0 else 0)
        self._fit_combo_popup(rule_combo)
        rule_combo.blockSignals(False)

    def _update_hidden_actions_label(self) -> None:
        configured = self._configured_softwares()
        if not configured:
            self.add_row_btn.setEnabled(False)
            self.hidden_rows_label.setText(
                "No software configured in Configuration. Configure JS8/VarAC/FLDigi first."
            )
            self._update_profile_action_styles()
            return
        self.add_row_btn.setEnabled(True)
        if self._hidden_actions:
            self.hidden_rows_label.setText(
                f"{len(self._hidden_actions)} row(s) hidden (software not configured). Stored and preserved."
            )
        else:
            self.hidden_rows_label.setText("")
        self._update_profile_action_styles()

    def _autosize_actions_table(self) -> None:
        try:
            for col in (
                self.COL_BAND,
                self.COL_FREQ,
                self.COL_SOFTWARE,
                self.COL_ACTION,
                self.COL_INTERVAL,
                self.COL_CONTACT,
                self.COL_REMOVE,
            ):
                self.actions_table.resizeColumnToContents(col)
            if self.actions_table.columnWidth(self.COL_CONTACT_TARGET) < 170:
                self.actions_table.setColumnWidth(self.COL_CONTACT_TARGET, 170)
            if self.actions_table.columnWidth(self.COL_DESC) < 260:
                self.actions_table.setColumnWidth(self.COL_DESC, 260)
        except Exception:
            pass

    def _mode_options_for_group_band(self, group: str, band: str) -> List[str]:
        grp = (group or "").strip().upper()
        band_uc = (band or "").strip().upper()
        values: set[str] = set()
        for row in self._operating_groups:
            if (row.get("group") or "").strip().upper() != grp:
                continue
            row_band = (row.get("band") or "").strip().upper()
            if band_uc and row_band and row_band != band_uc:
                continue
            mode = (row.get("mode") or "").strip().upper()
            if mode:
                values.add(mode)
        return sorted(values)

    def _refresh_all_layer_group_options(self) -> None:
        for r in range(self.layer_table.rowCount()):
            self._refresh_layer_freq_for_row(r)
            self._refresh_layer_mode_for_row(r)
        self._refresh_layer_validation_hints()

    def _refresh_layer_freq_for_row(self, row: int) -> None:
        if row < 0 or row >= self.layer_table.rowCount():
            return
        group = self.group_combo.currentText().strip().upper()
        band_combo = self.layer_table.cellWidget(row, self.LAYER_COL_BAND)
        freq_combo = self.layer_table.cellWidget(row, self.LAYER_COL_FREQ)
        if not isinstance(band_combo, QComboBox) or not isinstance(freq_combo, QComboBox):
            return
        band = band_combo.currentText().strip().upper()
        values = self._frequency_options_for_group_band(group, band)
        current = freq_combo.currentText().strip()
        freq_combo.blockSignals(True)
        freq_combo.clear()
        freq_combo.addItem("")
        for val in values:
            freq_combo.addItem(val)
        if not current and len(values) == 1:
            freq_combo.setCurrentText(values[0])
        else:
            freq_combo.setCurrentText(current)
        self._fit_combo_popup(freq_combo)
        freq_combo.blockSignals(False)
        self._refresh_layer_validation_hints()

    def _refresh_layer_mode_for_row(self, row: int) -> None:
        if row < 0 or row >= self.layer_table.rowCount():
            return
        group = self.group_combo.currentText().strip().upper()
        band_combo = self.layer_table.cellWidget(row, self.LAYER_COL_BAND)
        mode_combo = self.layer_table.cellWidget(row, self.LAYER_COL_MODE)
        if not isinstance(band_combo, QComboBox) or not isinstance(mode_combo, QComboBox):
            return
        band = band_combo.currentText().strip().upper()
        options = self._mode_options_for_group_band(group, band)
        current = mode_combo.currentText().strip().upper()
        mode_combo.blockSignals(True)
        mode_combo.clear()
        mode_combo.addItem("")
        for val in options:
            mode_combo.addItem(val)
        if current and mode_combo.findText(current) < 0:
            mode_combo.addItem(current)
        mode_combo.setCurrentText(current)
        self._fit_combo_popup(mode_combo)
        mode_combo.blockSignals(False)
        self._refresh_layer_validation_hints()

    def _autosize_layer_table(self) -> None:
        try:
            for col in (
                self.LAYER_COL_DAY,
                self.LAYER_COL_RECURRENCE,
                self.LAYER_COL_MONTH_WEEKS,
                self.LAYER_COL_REMOVE,
            ):
                self.layer_table.resizeColumnToContents(col)
            if self.layer_table.columnWidth(self.LAYER_COL_MONTH_WEEKS) < 88:
                self.layer_table.setColumnWidth(self.LAYER_COL_MONTH_WEEKS, 88)
        except Exception:
            pass

    @staticmethod
    def _normalize_weeks_text(value: str) -> str:
        weeks: List[int] = []
        for token in str(value or "").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                week = int(token)
            except Exception:
                continue
            if 1 <= week <= 5:
                weeks.append(week)
        return ",".join(str(w) for w in sorted(set(weeks)))

    @staticmethod
    def _is_valid_hhmm(value: str) -> bool:
        txt = (value or "").strip()
        if len(txt) != 5 or ":" not in txt:
            return False
        try:
            h, m = txt.split(":", 1)
            hh = int(h)
            mm = int(m)
            return 0 <= hh <= 23 and 0 <= mm <= 59
        except Exception:
            return False

    def _populate_schedule_layer(self, existing: List[Dict[str, Any]]) -> None:
        self.layer_table.setRowCount(0)
        ordered = sorted((existing or []), key=lambda x: int(x.get("sort_order") or 0))
        for row in ordered:
            if not isinstance(row, dict):
                continue
            self._add_layer_row(existing=row)
        self._autosize_layer_table()
        self._refresh_layer_validation_hints()

    def _add_layer_row(self, existing: Dict[str, Any] | None) -> None:
        row_data = existing or {}
        row = self.layer_table.rowCount()
        self.layer_table.insertRow(row)

        day_combo = QComboBox()
        for day in ("ALL", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
            day_combo.addItem(day, day)
        day_val = (row_data.get("day_utc") or "ALL").strip()
        day_combo.setCurrentText(day_val if day_val else "ALL")
        day_combo.setProperty("layer_id", int(row_data.get("id") or 0))
        day_combo.setProperty("sort_order", int(row_data.get("sort_order") or row))
        self.layer_table.setCellWidget(row, self.LAYER_COL_DAY, day_combo)

        rec_combo = QComboBox()
        for rec in ("Weekly", "Daily", "Periodic", "Bi-Weekly"):
            rec_combo.addItem(rec, rec)
        rec_val = (row_data.get("recurrence") or "Daily").strip()
        rec_combo.setCurrentText(rec_val if rec_val else "Daily")
        self.layer_table.setCellWidget(row, self.LAYER_COL_RECURRENCE, rec_combo)

        weeks_edit = QLineEdit(self._normalize_weeks_text(str(row_data.get("month_weeks") or "")))
        weeks_edit.setPlaceholderText("1,3,5")
        weeks_edit.setMaximumWidth(120)
        self.layer_table.setCellWidget(row, self.LAYER_COL_MONTH_WEEKS, weeks_edit)

        start_edit = QLineEdit(self._display_layer_hhmm_from_utc((row_data.get("start_utc") or "").strip()))
        start_edit.setPlaceholderText("HH:MM")
        start_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.layer_table.setCellWidget(row, self.LAYER_COL_START, start_edit)

        end_edit = QLineEdit(self._display_layer_hhmm_from_utc((row_data.get("end_utc") or "").strip()))
        end_edit.setPlaceholderText("HH:MM")
        end_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.layer_table.setCellWidget(row, self.LAYER_COL_END, end_edit)

        band_combo = self._make_band_widget((row_data.get("band") or "").strip().upper())
        self.layer_table.setCellWidget(row, self.LAYER_COL_BAND, band_combo)

        freq_combo = self._make_freq_widget((row_data.get("frequency") or "").strip())
        freq_combo.setProperty(
            "operating_group_frequency_key",
            str(row_data.get("operating_group_frequency_key") or "").strip(),
        )
        freq_combo.setProperty(
            "frequency_identity",
            (
                self.group_combo.currentText().strip().upper(),
                str(row_data.get("band") or "").strip().upper(),
                str(row_data.get("mode") or "").strip().upper(),
                str(row_data.get("frequency") or "").strip(),
            ),
        )
        self.layer_table.setCellWidget(row, self.LAYER_COL_FREQ, freq_combo)

        mode_combo = QComboBox()
        mode_combo.setEditable(True)
        self.layer_table.setCellWidget(row, self.LAYER_COL_MODE, mode_combo)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(lambda _=False, b=remove_btn: self._remove_layer_row_for_button(b))
        self.layer_table.setCellWidget(row, self.LAYER_COL_REMOVE, remove_btn)

        band_combo.currentIndexChanged.connect(lambda _=0, r=row: self._refresh_layer_freq_for_row(r))
        band_combo.currentTextChanged.connect(lambda _=None, r=row: self._refresh_layer_freq_for_row(r))
        band_combo.currentIndexChanged.connect(lambda _=0, r=row: self._refresh_layer_mode_for_row(r))
        band_combo.currentTextChanged.connect(lambda _=None, r=row: self._refresh_layer_mode_for_row(r))
        rec_combo.currentIndexChanged.connect(lambda _=0, r=row: self._on_layer_recurrence_changed(r))

        self._refresh_layer_freq_for_row(row)
        self._refresh_layer_mode_for_row(row)
        if (row_data.get("mode") or "").strip():
            mode_combo.setCurrentText((row_data.get("mode") or "").strip().upper())
        self._on_layer_recurrence_changed(row)

        day_combo.currentIndexChanged.connect(self._mark_dirty)
        day_combo.currentIndexChanged.connect(self._refresh_layer_validation_hints)
        rec_combo.currentIndexChanged.connect(self._mark_dirty)
        rec_combo.currentIndexChanged.connect(self._refresh_layer_validation_hints)
        weeks_edit.textChanged.connect(self._mark_dirty)
        weeks_edit.textChanged.connect(self._refresh_layer_validation_hints)
        start_edit.textChanged.connect(self._mark_dirty)
        start_edit.textChanged.connect(self._refresh_layer_validation_hints)
        end_edit.textChanged.connect(self._mark_dirty)
        end_edit.textChanged.connect(self._refresh_layer_validation_hints)
        band_combo.currentIndexChanged.connect(self._mark_dirty)
        band_combo.currentIndexChanged.connect(self._refresh_layer_validation_hints)
        band_combo.currentTextChanged.connect(self._mark_dirty)
        band_combo.currentTextChanged.connect(self._refresh_layer_validation_hints)
        freq_combo.currentIndexChanged.connect(self._mark_dirty)
        freq_combo.currentIndexChanged.connect(self._refresh_layer_validation_hints)
        freq_combo.currentTextChanged.connect(self._mark_dirty)
        freq_combo.currentTextChanged.connect(self._refresh_layer_validation_hints)
        mode_combo.currentIndexChanged.connect(self._mark_dirty)
        mode_combo.currentIndexChanged.connect(self._refresh_layer_validation_hints)
        mode_combo.currentTextChanged.connect(self._mark_dirty)
        mode_combo.currentTextChanged.connect(self._refresh_layer_validation_hints)
        self._mark_dirty()
        self._autosize_layer_table()
        self._refresh_layer_validation_hints()

    def _on_layer_recurrence_changed(self, row: int) -> None:
        if row < 0 or row >= self.layer_table.rowCount():
            return
        rec_combo = self.layer_table.cellWidget(row, self.LAYER_COL_RECURRENCE)
        weeks_edit = self.layer_table.cellWidget(row, self.LAYER_COL_MONTH_WEEKS)
        if not isinstance(rec_combo, QComboBox) or not isinstance(weeks_edit, QLineEdit):
            return
        recurrence = (rec_combo.currentText() or "Weekly").strip().title()
        periodic = recurrence == "Periodic"
        weeks_edit.setEnabled(periodic)
        if not periodic:
            weeks_edit.setText("")
        self._refresh_layer_validation_hints()

    def _remove_layer_row_for_button(self, btn: QPushButton) -> None:
        for r in range(self.layer_table.rowCount()):
            if self.layer_table.cellWidget(r, self.LAYER_COL_REMOVE) is btn:
                self.layer_table.removeRow(r)
                self._mark_dirty()
                self._autosize_layer_table()
                self._refresh_layer_validation_hints()
                break

    @staticmethod
    def _hhmm_to_minutes(value: str) -> int | None:
        txt = (value or "").strip()
        if len(txt) != 5 or ":" not in txt:
            return None
        try:
            hh, mm = txt.split(":", 1)
            h = int(hh)
            m = int(mm)
            if h < 0 or h > 23 or m < 0 or m > 59:
                return None
            return (h * 60) + m
        except Exception:
            return None

    @staticmethod
    def _normalize_freq_text(value: str) -> str:
        txt = (value or "").strip()
        if not txt:
            return ""
        normalized = format_frequency_mhz(txt)
        return normalized if normalize_frequency_hz(normalized) is not None else ""

    @staticmethod
    def _interval_segments(start_min: int, end_min: int) -> List[Tuple[int, int]]:
        if start_min == end_min:
            return [(0, 1440)]
        if end_min > start_min:
            return [(start_min, end_min)]
        return [(start_min, 1440), (0, end_min)]

    def _layer_time_windows_overlap(self, start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
        a0 = self._hhmm_to_minutes(start_a)
        a1 = self._hhmm_to_minutes(end_a)
        b0 = self._hhmm_to_minutes(start_b)
        b1 = self._hhmm_to_minutes(end_b)
        if a0 is None or a1 is None or b0 is None or b1 is None:
            return False
        for sa, ea in self._interval_segments(a0, a1):
            for sb, eb in self._interval_segments(b0, b1):
                if sa < eb and sb < ea:
                    return True
        return False

    @staticmethod
    def _row_may_share_day_context(row_a: Dict[str, Any], row_b: Dict[str, Any]) -> bool:
        rec_a = str(row_a.get("recurrence") or "Weekly").strip().title()
        rec_b = str(row_b.get("recurrence") or "Weekly").strip().title()
        day_a = str(row_a.get("day") or "ALL").strip() or "ALL"
        day_b = str(row_b.get("day") or "ALL").strip() or "ALL"
        if rec_a == "Daily" or rec_b == "Daily":
            return True
        if day_a != "ALL" and day_b != "ALL" and day_a != day_b:
            return False
        if rec_a == "Periodic" and rec_b == "Periodic":
            weeks_a = {int(x) for x in str(row_a.get("weeks") or "").split(",") if str(x).strip().isdigit()}
            weeks_b = {int(x) for x in str(row_b.get("weeks") or "").split(",") if str(x).strip().isdigit()}
            if weeks_a and weeks_b and not (weeks_a & weeks_b):
                return False
        return True

    @staticmethod
    def _set_layer_widget_warning(widget: QWidget | None, issues: List[str]) -> None:
        if not isinstance(widget, QWidget):
            return
        if issues:
            widget.setStyleSheet("border: 1px solid #C99700; border-radius: 3px;")
            widget.setToolTip("\n".join(issues))
        else:
            widget.setStyleSheet("")
            widget.setToolTip("")

    def _refresh_layer_validation_hints(self, *_args) -> None:
        row_meta: List[Dict[str, Any]] = []
        for r in range(self.layer_table.rowCount()):
            day_combo = self.layer_table.cellWidget(r, self.LAYER_COL_DAY)
            rec_combo = self.layer_table.cellWidget(r, self.LAYER_COL_RECURRENCE)
            weeks_edit = self.layer_table.cellWidget(r, self.LAYER_COL_MONTH_WEEKS)
            start_edit = self.layer_table.cellWidget(r, self.LAYER_COL_START)
            end_edit = self.layer_table.cellWidget(r, self.LAYER_COL_END)
            band_combo = self.layer_table.cellWidget(r, self.LAYER_COL_BAND)
            freq_combo = self.layer_table.cellWidget(r, self.LAYER_COL_FREQ)
            mode_combo = self.layer_table.cellWidget(r, self.LAYER_COL_MODE)
            if not isinstance(day_combo, QComboBox) or not isinstance(rec_combo, QComboBox):
                continue
            if not isinstance(weeks_edit, QLineEdit) or not isinstance(start_edit, QLineEdit):
                continue
            if not isinstance(end_edit, QLineEdit):
                continue
            if not isinstance(freq_combo, QComboBox):
                continue
            day_val = str(day_combo.currentData() or day_combo.currentText() or "ALL").strip() or "ALL"
            rec_val = (rec_combo.currentText() or "Daily").strip().title()
            weeks_val = self._normalize_weeks_text(weeks_edit.text())
            start_val = start_edit.text().strip()
            end_val = end_edit.text().strip()
            band_val = band_combo.currentText().strip().upper() if isinstance(band_combo, QComboBox) else ""
            freq_val = freq_combo.currentText().strip()
            mode_val = mode_combo.currentText().strip().upper() if isinstance(mode_combo, QComboBox) else ""
            row_blank = not any([weeks_val, start_val, end_val, band_val, freq_val, mode_val])
            issues: Set[str] = set()
            if not row_blank:
                if not self._is_valid_hhmm(start_val):
                    issues.add("Start time must be HH:MM.")
                if not self._is_valid_hhmm(end_val):
                    issues.add("End time must be HH:MM.")
                if not freq_val:
                    issues.add("Frequency is required.")
                elif self._normalize_freq_text(freq_val) == "":
                    issues.add("Frequency is invalid.")
                elif band_val:
                    variant_issue = self._sop_frequency_reference_issue(
                        group=self.group_combo.currentText().strip().upper(),
                        band=band_val,
                        mode=mode_val,
                        frequency=self._normalize_freq_text(freq_val),
                        frequency_key=str(freq_combo.property("operating_group_frequency_key") or ""),
                    )
                    if variant_issue:
                        issues.add(variant_issue)
                if rec_val == "Periodic" and not weeks_val:
                    issues.add("Weeks are required for Periodic recurrence.")
            row_meta.append(
                {
                    "row": r,
                    "day": "ALL" if rec_val == "Daily" else day_val,
                    "recurrence": rec_val,
                    "weeks": weeks_val,
                    "start": start_val,
                    "end": end_val,
                    "band": band_val,
                    "freq": self._normalize_freq_text(freq_val),
                    "blank": row_blank,
                    "issues": issues,
                    "widgets": {
                        "day": day_combo,
                        "recurrence": rec_combo,
                        "weeks": weeks_edit,
                        "start": start_edit,
                        "end": end_edit,
                        "band": band_combo,
                        "freq": freq_combo,
                        "mode": mode_combo,
                    },
                }
            )

        overlap_pairs: Set[Tuple[int, int]] = set()
        for i in range(len(row_meta)):
            a = row_meta[i]
            if a["blank"] or a["issues"] or not a["freq"]:
                continue
            for j in range(i + 1, len(row_meta)):
                b = row_meta[j]
                if b["blank"] or b["issues"] or not b["freq"]:
                    continue
                if a["band"] != b["band"] or a["freq"] != b["freq"]:
                    continue
                if not self._row_may_share_day_context(a, b):
                    continue
                if self._layer_time_windows_overlap(a["start"], a["end"], b["start"], b["end"]):
                    overlap_pairs.add((int(a["row"]), int(b["row"])))

        for ra, rb in overlap_pairs:
            for meta in row_meta:
                if int(meta["row"]) == ra:
                    meta["issues"].add(f"Potential overlap with row {rb + 1}.")
                if int(meta["row"]) == rb:
                    meta["issues"].add(f"Potential overlap with row {ra + 1}.")

        warnings: List[str] = []
        for meta in row_meta:
            row_issues = sorted(list(meta["issues"]))
            row_widgets = meta["widgets"]
            self._set_layer_widget_warning(row_widgets.get("start"), row_issues)
            self._set_layer_widget_warning(row_widgets.get("end"), row_issues)
            self._set_layer_widget_warning(row_widgets.get("freq"), row_issues)
            self._set_layer_widget_warning(row_widgets.get("weeks"), row_issues)
            if row_issues:
                warnings.append(f"Row {int(meta['row']) + 1}: {'; '.join(row_issues)}")

        if warnings:
            preview = warnings[:5]
            if len(warnings) > 5:
                preview.append(f"...and {len(warnings) - 5} more row warning(s).")
            self.layer_validation_label.setText("Layer warnings:\n" + "\n".join(preview))
            self.layer_validation_label.setVisible(True)
        else:
            self.layer_validation_label.setText("")
            self.layer_validation_label.setVisible(False)

    def _layer_row_key(self, row: Dict[str, Any]) -> Tuple[str, str, int, str, str, str, str, str, str]:
        return (
            str(row.get("day_utc") or "ALL"),
            str(row.get("recurrence") or "Weekly"),
            int(row.get("biweekly_offset_weeks") or 0),
            self._normalize_weeks_text(str(row.get("month_weeks") or "")),
            str(row.get("band") or "").strip().upper(),
            str(row.get("mode") or "").strip().upper(),
            str(row.get("frequency") or "").strip(),
            str(row.get("start_utc") or "").strip(),
            str(row.get("end_utc") or "").strip(),
        )

    def _collect_action_rows_for_layer_seed(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for r in range(self.actions_table.rowCount()):
            sw_combo = self.actions_table.cellWidget(r, self.COL_SOFTWARE)
            action_combo = self.actions_table.cellWidget(r, self.COL_ACTION)
            band_combo = self.actions_table.cellWidget(r, self.COL_BAND)
            freq_combo = self.actions_table.cellWidget(r, self.COL_FREQ)
            if not isinstance(sw_combo, QComboBox):
                continue
            software = sw_combo.currentText().strip()
            if software == self.LOCAL_NET_SOFTWARE:
                continue
            action_label = action_combo.currentText().strip() if isinstance(action_combo, QComboBox) else ""
            band = band_combo.currentText().strip().upper() if isinstance(band_combo, QComboBox) else ""
            frequency = freq_combo.currentText().strip() if isinstance(freq_combo, QComboBox) else ""
            rows.append(
                {
                    "software": software,
                    "action_label": action_label,
                    "band": band,
                    "frequency": frequency,
                }
            )
        return rows

    def _count_local_net_actions(self) -> int:
        count = 0
        for r in range(self.actions_table.rowCount()):
            if self._is_local_net_action_row(r):
                count += 1
        return count

    def _current_layer_rows(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for r in range(self.layer_table.rowCount()):
            day_combo = self.layer_table.cellWidget(r, self.LAYER_COL_DAY)
            rec_combo = self.layer_table.cellWidget(r, self.LAYER_COL_RECURRENCE)
            weeks_edit = self.layer_table.cellWidget(r, self.LAYER_COL_MONTH_WEEKS)
            start_edit = self.layer_table.cellWidget(r, self.LAYER_COL_START)
            end_edit = self.layer_table.cellWidget(r, self.LAYER_COL_END)
            band_combo = self.layer_table.cellWidget(r, self.LAYER_COL_BAND)
            freq_combo = self.layer_table.cellWidget(r, self.LAYER_COL_FREQ)
            mode_combo = self.layer_table.cellWidget(r, self.LAYER_COL_MODE)
            if not isinstance(day_combo, QComboBox) or not isinstance(rec_combo, QComboBox):
                continue
            if not isinstance(weeks_edit, QLineEdit) or not isinstance(start_edit, QLineEdit):
                continue
            if not isinstance(end_edit, QLineEdit) or not isinstance(freq_combo, QComboBox):
                continue
            out.append(
                {
                    "id": int(day_combo.property("layer_id") or 0),
                    "day_utc": str(day_combo.currentData() or day_combo.currentText() or "ALL").strip(),
                    "recurrence": (rec_combo.currentText() or "Daily").strip().title(),
                    "biweekly_offset_weeks": 0,
                    "month_weeks": self._normalize_weeks_text(weeks_edit.text()),
                    "band": band_combo.currentText().strip().upper() if isinstance(band_combo, QComboBox) else "",
                    "mode": mode_combo.currentText().strip().upper() if isinstance(mode_combo, QComboBox) else "",
                    "frequency": freq_combo.currentText().strip(),
                    "start_utc": self._utc_layer_hhmm_from_display(start_edit.text().strip(), show_local=self._show_local),
                    "end_utc": self._utc_layer_hhmm_from_display(end_edit.text().strip(), show_local=self._show_local),
                    "enabled": True,
                    "sort_order": int(day_combo.property("sort_order") if day_combo.property("sort_order") is not None else r),
                }
            )
        return out

    def _apply_layer_rows(self, rows: List[Dict[str, Any]], *, replace_existing: bool) -> int:
        if replace_existing:
            self.layer_table.setRowCount(0)
            for row in rows:
                self._add_layer_row(existing=row)
            self._mark_dirty()
            self._autosize_layer_table()
            self._refresh_layer_validation_hints()
            return len(rows)

        existing_rows = self._current_layer_rows()
        existing_keys = {self._layer_row_key(row) for row in existing_rows}
        added = 0
        for row in rows:
            key = self._layer_row_key(row)
            if key in existing_keys:
                continue
            self._add_layer_row(existing=row)
            existing_keys.add(key)
            added += 1
        if added > 0:
            self._mark_dirty()
            self._autosize_layer_table()
            self._refresh_layer_validation_hints()
        return added

    def _invalidate_layer_sync_cache(self) -> None:
        self._layer_sync_cache_key = None
        self._layer_sync_cache_value = None

    def _build_layer_candidates_from_actions(
        self,
        *,
        use_cache: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[str], int]:
        group = self.group_combo.currentText().strip().upper()
        if not group:
            raise ValueError("Select a Group Name (HF Operating Group) before populating layer rows.")
        action_rows = self._collect_action_rows_for_layer_seed()
        if not action_rows:
            raise ValueError(
                "No eligible non-local action rows found. "
                "Local Net SOP actions are supported, but Layer Sync applies to HF/Net schedule actions."
            )
        action_sig: List[Tuple[str, str, str, str]] = []
        for row in action_rows:
            band = str(row.get("band") or "").strip().upper()
            freq = self._normalize_freq_text(str(row.get("frequency") or "").strip())
            software = str(row.get("software") or "").strip().upper()
            action_label = str(row.get("action_label") or "").strip().upper()
            action_sig.append((software, action_label, band, freq))
        cache_key: Tuple[Any, ...] = ("layer_candidates", group, tuple(sorted(action_sig)))
        if use_cache and self._layer_sync_cache_key == cache_key and self._layer_sync_cache_value is not None:
            c_rows, c_unmatched, c_matched = self._layer_sync_cache_value
            return [dict(x) for x in c_rows], list(c_unmatched), int(c_matched)
        result = self.manager.build_schedule_layer_candidates(
            operating_group=group,
            action_rows=action_rows,
        )
        candidates = result.get("rows", []) or []
        unmatched = result.get("unmatched", []) or []
        matched_actions = int(result.get("matched_actions") or 0)
        self._layer_sync_cache_key = cache_key
        self._layer_sync_cache_value = ([dict(x) for x in candidates], list(unmatched), matched_actions)
        return candidates, unmatched, matched_actions

    def _refresh_layer_sync_hint(self) -> None:
        theme = resolve_theme(self.settings)
        default_style = f"color: {theme.get('text_muted', '#888')};"
        text = ""
        style = default_style
        show = False
        out_of_sync = False
        has_basis = False
        try:
            candidates, unmatched, matched_actions = self._build_layer_candidates_from_actions(use_cache=True)
            existing_rows = self._current_layer_rows()
            existing_keys = {self._layer_row_key(r) for r in existing_rows}
            candidate_keys = {self._layer_row_key(r) for r in candidates}
            add_count = len([key for key in candidate_keys if key not in existing_keys])
            remove_count = len([key for key in existing_keys if key not in candidate_keys])
            has_basis = True
            show = True
            if not candidates:
                text = "Layer Sync: No matching HF/Net schedule windows for current action targets."
                style = f"color: {theme.get('warning', '#C99700')};"
            elif add_count == 0 and remove_count == 0:
                text = "Layer Sync: In Sync"
                style = f"color: {theme.get('success', '#2E7D32')}; font-weight: 600;"
            else:
                out_of_sync = True
                unmatched_count = len(unmatched)
                text = f"Layer Sync: Out of Sync (+{add_count} / -{remove_count})"
                if unmatched_count > 0:
                    text += f" | {unmatched_count} unmatched action target(s)"
                if matched_actions >= 0:
                    text += f" | matched targets: {matched_actions}"
                style = f"color: {theme.get('warning', '#C99700')}; font-weight: 600;"
        except ValueError:
            group = self.group_combo.currentText().strip().upper()
            actions = self._collect_action_rows_for_layer_seed()
            local_count = self._count_local_net_actions()
            if not group and actions:
                show = True
                text = "Layer Sync: Select a Group Name (HF Operating Group) to evaluate layer alignment."
            elif group and not actions:
                show = True
                if local_count > 0:
                    text = (
                        "Layer Sync: Local Net actions are active reminders. "
                        "Layer Sync is only used for HF/Net schedule actions."
                    )
                else:
                    text = "Layer Sync: Add non-local action rows to evaluate layer alignment."
            else:
                show = False
                text = ""
            style = default_style
            has_basis = False
            out_of_sync = False
        except Exception as e:
            log.debug("SOP: layer sync hint refresh failed: %s", e)
            show = False
            text = ""
            has_basis = False
            out_of_sync = False

        self._layer_sync_has_basis = has_basis
        self._layer_sync_out_of_sync = out_of_sync
        self.layer_sync_label.setVisible(show)
        self.layer_sync_label.setText(text)
        self.layer_sync_label.setStyleSheet(style)
        self._update_profile_action_styles(theme)

    def _layer_row_summary(self, row: Dict[str, Any]) -> str:
        day = str(row.get("day_utc") or "ALL").strip() or "ALL"
        rec = str(row.get("recurrence") or "Daily").strip().title()
        weeks = self._normalize_weeks_text(str(row.get("month_weeks") or ""))
        start = str(row.get("start_utc") or "").strip()
        end = str(row.get("end_utc") or "").strip()
        band = str(row.get("band") or "").strip().upper()
        freq = str(row.get("frequency") or "").strip()
        mode = str(row.get("mode") or "").strip().upper()
        rec_txt = rec if rec != "Periodic" else f"{rec}({weeks or '-'})"
        return f"{day} {rec_txt} {start}-{end} {band} {freq} {mode}".strip()

    def _preview_layer_candidates(
        self,
        *,
        candidates: List[Dict[str, Any]],
        unmatched: List[str],
        matched_actions: int,
        title: str,
    ) -> None:
        if not candidates:
            detail = "\n".join(unmatched[:8]) if unmatched else "No matching schedule windows were found."
            QMessageBox.information(self, "SOP Layer", f"No layer candidates found.\n\n{detail}")
            return
        existing_rows = self._current_layer_rows()
        existing_keys = {self._layer_row_key(r) for r in existing_rows}
        candidate_keys = {self._layer_row_key(r) for r in candidates}
        add_rows = [r for r in candidates if self._layer_row_key(r) not in existing_keys]
        remove_rows = [r for r in existing_rows if self._layer_row_key(r) not in candidate_keys]
        unchanged = len(existing_keys & candidate_keys)
        summary_lines = [
            f"Generated {len(candidates)} candidate row(s) from {matched_actions} matched action target(s).",
            "",
            "Diff vs current layer:",
            f"+ Add: {len(add_rows)}",
            f"- Remove (rebuild): {len(remove_rows)}",
            f"= Unchanged: {unchanged}",
        ]
        if add_rows:
            summary_lines.append("")
            summary_lines.append("Adds:")
            summary_lines.extend(self._layer_row_summary(row) for row in add_rows[:4])
            if len(add_rows) > 4:
                summary_lines.append(f"...and {len(add_rows) - 4} more add row(s).")
        if remove_rows:
            summary_lines.append("")
            summary_lines.append("Removals on rebuild:")
            summary_lines.extend(self._layer_row_summary(row) for row in remove_rows[:4])
            if len(remove_rows) > 4:
                summary_lines.append(f"...and {len(remove_rows) - 4} more remove row(s).")
        if unmatched:
            summary_lines.append("")
            summary_lines.append("Unmatched actions:")
            summary_lines.extend(unmatched[:4])
            if len(unmatched) > 4:
                summary_lines.append(f"...and {len(unmatched) - 4} more unmatched action(s).")

        msg = QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setText("\n".join(summary_lines))
        append_btn = msg.addButton("Append Missing", QMessageBox.AcceptRole)
        rebuild_btn = msg.addButton("Apply Rebuild", QMessageBox.DestructiveRole)
        msg.addButton(QMessageBox.Cancel)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == append_btn:
            added = self._apply_layer_rows(candidates, replace_existing=False)
            QMessageBox.information(
                self,
                "SOP Layer",
                f"Appended {added} new row(s). {len(candidates) - added} duplicate row(s) were skipped.",
            )
        elif clicked == rebuild_btn:
            replaced = self._apply_layer_rows(candidates, replace_existing=True)
            QMessageBox.information(
                self,
                "SOP Layer",
                f"Rebuilt layer with {replaced} row(s). Removed {len(remove_rows)} row(s).",
            )

    def _on_populate_layer_from_actions(self) -> None:
        try:
            candidates, unmatched, matched_actions = self._build_layer_candidates_from_actions()
        except ValueError as e:
            QMessageBox.information(self, "SOP Layer", str(e))
            return
        self._preview_layer_candidates(
            candidates=candidates,
            unmatched=unmatched,
            matched_actions=matched_actions,
            title="Populate SOP Layer",
        )

    def _on_rebuild_layer_preview(self) -> None:
        try:
            candidates, unmatched, matched_actions = self._build_layer_candidates_from_actions()
        except ValueError as e:
            QMessageBox.information(self, "SOP Layer", str(e))
            return
        self._preview_layer_candidates(
            candidates=candidates,
            unmatched=unmatched,
            matched_actions=matched_actions,
            title="Rebuild SOP Layer Preview",
        )

    def _populate_actions(self, existing: List[Dict[str, Any]]) -> None:
        self.actions_table.setRowCount(0)
        self._hidden_actions = []

        configured = set(self._configured_softwares())
        ordered = sorted(existing, key=lambda x: int(x.get("sort_order") or 0))
        for row in ordered:
            sw = (row.get("software") or "").strip()
            if sw not in configured:
                self._hidden_actions.append(dict(row))
                continue
            self._add_action_row(existing=row)

        if self.actions_table.rowCount() == 0 and configured:
            self._add_action_row(existing=None)

        self._update_hidden_actions_label()
        self._autosize_actions_table()

    def _make_band_widget(self, value: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setMinimumContentsLength(4)
        combo.setMinimumWidth(84)
        combo.addItem("")
        for b in self._band_options_for_group(self.group_combo.currentText()):
            combo.addItem(b)
        combo.setCurrentText((value or "").strip().upper())
        self._fit_combo_popup(combo)
        return combo

    def _make_freq_widget(self, value: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItem("")
        for f in self._frequency_options_for_group_band(self.group_combo.currentText(), ""):
            combo.addItem(f)
        combo.setCurrentText((value or "").strip())
        self._fit_combo_popup(combo)
        return combo

    def _make_software_widget(self, value: str) -> QComboBox:
        combo = QComboBox()
        combo.setProperty("_fio_popup_max_width", 360)
        for sw in self._configured_softwares():
            combo.addItem(sw)
        if value and combo.findText(value) < 0:
            combo.addItem(value)
        if value:
            combo.setCurrentText(value)
        self._fit_combo_popup(combo)
        return combo

    def _apply_typeahead(self, combo: QComboBox) -> None:
        try:
            completer = QCompleter(combo.model(), combo)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchContains)
            completer.setCompletionMode(QCompleter.PopupCompletion)
            combo.setCompleter(completer)
        except Exception:
            pass

    def _tz_short_name(self) -> str:
        tz_name = str(self.settings.get("timezone", "UTC") or "UTC")
        upper = tz_name.upper()
        if "UTC" in upper:
            return "UTC"
        if "NEW_YORK" in upper or "EASTERN" in upper:
            return "ET"
        if "CHICAGO" in upper or "CENTRAL" in upper:
            return "CT"
        if "DENVER" in upper or "MOUNTAIN" in upper:
            return "MT"
        if "LOS_ANGELES" in upper or "PACIFIC" in upper:
            return "PT"
        try:
            tz = get_timezone(tz_name)
            now = datetime.datetime.now(datetime.timezone.utc).astimezone(tz)
            return now.tzname() or "Local"
        except Exception:
            return "Local"

    @staticmethod
    def _format_interval_hhmm(minutes: int) -> str:
        m = max(1, int(minutes))
        return f"{m // 60:02d}:{m % 60:02d}"

    @staticmethod
    def _format_interval_spec(interval_minutes: int, phase_minutes: int = 0) -> str:
        total = max(1, int(interval_minutes))
        base = "Daily" if total == (24 * 60) else SOPTab._format_interval_hhmm(total)
        phase = max(0, int(phase_minutes or 0))
        if phase <= 0:
            return base
        return f"{base}@{phase}m"

    def _parse_interval_minutes(self, text: str) -> int:
        minutes, _phase = self._parse_interval_spec(text)
        return minutes

    def _parse_simple_minutes(self, text: str) -> int:
        raw = (text or "").strip().lower()
        if not raw:
            raise ValueError("Interval is required.")
        if raw in {"daily", "day"}:
            return 24 * 60
        if raw.endswith("m"):
            return max(1, int(float(raw[:-1].strip())))
        if raw.endswith("h"):
            return max(1, int(round(float(raw[:-1].strip()) * 60)))
        if ":" in raw:
            hh, mm = raw.split(":", 1)
            total = (int(hh.strip() or "0") * 60) + int(mm.strip() or "0")
            if total <= 0:
                raise ValueError("Interval must be greater than 00:00.")
            return total
        if raw.isdigit() and len(raw) == 4:
            total = (int(raw[:2]) * 60) + int(raw[2:])
            if total <= 0:
                raise ValueError("Interval must be greater than 00:00.")
            return total
        if raw.isdigit():
            return max(1, int(raw))
        if "." in raw:
            return max(1, int(round(float(raw) * 60)))
        raise ValueError(f"Invalid interval: {text}")

    def _parse_phase_minutes(self, text: str) -> int:
        raw = (text or "").strip().lower()
        if not raw:
            return 0
        if raw.endswith("m"):
            raw = raw[:-1].strip()
        if ":" in raw:
            hh, mm = raw.split(":", 1)
            total = (int(hh.strip() or "0") * 60) + int(mm.strip() or "0")
            if total < 0:
                raise ValueError(f"Invalid interval phase: {text}")
            return total
        total = int(raw)
        if total < 0:
            raise ValueError(f"Invalid interval phase: {text}")
        return total

    def _parse_interval_spec(self, text: str) -> Tuple[int, int]:
        raw = (text or "").strip()
        if not raw:
            raise ValueError("Interval is required.")
        base = raw
        phase = 0
        if "@" in raw:
            base_part, phase_part = raw.split("@", 1)
            base = base_part.strip()
            phase = self._parse_phase_minutes(phase_part)
        interval_minutes = self._parse_simple_minutes(base)
        if phase:
            phase = phase % max(1, interval_minutes)
        return interval_minutes, phase

    def _refresh_action_combo_for_row(
        self,
        row: int,
        preferred_key: str | None = None,
        keep_current: bool = True,
    ) -> None:
        sw_combo = self.actions_table.cellWidget(row, self.COL_SOFTWARE)
        action_combo = self.actions_table.cellWidget(row, self.COL_ACTION)
        if not isinstance(sw_combo, QComboBox) or not isinstance(action_combo, QComboBox):
            return
        software = sw_combo.currentText().strip()
        preferred = preferred_key
        if preferred is None and keep_current:
            preferred = action_combo.currentData() if action_combo.count() else ""
        preferred = str(preferred or "").strip()
        catalog = self._action_catalog().get(software, [])
        action_combo.blockSignals(True)
        action_combo.clear()
        has_spotter = any(key.startswith("js8_spotter_") for key, _label in catalog)
        inserted_spotter_header = False
        for key, label in catalog:
            if key.startswith("js8_spotter_") and not inserted_spotter_header:
                action_combo.addItem("Spotter", "__spotter_header__")
                inserted_spotter_header = True
            action_combo.addItem(label, key)
        if has_spotter:
            model = action_combo.model()
            for idx in range(action_combo.count()):
                if action_combo.itemData(idx) == "__spotter_header__":
                    item = model.item(idx)
                    if item is not None:
                        item.setEnabled(False)
                    break
        if preferred and action_combo.findData(preferred) < 0 and keep_current:
            action_combo.addItem(preferred, preferred)
        idx = action_combo.findData(preferred)
        if idx >= 0:
            action_combo.setCurrentIndex(idx)
        elif action_combo.count() > 0:
            action_combo.setCurrentIndex(0)
        self._fit_combo_popup(action_combo)
        action_combo.blockSignals(False)

    def _add_action_row(self, existing: Dict[str, Any] | None) -> None:
        row = self.actions_table.rowCount()
        self.actions_table.insertRow(row)

        band_combo = self._make_band_widget((existing or {}).get("band", ""))
        self.actions_table.setCellWidget(row, self.COL_BAND, band_combo)

        freq_combo = self._make_freq_widget((existing or {}).get("frequency", ""))
        self.actions_table.setCellWidget(row, self.COL_FREQ, freq_combo)
        band_combo.currentIndexChanged.connect(lambda _=0, r=row: self._refresh_freq_combo_for_row(r))
        band_combo.currentTextChanged.connect(lambda _=None, r=row: self._refresh_freq_combo_for_row(r))
        self._refresh_freq_combo_for_row(row)

        sw_combo = self._make_software_widget((existing or {}).get("software", ""))
        sw_combo.setProperty("action_id", int((existing or {}).get("id") or 0))
        sw_combo.setProperty("sort_order", int((existing or {}).get("sort_order") or row))
        sw_combo.currentIndexChanged.connect(lambda _=0, r=row: self._on_software_changed(r))
        self.actions_table.setCellWidget(row, self.COL_SOFTWARE, sw_combo)

        action_combo = QComboBox()
        self.actions_table.setCellWidget(row, self.COL_ACTION, action_combo)
        self._refresh_action_combo_for_row(row, (existing or {}).get("action_key", ""))

        interval_combo = QComboBox()
        interval_combo.setEditable(True)
        for preset in self.INTERVAL_PRESETS:
            interval_combo.addItem(preset, preset)
        interval_minutes = int((existing or {}).get("interval_minutes") or 0)
        if interval_minutes <= 0:
            interval_minutes = int((existing or {}).get("interval_hours") or 24) * 60
        interval_phase = int((existing or {}).get("interval_phase_minutes") or 0)
        interval_txt = self._format_interval_spec(interval_minutes, interval_phase)
        if interval_combo.findText(interval_txt) < 0:
            interval_combo.addItem(interval_txt, interval_txt)
        interval_combo.setCurrentText(interval_txt)
        interval_combo.setToolTip("Examples: Daily, 00:45, 90m, 1.5h, 0130, 03:00@30m")
        if interval_combo.lineEdit() is not None:
            interval_combo.lineEdit().setPlaceholderText("type or select...")
        self._fit_combo_popup(interval_combo)
        self.actions_table.setCellWidget(row, self.COL_INTERVAL, interval_combo)

        rule_combo = QComboBox()
        for code, txt in self._contact_rule_options_for_row(row):
            rule_combo.addItem(txt, code)
        rule = ((existing or {}).get("contact_rule") or "none").strip()
        idx_rule = rule_combo.findData(rule)
        rule_combo.setCurrentIndex(idx_rule if idx_rule >= 0 else 0)
        self._fit_combo_popup(rule_combo)
        self.actions_table.setCellWidget(row, self.COL_CONTACT, rule_combo)

        target_combo = QComboBox()
        target_combo.setEditable(True)
        target_combo.setInsertPolicy(QComboBox.NoInsert)
        target_combo.setMaxVisibleItems(12)
        self._apply_typeahead(target_combo)
        target_combo.setProperty("saved_target", ((existing or {}).get("contact_target") or "").strip().upper())
        self.actions_table.setCellWidget(row, self.COL_CONTACT_TARGET, target_combo)
        self._sync_row_mode_for_action(row)
        self._refresh_contact_target_options_for_row(row)
        rule_combo.currentIndexChanged.connect(lambda _=0, r=row: self._on_contact_rule_changed(r))
        self._on_contact_rule_changed(row)

        desc_edit = QLineEdit((existing or {}).get("description", ""))
        desc_edit.setPlaceholderText("Optional description for reminder meaning")
        self.actions_table.setCellWidget(row, self.COL_DESC, desc_edit)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(lambda _=False, b=remove_btn: self._remove_row_for_button(b))
        self.actions_table.setCellWidget(row, self.COL_REMOVE, remove_btn)
        band_combo.currentIndexChanged.connect(self._mark_dirty)
        band_combo.currentTextChanged.connect(self._mark_dirty)
        freq_combo.currentIndexChanged.connect(self._mark_dirty)
        freq_combo.currentTextChanged.connect(self._mark_dirty)
        sw_combo.currentIndexChanged.connect(self._mark_dirty)
        action_combo.currentIndexChanged.connect(lambda _=0, r=row: self._on_action_selection_changed(r))
        action_combo.currentIndexChanged.connect(self._mark_dirty)
        interval_combo.currentIndexChanged.connect(self._mark_dirty)
        if interval_combo.lineEdit() is not None:
            interval_combo.lineEdit().textChanged.connect(self._mark_dirty)
        rule_combo.currentIndexChanged.connect(self._mark_dirty)
        target_combo.currentIndexChanged.connect(self._mark_dirty)
        target_combo.currentTextChanged.connect(self._mark_dirty)
        desc_edit.textChanged.connect(self._mark_dirty)
        self._mark_dirty()
        self._autosize_actions_table()

    def _on_software_changed(self, row: int) -> None:
        self._refresh_action_combo_for_row(row, preferred_key=None, keep_current=False)
        self._on_action_selection_changed(row)

    def _on_action_selection_changed(self, row: int) -> None:
        self._sync_row_mode_for_action(row)
        self._refresh_contact_rule_options_for_row(row)
        self._on_contact_rule_changed(row)
        self._refresh_freq_combo_for_row(row)

    def _sync_row_mode_for_action(self, row: int) -> None:
        if row < 0 or row >= self.actions_table.rowCount():
            return
        is_local = self._is_local_net_action_row(row)
        band_combo = self.actions_table.cellWidget(row, self.COL_BAND)
        freq_combo = self.actions_table.cellWidget(row, self.COL_FREQ)
        if isinstance(band_combo, QComboBox):
            if is_local:
                band_combo.blockSignals(True)
                band_combo.setCurrentText("")
                band_combo.blockSignals(False)
            band_combo.setEnabled(not is_local)
        if isinstance(freq_combo, QComboBox):
            if is_local:
                freq_combo.blockSignals(True)
                freq_combo.setCurrentText("")
                freq_combo.blockSignals(False)
            freq_combo.setEnabled(not is_local)

    def _remove_row_for_button(self, btn: QPushButton) -> None:
        for r in range(self.actions_table.rowCount()):
            if self.actions_table.cellWidget(r, self.COL_REMOVE) is btn:
                self.actions_table.removeRow(r)
                self._mark_dirty()
                self._autosize_actions_table()
                break

    def _refresh_freq_combo_for_row(self, row: int) -> None:
        if row < 0 or row >= self.actions_table.rowCount():
            return
        if self._is_local_net_action_row(row):
            return
        group = self.group_combo.currentText().strip().upper()
        band_combo = self.actions_table.cellWidget(row, self.COL_BAND)
        freq_combo = self.actions_table.cellWidget(row, self.COL_FREQ)
        if not isinstance(band_combo, QComboBox) or not isinstance(freq_combo, QComboBox):
            return
        selected_band = band_combo.currentText().strip().upper()
        options = self._frequency_options_for_group_band(group, selected_band)
        current = freq_combo.currentText().strip()
        freq_combo.blockSignals(True)
        freq_combo.clear()
        freq_combo.addItem("")
        for val in options:
            freq_combo.addItem(val)
        if not current and len(options) == 1:
            freq_combo.setCurrentText(options[0])
        else:
            freq_combo.setCurrentText(current)
        self._fit_combo_popup(freq_combo)
        freq_combo.blockSignals(False)

    def _role_targets_for_rule(self, rule: str) -> List[str]:
        group = self.group_combo.currentText().strip().upper()
        subgroup = self.secondary_combo.currentText().strip().upper()
        contacts = self.manager.resolve_primary_contacts(group, subgroup)
        if rule == "hub_or_hub_alt":
            return contacts.get("hub", []) or []
        if rule == "ncs_or_ancs":
            return contacts.get("ncs", []) or []
        if rule == "peer":
            return contacts.get("peer", []) or []
        return []

    def _refresh_contact_target_options_for_row(self, row: int) -> None:
        if row < 0 or row >= self.actions_table.rowCount():
            return
        rule_combo = self.actions_table.cellWidget(row, self.COL_CONTACT)
        target_combo = self.actions_table.cellWidget(row, self.COL_CONTACT_TARGET)
        if not isinstance(target_combo, QComboBox) or not isinstance(rule_combo, QComboBox):
            return
        current = (
            target_combo.currentText().strip().upper()
            or str(target_combo.property("saved_target") or "").strip().upper()
        )
        rule = str(rule_combo.currentData() or "none").strip()
        target_combo.blockSignals(True)
        target_combo.clear()
        target_combo.addItem("")
        if rule in {"local_group", "local_profile"}:
            for name in self._local_profile_names():
                target_combo.addItem(name, name.upper())
            if current and target_combo.findData(current) < 0:
                target_combo.addItem(current, current)
            idx = target_combo.findData(current)
            target_combo.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            for cs in self._available_callsign_targets():
                target_combo.addItem(cs, cs)
            if current and target_combo.findText(current) < 0:
                target_combo.addItem(current, current)
            target_combo.setCurrentText(current)
        self._fit_combo_popup(target_combo)
        target_combo.blockSignals(False)

    def _on_contact_rule_changed(self, row: int) -> None:
        if row < 0 or row >= self.actions_table.rowCount():
            return
        rule_combo = self.actions_table.cellWidget(row, self.COL_CONTACT)
        target_combo = self.actions_table.cellWidget(row, self.COL_CONTACT_TARGET)
        if not isinstance(rule_combo, QComboBox) or not isinstance(target_combo, QComboBox):
            return
        rule = str(rule_combo.currentData() or "none").strip()
        saved_target = str(target_combo.property("saved_target") or "").strip().upper()
        target_combo.blockSignals(True)
        target_combo.clear()
        target_combo.setEditable(True)
        target_combo.setEnabled(True)

        if rule in {"hub_or_hub_alt", "ncs_or_ancs"}:
            target_combo.addItem("Any (Role Match)", self.ANY_ROLE_TOKEN)
            for cs in self._role_targets_for_rule(rule):
                target_combo.addItem(cs, cs)
            chosen = saved_target if saved_target else self.ANY_ROLE_TOKEN
            idx = target_combo.findData(chosen)
            target_combo.setCurrentIndex(idx if idx >= 0 else 0)
            target_combo.setEnabled(True)
            target_combo.setEditable(False)
        elif rule == "peer":
            target_combo.addItem("", "")
            for cs in self._role_targets_for_rule("peer"):
                target_combo.addItem(cs, cs)
            if target_combo.lineEdit() is not None:
                target_combo.lineEdit().setPlaceholderText("type or select...")
            if saved_target:
                idx = target_combo.findData(saved_target)
                target_combo.setCurrentIndex(idx if idx >= 0 else 0)
            else:
                target_combo.setCurrentIndex(0)
            target_combo.setEnabled(True)
            target_combo.setEditable(True)
        elif rule == "callsign":
            target_combo.addItem("", "")
            for cs in self._available_callsign_targets():
                target_combo.addItem(cs, cs)
            if target_combo.lineEdit() is not None:
                target_combo.lineEdit().setPlaceholderText("type or select...")
            if saved_target:
                idx = target_combo.findData(saved_target)
                target_combo.setCurrentIndex(idx if idx >= 0 else 0)
            else:
                target_combo.setCurrentIndex(0)
            target_combo.setEnabled(True)
            target_combo.setEditable(True)
        elif rule in {"local_group", "local_profile"}:
            target_combo.addItem("", "")
            for name in self._local_profile_names():
                target_combo.addItem(name, name.upper())
            idx = target_combo.findData(saved_target)
            target_combo.setCurrentIndex(idx if idx >= 0 else 0)
            target_combo.setEnabled(target_combo.count() > 1)
            target_combo.setEditable(False)
        else:
            target_combo.addItem("", "")
            target_combo.setCurrentIndex(0)
            target_combo.setEnabled(False)
            target_combo.setEditable(False)
        self._fit_combo_popup(target_combo)
        target_combo.blockSignals(False)
        self._autosize_actions_table()

    def _fit_combo_popup(self, combo: QComboBox) -> None:
        try:
            fm = QFontMetrics(combo.font())
            text_w = 0
            for i in range(combo.count()):
                text_w = max(text_w, fm.horizontalAdvance(combo.itemText(i)))
            popup_w = max(combo.width(), text_w + 44)
            max_w_raw = combo.property("_fio_popup_max_width")
            try:
                max_w = int(max_w_raw)
            except Exception:
                max_w = 0
            if max_w > 0:
                popup_w = min(popup_w, max_w)
            view = combo.view()
            if view is not None:
                view.setMinimumWidth(popup_w)
        except Exception:
            pass

    def _collect_profile_payload(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("SOP name is required.")
        group = self.group_combo.currentText().strip().upper()
        start = self.start_edit.text().strip()
        if len(start) != 5 or ":" not in start:
            raise ValueError("SOP Start Time must be HH:MM.")
        start_utc = self._utc_start_hhmm_from_display(start)

        payload = {
            "id": self._selected_profile_id,
            "name": name,
            "operating_group": group,
            "secondary_group": self.secondary_combo.currentText().strip().upper(),
            "frequency": "",
            "sop_start_utc": start_utc,
            "priority": int(self.priority_spin.value()),
            "active": self.active_cb.isChecked(),
            "window_hours": int(self.horizon_spin.value()),
        }

        actions: List[Dict[str, Any]] = []
        schedule_layer: List[Dict[str, Any]] = []
        requires_operating_group = False
        for r in range(self.actions_table.rowCount()):
            band_combo = self.actions_table.cellWidget(r, self.COL_BAND)
            freq_combo = self.actions_table.cellWidget(r, self.COL_FREQ)
            sw_combo = self.actions_table.cellWidget(r, self.COL_SOFTWARE)
            action_combo = self.actions_table.cellWidget(r, self.COL_ACTION)
            interval_combo = self.actions_table.cellWidget(r, self.COL_INTERVAL)
            rule_combo = self.actions_table.cellWidget(r, self.COL_CONTACT)
            target_combo = self.actions_table.cellWidget(r, self.COL_CONTACT_TARGET)
            desc_edit = self.actions_table.cellWidget(r, self.COL_DESC)

            if not isinstance(sw_combo, QComboBox) or not isinstance(action_combo, QComboBox):
                continue

            software = sw_combo.currentText().strip()
            action_key = str(action_combo.currentData() or "").strip()
            action_label = action_combo.currentText().strip()
            if not software or not action_key or action_key == "__spotter_header__":
                continue
            is_local_action = software == self.LOCAL_NET_SOFTWARE and action_key in self.LOCAL_NET_ACTION_KEYS
            if not is_local_action:
                requires_operating_group = True
            contact_rule = rule_combo.currentData() if isinstance(rule_combo, QComboBox) else "none"
            if is_local_action:
                contact_rule = "local_group"
            contact_target = ""
            if isinstance(target_combo, QComboBox):
                if str(contact_rule) in {"hub_or_hub_alt", "ncs_or_ancs"}:
                    contact_target = str(target_combo.currentData() or "").strip().upper()
                elif str(contact_rule) in {"callsign", "peer", "local_group", "local_profile"}:
                    contact_target = str(target_combo.currentData() or target_combo.currentText() or "").strip().upper()
            if str(contact_rule) != "none" and not contact_target:
                raise ValueError(f"Contact Target is required on row {r + 1}.")
            interval_minutes, interval_phase_minutes = (
                self._parse_interval_spec(interval_combo.currentText())
                if isinstance(interval_combo, QComboBox)
                else (180, 0)
            )
            description = desc_edit.text().strip() if isinstance(desc_edit, QLineEdit) else ""
            if is_local_action and not description and contact_target:
                description = f"Open local net group {contact_target}"

            actions.append(
                {
                    "id": int(sw_combo.property("action_id") or 0),
                    "band": (
                        band_combo.currentText().strip().upper()
                        if isinstance(band_combo, QComboBox) and not is_local_action
                        else ""
                    ),
                    "frequency": (
                        freq_combo.currentText().strip()
                        if isinstance(freq_combo, QComboBox) and not is_local_action
                        else ""
                    ),
                    "software": software,
                    "action_key": action_key,
                    "action_label": action_label,
                    "enabled": True,
                    "interval_minutes": interval_minutes,
                    "interval_phase_minutes": interval_phase_minutes,
                    "interval_hours": max(1, int((interval_minutes + 59) // 60)),
                    "description": description,
                    "contact_rule": contact_rule,
                    "contact_target": contact_target,
                    "sort_order": int(sw_combo.property("sort_order") or r),
                }
            )

        for i, hidden in enumerate(self._hidden_actions):
            preserved = dict(hidden)
            preserved["sort_order"] = int(
                preserved.get("sort_order") if preserved.get("sort_order") is not None else len(actions) + i
            )
            actions.append(preserved)

        for r in range(self.layer_table.rowCount()):
            day_combo = self.layer_table.cellWidget(r, self.LAYER_COL_DAY)
            rec_combo = self.layer_table.cellWidget(r, self.LAYER_COL_RECURRENCE)
            weeks_edit = self.layer_table.cellWidget(r, self.LAYER_COL_MONTH_WEEKS)
            start_edit = self.layer_table.cellWidget(r, self.LAYER_COL_START)
            end_edit = self.layer_table.cellWidget(r, self.LAYER_COL_END)
            band_combo = self.layer_table.cellWidget(r, self.LAYER_COL_BAND)
            freq_combo = self.layer_table.cellWidget(r, self.LAYER_COL_FREQ)
            mode_combo = self.layer_table.cellWidget(r, self.LAYER_COL_MODE)
            if not isinstance(day_combo, QComboBox) or not isinstance(rec_combo, QComboBox):
                continue
            if not isinstance(weeks_edit, QLineEdit) or not isinstance(start_edit, QLineEdit):
                continue
            if not isinstance(end_edit, QLineEdit):
                continue
            if not isinstance(freq_combo, QComboBox):
                continue
            day = str(day_combo.currentData() or day_combo.currentText() or "ALL").strip()
            recurrence = (rec_combo.currentText() or "Daily").strip().title()
            weeks = self._normalize_weeks_text(weeks_edit.text())
            start_display = start_edit.text().strip()
            end_display = end_edit.text().strip()
            band_val = band_combo.currentText().strip().upper() if isinstance(band_combo, QComboBox) else ""
            mode_val = mode_combo.currentText().strip().upper() if isinstance(mode_combo, QComboBox) else ""
            freq_txt = freq_combo.currentText().strip()
            row_blank = not any([weeks, start_display, end_display, band_val, mode_val, freq_txt])
            if row_blank:
                continue
            if not self._is_valid_hhmm(start_display):
                raise ValueError(f"Layer row {r + 1}: Start time must be HH:MM.")
            if not self._is_valid_hhmm(end_display):
                raise ValueError(f"Layer row {r + 1}: End time must be HH:MM.")
            start_utc = self._utc_layer_hhmm_from_display(start_display, show_local=self._show_local)
            end_utc = self._utc_layer_hhmm_from_display(end_display, show_local=self._show_local)
            if not freq_txt:
                raise ValueError(f"Layer row {r + 1}: Frequency is required.")
            freq_norm = format_frequency_mhz(freq_txt)
            if normalize_frequency_hz(freq_norm) is None:
                raise ValueError(f"Layer row {r + 1}: Frequency is invalid.")
            layer_key = self._resolve_sop_frequency_row_key(
                group=group,
                band=band_val,
                mode=mode_val,
                frequency=freq_norm,
                frequency_key=str(freq_combo.property("operating_group_frequency_key") or ""),
                original_identity=freq_combo.property("frequency_identity"),
            )
            if recurrence not in {"Weekly", "Daily", "Periodic", "Bi-Weekly"}:
                recurrence = "Daily"
            if recurrence == "Periodic" and not weeks:
                raise ValueError(f"Layer row {r + 1}: Weeks are required for Periodic recurrence.")
            if recurrence != "Periodic":
                weeks = ""
            if recurrence == "Daily":
                day = "ALL"
            schedule_layer.append(
                {
                    "id": int(day_combo.property("layer_id") or 0),
                    "day_utc": day or "ALL",
                    "recurrence": recurrence,
                    "biweekly_offset_weeks": 0,
                    "month_weeks": weeks,
                    "band": band_val,
                    "group_name": group,
                    "mode": mode_val,
                    "vfo": "",
                    "frequency": freq_norm,
                    "operating_group_frequency_key": layer_key,
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                    "enabled": True,
                    "sort_order": int(day_combo.property("sort_order") if day_combo.property("sort_order") is not None else r),
                }
            )

        if not actions:
            raise ValueError("Add at least one action row.")
        if schedule_layer:
            requires_operating_group = True
        if requires_operating_group and not group:
            raise ValueError("Group Name (HF Operating Group) is required for non-local SOP actions.")

        return payload, actions, schedule_layer

    def _save_profile(self) -> None:
        try:
            payload, actions, schedule_layer = self._collect_profile_payload()
            profile_id = self.manager.save_profile(payload, actions, schedule_layer)
            self._reload_profiles(select_id=profile_id)
            self._set_save_dirty(False)
            self.refresh_upcoming()
            self._emit_sop_data_changed()
            QMessageBox.information(self, "SOP", "SOP saved.")
        except Exception as e:
            QMessageBox.warning(self, "SOP", str(e))

    def _delete_profile(self) -> None:
        if not self._selected_profile_id:
            return
        resp = QMessageBox.question(self, "Delete SOP", "Delete this SOP profile?")
        if resp != QMessageBox.Yes:
            return
        self.manager.delete_profile(int(self._selected_profile_id))
        self._reload_profiles(select_id=None)
        self.refresh_upcoming()
        self._emit_sop_data_changed()

    def _operator_callsign(self) -> str:
        return str(self.settings.get("operator_callsign", "") or "").strip().upper()

    def _export_pdf_default_name(self, *, scope: str, now_utc: datetime.datetime) -> str:
        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        stamp = now_utc.astimezone(tz).strftime("%Y%m%d-%H%M")
        callsign = self._operator_callsign() or "operator"
        scope_tag = "selected" if scope == "selected" else "active"
        return f"{callsign}-sop-{scope_tag}-{stamp}.pdf"

    def _prompt_pdf_export_options(self) -> Dict[str, Any] | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Export SOP to PDF")
        dlg.setModal(True)
        layout = QVBoxLayout(dlg)

        general_box = QGroupBox("SOP PDF Export Options")
        general_form = QFormLayout(general_box)

        selected_name = ""
        if self._selected_profile_id:
            sid = int(self._selected_profile_id)
            for p in self._profiles:
                if int(p.get("id") or 0) == sid:
                    selected_name = str(p.get("name") or "")
                    break

        scope_combo = QComboBox()
        selected_label = f"Selected SOP ({selected_name})" if selected_name else "Selected SOP"
        scope_combo.addItem(selected_label, "selected")
        scope_combo.addItem("All Active SOPs (Unified)", "active")
        if not self._selected_profile_id:
            scope_combo.setCurrentIndex(1)
        general_form.addRow("Scope:", scope_combo)

        time_combo = QComboBox()
        time_combo.addItem("Local", "Local")
        time_combo.addItem("UTC", "UTC")
        time_combo.setCurrentIndex(0 if self._show_local else 1)
        general_form.addRow("SOP Time Display:", time_combo)
        layout.addWidget(general_box)

        roster_box = QGroupBox("Optional Operator Appendix")
        roster_layout = QVBoxLayout(roster_box)
        include_roster_cb = QCheckBox("Include Operator Rosters")
        include_roster_cb.setChecked(False)
        roster_layout.addWidget(include_roster_cb)

        roster_options_widget = QWidget()
        roster_options_layout = QVBoxLayout(roster_options_widget)
        roster_options_layout.setContentsMargins(0, 0, 0, 0)
        roster_options_layout.setSpacing(6)

        source_row = QHBoxLayout()
        include_hf_cb = QCheckBox("HF Operators")
        include_local_cb = QCheckBox("Local Operators")
        include_hf_cb.setChecked(False)
        include_local_cb.setChecked(False)
        source_row.addWidget(include_hf_cb)
        source_row.addWidget(include_local_cb)
        source_row.addStretch()
        roster_options_layout.addLayout(source_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Roster Filter:"))
        filter_combo = QComboBox()
        filter_combo.addItem("All", "all")
        filter_combo.addItem("State", "state")
        filter_combo.addItem("FEMA Region", "region")
        filter_row.addWidget(filter_combo)

        state_edit = QLineEdit()
        state_edit.setPlaceholderText("State (e.g., TX)")
        state_edit.setMaxLength(2)
        state_edit.setMinimumWidth(130)
        filter_row.addWidget(state_edit)

        region_combo = QComboBox()
        for region in self.manager.list_export_regions():
            region_combo.addItem(region, region)
        region_combo.setMinimumWidth(130)
        filter_row.addWidget(region_combo)
        filter_row.addStretch()
        roster_options_layout.addLayout(filter_row)

        hf_groups = self.manager.list_hf_groups_for_export()
        hf_filter_box = QGroupBox("HF Operator Filters")
        hf_filter_layout = QVBoxLayout(hf_filter_box)
        hf_filter_layout.addWidget(QLabel("Groups (multi-select):"))
        hf_group_checks: List[QCheckBox] = []
        if hf_groups:
            hf_groups_grid = QGridLayout()
            for idx, grp in enumerate(hf_groups):
                cb = QCheckBox(grp)
                cb.setChecked(False)
                hf_group_checks.append(cb)
                hf_groups_grid.addWidget(cb, idx // 3, idx % 3)
            hf_filter_layout.addLayout(hf_groups_grid)
        else:
            hf_filter_layout.addWidget(QLabel("No HF groups detected."))

        trusted_row = QHBoxLayout()
        trusted_row.addWidget(QLabel("Trusted (multi-select):"))
        hf_trusted_yes_cb = QCheckBox("Trusted")
        hf_trusted_no_cb = QCheckBox("Untrusted")
        hf_trusted_yes_cb.setChecked(True)
        hf_trusted_no_cb.setChecked(False)
        trusted_row.addWidget(hf_trusted_yes_cb)
        trusted_row.addWidget(hf_trusted_no_cb)
        trusted_row.addStretch()
        hf_filter_layout.addLayout(trusted_row)
        roster_options_layout.addWidget(hf_filter_box)

        local_categories = self.manager.list_local_categories_for_export()
        local_filter_box = QGroupBox("Local Operator Filters")
        local_filter_layout = QVBoxLayout(local_filter_box)
        local_filter_layout.addWidget(QLabel("Category (multi-select):"))
        local_category_checks: List[QCheckBox] = []
        if local_categories:
            local_grid = QGridLayout()
            for idx, category in enumerate(local_categories):
                cb = QCheckBox(category)
                cb.setChecked(True)
                local_category_checks.append(cb)
                local_grid.addWidget(cb, idx // 3, idx % 3)
            local_filter_layout.addLayout(local_grid)
        else:
            local_filter_layout.addWidget(QLabel("No local categories detected."))
        roster_options_layout.addWidget(local_filter_box)

        roster_layout.addWidget(roster_options_widget)

        layout.addWidget(roster_box)

        def _sync_controls() -> None:
            roster_on = include_roster_cb.isChecked()
            roster_options_widget.setVisible(roster_on)
            hf_on = roster_on and include_hf_cb.isChecked()
            local_on = roster_on and include_local_cb.isChecked()
            include_hf_cb.setEnabled(roster_on)
            include_local_cb.setEnabled(roster_on)
            filter_combo.setEnabled(roster_on)
            mode = str(filter_combo.currentData() or "all")
            state_mode = roster_on and mode == "state"
            region_mode = roster_on and mode == "region"
            state_edit.setEnabled(state_mode)
            region_combo.setEnabled(region_mode)
            hf_filter_box.setEnabled(hf_on)
            local_filter_box.setEnabled(local_on)
            for cb in hf_group_checks:
                cb.setEnabled(hf_on)
            hf_trusted_yes_cb.setEnabled(hf_on)
            hf_trusted_no_cb.setEnabled(hf_on)
            for cb in local_category_checks:
                cb.setEnabled(local_on)

        include_roster_cb.toggled.connect(_sync_controls)
        include_hf_cb.toggled.connect(_sync_controls)
        include_local_cb.toggled.connect(_sync_controls)
        filter_combo.currentIndexChanged.connect(_sync_controls)
        _sync_controls()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dlg)

        def _accept() -> None:
            scope = str(scope_combo.currentData() or "selected")
            if scope == "selected" and not self._selected_profile_id:
                QMessageBox.warning(dlg, "Export SOP to PDF", "Select an SOP first, or choose All Active SOPs.")
                return
            if scope == "active" and not any(bool(p.get("active")) for p in self._profiles):
                QMessageBox.warning(dlg, "Export SOP to PDF", "No active SOP profiles found.")
                return
            if include_roster_cb.isChecked() and not (include_hf_cb.isChecked() or include_local_cb.isChecked()):
                QMessageBox.warning(dlg, "Export SOP to PDF", "Select HF Operators, Local Operators, or both.")
                return
            mode = str(filter_combo.currentData() or "all")
            if include_roster_cb.isChecked() and mode == "state":
                st = state_edit.text().strip().upper()
                if len(st) != 2:
                    QMessageBox.warning(dlg, "Export SOP to PDF", "State filter must be a 2-letter abbreviation.")
                    return
            if include_roster_cb.isChecked() and mode == "region" and not str(region_combo.currentData() or "").strip():
                QMessageBox.warning(dlg, "Export SOP to PDF", "Select a FEMA region for filtering.")
                return
            if include_roster_cb.isChecked() and include_hf_cb.isChecked():
                if hf_group_checks and not any(cb.isChecked() for cb in hf_group_checks):
                    QMessageBox.warning(dlg, "Export SOP to PDF", "Select at least one HF group filter.")
                    return
                if not (hf_trusted_yes_cb.isChecked() or hf_trusted_no_cb.isChecked()):
                    QMessageBox.warning(dlg, "Export SOP to PDF", "Select at least one HF trusted filter.")
                    return
            if include_roster_cb.isChecked() and include_local_cb.isChecked():
                if local_category_checks and not any(cb.isChecked() for cb in local_category_checks):
                    QMessageBox.warning(dlg, "Export SOP to PDF", "Select at least one local category filter.")
                    return
            dlg.accept()

        buttons.accepted.connect(_accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.Accepted:
            return None

        mode = str(filter_combo.currentData() or "all")
        return {
            "scope": str(scope_combo.currentData() or "selected"),
            "time_mode": str(time_combo.currentData() or "Local"),
            "include_roster": bool(include_roster_cb.isChecked()),
            "include_hf": bool(include_hf_cb.isChecked()),
            "include_local": bool(include_local_cb.isChecked()),
            "filter_mode": mode,
            "state_filter": state_edit.text().strip().upper() if mode == "state" else "",
            "region_filter": str(region_combo.currentData() or "").strip().upper() if mode == "region" else "",
            "hf_groups": [cb.text().strip() for cb in hf_group_checks if cb.isChecked()],
            "hf_trusted": [
                label
                for checked, label in (
                    (hf_trusted_yes_cb.isChecked(), "TRUSTED"),
                    (hf_trusted_no_cb.isChecked(), "UNTRUSTED"),
                )
                if checked
            ],
            "local_categories": [cb.text().strip() for cb in local_category_checks if cb.isChecked()],
        }

    def _collect_pdf_profiles(
        self,
        *,
        scope: str,
        now_utc: datetime.datetime,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if scope == "selected":
            profile_id = int(self._selected_profile_id or 0)
            if profile_id <= 0:
                return rows
            profile = self.manager.get_profile(profile_id)
            if not profile:
                return rows
            rows.append({"profile": profile})
            return rows
        for p in self._profiles:
            if not bool(p.get("active")):
                continue
            profile_id = int(p.get("id") or 0)
            if profile_id <= 0:
                continue
            profile = self.manager.get_profile(profile_id)
            if not profile:
                continue
            rows.append({"profile": profile})
        rows.sort(key=lambda x: str((x.get("profile") or {}).get("name") or "").upper())
        return rows

    def _daily_export_window_utc(
        self,
        *,
        time_mode: str,
        now_utc: datetime.datetime,
    ) -> Tuple[datetime.datetime, datetime.datetime, str]:
        mode = str(time_mode or "Local").strip().upper()
        now_norm = now_utc if isinstance(now_utc, datetime.datetime) else datetime.datetime.now(datetime.timezone.utc)
        if now_norm.tzinfo is None:
            now_norm = now_norm.replace(tzinfo=datetime.timezone.utc)
        else:
            now_norm = now_norm.astimezone(datetime.timezone.utc)
        if mode == "UTC":
            day_start = now_norm.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start.replace(hour=23, minute=59, second=59, microsecond=0)
            return day_start, day_end, f"{day_start.strftime('%Y-%m-%d')} UTC"
        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        local_now = now_norm.astimezone(tz)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end = local_start.replace(hour=23, minute=59, second=59, microsecond=0)
        return (
            local_start.astimezone(datetime.timezone.utc),
            local_end.astimezone(datetime.timezone.utc),
            f"{local_start.strftime('%Y-%m-%d')} Local ({tz_name})",
        )

    def _format_due_for_pdf(self, due_utc: datetime.datetime, *, time_mode: str) -> str:
        if not isinstance(due_utc, datetime.datetime):
            return ""
        if str(time_mode).strip() == "UTC":
            return due_utc.strftime("%Y-%m-%d %H:%M")
        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        return due_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")

    def _format_due_clock_for_pdf(self, due_utc: datetime.datetime, *, time_mode: str) -> str:
        if not isinstance(due_utc, datetime.datetime):
            return ""
        if str(time_mode).strip().upper() == "UTC":
            return due_utc.astimezone(datetime.timezone.utc).strftime("%H:%M")
        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        return due_utc.astimezone(tz).strftime("%H:%M")

    def _build_daily_action_plan_html(self, rows: List[Dict[str, Any]], *, time_mode: str) -> str:
        if not rows:
            return "<p class='empty'>No actions found for the selected day.</p>"
        out = [
            "<table>",
            "<thead><tr>"
            "<th style='width: 10%;'>Time</th>"
            "<th style='width: 14%;'>Resource</th>"
            "<th style='width: 12%;'>Action</th>"
            "<th style='width: 12%;'>Band/Freq</th>"
            "<th style='width: 18%;'>Contact</th>"
            "<th style='width: 34%;'>Description</th>"
            "</tr></thead><tbody>",
        ]
        for row in rows:
            out.append(
                "<tr>"
                f"<td>{html.escape(self._format_due_clock_for_pdf(row.get('due_utc'), time_mode=time_mode))}</td>"
                f"<td>{html.escape(str(row.get('resource') or ''))}</td>"
                f"<td>{html.escape(str(row.get('action_label') or ''))}</td>"
                f"<td>{html.escape(str(row.get('band_freq') or '--'))}</td>"
                f"<td>{html.escape(str(row.get('contact_display') or '--'))}</td>"
                f"<td>{html.escape(str(row.get('description') or ''))}</td>"
                "</tr>"
            )
        out.append("</tbody></table>")
        return "".join(out)

    def _build_periodic_action_plan_html(self, rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return "<p class='empty'>No periodic action rows found.</p>"
        out = [
            "<table>",
            "<thead><tr>"
            "<th style='width: 12%;'>Week(s) of Month</th>"
            "<th style='width: 11%;'>Day of Week</th>"
            "<th style='width: 12%;'>Resource</th>"
            "<th style='width: 12%;'>Action</th>"
            "<th style='width: 12%;'>Band/Freq</th>"
            "<th style='width: 17%;'>Contact</th>"
            "<th style='width: 24%;'>Description</th>"
            "</tr></thead><tbody>",
        ]
        for row in rows:
            out.append(
                "<tr>"
                f"<td>{html.escape(str(row.get('weeks_of_month') or ''))}</td>"
                f"<td>{html.escape(str(row.get('day_of_week') or ''))}</td>"
                f"<td>{html.escape(str(row.get('resource') or ''))}</td>"
                f"<td>{html.escape(str(row.get('action_label') or ''))}</td>"
                f"<td>{html.escape(str(row.get('band_freq') or '--'))}</td>"
                f"<td>{html.escape(str(row.get('contact_display') or '--'))}</td>"
                f"<td>{html.escape(str(row.get('description') or ''))}</td>"
                "</tr>"
            )
        out.append("</tbody></table>")
        return "".join(out)

    def _build_hf_operators_html(self, rows: List[Dict[str, str]]) -> str:
        if not rows:
            return "<p class='empty'>No HF operators match the selected filter.</p>"
        out = [
            "<table>",
            "<thead><tr>"
            "<th style='width: 12%;'>Callsign</th>"
            "<th style='width: 20%;'>Name</th>"
            "<th style='width: 8%;'>State</th>"
            "<th style='width: 10%;'>SitRep</th>"
            "<th style='width: 50%;'>Notes</th>"
            "</tr></thead><tbody>",
        ]
        for row in rows:
            out.append(
                "<tr>"
                f"<td>{html.escape(str(row.get('callsign') or ''))}</td>"
                f"<td>{html.escape(str(row.get('name') or ''))}</td>"
                f"<td>{html.escape(str(row.get('state') or ''))}</td>"
                f"<td>{html.escape(str(row.get('sitrep') or 'UNKNOWN'))}</td>"
                f"<td>{html.escape(str(row.get('notes') or ''))}</td>"
                "</tr>"
            )
        out.append("</tbody></table>")
        return "".join(out)

    def _build_local_operators_html(self, rows: List[Dict[str, str]]) -> str:
        if not rows:
            return "<p class='empty'>No local operators match the selected filter.</p>"
        out = [
            "<table>",
            "<thead><tr>"
            "<th style='width: 11%;'>Callsign</th>"
            "<th style='width: 10%;'>First</th>"
            "<th style='width: 10%;'>Last</th>"
            "<th style='width: 11%;'>City</th>"
            "<th style='width: 7%;'>State</th>"
            "<th style='width: 11%;'>Category</th>"
            "<th style='width: 8%;'>SitRep</th>"
            "<th style='width: 32%;'>Notes</th>"
            "</tr></thead><tbody>",
        ]
        for row in rows:
            out.append(
                "<tr>"
                f"<td>{html.escape(str(row.get('callsign') or ''))}</td>"
                f"<td>{html.escape(str(row.get('first_name') or ''))}</td>"
                f"<td>{html.escape(str(row.get('last_name') or ''))}</td>"
                f"<td>{html.escape(str(row.get('city') or ''))}</td>"
                f"<td>{html.escape(str(row.get('state') or ''))}</td>"
                f"<td>{html.escape(str(row.get('category') or ''))}</td>"
                f"<td>{html.escape(str(row.get('sitrep') or 'UNKNOWN'))}</td>"
                f"<td>{html.escape(str(row.get('notes') or ''))}</td>"
                "</tr>"
            )
        out.append("</tbody></table>")
        return "".join(out)

    def _format_pdf_condition_levels(self, value: Any) -> str:
        try:
            normalized = self.manager._normalize_condition_levels(value)
        except Exception:
            normalized = str(value or "").strip().upper() or "ALL"
        return normalized or "ALL"

    def _format_pdf_contact_rule(self, action: Dict[str, Any]) -> str:
        rule = str(action.get("contact_rule") or "none").strip().lower()
        target = str(action.get("contact_target") or "").strip()
        labels = {
            "none": "None",
            "hub_or_hub_alt": "HUB / HUB-ALT",
            "ncs_or_ancs": "NCS / ANCS",
            "callsign": "Callsign",
            "peer": "Peer",
            "local_group": "Local Group",
            "local_profile": "Local Profile",
        }
        label = labels.get(rule, rule.upper() if rule else "None")
        if target:
            return f"{label}: {target}"
        return label

    def _build_pdf_text_block_html(self, text: str, *, placeholders: Dict[str, str]) -> str:
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return ""
        rendered = raw
        for token, value in placeholders.items():
            rendered = rendered.replace(token, str(value or ""))
        paragraphs: List[str] = []
        current: List[str] = []
        for line in rendered.split("\n"):
            if line.strip():
                current.append(html.escape(line))
                continue
            if current:
                paragraphs.append(f"<p>{'<br>'.join(current)}</p>")
                current = []
        if current:
            paragraphs.append(f"<p>{'<br>'.join(current)}</p>")
        return "".join(paragraphs)

    def _build_pdf_profile_summary_html(self, profiles: List[Dict[str, Any]]) -> str:
        if not profiles:
            return "<p class='empty'>No SOP profiles were selected for export.</p>"
        out = [
            "<table>",
            "<thead><tr>"
            "<th style='width: 19%;'>Profile</th>"
            "<th style='width: 10%;'>Category</th>"
            "<th style='width: 15%;'>Operating Group</th>"
            "<th style='width: 15%;'>Secondary Group</th>"
            "<th style='width: 9%;'>Priority</th>"
            "<th style='width: 9%;'>Window</th>"
            "<th style='width: 9%;'>Status</th>"
            "<th style='width: 14%;'>Start (UTC)</th>"
            "</tr></thead><tbody>",
        ]
        for section in profiles:
            profile = section.get("profile") or {}
            window_hours = str(profile.get("window_hours") or "").strip()
            window_label = f"{window_hours} hr" if window_hours else "--"
            out.append(
                "<tr>"
                f"<td>{html.escape(str(profile.get('name') or ''))}</td>"
                f"<td>{html.escape(str(profile.get('category') or 'HF'))}</td>"
                f"<td>{html.escape(str(profile.get('operating_group') or '--'))}</td>"
                f"<td>{html.escape(str(profile.get('secondary_group') or '--'))}</td>"
                f"<td>{html.escape(str(profile.get('priority') or ''))}</td>"
                f"<td>{html.escape(window_label)}</td>"
                f"<td>{'Active' if bool(profile.get('active')) else 'Inactive'}</td>"
                f"<td>{html.escape(str(profile.get('sop_start_utc') or '--'))}</td>"
                "</tr>"
            )
        out.append("</tbody></table>")
        return "".join(out)

    def _collect_pdf_operating_group_scope(
        self,
        profiles: List[Dict[str, Any]],
    ) -> Tuple[Set[str], Set[Tuple[str, str]], Set[Tuple[str, str, str]]]:
        group_refs: Set[str] = set()
        group_band_refs: Set[Tuple[str, str]] = set()
        group_band_freq_refs: Set[Tuple[str, str, str]] = set()

        def _track(group_value: Any, band_value: Any = "", freq_value: Any = "") -> None:
            group_name = str(group_value or "").strip().upper()
            if not group_name:
                return
            group_refs.add(group_name)
            band_name = str(band_value or "").strip().upper()
            freq_text = str(freq_value or "").strip()
            if band_name and freq_text:
                group_band_freq_refs.add((group_name, band_name, freq_text))
                return
            if band_name:
                group_band_refs.add((group_name, band_name))

        for section in profiles:
            profile = section.get("profile") or {}
            _track(profile.get("operating_group"))
            _track(profile.get("secondary_group"))
            for row in list(profile.get("schedule_layer") or []):
                if not isinstance(row, dict):
                    continue
                _track(row.get("group_name"), row.get("band"), row.get("frequency"))
            for row in list(profile.get("actions") or []):
                if not isinstance(row, dict):
                    continue
                _track(row.get("group_name"), row.get("band"), row.get("frequency"))
        return group_refs, group_band_refs, group_band_freq_refs

    def _filter_pdf_operating_groups(
        self,
        rows: List[Dict[str, Any]],
        *,
        profiles: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        valid_rows = [row for row in rows if isinstance(row, dict)]
        if not valid_rows:
            return []
        group_refs, group_band_refs, group_band_freq_refs = self._collect_pdf_operating_group_scope(profiles)
        if not group_refs:
            return []
        out: List[Dict[str, Any]] = []
        for row in valid_rows:
            group_name = str(row.get("group") or "").strip().upper()
            if not group_name or group_name not in group_refs:
                continue
            band_name = str(row.get("band") or "").strip().upper()
            freq_text = str(row.get("frequency") or "").strip()
            if (group_name, band_name, freq_text) in group_band_freq_refs:
                out.append(row)
                continue
            if (group_name, band_name) in group_band_refs:
                out.append(row)
                continue
            has_specific_refs = any(ref_group == group_name for ref_group, _ in group_band_refs) or any(
                ref_group == group_name for ref_group, _, _ in group_band_freq_refs
            )
            if not has_specific_refs:
                out.append(row)
        return out

    def _build_pdf_operating_groups_html(self, rows: List[Dict[str, Any]]) -> str:
        valid_rows = [row for row in rows if isinstance(row, dict)]
        if not valid_rows:
            return "<p class='empty'>No referenced operating-group rows were found for the exported SOP scope.</p>"
        sorted_rows = sorted(
            valid_rows,
            key=lambda row: (
                str(row.get("group") or "").upper(),
                str(row.get("band") or "").upper(),
                str(row.get("frequency") or ""),
            ),
        )
        out = [
            "<table>",
            "<thead><tr>"
            "<th style='width: 16%;'>Group</th>"
            "<th style='width: 9%;'>Band</th>"
            "<th style='width: 12%;'>Frequency</th>"
            "<th style='width: 10%;'>Mode</th>"
            "<th style='width: 8%;'>VFO</th>"
            "<th style='width: 16%;'>FLDigi Mode</th>"
            "<th style='width: 12%;'>Start Offset</th>"
            "<th style='width: 8%;'>Auto-Tune</th>"
            "<th style='width: 9%;'>Levels</th>"
            "</tr></thead><tbody>",
        ]
        for row in sorted_rows:
            levels = "On" if bool(row.get("use_condition_levels", False)) else "Off"
            out.append(
                "<tr>"
                f"<td>{html.escape(str(row.get('group') or ''))}</td>"
                f"<td>{html.escape(str(row.get('band') or ''))}</td>"
                f"<td>{html.escape(str(row.get('frequency') or ''))}</td>"
                f"<td>{html.escape(str(row.get('mode') or ''))}</td>"
                f"<td>{html.escape(str(row.get('vfo') or 'A'))}</td>"
                f"<td>{html.escape(str(row.get('fldigi_mode') or '--'))}</td>"
                f"<td>{html.escape(str(row.get('fldigi_offset') or '--'))}</td>"
                f"<td>{'Yes' if bool(row.get('auto_tune', False)) else 'No'}</td>"
                f"<td>{levels}</td>"
                "</tr>"
            )
        out.append("</tbody></table>")
        return "".join(out)

    def _build_pdf_schedule_layer_html(self, profiles: List[Dict[str, Any]]) -> str:
        rows: List[Tuple[str, Dict[str, Any]]] = []
        for section in profiles:
            profile = section.get("profile") or {}
            profile_name = str(profile.get("name") or "").strip()
            for row in list(profile.get("schedule_layer") or []):
                if isinstance(row, dict):
                    rows.append((profile_name, row))
        if not rows:
            return "<p class='empty'>No SOP schedule rows found.</p>"
        rows.sort(
            key=lambda item: (
                item[0].upper(),
                int(item[1].get("sort_order") or 0),
                int(item[1].get("id") or 0),
            )
        )
        out = [
            "<table>",
            "<thead><tr>"
            "<th style='width: 16%;'>Profile</th>"
            "<th style='width: 10%;'>Day (UTC)</th>"
            "<th style='width: 9%;'>Repeat</th>"
            "<th style='width: 9%;'>Weeks</th>"
            "<th style='width: 10%;'>Levels</th>"
            "<th style='width: 14%;'>Group</th>"
            "<th style='width: 8%;'>Band</th>"
            "<th style='width: 11%;'>Frequency</th>"
            "<th style='width: 7%;'>Mode</th>"
            "<th style='width: 6%;'>VFO</th>"
            "<th style='width: 10%;'>Start-End</th>"
            "</tr></thead><tbody>",
        ]
        for profile_name, row in rows:
            weeks = ",".join(str(v) for v in list(row.get("month_weeks") or [])) or "--"
            start_end = f"{str(row.get('start_utc') or '--')}-{str(row.get('end_utc') or '--')}"
            out.append(
                "<tr>"
                f"<td>{html.escape(profile_name)}</td>"
                f"<td>{html.escape(str(row.get('day_utc') or 'ALL'))}</td>"
                f"<td>{html.escape(str(row.get('recurrence') or 'weekly'))}</td>"
                f"<td>{html.escape(weeks)}</td>"
                f"<td>{html.escape(self._format_pdf_condition_levels(row.get('condition_levels')))}</td>"
                f"<td>{html.escape(str(row.get('group_name') or '--'))}</td>"
                f"<td>{html.escape(str(row.get('band') or ''))}</td>"
                f"<td>{html.escape(str(row.get('frequency') or ''))}</td>"
                f"<td>{html.escape(str(row.get('mode') or '--'))}</td>"
                f"<td>{html.escape(str(row.get('vfo') or '--'))}</td>"
                f"<td>{html.escape(start_end)}</td>"
                "</tr>"
            )
        out.append("</tbody></table>")
        return "".join(out)

    def _build_pdf_actions_html(self, profiles: List[Dict[str, Any]]) -> str:
        rows: List[Tuple[str, Dict[str, Any]]] = []
        for section in profiles:
            profile = section.get("profile") or {}
            profile_name = str(profile.get("name") or "").strip()
            for row in list(profile.get("actions") or []):
                if isinstance(row, dict):
                    rows.append((profile_name, row))
        if not rows:
            return "<p class='empty'>No SOP action rows found.</p>"
        rows.sort(
            key=lambda item: (
                item[0].upper(),
                str(item[1].get("software") or "").upper(),
                int(item[1].get("sort_order") or 0),
                int(item[1].get("id") or 0),
            )
        )
        out = [
            "<table>",
            "<thead><tr>"
            "<th style='width: 12%;'>Profile</th>"
            "<th style='width: 11%;'>Group</th>"
            "<th style='width: 8%;'>Levels</th>"
            "<th style='width: 11%;'>Software</th>"
            "<th style='width: 8%;'>Mode</th>"
            "<th style='width: 12%;'>Action</th>"
            "<th style='width: 14%;'>Contact</th>"
            "<th style='width: 11%;'>Band/Freq</th>"
            "<th style='width: 8%;'>Time (UTC)</th>"
            "<th style='width: 5%;'>Int</th>"
            "</tr></thead><tbody>",
        ]
        for profile_name, row in rows:
            band = str(row.get("band") or "").strip()
            freq = str(row.get("frequency") or "").strip()
            band_freq = band if not freq else f"{band} {freq}".strip()
            interval_hours = int(row.get("interval_hours") or 0)
            interval_minutes = int(row.get("interval_minutes") or 0)
            interval_label = f"{interval_hours}h" if interval_hours > 0 else (f"{interval_minutes}m" if interval_minutes > 0 else "--")
            time_label = f"{str(row.get('daily_start_utc') or '--')}-{str(row.get('daily_end_utc') or '--')}"
            out.append(
                "<tr>"
                f"<td>{html.escape(profile_name)}</td>"
                f"<td>{html.escape(str(row.get('group_name') or '--'))}</td>"
                f"<td>{html.escape(self._format_pdf_condition_levels(row.get('condition_levels')))}</td>"
                f"<td>{html.escape(str(row.get('software') or ''))}</td>"
                f"<td>{html.escape(str(row.get('mode') or '--'))}</td>"
                f"<td>{html.escape(str(row.get('action_label') or ''))}</td>"
                f"<td>{html.escape(self._format_pdf_contact_rule(row))}</td>"
                f"<td>{html.escape(band_freq or '--')}</td>"
                f"<td>{html.escape(time_label)}</td>"
                f"<td>{html.escape(interval_label)}</td>"
                "</tr>"
            )
            description = str(row.get("description") or "").strip()
            if description:
                out.append(
                    "<tr>"
                    "<td></td><td colspan='9'>"
                    f"<b>Notes:</b> {html.escape(description)}"
                    "</td></tr>"
                )
        out.append("</tbody></table>")
        return "".join(out)

    def _planner_export_sop_rows(self, planner: FreqPlannerTab, profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cond_map = planner._condition_level_map()
        for section in profiles:
            profile = section.get("profile") or {}
            profile_id = int(profile.get("id") or 0)
            default_group = (
                str(profile.get("operating_group") or "").strip()
                or str(profile.get("secondary_group") or "").strip()
                or str(profile.get("name") or "").strip()
            )
            profile_name = str(profile.get("name") or "").strip()
            for row in list(profile.get("schedule_layer") or []):
                if not isinstance(row, dict):
                    continue
                if not bool(row.get("enabled", True)):
                    continue
                band = str(row.get("band") or "").strip()
                frequency = str(row.get("frequency") or "").strip()
                if not band and not frequency:
                    continue
                group_name = str(row.get("group_name") or "").strip() or default_group
                normalized_group = group_name.upper()
                group_level = cond_map.get(normalized_group)
                if not planner._condition_level_match(str(row.get("condition_levels") or "ALL"), group_level):
                    continue
                out.append(
                    {
                        "id": int(row.get("id") or 0),
                        "sop_layer_id": int(row.get("id") or 0),
                        "sop_profile_id": profile_id,
                        "day_utc": str(row.get("day_utc") or "ALL"),
                        "recurrence": str(row.get("recurrence") or "Weekly"),
                        "biweekly_offset_weeks": int(row.get("biweekly_offset_weeks") or 0),
                        "month_weeks": str(row.get("month_weeks") or ""),
                        "condition_levels": planner._normalize_condition_levels(row.get("condition_levels")),
                        "band": band,
                        "mode": str(row.get("mode") or ""),
                        "vfo": str(row.get("vfo") or "A").strip().upper() or "A",
                        "frequency": frequency,
                        "start_utc": str(row.get("start_utc") or ""),
                        "end_utc": str(row.get("end_utc") or ""),
                        "group_name": normalized_group,
                        "profile_name": profile_name,
                    }
                )
        return out

    def _build_pdf_freq_planner_html(self, profiles: List[Dict[str, Any]]) -> str:
        planner: FreqPlannerTab | None = None
        try:
            planner = FreqPlannerTab(self)
            try:
                planner.set_tab_active(False)
            except Exception:
                pass
            if getattr(planner, "_clock_timer", None) is not None:
                try:
                    planner._clock_timer.stop()
                except Exception:
                    pass
            hf_sched, net_sched, _existing_sop_sched, policy_rows = planner._load_schedules()
            scoped_sop = self._planner_export_sop_rows(planner, profiles)
            planner._load_schedules = lambda: (hf_sched, net_sched, scoped_sop, policy_rows)  # type: ignore[method-assign]
            planner._show_local = False
            planner._show_band = False
            planner._last_snapshot = ""
            planner.rebuild_table()

            header_cells: List[str] = []
            for col in range(planner.table.columnCount()):
                item = planner.table.horizontalHeaderItem(col)
                header_cells.append(html.escape(item.text() if item else ""))
            out = [
                "<table class='planner-grid'>",
                "<thead><tr>",
            ]
            for label in header_cells:
                out.append(f"<th>{label}</th>")
            out.append("</tr></thead><tbody>")
            for row in range(planner.table.rowCount()):
                out.append("<tr>")
                for col in range(planner.table.columnCount()):
                    item = planner.table.item(row, col)
                    cell_text = item.text() if item else ""
                    out.append(f"<td>{html.escape(cell_text)}</td>")
                out.append("</tr>")
            out.append("</tbody></table>")
            return "".join(out)
        except Exception as e:
            log.debug("SOP: FreqPlanner export snapshot failed: %s", e)
            return "<p class='empty'>FreqPlanner snapshot unavailable for this export.</p>"
        finally:
            if planner is not None:
                try:
                    if getattr(planner, "_clock_timer", None) is not None:
                        planner._clock_timer.stop()
                except Exception:
                    pass
                try:
                    planner.deleteLater()
                except Exception:
                    pass

    def _build_pdf_html(
        self,
        *,
        profiles: List[Dict[str, Any]],
        options: Dict[str, Any],
        now_utc: datetime.datetime,
    ) -> str:
        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        as_of_local = now_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        as_of_utc = now_utc.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
        scope_label = "Selected SOP" if options.get("scope") == "selected" else "All Active SOPs"
        time_label = "UTC" if str(options.get("time_mode") or "Local") == "UTC" else f"Local ({tz_name})"
        placeholders = {
            "{{operator_callsign}}": self._operator_callsign(),
            "{{as_of_local}}": as_of_local,
            "{{as_of_utc}}": as_of_utc,
            "{{scope}}": scope_label,
            "{{timezone}}": tz_name,
        }
        parts = [
            "<html><head><meta charset='utf-8'>",
            "<style>"
            "@page { size: Letter; margin: 0.55in; }"
            # uia-0: ignore[raw-font-size] offline print/export HTML rendering surface, not an app screen
            "body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 10.5pt; color: #111; }"  # uia-0: ignore[raw-literal-color] offline print/export HTML uses a stable white-paper palette
            # uia-0: ignore[raw-font-size] offline print/export HTML rendering surface, not an app screen
            "h1 { font-size: 18pt; margin: 0 0 6pt 0; }"
            # uia-0: ignore[raw-font-size] offline print/export HTML rendering surface, not an app screen
            "h2 { font-size: 13pt; margin: 16pt 0 6pt 0; }"
            # uia-0: ignore[raw-font-size] offline print/export HTML rendering surface, not an app screen
            ".meta { font-size: 9.5pt; color: #333; margin: 0 0 3pt 0; }"  # uia-0: ignore[raw-literal-color] offline print/export HTML uses a stable white-paper palette
            ".section { margin-top: 8pt; }"
            ".page-break { page-break-before: always; }"
            "p { margin: 5pt 0 8pt 0; }"
            "table { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 6pt 0 14pt 0; }"
            "th, td { border: 1px solid #6f7682; padding: 5px 6px; vertical-align: top; word-wrap: break-word; }"
            "th { background: #edf1f5; font-weight: 700; }"  # uia-0: ignore[raw-literal-color] offline print/export HTML uses a stable white-paper palette
            # uia-0: ignore[raw-font-size] offline print/export HTML rendering surface, not an app screen
            ".planner-grid { font-size: 7.4pt; }"
            ".planner-grid th, .planner-grid td { padding: 2px 3px; }"
            ".empty { font-style: italic; color: #444; margin: 6pt 0 10pt 0; }"  # uia-0: ignore[raw-literal-color] offline print/export HTML uses a stable white-paper palette
            "</style></head><body>",
            "<h1>SOP Export</h1>",
            f"<div class='meta'><b>As Of:</b> {html.escape(as_of_local)} Local</div>",
            f"<div class='meta'><b>Scope:</b> {html.escape(scope_label)}</div>",
            f"<div class='meta'><b>SOP Time Basis:</b> {html.escape(time_label)}</div>",
        ]
        callsign = self._operator_callsign()
        if callsign:
            parts.append(f"<div class='meta'><b>Operator:</b> {html.escape(callsign)}</div>")
        preamble_html = self._build_pdf_text_block_html(
            str(self.settings.get("sop_export_preamble", "") or ""),
            placeholders=placeholders,
        )
        if preamble_html:
            parts.append("<div class='section'>")
            parts.append("<h2>Preamble</h2>")
            parts.append(preamble_html)
            parts.append("</div>")

        operating_groups = self.settings.get("operating_groups", [])
        if not isinstance(operating_groups, list):
            operating_groups = []
        operating_groups = self._filter_pdf_operating_groups(operating_groups, profiles=profiles)

        parts.append("<div class='section'>")
        parts.append("<h2>SOP Profiles</h2>")
        parts.append(self._build_pdf_profile_summary_html(profiles))
        parts.append("</div>")

        parts.append("<div class='page-break'>")
        parts.append("<h2>SOP Schedule Layer</h2>")
        parts.append(
            "<div class='meta'><b>Columns:</b> Profile, Day (UTC), Repeat, Weeks, Condition Levels, Group, Band, Frequency, Mode, VFO, Start-End (UTC)</div>"
        )
        parts.append(self._build_pdf_schedule_layer_html(profiles))
        parts.append("</div>")

        parts.append("<div class='page-break'>")
        parts.append("<h2>SOP Actions</h2>")
        parts.append(
            "<div class='meta'><b>Columns:</b> Profile, Group, Condition Levels, Software, Mode, Action, Contact, Band/Freq, Time (UTC), Interval</div>"
        )
        parts.append(self._build_pdf_actions_html(profiles))
        parts.append("</div>")

        parts.append("<div class='page-break'>")
        parts.append("<h2>Referenced Operating Groups</h2>")
        parts.append("<div class='meta'><b>Scope:</b> Only rows referenced by the exported SOP profile(s).</div>")
        parts.append(
            "<div class='meta'><b>Columns:</b> Group, Band, Frequency, Mode, VFO, FLDigi Mode, Start Offset, Auto-Tune, Condition Levels</div>"
        )
        parts.append(self._build_pdf_operating_groups_html(operating_groups))
        parts.append("</div>")

        parts.append("<div class='page-break'>")
        parts.append("<h2>FreqPlanner Snapshot</h2>")
        parts.append(
            "<div class='meta'><b>View:</b> UTC primary with Local conversion column; current HF and Net schedules plus the exported SOP profile(s).</div>"
        )
        parts.append(self._build_pdf_freq_planner_html(profiles))
        parts.append("</div>")

        time_mode = str(options.get("time_mode") or "Local")
        day_start_utc, day_end_utc, day_label = self._daily_export_window_utc(time_mode=time_mode, now_utc=now_utc)
        blended_daily_rows: List[Dict[str, Any]] = []
        periodic_rows: List[Dict[str, Any]] = []
        for section in profiles:
            profile = section.get("profile") or {}
            profile_id = int(profile.get("id") or 0)
            if profile_id <= 0:
                continue
            blended_daily_rows.extend(
                self.manager.build_profile_daily_plan_rows(
                    profile_id,
                    day_start_utc=day_start_utc,
                    day_end_utc=day_end_utc,
                )
            )
            periodic_rows.extend(self.manager.build_profile_periodic_action_rows(profile_id))

        daily_seen: Set[Tuple[str, str, str, str, str, str]] = set()
        daily_unique: List[Dict[str, Any]] = []
        for row in blended_daily_rows:
            due = row.get("due_utc")
            due_key = (
                due.astimezone(datetime.timezone.utc).replace(second=0, microsecond=0).isoformat()
                if isinstance(due, datetime.datetime)
                else ""
            )
            key = (
                due_key,
                str(row.get("resource") or ""),
                str(row.get("action_label") or ""),
                str(row.get("band_freq") or ""),
                str(row.get("contact_display") or ""),
                str(row.get("description") or ""),
            )
            if key in daily_seen:
                continue
            daily_seen.add(key)
            daily_unique.append(row)
        daily_unique.sort(
            key=lambda x: (
                x.get("due_utc")
                if isinstance(x.get("due_utc"), datetime.datetime)
                else datetime.datetime.max.replace(tzinfo=datetime.timezone.utc),
                str(x.get("resource") or ""),
                str(x.get("action_label") or ""),
            )
        )

        periodic_seen: Set[Tuple[str, str, str, str, str, str, str]] = set()
        periodic_unique: List[Dict[str, Any]] = []
        for row in periodic_rows:
            key = (
                str(row.get("weeks_of_month") or ""),
                str(row.get("day_of_week") or ""),
                str(row.get("resource") or ""),
                str(row.get("action_label") or ""),
                str(row.get("band_freq") or ""),
                str(row.get("contact_display") or ""),
                str(row.get("description") or ""),
            )
            if key in periodic_seen:
                continue
            periodic_seen.add(key)
            periodic_unique.append(row)
        periodic_unique.sort(
            key=lambda x: (
                str(x.get("weeks_of_month") or ""),
                str(x.get("day_of_week") or ""),
                str(x.get("resource") or ""),
                str(x.get("action_label") or ""),
            )
        )

        parts.append("<div class='section'>")
        parts.append("<h2>Derived Daily Action Plan</h2>")
        parts.append(f"<div class='meta'><b>Day:</b> {html.escape(day_label)}</div>")
        parts.append(
            "<div class='meta'><b>Columns:</b> Time, Resource, Action, Band/Freq, Contact, Description</div>"
        )
        parts.append(self._build_daily_action_plan_html(daily_unique, time_mode=time_mode))
        parts.append("</div>")

        if periodic_unique:
            parts.append("<div class='page-break'>")
            parts.append("<h2>Derived Periodic Actions</h2>")
            parts.append("<div class='meta'><b>Columns:</b> Week(s) of Month, Day of Week, Resource, Action, Band/Freq, Contact, Description</div>")
            parts.append(self._build_periodic_action_plan_html(periodic_unique))
            parts.append("</div>")

        if options.get("include_roster"):
            filter_mode = str(options.get("filter_mode") or "all")
            state_filter = str(options.get("state_filter") or "").strip().upper() if filter_mode == "state" else ""
            region_filter = str(options.get("region_filter") or "").strip().upper() if filter_mode == "region" else ""
            filter_desc = "All"
            if state_filter:
                filter_desc = f"State: {state_filter}"
            elif region_filter:
                filter_desc = f"FEMA Region: {region_filter}"

            if options.get("include_hf"):
                selected_groups = [str(v).strip() for v in list(options.get("hf_groups") or []) if str(v).strip()]
                selected_trusted = [str(v).strip() for v in list(options.get("hf_trusted") or []) if str(v).strip()]
                hf_rows = self.manager.list_hf_operators_for_export(
                    state_filter=state_filter,
                    region_filter=region_filter,
                    group_filters=selected_groups,
                    trusted_filters=selected_trusted,
                )
                parts.append("<div class='page-break'>")
                parts.append("<h2>HF Operators</h2>")
                parts.append(f"<div class='meta'><b>Filter:</b> {html.escape(filter_desc)}</div>")
                if selected_groups:
                    parts.append(f"<div class='meta'><b>Groups:</b> {html.escape(', '.join(selected_groups))}</div>")
                if selected_trusted:
                    parts.append(f"<div class='meta'><b>Trusted:</b> {html.escape(', '.join(selected_trusted))}</div>")
                parts.append(self._build_hf_operators_html(hf_rows))
                parts.append("</div>")

            if options.get("include_local"):
                selected_categories = [
                    str(v).strip()
                    for v in list(options.get("local_categories") or [])
                    if str(v).strip()
                ]
                local_rows = self.manager.list_local_operators_for_export(
                    state_filter=state_filter,
                    region_filter=region_filter,
                    category_filters=selected_categories,
                )
                parts.append("<div class='page-break'>")
                parts.append("<h2>Local Operators</h2>")
                parts.append(f"<div class='meta'><b>Filter:</b> {html.escape(filter_desc)}</div>")
                if selected_categories:
                    parts.append(
                        f"<div class='meta'><b>Category:</b> {html.escape(', '.join(selected_categories))}</div>"
                    )
                parts.append(self._build_local_operators_html(local_rows))
                parts.append("</div>")

        postamble_html = self._build_pdf_text_block_html(
            str(self.settings.get("sop_export_postamble", "") or ""),
            placeholders=placeholders,
        )
        if postamble_html:
            parts.append("<div class='page-break'>")
            parts.append("<h2>Postamble</h2>")
            parts.append(postamble_html)
            parts.append("</div>")

        parts.append("</body></html>")
        return "".join(parts)

    def _export_pdf(self) -> None:
        try:
            options = self._prompt_pdf_export_options()
            if not options:
                return
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            profiles = self._collect_pdf_profiles(scope=str(options.get("scope") or "selected"), now_utc=now_utc)
            if not profiles:
                QMessageBox.information(self, "Export SOP to PDF", "No SOP data available for the selected scope.")
                return
            default_name = self._export_pdf_default_name(scope=str(options.get("scope") or "selected"), now_utc=now_utc)
            out, _ = QFileDialog.getSaveFileName(self, "Export SOP to PDF", default_name, "PDF Files (*.pdf)")
            if not out:
                return

            doc = QTextDocument(self)
            doc.setDocumentMargin(18.0)
            doc.setHtml(self._build_pdf_html(profiles=profiles, options=options, now_utc=now_utc))

            pdf = QPdfWriter(out)
            pdf.setResolution(300)
            page_layout = QPageLayout()
            page_layout.setPageSize(QPageSize(QPageSize.Letter))
            page_layout.setOrientation(QPageLayout.Portrait)
            pdf.setPageLayout(page_layout)
            doc.print_(pdf)
            QMessageBox.information(self, "Export SOP to PDF", f"SOP PDF exported to:\n{out}")
        except Exception as e:
            QMessageBox.warning(self, "Export SOP to PDF", str(e))

    def _export_profile(self) -> None:
        if not self._selected_profile_id:
            QMessageBox.information(self, "Export SOP", "Save the SOP first before exporting.")
            return
        try:
            payload = self.manager.export_profile_json(int(self._selected_profile_id))
            out, _ = QFileDialog.getSaveFileName(
                self,
                "Export SOP",
                f"{payload['profile'].get('name', 'sop')}.json",
                "JSON Files (*.json)",
            )
            if not out:
                return
            Path(out).write_text(self.manager.dumps_json(payload), encoding="utf-8")
            QMessageBox.information(self, "Export SOP", f"Exported SOP to:\n{out}")
        except Exception as e:
            QMessageBox.warning(self, "Export SOP", str(e))

    def _import_profile(self) -> None:
        try:
            src, _ = QFileDialog.getOpenFileName(self, "Import SOP", "", "JSON Files (*.json)")
            if not src:
                return
            payload = json.loads(Path(src).read_text(encoding="utf-8"))
            profile_id = self.manager.import_profile_json(payload)
            self._reload_profiles(select_id=profile_id)
            self.refresh_upcoming()
            self._emit_sop_data_changed()
            QMessageBox.information(self, "Import SOP", "SOP imported.")
        except Exception as e:
            QMessageBox.warning(self, "Import SOP", str(e))

    def _refresh_contact_label(self) -> None:
        group = self.group_combo.currentText().strip().upper()
        subgroup = self.secondary_combo.currentText().strip().upper()
        contacts = self.manager.resolve_primary_contacts(group, subgroup)
        hub = contacts.get("hub", [])
        ncs = contacts.get("ncs", [])
        parts = []
        if hub:
            parts.append(f"HUB/HUB-ALT: {', '.join(hub[:6])}")
        if ncs:
            parts.append(f"NCS/ANCS: {', '.join(ncs[:6])}")
        if parts:
            self.contact_label.setText(f"Primary Contacts ({group or 'N/A'}): " + " | ".join(parts))
        else:
            self.contact_label.setText(f"Primary Contacts ({group or 'N/A'}): None")

    def _format_due(self, due_utc, tz_mode: str) -> str:
        if tz_mode == "Local":
            tz_name = self.settings.get("timezone", "UTC")
            tz = get_timezone(tz_name)
            return due_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        return due_utc.strftime("%Y-%m-%d %H:%M")

    def _update_clock_labels(self) -> None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        utc_day = now_utc.strftime("%a").upper()
        self.utc_label.setText(now_utc.strftime(f"<b>UTC ({utc_day}):</b> %y%m%d %H:%M:%S Z"))
        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        try:
            local_dt = now_utc.astimezone(get_timezone(tz_name))
            local_day = local_dt.strftime("%a").upper()
            self.local_label.setText(local_dt.strftime(f"<b>{tz_name} ({local_day}):</b> %y%m%d %H:%M:%S"))
        except Exception:
            self.local_label.setText("<b>Local:</b> --")
        self.time_toggle_btn.setText("Times: Local" if self._show_local else "Times: UTC")
        self._update_time_toggle_style()
        tz_short = self._tz_short_name() if self._show_local else "UTC"
        self.start_label.setText(f"SOP Daily Start ({tz_short}):")
        self._update_layer_time_headers(tz_short)
        self._refresh_runtime_layer_hint()

    def _update_layer_time_headers(self, tz_short: str) -> None:
        if not hasattr(self, "layer_table"):
            return
        key = str(tz_short or "UTC").strip().upper()
        if key == self._layer_time_header_tag:
            return
        self._layer_time_header_tag = key
        self.layer_table.setHorizontalHeaderItem(self.LAYER_COL_START, QTableWidgetItem(f"Start ({tz_short})"))
        self.layer_table.setHorizontalHeaderItem(self.LAYER_COL_END, QTableWidgetItem(f"End ({tz_short})"))

    def _toggle_time_view(self) -> None:
        prev_show_local = self._show_local
        prior_text = self.start_edit.text().strip()
        prior_utc = self._utc_start_hhmm_from_display(prior_text, show_local=prev_show_local)
        layer_prior: List[Tuple[QLineEdit, QLineEdit, str, str]] = []
        for r in range(self.layer_table.rowCount()):
            start_edit = self.layer_table.cellWidget(r, self.LAYER_COL_START)
            end_edit = self.layer_table.cellWidget(r, self.LAYER_COL_END)
            if not isinstance(start_edit, QLineEdit) or not isinstance(end_edit, QLineEdit):
                continue
            layer_prior.append(
                (
                    start_edit,
                    end_edit,
                    self._utc_layer_hhmm_from_display(start_edit.text().strip(), show_local=prev_show_local),
                    self._utc_layer_hhmm_from_display(end_edit.text().strip(), show_local=prev_show_local),
                )
            )
        self._show_local = not self._show_local
        self.start_edit.setText(self._display_start_hhmm_from_utc(prior_utc))
        for start_edit, end_edit, start_utc, end_utc in layer_prior:
            start_edit.setText(self._display_layer_hhmm_from_utc(start_utc))
            end_edit.setText(self._display_layer_hhmm_from_utc(end_utc))
        self._update_clock_labels()
        self._refresh_layer_validation_hints()
        self.refresh_upcoming()

    def _display_start_hhmm_from_utc(self, utc_hhmm: str, show_local: bool | None = None) -> str:
        text = (utc_hhmm or "00:00").strip()
        if len(text) != 5 or ":" not in text:
            return "00:00"
        use_local = self._show_local if show_local is None else bool(show_local)
        if not use_local:
            return text
        try:
            tz_name = self.settings.get("timezone", "UTC") or "UTC"
            tz = get_timezone(tz_name)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            h, m = [int(x) for x in text.split(":")]
            dt_utc = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
            return dt_utc.astimezone(tz).strftime("%H:%M")
        except Exception:
            return text

    def _display_layer_hhmm_from_utc(self, utc_hhmm: str, show_local: bool | None = None) -> str:
        return self._display_start_hhmm_from_utc(utc_hhmm, show_local=show_local)

    def _utc_start_hhmm_from_display(self, display_hhmm: str, show_local: bool | None = None) -> str:
        text = (display_hhmm or "00:00").strip()
        if len(text) != 5 or ":" not in text:
            return "00:00"
        use_local = self._show_local if show_local is None else bool(show_local)
        if not use_local:
            return text
        try:
            tz_name = self.settings.get("timezone", "UTC") or "UTC"
            tz = get_timezone(tz_name)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            now_local = now_utc.astimezone(tz)
            h, m = [int(x) for x in text.split(":")]
            dt_local = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
            return dt_local.astimezone(datetime.timezone.utc).strftime("%H:%M")
        except Exception:
            return text

    def _utc_layer_hhmm_from_display(self, display_hhmm: str, show_local: bool | None = None) -> str:
        return self._utc_start_hhmm_from_display(display_hhmm, show_local=show_local)

    def refresh_upcoming(self) -> None:
        try:
            horizon = int(self.horizon_spin.value())
            rows = self.manager.build_upcoming_actions(horizon_hours=horizon, only_active=True)
            configured = set(self._configured_softwares())
            if configured:
                rows = [r for r in rows if (r.get("software") or "").strip() in configured]
            else:
                rows = []
            self._upcoming_rows = rows
            self._populate_upcoming_table()
        except Exception as e:
            log.debug("SOP: refresh_upcoming failed: %s", e)
        self._refresh_runtime_layer_hint()

    def _refresh_runtime_layer_hint(self) -> None:
        theme = resolve_theme(self.settings)
        text = ""
        style = f"color: {theme.get('text_muted', '#888')};"
        visible = False
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

                if source in {"SOP", "NET", "HF"}:
                    visible = True
                    text = f"Runtime Source: {net_kind or source.title()}"
                    if source == "SOP":
                        style = f"color: {theme.get('warning', '#C99700')}; font-weight: 600;"
                    elif source == "NET":
                        style = f"color: {theme.get('info', '#1E88E5')}; font-weight: 600;"
                    else:
                        style = f"color: {theme.get('text', '#111')};"
                    extras: List[str] = []
                    if source_reason_detail:
                        extras.append(source_reason_detail)
                    if source == "SOP" and sop_contention:
                        contenders = [p for p in sop_profiles if p and p != sop_selected]
                        if contenders:
                            extras.append(f"Contention: {sop_selected or 'Winner'} over {', '.join(contenders[:3])}")
                        else:
                            extras.append("Contention detected")
                    if next_source_change and next_source and next_source != source:
                        next_label = next_net_kind or next_source
                        extras.append(f"Next Source: {next_label}")
                    if extras:
                        text = f"{text} | {' | '.join(extras)}"
        except Exception:
            visible = False
        self.layer_runtime_label.setVisible(visible)
        self.layer_runtime_label.setText(text)
        self.layer_runtime_label.setStyleSheet(style)

    def _populate_upcoming_table(self) -> None:
        tz_mode = "Local" if self._show_local else "UTC"
        theme = resolve_theme(self.settings)
        self.upcoming_table.setRowCount(0)
        misaligned = 0
        for row in self._upcoming_rows:
            r = self.upcoming_table.rowCount()
            self.upcoming_table.insertRow(r)
            self.upcoming_table.setItem(r, 0, QTableWidgetItem(row.get("profile_name", "")))

            band = (row.get("band") or "").strip()
            freq = (row.get("frequency") or "").strip()
            band_freq = f"{band} {freq}".strip() if (band or freq) else "--"
            band_item = QTableWidgetItem(band_freq)
            aligned = bool(row.get("aligned", True))
            if not aligned:
                warn_bg = theme.get("warning", "#C99700")
                band_item.setBackground(QColor(warn_bg))
                band_item.setForeground(QColor(_contrast_text_hex(warn_bg)))
                band_item.setToolTip("Scheduling Mismatch")
                misaligned += 1
            self.upcoming_table.setItem(r, 1, band_item)

            self.upcoming_table.setItem(r, 2, QTableWidgetItem(row.get("software", "")))
            self.upcoming_table.setItem(r, 3, QTableWidgetItem(row.get("action_label", "")))
            self.upcoming_table.setItem(r, 4, QTableWidgetItem(row.get("description", "")))
            self.upcoming_table.setItem(r, 5, QTableWidgetItem(self._format_due(row.get("next_due_utc"), tz_mode)))
            targets = row.get("contact_targets", []) or []
            contact_rule = str(row.get("contact_rule") or "").strip()
            if contact_rule in {"local_group", "local_profile"} and targets:
                contact_txt = self._local_profile_display(str(targets[0] or "").strip().upper())
            elif targets:
                contact_txt = " OR ".join(targets[:4])
            else:
                contact_txt = "--"
            self.upcoming_table.setItem(r, 6, QTableWidgetItem(contact_txt))

            btn = QPushButton("Complete")
            pid = int(row.get("profile_id"))
            aid = int(row.get("action_id"))
            status = (row.get("status") or "").strip()
            if status == "Due Now":
                btn.setText("Due Now")
                btn.setStyleSheet(button_style("info", theme))
            elif status == "Overdue":
                btn.setText(status)
                btn.setStyleSheet(button_style("warning", theme))
            elif status == "Completed":
                btn.setText("Completed")
                btn.setStyleSheet(button_style("success", theme))
            else:
                btn.setText(status or "Upcoming")
            btn.clicked.connect(lambda _=False, p=pid, a=aid: self._complete_action(p, a))
            self.upcoming_table.setCellWidget(r, 7, btn)

        if misaligned > 0:
            self.alignment_label.setText(
                f"Warning: {misaligned} upcoming SOP check-in reminder(s) do not align with Daily/Net/SOP Layer schedule windows."
            )
            self.alignment_label.setVisible(True)
        else:
            self.alignment_label.setVisible(False)

    def _complete_action(self, profile_id: int, action_id: int) -> None:
        try:
            self.manager.mark_action_complete(profile_id, action_id)
            self.refresh_upcoming()
            self._emit_sop_data_changed()
        except Exception as e:
            QMessageBox.warning(self, "SOP", f"Could not complete action: {e}")

    def _emit_sop_data_changed(self) -> None:
        self._sop_data_changed_emit_pending = True
        timer = getattr(self, "_sop_data_changed_emit_timer", None)
        if isinstance(timer, QTimer):
            timer.start()
            return
        self._flush_sop_data_changed_emit()

    def _flush_sop_data_changed_emit(self) -> None:
        if not bool(getattr(self, "_sop_data_changed_emit_pending", False)):
            return
        self._sop_data_changed_emit_pending = False
        try:
            self.sop_data_changed.emit()
        except Exception:
            pass

    def on_settings_saved(self) -> None:
        try:
            self.settings.reload()
        except Exception:
            pass
        self._invalidate_layer_sync_cache()
        current_group = self.group_combo.currentText()
        current_start_utc = self._utc_start_hhmm_from_display(self.start_edit.text().strip())
        self._refresh_reference_data()
        self.group_combo.setCurrentText(current_group)
        self.start_edit.setText(self._display_start_hhmm_from_utc(current_start_utc))
        self._update_clock_labels()
        self._refresh_contact_label()
        self._on_profile_selected(self.profile_combo.currentIndex())
        self.refresh_upcoming()
        self._schedule_layer_sync_refresh()

    def on_local_net_profiles_updated(self) -> None:
        try:
            self.settings.reload()
        except Exception:
            pass
        try:
            self._load_local_net_profiles_from_data(self.settings.all())
            self._refresh_all_contact_target_options()
            self._schedule_layer_sync_refresh()
        except Exception as e:
            log.debug("SOP: local net profiles refresh failed: %s", e)

    def on_tab_activated(self) -> None:
        self._invalidate_layer_sync_cache()
        self._schedule_layer_sync_refresh()
        self.refresh_upcoming()

    def set_tab_active(self, active: bool) -> None:
        self._active = bool(active)
        if active:
            if not self._timer.isActive():
                self._timer.start()
            if not self._clock_timer.isActive():
                self._clock_timer.start()
            if self._defer_initial_load and not self._initial_data_loaded:
                if not self._initial_data_load_pending:
                    self._initial_data_load_pending = True
                    # The workspace is already selected.  Yield once before
                    # its first data projection so the operator sees a stable
                    # tab transition rather than a frozen shell.
                    QTimer.singleShot(75, self._ensure_initial_data_projection)
                return
            self.on_tab_activated()
            return
        for timer_name in (
            "_timer",
            "_clock_timer",
            "_layer_sync_timer",
            "_realtime_conflict_timer",
            "_conflict_workbench_suggest_timer",
        ):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()

    def on_sop_profiles_updated(self) -> None:
        current_id = self._selected_profile_id
        self._reload_profiles(select_id=current_id)
        self.refresh_upcoming()
        self._schedule_layer_sync_refresh()

    def select_profile(self, profile_id: int) -> bool:
        """
        Programmatically select a profile in the SOP tab.
        Returns True when selection succeeds.
        """
        try:
            target_id = int(profile_id or 0)
        except Exception:
            return False
        if target_id <= 0:
            return False
        try:
            self._reload_profiles(select_id=target_id)
            for idx in range(self.profile_combo.count()):
                if int(self.profile_combo.itemData(idx) or 0) != target_id:
                    continue
                if self.profile_combo.currentIndex() != idx:
                    self.profile_combo.setCurrentIndex(idx)
                else:
                    self._on_profile_selected(idx)
                return True
        except Exception:
            return False
        return False

    def focus_source_segment(self, segment: Any) -> bool:
        raw = getattr(segment, "raw", {}) if segment is not None else {}
        try:
            profile_id = int(raw.get("sop_profile_id") or getattr(segment, "raw", {}).get("profile_id") or 0)
        except Exception:
            profile_id = 0
        if profile_id > 0 and not self.select_profile(profile_id):
            return False
        try:
            target_layer_id = int(raw.get("sop_layer_id") or raw.get("source_row_id") or raw.get("id") or 0)
        except Exception:
            target_layer_id = 0
        if target_layer_id > 0:
            for row in range(self.layer_table.rowCount()):
                day_combo = self.layer_table.cellWidget(row, self.LAYER_COL_DAY)
                try:
                    layer_id = int(day_combo.property("layer_id") or 0) if isinstance(day_combo, QWidget) else 0
                except Exception:
                    layer_id = 0
                if layer_id != target_layer_id:
                    continue
                self.layer_table.selectRow(row)
                widget = self.layer_table.cellWidget(row, self.LAYER_COL_START)
                if isinstance(widget, QLineEdit):
                    widget.setFocus(Qt.TabFocusReason)
                    widget.selectAll()
                else:
                    self.layer_table.setFocus(Qt.TabFocusReason)
                return True
        return False

    def apply_theme(self) -> None:
        try:
            theme = resolve_theme(self.settings)
            self.alignment_label.setStyleSheet(label_style("warning", theme, weight=600))
            self.layer_validation_label.setStyleSheet(label_style("warning", theme, weight=600))
            self.terms_hint_label.setStyleSheet(label_style("muted", theme))
            if hasattr(self, "activation_defaults_hint_label"):
                self.activation_defaults_hint_label.setStyleSheet(label_style("muted", theme))
            if hasattr(self, "activation_conflict_summary_label"):
                self.activation_conflict_summary_label.setStyleSheet(
                    label_style("text", theme, weight=600)
                )
            self._update_time_toggle_style(theme)
            self._update_profile_action_styles(theme)
            self._apply_accessibility_width_guards()
            self._refresh_layer_sync_hint()
        except Exception:
            pass


class SOPTab(_LegacySOPTab):
    """
    SOP Builder v2.
    Category-first workflow with one HF SOP and one Local Comms SOP profile,
    per-action UTC scheduling fields, and conflict-gated HF activation.
    """

    CAT_HF = SOPManager.CATEGORY_HF
    CAT_LOCAL = SOPManager.CATEGORY_LOCAL

    COL_GROUP = 0
    COL_COND = 1
    COL_RESOURCE = 2
    COL_MODE = 3
    COL_ACTION = 4
    COL_BANDFREQ = 5
    COL_START = 6
    COL_END = 7
    COL_DURATION = 8
    COL_INTERVAL = 9
    COL_CONTACT = 10
    COL_CONTACT_TARGET = 11
    COL_DESC = 12
    COL_CONFLICT = 13
    COL_REMOVE = 14
    WB_COL_ROW = 0
    WB_COL_ACTION = 1
    WB_COL_GROUP = 2
    WB_COL_HF = 3
    WB_COL_NET = 4
    WB_COL_SOP = 5
    WB_COL_POLICY = 6
    WB_COL_NEXT = 7
    WB_COL_SUGGESTED = 8
    WB_COL_APPLY = 9
    WB_COL_DETAILS = 10

    DURATION_OPTIONS: List[Tuple[str, int]] = [("00:30", 30), ("01:00", 60)]
    HF_RESOURCE_OPTIONS = ["FLDigi", "JS8Call", "VarAC", "SSB"]
    LOCAL_RESOURCE_FALLBACK = ["VHF", "UHF", "GMRS", "MURS", "FRS", "Meshtastic"]
    LOCAL_MODE_FALLBACK = ["Voice", "FM", "Digital", "Data", "Mixed", "Simplex", "Repeater"]
    LOCAL_CONTACT_OPTIONS: List[Tuple[str, str]] = [
        ("ncs_or_ancs", "NCS"),
        ("callsign", "Callsign"),
    ]
    SOP_BUILDER_SETTING_HF_CONFLICT_MODE = "sop_builder_hf_conflict_mode"
    SOP_BUILDER_SETTING_NET_CONFLICT_MODE = "sop_builder_net_conflict_mode"
    HF_CONFLICT_MODE_AUTO_ADJUST = "AUTO_ADJUST"
    HF_CONFLICT_MODE_REVIEW_DAILY = "REVIEW_DAILY"
    NET_CONFLICT_MODE_SOP_PRIORITY_TEMP = "SOP_PRIORITY_TEMP"
    NET_CONFLICT_MODE_REVIEW_NET = "REVIEW_NET"
    local_net_return_requested = Signal(object)
    WB_FILTER_ALL = "ALL"
    WB_FILTER_HF = "HF"
    WB_FILTER_NET = "NET"
    WB_FILTER_SOP = "SOP"
    WB_FILTER_NEEDS_TIME = "NEEDS_TIME"

    def _build_ui(self) -> None:
        # Unlike the retired v1 layout, v2 owns a real scrollable workspace.
        # This keeps the card workflow reachable at compact and Large Text sizes.
        outer = QVBoxLayout(self)
        self.sop_scroll = QScrollArea(self)
        self.sop_scroll.setObjectName("sopBuilderScroll")
        self.sop_scroll.setWidgetResizable(True)
        self.sop_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sop_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.sop_scroll_content = QWidget(self.sop_scroll)
        self.sop_scroll.setWidget(self.sop_scroll_content)
        root = QVBoxLayout(self.sop_scroll_content)
        self._action_drafts = SopActionDraftCollection()
        self._sop_card_page = 0
        self._syncing_action_drafts = False

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("<h3>SOP Builder</h3>"))
        self.help_btn = QPushButton("Help")
        self.help_btn.setToolTip("Open SOP Builder help.")
        self.help_btn.clicked.connect(lambda: self._open_context_help("tab.sop-builder"))
        title_row.addWidget(self.help_btn)
        title_row.addStretch()
        self.utc_label = QLabel()
        self.local_label = QLabel()
        self.utc_label.setVisible(False)
        self.local_label.setVisible(False)
        title_row.addWidget(self.utc_label)
        title_row.addWidget(self.local_label)
        self.time_toggle_btn = QPushButton("Times: Local")
        self.time_toggle_btn.clicked.connect(self._toggle_time_view)
        outer.addLayout(title_row)

        self.plan_context_label = PlanContextLabel(
            "sop",
            service=self.plan_context_service,
            fallback_text="SOP Builder uses the current Frequency Plan and radio context when reviewing HF and Local procedures.",
        )
        self.plan_context_label.setToolTip(
            "Use this context to confirm which radio and assigned Frequency Plan SOP work should be reviewed against."
        )
        self.plan_context_label.setVisible(False)
        root.addWidget(self.plan_context_label)
        if not self._defer_initial_load:
            self.plan_context_label.refresh_context(refresh=True)
        self.operating_plan_inputs_label = QLabel("")
        self.operating_plan_inputs_label.setObjectName("sopOperatingPlanInputsSummary")
        self.operating_plan_inputs_label.setWordWrap(True)
        self.operating_plan_inputs_label.setToolTip(
            "Read-only summary of the current radio, assigned Frequency Plan, and source inputs SOP Builder should review against."
        )
        root.addWidget(self.operating_plan_inputs_label)
        if not self._defer_initial_load:
            self._refresh_operating_plan_inputs_summary()

        self.local_net_context_bar = QGroupBox("Local Net reminder context", self.sop_scroll_content)
        local_net_context_layout = QHBoxLayout(self.local_net_context_bar)
        self.local_net_context_label = QLabel("", self.local_net_context_bar)
        self.local_net_context_label.setWordWrap(True)
        local_net_context_layout.addWidget(self.local_net_context_label, 1)
        self.local_net_context_return_btn = QPushButton("Return to Ops Center", self.local_net_context_bar)
        self.local_net_context_return_btn.clicked.connect(self._return_from_local_net_context)
        local_net_context_layout.addWidget(self.local_net_context_return_btn)
        self.local_net_context_bar.setVisible(False)
        self._local_net_navigation_intent: NavigationIntent | None = None
        root.addWidget(self.local_net_context_bar)

        self.traffic_suggestions_box = QGroupBox("Traffic Suggestions")
        traffic_layout = QHBoxLayout(self.traffic_suggestions_box)
        traffic_layout.setContentsMargins(8, 8, 8, 8)
        traffic_layout.setSpacing(8)
        self.traffic_suggestions_label = QLabel("No recent condition alerts mapped to SOP layers.")
        self.traffic_suggestions_label.setObjectName("sopTrafficSuggestionsLabel")
        self.traffic_suggestions_label.setWordWrap(True)
        self.traffic_suggestions_label.setToolTip(
            "Read-only view of recent condition-alert traffic that matches configured SOP layers."
        )
        traffic_layout.addWidget(self.traffic_suggestions_label, stretch=1)
        self.traffic_suggestions_apply_btn = QPushButton("Apply Level")
        self.traffic_suggestions_apply_btn.setToolTip("Apply the first matching condition alert level after review.")
        self.traffic_suggestions_apply_btn.clicked.connect(self._apply_first_traffic_suggestion)
        self.traffic_suggestions_apply_btn.setEnabled(False)
        traffic_layout.addWidget(self.traffic_suggestions_apply_btn)
        self.traffic_suggestions_refresh_btn = QPushButton("Refresh")
        self.traffic_suggestions_refresh_btn.setToolTip("Refresh recent condition alerts and matching SOP layer suggestions.")
        self.traffic_suggestions_refresh_btn.clicked.connect(self.refresh_traffic_suggestions)
        traffic_layout.addWidget(self.traffic_suggestions_refresh_btn)
        self.traffic_suggestions_review_btn = QPushButton("Review Automation")
        self.traffic_suggestions_review_btn.setToolTip("Review recent SOP automation actions and revert the latest reversible applied action.")
        self.traffic_suggestions_review_btn.clicked.connect(self._open_condition_sop_automation_review)
        traffic_layout.addWidget(self.traffic_suggestions_review_btn)
        root.addWidget(self.traffic_suggestions_box)
        self._sop_traffic_layout = traffic_layout
        self._traffic_suggestion_decisions: List[Any] = []
        self._traffic_focus_context: Dict[str, str] = {}
        if not self._defer_initial_load:
            QTimer.singleShot(0, self.refresh_traffic_suggestions)

        self.sop_workbench_box = QGroupBox("SOP Workbench")
        sop_workbench_layout = QHBoxLayout(self.sop_workbench_box)
        sop_workbench_layout.setContentsMargins(8, 8, 8, 8)
        sop_workbench_layout.setSpacing(8)

        self.sop_profile_selector_box = QGroupBox("SOP Profile")
        profile_selector_layout = QVBoxLayout(self.sop_profile_selector_box)
        self.sop_profile_selector_label = QLabel("Select or create an SOP, then review linked plans and radios.")
        self.sop_profile_selector_label.setObjectName("sopProfileSelector")
        self.sop_profile_selector_label.setWordWrap(True)
        self.sop_profile_selector_label.setToolTip(
            "SopProfileSelector: selected SOP, group/category, active state, linked plans/radios, versions, import/export."
        )
        profile_selector_layout.addWidget(self.sop_profile_selector_label)
        sop_workbench_layout.addWidget(self.sop_profile_selector_box, 1)

        self.sop_context_summary_box = QGroupBox("Context")
        context_summary_layout = QVBoxLayout(self.sop_context_summary_box)
        self.sop_context_summary_label = QLabel("Plan, radio, group, schedule, and source context will appear here.")
        self.sop_context_summary_label.setObjectName("sopContextSummary")
        self.sop_context_summary_label.setWordWrap(True)
        self.sop_context_summary_label.setToolTip(
            "SopContextSummary and SopScheduleImpact: shows assigned plan/radio context and expected schedule impact."
        )
        context_summary_layout.addWidget(self.sop_context_summary_label)
        sop_workbench_layout.addWidget(self.sop_context_summary_box, 1)

        self.sop_preview_box = QGroupBox("Ops Center Preview")
        sop_preview_layout = QVBoxLayout(self.sop_preview_box)
        self.sop_preview_label = QLabel("Save the SOP to preview the guidance Ops Center will render.")
        self.sop_preview_label.setObjectName("sopPreview")
        self.sop_preview_label.setWordWrap(True)
        self.sop_preview_label.setToolTip(
            "SopPreview: operator-facing preview of what Ops Center will show for this SOP."
        )
        sop_preview_layout.addWidget(self.sop_preview_label)
        sop_workbench_layout.addWidget(self.sop_preview_box, 1)
        root.addWidget(self.sop_workbench_box)
        self._sop_workbench_layout = sop_workbench_layout

        header = QHBoxLayout()
        header.setSpacing(8)
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        header.addWidget(QLabel("Manage SOP:"))
        header.addWidget(self.profile_combo, stretch=1)
        header.addWidget(self.time_toggle_btn)

        self.new_btn = QPushButton("New SOP")
        self.save_btn = QPushButton("Save SOP")
        self.delete_btn = QPushButton("Delete")
        self.versions_btn = QToolButton()
        self.versions_btn.setText("Versions")
        self.versions_btn.setPopupMode(QToolButton.InstantPopup)
        self.versions_menu = QMenu(self.versions_btn)
        self.save_version_action = self.versions_menu.addAction("Save Version")
        self.load_version_action = self.versions_menu.addAction("Load Version")
        self.delete_version_action = self.versions_menu.addAction("Delete Version")
        self.versions_btn.setMenu(self.versions_menu)
        self.export_pdf_btn = QPushButton("Export PDF")
        self.export_import_btn = QToolButton()
        self.export_import_btn.setText("Export/Import")
        self.export_import_btn.setPopupMode(QToolButton.InstantPopup)
        self.export_import_menu = QMenu(self.export_import_btn)
        self.export_json_action = self.export_import_menu.addAction("Export JSON")
        self.import_json_action = self.export_import_menu.addAction("Import JSON")
        self.export_import_btn.setMenu(self.export_import_menu)

        self.new_btn.clicked.connect(self._new_profile)
        self.save_btn.clicked.connect(self._save_profile)
        self.delete_btn.clicked.connect(self._delete_profile)
        self.save_version_action.triggered.connect(self._save_current_version)
        self.load_version_action.triggered.connect(self._load_saved_version)
        self.delete_version_action.triggered.connect(self._delete_saved_version)
        self.export_pdf_btn.clicked.connect(self._export_pdf)
        self.export_json_action.triggered.connect(self._export_profile)
        self.import_json_action.triggered.connect(self._import_profile)
        header.addWidget(self.new_btn)
        header.addWidget(self.save_btn)
        self.sop_more_btn = QToolButton(self)
        self.sop_more_btn.setText("More")
        self.sop_more_btn.setPopupMode(QToolButton.InstantPopup)
        more_menu = QMenu(self.sop_more_btn)
        more_menu.addMenu(self.versions_menu).setText("Versions")
        more_menu.addMenu(self.export_import_menu).setText("Import / export")
        more_menu.addAction("Export PDF", self._export_pdf)
        self.sop_delete_action = more_menu.addAction("Delete SOP", self._delete_profile)
        self.sop_more_btn.setMenu(more_menu)
        header.addWidget(self.sop_more_btn)
        for button in (self.delete_btn, self.versions_btn, self.export_pdf_btn, self.export_import_btn):
            button.hide()
        outer.addLayout(header)
        self._sop_management_row = header

        cfg_box = QGroupBox("SOP")
        cfg_layout = QVBoxLayout(cfg_box)

        top_row = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("SOP name")
        self.category_combo = QComboBox()
        self.category_combo.addItem("HF SOP", self.CAT_HF)
        self.category_combo.addItem("Local Comms SOP", self.CAT_LOCAL)
        self.category_combo.currentIndexChanged.connect(self._on_category_changed)
        self.active_cb = QCheckBox("Active")
        self.active_cb.toggled.connect(self._mark_dirty)
        top_row.addWidget(QLabel("Name:"))
        top_row.addWidget(self.name_edit, stretch=2)
        top_row.addWidget(QLabel("Category:"))
        top_row.addWidget(self.category_combo, stretch=1)
        top_row.addWidget(self.active_cb)
        outer.addLayout(top_row)

        self.terms_hint_label = QLabel(
            "Action rows are stored in UTC and displayed in Local/UTC based on the Showing toggle."
        )
        self.terms_hint_label.setWordWrap(True)
        self.terms_hint_label.setVisible(False)
        cfg_layout.addWidget(self.terms_hint_label)
        self.sop_workflow_status_label = QLabel("")
        self.sop_workflow_status_label.setWordWrap(True)
        self.sop_authoring_status = QLabel(self)
        self.sop_authoring_status.setObjectName("sopAuthoringStatus")
        self.sop_authoring_status.setWordWrap(True)
        outer.addWidget(self.sop_authoring_status)
        cfg_layout.addWidget(self.sop_workflow_status_label)

        self.activation_defaults_box = QGroupBox("SOP Activation Conflict Defaults")
        activation_defaults_layout = QFormLayout(self.activation_defaults_box)
        activation_defaults_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.hf_activation_conflict_mode_combo = QComboBox()
        self.hf_activation_conflict_mode_combo.addItem(
            "Auto-adjust HF Schedule around SOP (Reversible)",
            self.HF_CONFLICT_MODE_AUTO_ADJUST,
        )
        self.hf_activation_conflict_mode_combo.addItem(
            "Add SOP, review HF conflicts in Daily Schedule",
            self.HF_CONFLICT_MODE_REVIEW_DAILY,
        )
        self.net_activation_conflict_mode_combo = QComboBox()
        self.net_activation_conflict_mode_combo.addItem(
            "Temporary SOP Priority for Net overlaps (this SOP session)",
            self.NET_CONFLICT_MODE_SOP_PRIORITY_TEMP,
        )
        self.net_activation_conflict_mode_combo.addItem(
            "Add SOP, review conflicts in Net Schedule",
            self.NET_CONFLICT_MODE_REVIEW_NET,
        )
        self.hf_activation_conflict_mode_combo.currentIndexChanged.connect(self._on_activation_conflict_defaults_changed)
        self.net_activation_conflict_mode_combo.currentIndexChanged.connect(self._on_activation_conflict_defaults_changed)
        activation_defaults_layout.addRow("HF Schedule:", self.hf_activation_conflict_mode_combo)
        activation_defaults_layout.addRow("Net Schedule:", self.net_activation_conflict_mode_combo)
        self.activation_defaults_hint_label = QLabel(
            "These defaults are stored locally and applied when saving an active HF SOP. "
            "You can still review timing conflicts when a row needs manual adjustment."
        )
        self.activation_defaults_hint_label.setWordWrap(True)
        activation_defaults_layout.addRow(self.activation_defaults_hint_label)
        self.activation_conflict_summary_label = QLabel("Conflict Summary: No HF action rows to evaluate yet.")
        self.activation_conflict_summary_label.setWordWrap(True)
        activation_defaults_layout.addRow(self.activation_conflict_summary_label)
        self.activation_defaults_toggle_btn = QToolButton()
        self.activation_defaults_toggle_btn.setCheckable(True)
        self.activation_defaults_toggle_btn.clicked.connect(
            lambda checked=False: self._set_activation_defaults_expanded(bool(checked))
        )
        self.activation_defaults_summary_label = QLabel("")
        self.activation_defaults_summary_label.setWordWrap(True)
        activation_header = QHBoxLayout()
        activation_header.addWidget(self.activation_defaults_toggle_btn)
        activation_header.addWidget(self.activation_defaults_summary_label, stretch=1)
        cfg_layout.addLayout(activation_header)
        cfg_layout.addWidget(self.activation_defaults_box)
        self._activation_defaults_expanded = False
        self._set_activation_defaults_expanded(False, refresh=False)

        self.sop_action_builder_box = QGroupBox("Procedure actions")
        action_builder_layout = QVBoxLayout(self.sop_action_builder_box)
        action_builder_layout.setContentsMargins(10, 8, 10, 8)
        action_builder_layout.setSpacing(8)
        action_builder_header = QHBoxLayout()
        action_builder_title = QLabel("SOP summary")
        action_builder_title.setStyleSheet(label_style("text", resolve_theme(self.settings), weight=700))
        action_builder_title.setVisible(False)
        action_builder_header.addWidget(action_builder_title)
        action_builder_header.addStretch()
        self.hidden_rows_label = QLabel("")
        action_builder_header.addWidget(self.hidden_rows_label)
        self.add_row_btn = QPushButton("Add action")
        self.add_row_btn.setObjectName("sopAddActionRow")
        self.add_row_btn.setToolTip("Add a guided SOP action row for this SOP.")
        self.add_row_btn.clicked.connect(lambda: self._edit_summary_action(new=True))
        self._sop_action_header = action_builder_header
        action_builder_header.addWidget(self.add_row_btn)
        action_builder_layout.addLayout(action_builder_header)
        self.sop_action_builder_hint = QLabel(
            "Add actions using known settings. Select a row to edit; review the whole procedure here before Save."
        )
        self.sop_action_builder_hint.setObjectName("sopActionBuilderHint")
        self.sop_action_builder_hint.setWordWrap(True)
        action_builder_layout.addWidget(self.sop_action_builder_hint)
        self.sop_action_cards_label = QLabel("No SOP actions yet.")
        self.sop_action_cards_label.setObjectName("sopActionBuilderCards")
        self.sop_action_cards_label.setWordWrap(True)
        self.sop_action_cards_label.setTextFormat(Qt.RichText)
        self.sop_action_cards_label.setToolTip(
            "SOP action cards are the primary editor and the source used by Save."
        )
        self.sop_action_cards_label.setVisible(False)
        action_builder_layout.addWidget(self.sop_action_cards_label)
        self.sop_action_cards_container = QWidget(self.sop_action_builder_box)
        self.sop_action_cards_container.setObjectName("sopActionCards")
        self.sop_action_cards_layout = QVBoxLayout(self.sop_action_cards_container)
        self.sop_action_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.sop_action_cards_layout.setSpacing(8)
        action_builder_layout.addWidget(self.sop_action_cards_container)
        self.sop_cards_more_btn = QToolButton(self.sop_action_builder_box)
        self.sop_cards_more_btn.clicked.connect(self._toggle_sop_cards_expanded)
        self.sop_cards_more_btn.setVisible(False)
        action_builder_layout.addWidget(self.sop_cards_more_btn, alignment=Qt.AlignLeft)
        self.sop_action_builder_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.sop_action_cards_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.sop_action_builder_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer.addWidget(self.sop_action_builder_box, 1)
        self._summary_drafts_pending = False

        self.actions_table = QTableWidget(0, 15)
        self.actions_table.setHorizontalHeaderLabels(
            [
                "Group",
                "Condition Levels",
                "Resource",
                "Mode",
                "Action",
                "Band - Freq",
                "Start (UTC)",
                "End (UTC)",
                "Duration",
                "Interval",
                "Contact Type",
                "Contact Target",
                "Description",
                "Conflict",
                "Remove",
            ]
        )
        self.actions_table.verticalHeader().setVisible(False)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_GROUP, QHeaderView.Interactive)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_COND, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_RESOURCE, QHeaderView.Interactive)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_MODE, QHeaderView.Interactive)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_ACTION, QHeaderView.Interactive)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_BANDFREQ, QHeaderView.Interactive)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_START, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_END, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_DURATION, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_INTERVAL, QHeaderView.Interactive)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_CONTACT, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_CONTACT_TARGET, QHeaderView.Interactive)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_DESC, QHeaderView.Stretch)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_CONFLICT, QHeaderView.ResizeToContents)
        self.actions_table.horizontalHeader().setSectionResizeMode(self.COL_REMOVE, QHeaderView.ResizeToContents)
        self.actions_table.setColumnWidth(self.COL_GROUP, 150)
        self.actions_table.setColumnWidth(self.COL_RESOURCE, 130)
        self.actions_table.setColumnWidth(self.COL_MODE, 120)
        self.actions_table.setColumnWidth(self.COL_ACTION, 140)
        self.actions_table.setColumnWidth(self.COL_BANDFREQ, 180)
        self.actions_table.setColumnWidth(self.COL_INTERVAL, 110)
        self.actions_table.setColumnWidth(self.COL_CONTACT_TARGET, 170)
        self.actions_table.setMinimumHeight(0)
        self.actions_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.advanced_table_toggle_btn = QToolButton()
        self.advanced_table_toggle_btn.setObjectName("sopAdvancedBulkEditorToggle")
        self.advanced_table_toggle_btn.setCheckable(True)
        self.advanced_table_toggle_btn.clicked.connect(
            lambda checked=False: self._set_advanced_table_expanded(bool(checked))
        )
        cfg_layout.addWidget(self.advanced_table_toggle_btn, alignment=Qt.AlignLeft)
        self.advanced_table_box = QGroupBox("Advanced bulk editor")
        self.advanced_table_box.setObjectName("sopAdvancedTableTemporary")
        advanced_table_layout = QVBoxLayout(self.advanced_table_box)
        advanced_table_layout.setContentsMargins(10, 8, 10, 8)
        advanced_table_layout.setSpacing(8)
        self.advanced_table_hint = QLabel(
            "Optional spreadsheet-style editing for experienced users. Changes update the same actions shown in the cards."
        )
        self.advanced_table_hint.setObjectName("sopAdvancedTableRemovalFlag")
        self.advanced_table_hint.setWordWrap(True)
        advanced_table_layout.addWidget(self.advanced_table_hint)
        advanced_table_layout.addWidget(self.actions_table)
        self.advanced_table_box.setMinimumHeight(0)
        self.advanced_table_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        cfg_layout.addWidget(self.advanced_table_box)
        self._set_advanced_table_expanded(False)

        self.conflict_workbench_toggle_btn = QToolButton()
        self.conflict_workbench_toggle_btn.setCheckable(True)
        self.conflict_workbench_toggle_btn.clicked.connect(
            lambda checked=False: self._set_conflict_workbench_expanded(bool(checked))
        )
        self.conflict_workbench_summary_label = QLabel("")
        self.conflict_workbench_summary_label.setWordWrap(True)
        workbench_header = QHBoxLayout()
        workbench_header.addWidget(self.conflict_workbench_toggle_btn)
        workbench_header.addWidget(self.conflict_workbench_summary_label, stretch=1)
        cfg_layout.addLayout(workbench_header)

        self.conflict_workbench_box = QGroupBox("Conflict Workbench")
        conflict_workbench_layout = QVBoxLayout(self.conflict_workbench_box)
        self.conflict_workbench_box.setMinimumHeight(0)
        self.conflict_workbench_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.conflict_workbench_hint_label = QLabel(
            "Resolve conflict policy choices here before Save. Rows that still need timing changes will be flagged and handled in the Save-time conflict dialog."
        )
        self.conflict_workbench_hint_label.setWordWrap(True)
        conflict_workbench_layout.addWidget(self.conflict_workbench_hint_label)

        wb_filter_row = QHBoxLayout()
        self.conflict_workbench_filter_label = QLabel("Show:")
        wb_filter_row.addWidget(self.conflict_workbench_filter_label)
        self._conflict_workbench_filter_mode = self.WB_FILTER_ALL
        self._conflict_workbench_filter_buttons: Dict[str, QToolButton] = {}
        filter_defs = [
            (self.WB_FILTER_ALL, "All"),
            (self.WB_FILTER_HF, "HF"),
            (self.WB_FILTER_NET, "Net"),
            (self.WB_FILTER_SOP, "SOP"),
            (self.WB_FILTER_NEEDS_TIME, "Needs Time"),
        ]
        for mode_key, label in filter_defs:
            btn = QToolButton(self.conflict_workbench_box)
            btn.setText(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, m=mode_key: self._set_conflict_workbench_filter_mode(m))
            self._conflict_workbench_filter_buttons[mode_key] = btn
            wb_filter_row.addWidget(btn)
        wb_filter_row.addStretch()
        conflict_workbench_layout.addLayout(wb_filter_row)

        wb_actions_row = QHBoxLayout()
        self.conflict_workbench_status_label = QLabel("No HF conflicts detected.")
        self.conflict_workbench_status_label.setWordWrap(True)
        wb_actions_row.addWidget(self.conflict_workbench_status_label, stretch=1)
        self.workbench_set_sop_btn = QPushButton("Set SOP Priority")
        self.workbench_set_net_btn = QPushButton("Set Net Priority")
        self.workbench_set_daily_btn = QPushButton("Set Daily Priority")
        self.workbench_apply_defaults_btn = QPushButton("Apply Builder Defaults")
        self.workbench_set_sop_btn.clicked.connect(lambda: self._apply_conflict_workbench_policy_batch("SOP"))
        self.workbench_set_net_btn.clicked.connect(lambda: self._apply_conflict_workbench_policy_batch("NET"))
        self.workbench_set_daily_btn.clicked.connect(lambda: self._apply_conflict_workbench_policy_batch("DAILY"))
        self.workbench_apply_defaults_btn.clicked.connect(self._apply_conflict_workbench_builder_defaults)
        for btn in (
            self.workbench_set_sop_btn,
            self.workbench_set_net_btn,
            self.workbench_set_daily_btn,
            self.workbench_apply_defaults_btn,
        ):
            wb_actions_row.addWidget(btn)
        conflict_workbench_layout.addLayout(wb_actions_row)

        self.conflict_workbench_table = QTableWidget(0, 11)
        self.conflict_workbench_table.setHorizontalHeaderLabels(
            [
                "Row",
                "Action",
                "Group",
                "HF Schedule",
                "Net Schedule",
                "SOP Actions",
                "Policy",
                "Next Step",
                "Suggested Start",
                "Apply",
                "Details",
            ]
        )
        self.conflict_workbench_table.verticalHeader().setVisible(False)
        self.conflict_workbench_table.setAlternatingRowColors(True)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_ROW, QHeaderView.ResizeToContents)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_ACTION, QHeaderView.ResizeToContents)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_GROUP, QHeaderView.ResizeToContents)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_HF, QHeaderView.Stretch)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_NET, QHeaderView.Stretch)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_SOP, QHeaderView.Stretch)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_POLICY, QHeaderView.ResizeToContents)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_NEXT, QHeaderView.ResizeToContents)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_SUGGESTED, QHeaderView.ResizeToContents)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_APPLY, QHeaderView.ResizeToContents)
        self.conflict_workbench_table.horizontalHeader().setSectionResizeMode(self.WB_COL_DETAILS, QHeaderView.ResizeToContents)
        self.conflict_workbench_table.setMinimumHeight(0)
        self.conflict_workbench_table.setMaximumHeight(16777215)
        conflict_workbench_layout.addWidget(self.conflict_workbench_table)
        cfg_layout.addWidget(self.conflict_workbench_box)
        self._conflict_workbench_total_conflicts = 0
        self._conflict_workbench_expanded = False
        self._set_conflict_workbench_expanded(False, refresh=False)
        self._set_conflict_workbench_filter_mode(self.WB_FILTER_ALL, refresh=False)
        root.addWidget(cfg_box, stretch=1)

        # Compatibility placeholders for inherited methods not used in v2 UI.
        self.group_combo = QComboBox()
        self.secondary_combo = QComboBox()
        self.start_edit = QLineEdit("00:00")
        self.priority_spin = QSpinBox()
        self.horizon_spin = QSpinBox()
        self.contact_label = QLabel("")
        self.start_label = QLabel("")
        self.layer_sync_label = QLabel("")
        self.layer_validation_label = QLabel("")
        self.layer_runtime_label = QLabel("")
        self.alignment_label = QLabel("")
        self.populate_layer_btn = QPushButton()
        self.rebuild_layer_btn = QPushButton()
        self.add_layer_row_btn = QPushButton()
        self.refresh_btn = QPushButton()
        self.layer_table = QTableWidget(0, 0)
        self.upcoming_table = QTableWidget(0, 0)

        self._wire_dirty_tracking()
        self._realtime_conflict_timer = QTimer(self)
        self._realtime_conflict_timer.setSingleShot(True)
        self._realtime_conflict_timer.setInterval(550)
        self._realtime_conflict_timer.timeout.connect(self._run_realtime_hf_conflict_check)
        self._last_realtime_conflict_signature: Tuple[Any, ...] | None = None
        self._last_realtime_hf_analyses_cache: List[Tuple[int, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]] = []
        self._suppress_realtime_conflict_checks = False
        self._conflict_workbench_updating = False
        self._conflict_workbench_signature: Tuple[Any, ...] | None = None
        self._conflict_workbench_diag_cache: Dict[int, Dict[str, Any]] = {}
        self._conflict_workbench_suggested_start_utc: Dict[int, str] = {}
        self._conflict_workbench_suggest_queue: List[int] = []
        self._conflict_workbench_suggest_timer = QTimer(self)
        self._conflict_workbench_suggest_timer.setSingleShot(True)
        self._conflict_workbench_suggest_timer.setInterval(15)
        self._conflict_workbench_suggest_timer.timeout.connect(self._run_conflict_workbench_auto_suggest_step)
        self._apply_accessibility_width_guards()
        self._update_clock_labels()
        self._update_action_time_headers()
        self._update_time_toggle_style()
        self._load_activation_conflict_defaults_ui()
        self._apply_action_table_visual_order()
        self._apply_category_table_view()
        if not self._defer_initial_load:
            self._refresh_sop_workbench_contracts()
        self.sop_review_toggle = QToolButton(self)
        self.sop_review_toggle.setText("Review context, conflicts and advanced options")
        self.sop_review_toggle.setCheckable(True)
        self.sop_review_toggle.toggled.connect(self._show_summary_review)
        outer.addWidget(self.sop_review_toggle)
        self.sop_scroll.setVisible(False)
        outer.addWidget(self.sop_scroll, stretch=1)
        QTimer.singleShot(0, self._apply_sop_responsive_layout)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_sop_responsive_layout()

    def _apply_sop_responsive_layout(self) -> None:
        """Stack fixed-width builder bands before they create page scrolling."""
        compact = self.width() < 1050
        hint = getattr(self, "sop_action_builder_hint", None)
        if hint is not None:
            hint.setVisible(self.width() >= 900 and self.height() >= 480)
        direction = QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight
        for layout_name in ("_sop_traffic_layout", "_sop_workbench_layout"):
            layout = getattr(self, layout_name, None)
            if isinstance(layout, QBoxLayout):
                layout.setDirection(direction)

    def _wire_dirty_tracking(self) -> None:
        self.name_edit.textChanged.connect(self._mark_dirty)
        self.category_combo.currentIndexChanged.connect(self._mark_dirty)
        self.active_cb.toggled.connect(self._mark_dirty)

    def _set_advanced_table_expanded(self, expanded: bool) -> None:
        shown = bool(expanded)
        # The cards own the editing model.  Rebuild the compatibility table
        # from that model only when the operator asks to use the bulk editor;
        # when it closes, absorb bulk changes and refresh the card projection.
        if shown:
            self._reload_advanced_table_from_drafts()
        else:
            self._sync_drafts_from_advanced_table()
            self._render_sop_action_cards()
        self.advanced_table_box.setVisible(shown)
        self.advanced_table_toggle_btn.blockSignals(True)
        self.advanced_table_toggle_btn.setChecked(shown)
        self.advanced_table_toggle_btn.blockSignals(False)
        self.advanced_table_toggle_btn.setText(
            "Hide Advanced bulk editor" if shown else "Show Advanced bulk editor"
        )
        self.advanced_table_toggle_btn.setToolTip(
            "Optional spreadsheet-style editor. The action cards above are the primary workflow."
        )

    def _toggle_sop_cards_expanded(self) -> None:
        rows = self._action_drafts.rows()
        page_count = max(1, (len(rows) + 11) // 12)
        self._sop_card_page = (int(getattr(self, "_sop_card_page", 0) or 0) + 1) % page_count
        self._render_sop_action_cards()

    @staticmethod
    def _card_value(draft: object, field_name: str) -> str:
        return str(getattr(draft, field_name, "") or "")

    def _render_sop_action_cards(self) -> None:
        """Project in-memory drafts into one accumulating native summary viewport."""
        layout = getattr(self, "sop_action_cards_layout", None)
        if layout is None:
            return
        if not hasattr(self, "sop_summary_table"):
            self.sop_summary_table = QTableWidget(0, 2, self.sop_action_cards_container)
            table = self.sop_summary_table
            table.setObjectName("sopActionSummary")
            table.setHorizontalHeaderLabels(["Action / destination", "When / state"])
            table.verticalHeader().hide()
            table.setSelectionBehavior(QAbstractItemView.SelectRows)
            table.setSelectionMode(QAbstractItemView.SingleSelection)
            table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            table.setWordWrap(False)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            table.cellDoubleClicked.connect(lambda row, _col: self._edit_summary_action(row=row))
            layout.addWidget(table, 1)
            buttons = QHBoxLayout()
            self._sop_action_header.removeWidget(self.add_row_btn)
            buttons.addWidget(self.add_row_btn)
            for label, handler in (
                ("Edit action", lambda: self._edit_summary_action()),
                ("Duplicate", lambda: self._duplicate_sop_action_card(table.currentRow())),
                ("Remove", lambda: self._remove_sop_action_card(table.currentRow())),
            ):
                button = QPushButton(label, self.sop_action_cards_container)
                button.clicked.connect(lambda _checked=False, fn=handler: fn())
                buttons.addWidget(button)
            buttons.addStretch()
            layout.addLayout(buttons)
        table = self.sop_summary_table
        selected = table.currentRow()
        rows = self._action_drafts.rows()
        table.setRowCount(len(rows))
        for index, draft in enumerate(rows):
            destination = " ".join(filter(None, (draft.band, draft.frequency, draft.contact_target)))
            what = f"{index + 1}. {draft.group_name or 'Choose group'} · {draft.action_label or draft.action_key or 'Choose action'}"
            what += f"\n{draft.software or 'Choose tool'}" + (f" · {destination}" if destination else "")
            variant_issue = self._sop_frequency_reference_issue(
                group=draft.group_name.strip().upper(),
                band=draft.band.strip().upper(),
                mode=draft.mode.strip().upper(),
                frequency=draft.frequency.strip(),
                frequency_key=draft.operating_group_frequency_key,
            )
            if variant_issue:
                what += f"\n⚠ {variant_issue}"
            if draft.description:
                what += f" · {draft.description}"
            state = "Disabled" if not draft.enabled else ("Scheduled" if draft.schedule_applied else "Guidance only")
            start_text = self._summary_display_start(draft.daily_start_utc)
            time_mode = "Local" if self._show_local else "UTC"
            repeat = "daily" if draft.interval_minutes == 1440 else (f"every {draft.interval_minutes // 60}h" if draft.interval_minutes % 60 == 0 else f"every {draft.interval_minutes}m")
            when = f"{start_text} {time_mode} · {draft.duration_minutes}m · {repeat}"
            if draft.interval_phase_minutes:
                when += f" · phase +{draft.interval_phase_minutes}m"
            when += f"\nLevels {draft.condition_levels or 'ALL'} · {state}"
            conflicts = draft.daily_conflict_summary or draft.net_conflict_summary
            when += " · " + ("Review pending" if getattr(self, "_summary_drafts_pending", False) else (conflicts or "Validate on Save"))
            for column, value in enumerate((what, when)):
                item = QTableWidgetItem(value)
                item.setToolTip(value + f"\nUTC start: {draft.daily_start_utc} · Contact: {draft.contact_rule} · Conflict policy: {draft.conflict_policy}")
                table.setItem(index, column, item)
        row_height = table.fontMetrics().lineSpacing() * 2 + 12
        for index in range(table.rowCount()):
            table.setRowHeight(index, row_height)
        if rows:
            table.selectRow(min(max(selected, 0), len(rows) - 1))
        self.sop_cards_more_btn.hide()
        self._update_summary_status()

    def _summary_display_start(self, utc_text: str) -> str:
        if not self._show_local:
            return str(utc_text)
        try:
            hour, minute = map(int, str(utc_text).split(":"))
            now = datetime.datetime.now(datetime.timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
            return now.astimezone(self._summary_timezone).strftime("%H:%M")
        except (ValueError, TypeError):
            return str(utc_text)

    def _update_summary_status(self) -> None:
        label = getattr(self, "sop_authoring_status", None)
        if label is None:
            return
        state = "Unsaved changes" if self._dirty else "Saved"
        consequence = "Save activates this SOP and may change schedules." if self.active_cb.isChecked() else "Inactive SOP: Save stores the procedure."
        label.setText(f"{state} · {len(self._action_drafts.rows())} actions · {consequence}")
        if hasattr(self, "sop_delete_action"):
            self.sop_delete_action.setEnabled(bool(self._selected_profile_id))

    def _choose_local_frequency_reference(self, parent):
        from freqinout.core.known_operating_groups import net_resources_db_path
        from freqinout.core.resource_catalog_store import ResourceCatalogStore
        from freqinout.gui.resource_picker import choose_frequency_resource
        return choose_frequency_resource(ResourceCatalogStore(net_resources_db_path()), parent)

    def _summary_editor_options(self, row: int) -> Dict[str, Any]:
        """Copy loaded choices only; opening/editing an action never queries a source."""
        options: Dict[str, Any] = {"category": self._current_category()}
        if self._current_category() == self.CAT_LOCAL:
            options["choose_frequency_reference"] = self._choose_local_frequency_reference
            options["local_frequency_profiles"] = [dict(profile) for profile in self._local_net_profiles]
        for field, column in (("group_name", self.COL_GROUP), ("software", self.COL_RESOURCE),
                              ("mode", self.COL_MODE), ("contact_target", self.COL_CONTACT_TARGET)):
            values = []
            for index in range(self.actions_table.rowCount()):
                combo = self.actions_table.cellWidget(index, column)
                if isinstance(combo, QComboBox):
                    values.extend(combo.itemText(i) for i in range(combo.count()))
            options[field] = list(dict.fromkeys(values))
        if self._current_category() == self.CAT_LOCAL:
            group_resources: Dict[str, List[str]] = {}
            for profile in self._local_net_profiles:
                group = str(profile.get("group") or "").strip()
                resource = str(profile.get("resource") or "").strip()
                mode = str(profile.get("mode") or "").strip()
                if group and group not in options["group_name"]:
                    options["group_name"].append(group)
                if resource and resource not in options["software"]:
                    options["software"].append(resource)
                if mode and mode not in options["mode"]:
                    options["mode"].append(mode)
                if group and resource:
                    resources = group_resources.setdefault(group.upper(), [])
                    if resource not in resources:
                        resources.append(resource)
            options["local_group_resources"] = group_resources
        options["condition_levels"] = ["ALL", "1", "2", "3", "4", "5"]
        options["operating_groups"] = [dict(row) for row in self._operating_groups if isinstance(row, dict)]
        options["band"] = list(self.BAND_CHOICES) if self._current_category() == self.CAT_HF else []
        options["contact_rule"] = list(self.LOCAL_CONTACT_OPTIONS if self._current_category() == self.CAT_LOCAL else self.CONTACT_RULE_OPTIONS)
        options["conflict_policy"] = [(self.manager.CONFLICT_POLICY_SOP, "SOP Priority"),
                                      (self.manager.CONFLICT_POLICY_NET, "Net Priority"),
                                      (self.manager.CONFLICT_POLICY_DAILY, "Daily Priority")]
        catalog = dict(self._action_catalog_cache_value or {})
        draft_rows = self._action_drafts.rows()
        resource = draft_rows[row].software if 0 <= row < len(draft_rows) else ""
        options["action_key"] = list(catalog.get("Local Net" if self._current_category() == self.CAT_LOCAL else resource, []))
        dependent = {}
        for group in options.get("group_name", []):
            resources = {}
            configured = [g for g in self._operating_groups if str(g.get("group", "")).upper() == group.upper()]
            pairs = list(dict.fromkeys((str(g.get("band", "")), str(g.get("frequency", ""))) for g in configured if g.get("band") and g.get("frequency")))
            group_targets = list(dict.fromkeys(call for (cached_group, _rule), (_stamp, calls) in self._contact_lookup_cache.items() if cached_group == group.upper() for call in calls))
            for tool in options.get("software", []):
                if self._current_category() == self.CAT_LOCAL:
                    modes = self._local_modes_for_group_resource(group, tool)
                else:
                    modes = list(dict.fromkeys(str(g.get("mode") or "").strip() for g in configured if g.get("mode"))) or options.get("mode", [])
                resources[tool] = {"action_key": list(catalog.get("Local Net" if self._current_category() == self.CAT_LOCAL else tool, [])),
                                   "mode": modes, "band_frequency": pairs,
                                   "contact_target": group_targets,
                                   "condition_levels": options["condition_levels"]}
            dependent[group] = resources
        options["dependent_options"] = dependent
        options["frequency"] = list(dict.fromkeys(str(g.get("frequency", "")) for g in self._operating_groups if g.get("frequency")))
        return options

    def _resolve_sop_frequency_row_key(
        self,
        *,
        group: str,
        band: str,
        mode: str,
        frequency: str,
        frequency_key: str = "",
        original_identity: tuple[str, str, str, str] | None = None,
    ) -> str:
        identity = (
            group.strip().upper(),
            band.strip().upper(),
            mode.strip().upper(),
            normalize_frequency_hz(frequency),
        )
        original = None
        if original_identity is not None:
            original = (
                str(original_identity[0]).strip().upper(),
                str(original_identity[1]).strip().upper(),
                str(original_identity[2]).strip().upper(),
                normalize_frequency_hz(original_identity[3]),
            )
        supplied_key = frequency_key if original == identity else ""
        group_family = resolve_operating_group_frequency(self._operating_groups, group=group).candidates
        group_rows = [row for row in group_family if normalize_frequency_hz(row.get("frequency")) is not None]
        if not group_rows and not supplied_key:
            # Preserve explicit legacy groups that have no catalog frequency rows.
            return ""
        resolution = resolve_operating_group_frequency(
            self._operating_groups,
            group=group,
            frequency_key=supplied_key,
            band=band,
            mode=mode,
            frequency=frequency,
        )
        if not resolution.resolved or resolution.row is None:
            raise ValueError(resolution.reason or "Choose a configured group frequency.")
        return str(resolution.row.get("operating_group_frequency_key") or "")

    def _sop_frequency_reference_issue(
        self, *, group: str, band: str, mode: str, frequency: str, frequency_key: str = ""
    ) -> str:
        if not group or not frequency:
            return ""
        identity = (group, band, mode, frequency)
        try:
            self._resolve_sop_frequency_row_key(
                group=group,
                band=band,
                mode=mode,
                frequency=frequency,
                frequency_key=frequency_key,
                original_identity=identity,
            )
            return ""
        except ValueError:
            return "Frequency variant unresolved. Choose the intended configured group frequency before saving."

    def _edit_summary_action(self, row: int | None = None, *, new: bool = False) -> None:
        if row is None:
            row = self.sop_summary_table.currentRow() if hasattr(self, "sop_summary_table") else 0
        rows = self._action_drafts.payloads()
        if not new and not 0 <= row < len(rows):
            return
        seed = dict(rows[row]) if 0 <= row < len(rows) else {}
        if new:
            seed["id"] = 0
            seed["description"] = ""
        dialog = SopActionEditorDialog(self, seed, self._summary_editor_options(row))
        if dialog.exec() != QDialog.Accepted:
            return
        payload = dialog.payload()
        if new:
            # Replace the initial placeholder rather than adding a phantom blank action.
            if len(rows) == 1 and not any(rows[0].get(key) for key in ("group_name", "action_key", "description")):
                rows[0] = payload
                self._action_drafts.replace(rows)
            else:
                self._action_drafts.append(payload)
        else:
            rows[row] = payload
            self._action_drafts.replace(rows)
        self._summary_drafts_pending = True
        self._set_save_dirty(True)
        self._render_sop_action_cards()

    def _show_summary_review(self, shown: bool) -> None:
        if shown:
            self._ensure_summary_compatibility_projection()
        self.sop_scroll.setVisible(shown)
        self.sop_review_toggle.setText("Hide review options" if shown else "Review context, conflicts and advanced options")

    def _ensure_summary_compatibility_projection(self) -> None:
        if not getattr(self, "_summary_drafts_pending", False):
            return
        payloads = self._action_drafts.payloads()
        self._reload_advanced_table_from_drafts()
        # Compatibility combos must never overwrite custom draft-only values.
        self._action_drafts.replace(payloads)
        self._summary_drafts_pending = False
        self._render_sop_action_cards()

    def _on_sop_card_field_changed(self, row: int, field_name: str, value: Any) -> None:
        if bool(getattr(self, "_syncing_action_drafts", False)):
            return
        numeric = {"duration_minutes", "interval_minutes", "interval_phase_minutes"}
        converted: Any = value
        if field_name in numeric:
            try:
                converted = max(0, int(value or 0))
            except ValueError:
                return
        if self._action_drafts.update(row, field_name, converted):
            if field_name in {"group_name", "band", "frequency", "mode"}:
                # An explicit edit is a deliberate reselection path. Keep an
                # untouched legacy key pinned so a removed variant stays unresolved.
                self._action_drafts.update(row, "operating_group_frequency_key", "")
            if field_name in {"daily_start_utc", "duration_minutes"}:
                draft = self._action_drafts.rows()[row]
                raw_start = str(draft.daily_start_utc or "").strip()
                if self._is_valid_hhmm(raw_start):
                    self._action_drafts.update(
                        row,
                        "daily_end_utc",
                        self._add_minutes_hhmm(raw_start, int(draft.duration_minutes or 60)),
                    )
            self._card_edit_in_progress = True
            try:
                self._mirror_card_field_to_advanced_table(row, field_name, converted)
                self._mark_dirty()
            finally:
                self._card_edit_in_progress = False

    def _mark_dirty(self, *_args) -> None:
        # The advanced editor is a view/editor adapter: changes enter the draft
        # collection immediately, while card changes never read their values back
        # from the table.
        sender = self.sender()
        advanced_change = isinstance(sender, QWidget) and (
            sender is getattr(self, "actions_table", None)
            or bool(getattr(self, "actions_table", None) and self.actions_table.isAncestorOf(sender))
        )
        if advanced_change and not bool(getattr(self, "_card_edit_in_progress", False)) and not bool(getattr(self, "_loading_ui", False)):
            self._sync_drafts_from_advanced_table()
        super()._mark_dirty(*_args)

    def _sync_drafts_from_advanced_table(self) -> None:
        if not hasattr(self, "_action_drafts") or not hasattr(self, "actions_table") or bool(getattr(self, "_syncing_action_drafts", False)):
            return
        rows: List[Dict[str, Any]] = []
        existing_rows = self._action_drafts.payloads()
        for row in range(self.actions_table.rowCount()):
            group = self.actions_table.cellWidget(row, self.COL_GROUP)
            resource = self.actions_table.cellWidget(row, self.COL_RESOURCE)
            mode = self.actions_table.cellWidget(row, self.COL_MODE)
            action = self.actions_table.cellWidget(row, self.COL_ACTION)
            bandfreq = self.actions_table.cellWidget(row, self.COL_BANDFREQ)
            duration = self.actions_table.cellWidget(row, self.COL_DURATION)
            interval = self.actions_table.cellWidget(row, self.COL_INTERVAL)
            contact = self.actions_table.cellWidget(row, self.COL_CONTACT)
            target = self.actions_table.cellWidget(row, self.COL_CONTACT_TARGET)
            desc = self.actions_table.cellWidget(row, self.COL_DESC)
            cond = self.actions_table.cellWidget(row, self.COL_COND)
            start = self._action_row_start_edit(row)
            if not isinstance(group, QComboBox):
                continue
            band, frequency = self._split_band_freq(bandfreq.currentText()) if isinstance(bandfreq, QComboBox) else ("", "")
            mode_text = mode.currentText().strip().upper() if isinstance(mode, QComboBox) else ""
            interval_minutes, phase_minutes = self._parse_interval_spec(interval.currentText()) if isinstance(interval, QComboBox) else (180, 0)
            preserved = dict(existing_rows[row]) if row < len(existing_rows) else {}
            original_identity = group.property("operating_group_frequency_original_identity")
            current_identity = (group.currentText().strip().upper(), band.strip().upper(), mode_text, frequency.strip())
            frequency_key = str(group.property("operating_group_frequency_key") or preserved.get("operating_group_frequency_key") or "")
            try:
                frequency_key = self._resolve_sop_frequency_row_key(
                    group=current_identity[0], band=current_identity[1], mode=current_identity[2],
                    frequency=current_identity[3], frequency_key=frequency_key,
                    original_identity=tuple(original_identity) if isinstance(original_identity, (tuple, list)) and len(original_identity) == 4 else None,
                )
                group.setProperty("operating_group_frequency_key", frequency_key)
                group.setProperty("operating_group_frequency_original_identity", current_identity)
            except ValueError:
                pass
            preserved.update(
                {
                    "id": int(group.property("action_id") or 0),
                    "group_name": group.currentText().strip().upper(),
                    "condition_levels": self._condition_levels_from_widget(cond),
                    "band": band,
                    "frequency": frequency,
                    "operating_group_frequency_key": frequency_key,
                    "software": resource.currentText().strip() if isinstance(resource, QComboBox) else "",
                    "mode": mode_text,
                    "action_key": str(action.currentData() or "").strip() if isinstance(action, QComboBox) else "",
                    "action_label": action.currentText().strip() if isinstance(action, QComboBox) else "",
                    "daily_start_utc": self._utc_start_hhmm_from_display(start.text().strip(), show_local=self._show_local) if isinstance(start, QLineEdit) else "00:00",
                    "duration_minutes": int(duration.currentData() or 60) if isinstance(duration, QComboBox) else 60,
                    "interval_minutes": interval_minutes,
                    "interval_phase_minutes": phase_minutes,
                    "interval_hours": max(1, int((interval_minutes + 59) // 60)),
                    "conflict_policy": self.manager._normalize_conflict_policy(group.property("conflict_policy")),
                    "schedule_applied": bool(group.property("schedule_applied")),
                    "contact_rule": str(contact.currentData() or "none") if isinstance(contact, QComboBox) else "none",
                    "contact_target": target.currentText().strip().upper() if isinstance(target, QComboBox) else "",
                    "description": desc.text().strip() if isinstance(desc, QLineEdit) else "",
                    "sort_order": row,
                }
            )
            rows.append(preserved)
        if rows:
            self._action_drafts.replace(rows)

    def _mirror_card_field_to_advanced_table(self, row: int, field_name: str, value: Any) -> None:
        """Keep the temporary table compatible without using it as the authority."""
        if row < 0 or row >= self.actions_table.rowCount():
            return
        mapping = {
            "group_name": self.COL_GROUP,
            "software": self.COL_RESOURCE,
            "mode": self.COL_MODE,
            "daily_start_utc": self.COL_START,
            "contact_target": self.COL_CONTACT_TARGET,
            "description": self.COL_DESC,
        }
        self._syncing_action_drafts = True
        try:
            if field_name == "condition_levels":
                widget = self.actions_table.cellWidget(row, self.COL_COND)
                if isinstance(widget, _ConditionLevelsMultiCombo):
                    widget.set_normalized_value(str(value), emit=False)
                return
            if field_name in {"band", "frequency"}:
                widget = self.actions_table.cellWidget(row, self.COL_BANDFREQ)
                draft = self._action_drafts.rows()[row]
                if isinstance(widget, QComboBox):
                    widget.setCurrentText(f"{draft.band} - {draft.frequency}".strip(" -"))
                return
            if field_name == "action_key":
                widget = self.actions_table.cellWidget(row, self.COL_ACTION)
                if isinstance(widget, QComboBox):
                    idx = widget.findData(value)
                    if idx >= 0:
                        widget.setCurrentIndex(idx)
                return
            if field_name == "duration_minutes":
                widget = self.actions_table.cellWidget(row, self.COL_DURATION)
                if isinstance(widget, QComboBox):
                    idx = widget.findData(int(value or 60))
                    if idx >= 0:
                        widget.setCurrentIndex(idx)
                return
            if field_name == "schedule_applied":
                widget = self.actions_table.cellWidget(row, self.COL_GROUP)
                if isinstance(widget, QComboBox):
                    widget.setProperty("schedule_applied", bool(value))
                return
            if field_name == "conflict_policy":
                widget = self.actions_table.cellWidget(row, self.COL_GROUP)
                if isinstance(widget, QComboBox):
                    widget.setProperty("conflict_policy", self.manager._normalize_conflict_policy(value))
                return
            col = mapping.get(field_name)
            if col is None:
                return
            widget = self._action_row_start_edit(row) if col == self.COL_START else self.actions_table.cellWidget(row, col)
            if isinstance(widget, QLineEdit):
                widget.setText(str(value))
            elif isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
        finally:
            self._syncing_action_drafts = False

    def _duplicate_sop_action_card(self, row: int) -> None:
        if self._action_drafts.duplicate(row) is None:
            return
        self._summary_drafts_pending = True
        self._set_save_dirty(True)
        self._render_sop_action_cards()

    def _remove_sop_action_card(self, row: int) -> None:
        if not self._action_drafts.remove(row):
            return
        if not self._action_drafts.rows():
            self._action_drafts.append({})
        self._summary_drafts_pending = True
        self._set_save_dirty(True)
        self._render_sop_action_cards()

    def _reload_advanced_table_from_drafts(self) -> None:
        self._loading_ui = True
        try:
            self._populate_actions(self._action_drafts.payloads())
        finally:
            self._loading_ui = False

    def _normalize_hf_activation_conflict_mode(self, value: Any) -> str:
        raw = str(value or "").strip().upper()
        if raw == self.HF_CONFLICT_MODE_REVIEW_DAILY:
            return self.HF_CONFLICT_MODE_REVIEW_DAILY
        return self.HF_CONFLICT_MODE_AUTO_ADJUST

    def _normalize_net_activation_conflict_mode(self, value: Any) -> str:
        raw = str(value or "").strip().upper()
        if raw == self.NET_CONFLICT_MODE_REVIEW_NET:
            return self.NET_CONFLICT_MODE_REVIEW_NET
        return self.NET_CONFLICT_MODE_SOP_PRIORITY_TEMP

    def _activation_conflict_defaults(self) -> Dict[str, str]:
        hf_mode = self._normalize_hf_activation_conflict_mode(
            self.settings.get(self.SOP_BUILDER_SETTING_HF_CONFLICT_MODE, self.HF_CONFLICT_MODE_AUTO_ADJUST)
        )
        net_mode = self._normalize_net_activation_conflict_mode(
            self.settings.get(
                self.SOP_BUILDER_SETTING_NET_CONFLICT_MODE,
                self.NET_CONFLICT_MODE_SOP_PRIORITY_TEMP,
            )
        )
        return {"hf_mode": hf_mode, "net_mode": net_mode}

    def _load_activation_conflict_defaults_ui(self) -> None:
        hf_combo = getattr(self, "hf_activation_conflict_mode_combo", None)
        net_combo = getattr(self, "net_activation_conflict_mode_combo", None)
        if not isinstance(hf_combo, QComboBox) or not isinstance(net_combo, QComboBox):
            return
        defaults = self._activation_conflict_defaults()
        hf_combo.blockSignals(True)
        net_combo.blockSignals(True)
        try:
            hf_idx = hf_combo.findData(defaults.get("hf_mode"))
            net_idx = net_combo.findData(defaults.get("net_mode"))
            hf_combo.setCurrentIndex(hf_idx if hf_idx >= 0 else 0)
            net_combo.setCurrentIndex(net_idx if net_idx >= 0 else 0)
        finally:
            hf_combo.blockSignals(False)
            net_combo.blockSignals(False)
        self._update_activation_defaults_header_summary()
        self._update_activation_conflict_summary([])
        self._update_sop_builder_workflow_status(self._last_realtime_hf_analyses_cache)

    def _on_activation_conflict_defaults_changed(self, *_args) -> None:
        if getattr(self, "_loading_ui", False):
            return
        hf_combo = getattr(self, "hf_activation_conflict_mode_combo", None)
        net_combo = getattr(self, "net_activation_conflict_mode_combo", None)
        if not isinstance(hf_combo, QComboBox) or not isinstance(net_combo, QComboBox):
            return
        hf_mode = self._normalize_hf_activation_conflict_mode(hf_combo.currentData())
        net_mode = self._normalize_net_activation_conflict_mode(net_combo.currentData())
        try:
            self.settings.set_many(
                {
                    self.SOP_BUILDER_SETTING_HF_CONFLICT_MODE: hf_mode,
                    self.SOP_BUILDER_SETTING_NET_CONFLICT_MODE: net_mode,
                }
            )
        except Exception as e:
            log.debug("SOP Builder: failed saving activation conflict defaults: %s", e)
        self._update_activation_defaults_header_summary()
        try:
            self._update_conflict_workbench_batch_actions()
        except Exception:
            pass
        self._update_sop_builder_workflow_status(self._last_realtime_hf_analyses_cache)

    def _activation_defaults_summary_text(self) -> str:
        defaults = self._activation_conflict_defaults()
        hf_txt = (
            "Auto-adjust"
            if defaults.get("hf_mode") == self.HF_CONFLICT_MODE_AUTO_ADJUST
            else "Review in Daily"
        )
        net_txt = (
            "Temporary SOP Priority"
            if defaults.get("net_mode") == self.NET_CONFLICT_MODE_SOP_PRIORITY_TEMP
            else "Review in Net"
        )
        return f"HF: {hf_txt} | Net: {net_txt}"

    def _update_activation_defaults_header_summary(self) -> None:
        label = getattr(self, "activation_defaults_summary_label", None)
        if not isinstance(label, QLabel):
            return
        prefix = "Activation Defaults"
        summary = self._activation_defaults_summary_text()
        if bool(getattr(self, "_activation_defaults_expanded", False)):
            label.setText(f"{prefix}: {summary}")
        else:
            label.setText(f"{prefix}: {summary}")
        btn = getattr(self, "activation_defaults_toggle_btn", None)
        if isinstance(btn, QToolButton):
            btn.setText("Hide Defaults" if bool(getattr(self, "_activation_defaults_expanded", False)) else "Show Defaults")

    def _set_activation_defaults_expanded(self, expanded: bool, *, refresh: bool = True) -> None:
        is_expanded = bool(expanded)
        self._activation_defaults_expanded = is_expanded
        box = getattr(self, "activation_defaults_box", None)
        if isinstance(box, QGroupBox):
            box.setVisible(is_expanded)
        btn = getattr(self, "activation_defaults_toggle_btn", None)
        if isinstance(btn, QToolButton):
            btn.blockSignals(True)
            btn.setChecked(is_expanded)
            btn.blockSignals(False)
        self._update_activation_defaults_header_summary()
        self._apply_workflow_section_toggle_styles()
        if refresh:
            self._update_sop_builder_workflow_status(self._last_realtime_hf_analyses_cache)

    def _update_conflict_workbench_header_summary(
        self,
        *,
        total_rows: int,
        visible_rows: int,
        needs_time_rows: int,
    ) -> None:
        label = getattr(self, "conflict_workbench_summary_label", None)
        btn = getattr(self, "conflict_workbench_toggle_btn", None)
        total = max(0, int(total_rows))
        self._conflict_workbench_total_conflicts = total
        if isinstance(btn, QToolButton):
            is_expanded = bool(getattr(self, "_conflict_workbench_expanded", False))
            if is_expanded:
                btn.setText("Hide Workbench")
            elif total > 0:
                btn.setText(f"Review Conflicts ({total})")
            else:
                btn.setText("Show Workbench")
        if not isinstance(label, QLabel):
            self._apply_workflow_section_toggle_styles()
            return
        if total <= 0:
            label.setText("Conflict Workbench: No conflicts detected.")
            self._apply_workflow_section_toggle_styles()
            return
        if visible_rows != total:
            base = f"Conflict Workbench: Showing {visible_rows} of {total} conflicts"
        else:
            base = f"Conflict Workbench: {total} conflict row(s)"
        if needs_time_rows > 0:
            base += f" | {needs_time_rows} need timing changes"
        label.setText(base)
        self._apply_workflow_section_toggle_styles()

    def _set_conflict_workbench_expanded(self, expanded: bool, *, refresh: bool = True) -> None:
        is_expanded = bool(expanded)
        self._conflict_workbench_expanded = is_expanded
        box = getattr(self, "conflict_workbench_box", None)
        if isinstance(box, QGroupBox):
            box.setVisible(is_expanded)
        btn = getattr(self, "conflict_workbench_toggle_btn", None)
        if isinstance(btn, QToolButton):
            btn.blockSignals(True)
            btn.setChecked(is_expanded)
            btn.blockSignals(False)
            btn.setText("Hide Workbench" if is_expanded else "Show Workbench")
        self._apply_workflow_section_toggle_styles()
        if refresh:
            self._update_sop_builder_workflow_status(self._last_realtime_hf_analyses_cache)

    def _apply_workflow_section_toggle_styles(self) -> None:
        try:
            theme = resolve_theme(self.settings)
        except Exception:
            return
        activation_btn = getattr(self, "activation_defaults_toggle_btn", None)
        if isinstance(activation_btn, QToolButton):
            activation_btn.setStyleSheet(
                button_style("eligible_info" if bool(getattr(self, "_activation_defaults_expanded", False)) else "muted", theme)
            )
        workbench_btn = getattr(self, "conflict_workbench_toggle_btn", None)
        if isinstance(workbench_btn, QToolButton):
            conflict_total = max(0, int(getattr(self, "_conflict_workbench_total_conflicts", 0) or 0))
            is_expanded = bool(getattr(self, "_conflict_workbench_expanded", False))
            if is_expanded:
                wb_role = "eligible_info"
            elif conflict_total > 0:
                wb_role = "eligible_warning"
            else:
                wb_role = "muted"
            workbench_btn.setStyleSheet(
                button_style(wb_role, theme)
            )

    def _update_sop_builder_workflow_status(
        self,
        analyses: List[Tuple[int, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]] | None = None,
    ) -> None:
        label = getattr(self, "sop_workflow_status_label", None)
        if not isinstance(label, QLabel):
            return
        cat = self._current_category()
        total_rows = int(self.actions_table.rowCount()) if isinstance(getattr(self, "actions_table", None), QTableWidget) else 0
        checked_rows = len(list(analyses or []))
        conflict_rows = 0
        needs_time_rows = 0
        for _row_index, action, diag, _peers in list(analyses or []):
            if not bool(diag.get("has_conflict")):
                continue
            conflict_rows += 1
            policy = self._action_row_conflict_policy(int(action.get("sort_order") or _row_index))
            if bool(diag.get("sop_conflicts")) or (
                policy in {self.manager.CONFLICT_POLICY_NET, self.manager.CONFLICT_POLICY_DAILY}
                and bool(diag.get("first_occurrence_conflict"))
            ):
                needs_time_rows += 1
        if cat != self.CAT_HF:
            label.setText("Workflow: 1) Configure Local Comms action rows 2) Review contacts/targets 3) Save.")
            return
        defaults_summary = self._activation_defaults_summary_text()
        if total_rows <= 0:
            label.setText(f"Workflow: Add action rows, then save. Defaults: {defaults_summary}.")
            return
        next_step = "Next: Save SOP."
        if checked_rows < total_rows:
            next_step = "Next: complete required fields in Action Rows."
        elif conflict_rows > 0 and needs_time_rows > 0:
            next_step = "Next: use Conflict Workbench Suggest/Apply for timing rows, then Save."
        elif conflict_rows > 0:
            next_step = "Next: review policy choices in Conflict Workbench, then Save."
        label.setText(
            f"Workflow: Rows validated {checked_rows}/{total_rows} | Conflicts {conflict_rows} | Timing {needs_time_rows}. {next_step} Defaults: {defaults_summary}."
        )

    def _update_activation_conflict_summary(
        self,
        analyses: List[Tuple[int, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]] | None = None,
    ) -> None:
        label = getattr(self, "activation_conflict_summary_label", None)
        if not isinstance(label, QLabel):
            return
        if self._current_category() != self.CAT_HF:
            label.setText("Conflict Summary: Local Comms SOP does not use HF/Net conflict checks.")
            self._update_sop_builder_workflow_status([])
            return
        total_rows = int(self.actions_table.rowCount()) if isinstance(getattr(self, "actions_table", None), QTableWidget) else 0
        if total_rows <= 0:
            label.setText("Conflict Summary: No action rows to evaluate.")
            self._update_sop_builder_workflow_status([])
            return
        checked = list(analyses or [])
        if not checked:
            label.setText(
                "Conflict Summary: Pending. Complete Group, Action, Band-Freq, Start, Duration, and Interval to evaluate conflicts."
            )
            self._update_sop_builder_workflow_status([])
            return
        rows_with_any = 0
        rows_hf = 0
        rows_net = 0
        rows_sop = 0
        for _row_index, _action, diag, _peers in checked:
            has_daily = bool(diag.get("daily_conflicts"))
            has_net = bool(diag.get("net_conflicts"))
            has_sop = bool(diag.get("sop_conflicts"))
            if has_daily:
                rows_hf += 1
            if has_net:
                rows_net += 1
            if has_sop:
                rows_sop += 1
            if has_daily or has_net or has_sop:
                rows_with_any += 1
        pending_rows = max(0, total_rows - len(checked))
        parts = [
            f"Rows needing review: {rows_with_any}",
            f"HF Schedule: {rows_hf}",
            f"Net Schedule: {rows_net}",
            f"SOP Actions: {rows_sop}",
        ]
        if pending_rows > 0:
            parts.append(f"Pending rows: {pending_rows}")
        label.setText("Conflict Summary: " + " | ".join(parts))
        self._update_sop_builder_workflow_status(checked)

    def _action_row_group_combo(self, row_index: int) -> QComboBox | None:
        try:
            row = int(row_index)
        except Exception:
            return None
        if row < 0 or row >= self.actions_table.rowCount():
            return None
        widget = self.actions_table.cellWidget(row, self.COL_GROUP)
        return widget if isinstance(widget, QComboBox) else None

    def _action_row_conflict_policy(self, row_index: int) -> str:
        combo = self._action_row_group_combo(row_index)
        if combo is None:
            return self.manager.CONFLICT_POLICY_SOP
        return self.manager._normalize_conflict_policy(combo.property("conflict_policy"))

    def _set_action_row_conflict_policy(
        self,
        row_index: int,
        policy: Any,
        *,
        mark_dirty: bool = True,
        schedule_refresh: bool = True,
    ) -> bool:
        combo = self._action_row_group_combo(row_index)
        if combo is None:
            return False
        normalized = self.manager._normalize_conflict_policy(policy)
        current = self.manager._normalize_conflict_policy(combo.property("conflict_policy"))
        if current == normalized:
            return False
        combo.setProperty("conflict_policy", normalized)
        if mark_dirty:
            self._mark_dirty()
        else:
            self._refresh_sop_workbench_contracts()
        if schedule_refresh:
            self._schedule_realtime_hf_conflict_check()
            try:
                self._update_conflict_workbench(self._last_realtime_hf_analyses_cache)
            except Exception:
                pass
        return True

    def _conflict_policy_combo_index(self, combo: QComboBox, policy: str) -> int:
        idx = combo.findData(policy)
        return idx if idx >= 0 else 0

    def _workbench_policy_combo_changed(self, combo: QComboBox) -> None:
        if getattr(self, "_conflict_workbench_updating", False):
            return
        if not isinstance(combo, QComboBox):
            return
        try:
            row_index = int(combo.property("sop_row_index") or -1)
        except Exception:
            row_index = -1
        if row_index < 0:
            return
        self._set_action_row_conflict_policy(row_index, combo.currentData())

    def _normalize_conflict_workbench_filter_mode(self, value: Any) -> str:
        raw = str(value or "").strip().upper()
        valid = {
            self.WB_FILTER_ALL,
            self.WB_FILTER_HF,
            self.WB_FILTER_NET,
            self.WB_FILTER_SOP,
            self.WB_FILTER_NEEDS_TIME,
        }
        return raw if raw in valid else self.WB_FILTER_ALL

    def _apply_conflict_workbench_filter_button_styles(self) -> None:
        try:
            theme = resolve_theme(self.settings)
        except Exception:
            return
        for mode_key, btn in dict(getattr(self, "_conflict_workbench_filter_buttons", {}) or {}).items():
            if not isinstance(btn, QToolButton):
                continue
            role = "eligible_primary" if str(mode_key) == str(getattr(self, "_conflict_workbench_filter_mode", self.WB_FILTER_ALL)) else "muted"
            btn.setStyleSheet(button_style(role, theme))

    def _set_conflict_workbench_filter_mode(self, mode: Any, *, refresh: bool = True) -> None:
        selected = self._normalize_conflict_workbench_filter_mode(mode)
        self._conflict_workbench_filter_mode = selected
        for mode_key, btn in dict(getattr(self, "_conflict_workbench_filter_buttons", {}) or {}).items():
            if not isinstance(btn, QToolButton):
                continue
            btn.blockSignals(True)
            btn.setChecked(str(mode_key) == selected)
            btn.blockSignals(False)
        self._apply_conflict_workbench_filter_button_styles()
        if refresh:
            self._update_conflict_workbench(self._last_realtime_hf_analyses_cache, force=True)
            self._update_sop_builder_workflow_status(self._last_realtime_hf_analyses_cache)

    def _workbench_row_matches_filter(
        self,
        *,
        diag: Dict[str, Any],
        policy: str,
    ) -> bool:
        mode = self._normalize_conflict_workbench_filter_mode(getattr(self, "_conflict_workbench_filter_mode", self.WB_FILTER_ALL))
        has_daily = bool(diag.get("daily_conflicts"))
        has_net = bool(diag.get("net_conflicts"))
        has_sop = bool(diag.get("sop_conflicts"))
        first_occurrence = bool(diag.get("first_occurrence_conflict"))
        needs_time = has_sop or (policy in {self.manager.CONFLICT_POLICY_NET, self.manager.CONFLICT_POLICY_DAILY} and first_occurrence)
        if mode == self.WB_FILTER_HF:
            return has_daily
        if mode == self.WB_FILTER_NET:
            return has_net
        if mode == self.WB_FILTER_SOP:
            return has_sop
        if mode == self.WB_FILTER_NEEDS_TIME:
            return needs_time
        return True

    def _conflict_workbench_action_state(self) -> Dict[str, int | bool]:
        rows_total = 0
        rows_policy_relevant = 0
        rows_timing_only = 0
        can_set_sop = 0
        can_set_net = 0
        can_set_daily = 0
        can_apply_defaults = 0
        for row_index, diag in list((self._conflict_workbench_diag_cache or {}).items()):
            if not isinstance(diag, dict):
                continue
            rows_total += 1
            has_daily = bool(diag.get("daily_conflicts"))
            has_net = bool(diag.get("net_conflicts"))
            has_sop = bool(diag.get("sop_conflicts"))
            policy_relevant = bool(has_daily or has_net)
            if policy_relevant:
                rows_policy_relevant += 1
            elif has_sop:
                rows_timing_only += 1
            policy = self._action_row_conflict_policy(int(row_index))
            default_policy = self._activation_row_builder_default_policy(diag)
            if policy_relevant and policy != self.manager.CONFLICT_POLICY_SOP:
                can_set_sop += 1
            if policy_relevant and policy != self.manager.CONFLICT_POLICY_NET:
                can_set_net += 1
            if policy_relevant and policy != self.manager.CONFLICT_POLICY_DAILY:
                can_set_daily += 1
            if policy != default_policy:
                can_apply_defaults += 1
        return {
            "rows_total": rows_total,
            "rows_policy_relevant": rows_policy_relevant,
            "rows_timing_only": rows_timing_only,
            "can_set_sop": can_set_sop,
            "can_set_net": can_set_net,
            "can_set_daily": can_set_daily,
            "can_apply_defaults": can_apply_defaults,
            "all_timing_only": bool(rows_total > 0 and rows_policy_relevant == 0 and rows_timing_only > 0),
        }

    def _apply_conflict_workbench_batch_button_styles(self) -> None:
        try:
            theme = resolve_theme(self.settings)
        except Exception:
            return
        styled_buttons = [
            (getattr(self, "workbench_set_sop_btn", None), "eligible_success"),
            (getattr(self, "workbench_set_net_btn", None), "eligible_warning"),
            (getattr(self, "workbench_set_daily_btn", None), "eligible_info"),
            (getattr(self, "workbench_apply_defaults_btn", None), "eligible_primary"),
        ]
        for btn, role in styled_buttons:
            if not isinstance(btn, QPushButton):
                continue
            btn.setStyleSheet(button_style(role if btn.isEnabled() else "muted", theme))

    def _update_conflict_workbench_batch_actions(self) -> None:
        state = self._conflict_workbench_action_state()
        rows_total = int(state.get("rows_total") or 0)
        rows_policy_relevant = int(state.get("rows_policy_relevant") or 0)
        all_timing_only = bool(state.get("all_timing_only"))

        btn_sop = getattr(self, "workbench_set_sop_btn", None)
        btn_net = getattr(self, "workbench_set_net_btn", None)
        btn_daily = getattr(self, "workbench_set_daily_btn", None)
        btn_defaults = getattr(self, "workbench_apply_defaults_btn", None)

        if isinstance(btn_sop, QPushButton):
            btn_sop.setEnabled(int(state.get("can_set_sop") or 0) > 0)
            if all_timing_only:
                btn_sop.setToolTip("Timing-only SOP overlaps require start-time adjustments; policy changes do not apply.")
            elif rows_total <= 0:
                btn_sop.setToolTip("No conflicts to update.")
            elif rows_policy_relevant <= 0:
                btn_sop.setToolTip("No HF/Net conflict rows are available for batch policy changes.")
            elif btn_sop.isEnabled():
                btn_sop.setToolTip("Set SOP Priority for all workbench rows with HF/Net conflicts.")
            else:
                btn_sop.setToolTip("Applicable rows are already set to SOP Priority.")
        if isinstance(btn_net, QPushButton):
            btn_net.setEnabled(int(state.get("can_set_net") or 0) > 0)
            if all_timing_only:
                btn_net.setToolTip("Timing-only SOP overlaps require start-time adjustments; policy changes do not apply.")
            elif rows_total <= 0:
                btn_net.setToolTip("No conflicts to update.")
            elif rows_policy_relevant <= 0:
                btn_net.setToolTip("No HF/Net conflict rows are available for batch policy changes.")
            elif btn_net.isEnabled():
                btn_net.setToolTip("Set Net Priority for all workbench rows with HF/Net conflicts.")
            else:
                btn_net.setToolTip("Applicable rows are already set to Net Priority.")
        if isinstance(btn_daily, QPushButton):
            btn_daily.setEnabled(int(state.get("can_set_daily") or 0) > 0)
            if all_timing_only:
                btn_daily.setToolTip("Timing-only SOP overlaps require start-time adjustments; policy changes do not apply.")
            elif rows_total <= 0:
                btn_daily.setToolTip("No conflicts to update.")
            elif rows_policy_relevant <= 0:
                btn_daily.setToolTip("No HF/Net conflict rows are available for batch policy changes.")
            elif btn_daily.isEnabled():
                btn_daily.setToolTip("Set Daily Priority for all workbench rows with HF/Net conflicts.")
            else:
                btn_daily.setToolTip("Applicable rows are already set to Daily Priority.")
        if isinstance(btn_defaults, QPushButton):
            btn_defaults.setEnabled(int(state.get("can_apply_defaults") or 0) > 0)
            if rows_total <= 0:
                btn_defaults.setToolTip("No conflicts to update.")
            elif btn_defaults.isEnabled():
                btn_defaults.setToolTip("Reset row policies in the workbench to the current SOP Builder Activation Conflict Defaults.")
            else:
                btn_defaults.setToolTip("Workbench row policies already match the current builder defaults.")

        self._apply_conflict_workbench_batch_button_styles()

    def _apply_conflict_workbench_policy_batch(self, mode: str) -> None:
        if self._current_category() != self.CAT_HF:
            return
        mode_key = str(mode or "").strip().upper()
        if mode_key == "NET":
            target_policy = self.manager.CONFLICT_POLICY_NET
        elif mode_key == "DAILY":
            target_policy = self.manager.CONFLICT_POLICY_DAILY
        else:
            target_policy = self.manager.CONFLICT_POLICY_SOP
            mode_key = "SOP"

        changed = 0
        for row_index, diag in list((self._conflict_workbench_diag_cache or {}).items()):
            if not isinstance(diag, dict):
                continue
            if mode_key == "NET" and not bool(diag.get("net_conflicts")):
                continue
            if mode_key == "DAILY" and not bool(diag.get("daily_conflicts")):
                continue
            if self._set_action_row_conflict_policy(
                int(row_index),
                target_policy,
                mark_dirty=False,
                schedule_refresh=False,
            ):
                changed += 1
        if changed > 0:
            self._mark_dirty()
            self._schedule_realtime_hf_conflict_check()
            self._update_conflict_workbench(self._last_realtime_hf_analyses_cache)
            return
        status_label = getattr(self, "conflict_workbench_status_label", None)
        if isinstance(status_label, QLabel):
            state = self._conflict_workbench_action_state()
            if bool(state.get("all_timing_only")):
                status_label.setText(
                    "Current workbench conflicts are timing-only SOP overlaps. Use Details or Save to review timing suggestions."
                )
            else:
                policy_name = "SOP Priority" if mode_key == "SOP" else ("Net Priority" if mode_key == "NET" else "Daily Priority")
                status_label.setText(f"No rows changed. Applicable workbench rows already use {policy_name}.")

    def _apply_conflict_workbench_builder_defaults(self) -> None:
        if self._current_category() != self.CAT_HF:
            return
        changed = 0
        for row_index, diag in list((self._conflict_workbench_diag_cache or {}).items()):
            if not isinstance(diag, dict):
                continue
            default_policy = self._activation_row_builder_default_policy(diag)
            if self._set_action_row_conflict_policy(
                int(row_index),
                default_policy,
                mark_dirty=False,
                schedule_refresh=False,
            ):
                changed += 1
        if changed > 0:
            self._mark_dirty()
            self._schedule_realtime_hf_conflict_check()
            self._update_conflict_workbench(self._last_realtime_hf_analyses_cache)
            return
        status_label = getattr(self, "conflict_workbench_status_label", None)
        if isinstance(status_label, QLabel):
            state = self._conflict_workbench_action_state()
            if bool(state.get("all_timing_only")):
                status_label.setText(
                    "Current workbench conflicts are timing-only SOP overlaps. Builder defaults do not resolve timing conflicts."
                )
            else:
                status_label.setText("No rows changed. Workbench row policies already match the current builder defaults.")

    def _open_conflict_workbench_details_for_button(self, btn: QToolButton) -> None:
        if not isinstance(btn, QToolButton):
            return
        try:
            row_index = int(btn.property("sop_row_index") or -1)
        except Exception:
            row_index = -1
        if row_index < 0:
            return
        self._show_inline_conflict_details_for_row(row_index)

    def _request_conflict_workbench_suggested_start_for_button(self, btn: QToolButton) -> None:
        if not isinstance(btn, QToolButton):
            return
        try:
            row_index = int(btn.property("sop_row_index") or -1)
        except Exception:
            row_index = -1
        if row_index < 0:
            return
        self._request_conflict_workbench_suggested_start(row_index)

    def _compute_conflict_workbench_suggested_start_utc(self, row_index: int) -> str:
        if self._current_category() != self.CAT_HF:
            return ""
        try:
            target_row = int(row_index)
        except Exception:
            return ""
        action_rows = self._collect_hf_actions_for_realtime()
        action_map = {int(r): dict(a) for r, a in action_rows if isinstance(a, dict)}
        action = action_map.get(target_row)
        if not isinstance(action, dict):
            return ""
        peers = [dict(other) for r, other in action_rows if int(r) != target_row and isinstance(other, dict)]
        group_name = str(action.get("group_name") or "").strip().upper()
        try:
            suggested_utc = self.manager.suggest_non_conflicting_start(
                action=action,
                operating_group=group_name,
                check_all_groups=True,
                peer_actions=peers,
            )
        except Exception as e:
            log.debug("SOP Builder: failed computing workbench suggested start for row %s: %s", target_row, e)
            suggested_utc = ""
        return self.manager._normalize_hhmm(suggested_utc or "")

    def _queue_conflict_workbench_auto_suggestions(self, row_indices: List[int]) -> None:
        if self._current_category() != self.CAT_HF:
            return
        pending = list(getattr(self, "_conflict_workbench_suggest_queue", []) or [])
        valid_rows = {int(r) for r in self._conflict_workbench_diag_cache.keys()}
        pending = [
            int(r)
            for r in pending
            if int(r) in valid_rows and not bool(self._conflict_workbench_suggested_start_utc.get(int(r)))
        ]
        for row_index in row_indices:
            try:
                row = int(row_index)
            except Exception:
                continue
            if row not in valid_rows:
                continue
            if bool(self._conflict_workbench_suggested_start_utc.get(row)):
                continue
            if row not in pending:
                pending.append(row)
        self._conflict_workbench_suggest_queue = pending
        timer = getattr(self, "_conflict_workbench_suggest_timer", None)
        if pending and isinstance(timer, QTimer) and not timer.isActive():
            self._conflict_workbench_suggest_timer.start()

    def _run_conflict_workbench_auto_suggest_step(self) -> None:
        if self._current_category() != self.CAT_HF:
            self._conflict_workbench_suggest_queue = []
            return
        queue = list(getattr(self, "_conflict_workbench_suggest_queue", []) or [])
        if not queue:
            self._conflict_workbench_suggest_queue = []
            return
        row_index = int(queue.pop(0))
        self._conflict_workbench_suggest_queue = queue
        if row_index not in self._conflict_workbench_diag_cache:
            if queue:
                self._conflict_workbench_suggest_timer.start()
            return
        if bool(self._conflict_workbench_suggested_start_utc.get(row_index)):
            if queue:
                self._conflict_workbench_suggest_timer.start()
            return
        suggested_utc = self._compute_conflict_workbench_suggested_start_utc(row_index)
        if suggested_utc:
            self._conflict_workbench_suggested_start_utc[row_index] = suggested_utc
            self._update_conflict_workbench(self._last_realtime_hf_analyses_cache, force=True)
        if self._conflict_workbench_suggest_queue:
            self._conflict_workbench_suggest_timer.start()

    def _apply_conflict_workbench_suggested_start_for_button(self, btn: QPushButton) -> None:
        if not isinstance(btn, QPushButton):
            return
        try:
            row_index = int(btn.property("sop_row_index") or -1)
        except Exception:
            row_index = -1
        if row_index < 0:
            return
        self._apply_conflict_workbench_suggested_start(row_index)

    def _request_conflict_workbench_suggested_start(self, row_index: int) -> None:
        if self._current_category() != self.CAT_HF:
            return
        try:
            target_row = int(row_index)
        except Exception:
            return
        suggested_utc = self._compute_conflict_workbench_suggested_start_utc(target_row)
        if not suggested_utc:
            status_label = getattr(self, "conflict_workbench_status_label", None)
            if isinstance(status_label, QLabel):
                status_label.setText(f"Could not compute a suggested start for row {target_row + 1}.")
            return
        self._conflict_workbench_suggested_start_utc[int(target_row)] = suggested_utc
        display_txt = self._display_start_hhmm_from_utc(suggested_utc, show_local=self._show_local)
        status_label = getattr(self, "conflict_workbench_status_label", None)
        if isinstance(status_label, QLabel):
            status_label.setText(f"Suggested start for row {target_row + 1}: {display_txt}. Click Apply to use it.")
        self._update_conflict_workbench(self._last_realtime_hf_analyses_cache, force=True)

    def _apply_conflict_workbench_suggested_start(self, row_index: int) -> None:
        if self._current_category() != self.CAT_HF:
            return
        try:
            target_row = int(row_index)
        except Exception:
            return
        suggested_utc = self.manager._normalize_hhmm(self._conflict_workbench_suggested_start_utc.get(target_row) or "")
        if not suggested_utc:
            self._request_conflict_workbench_suggested_start(target_row)
            suggested_utc = self.manager._normalize_hhmm(self._conflict_workbench_suggested_start_utc.get(target_row) or "")
            if not suggested_utc:
                return

        start_edit = self._action_row_start_edit(target_row)
        end_edit = self.actions_table.cellWidget(target_row, self.COL_END)
        duration_combo = self.actions_table.cellWidget(target_row, self.COL_DURATION)
        if not isinstance(start_edit, QLineEdit):
            return
        duration_minutes = 60
        if isinstance(duration_combo, QComboBox):
            try:
                duration_minutes = int(duration_combo.currentData() or 60)
            except Exception:
                duration_minutes = 60
        if duration_minutes not in {30, 60}:
            duration_minutes = 60
        end_utc = self._add_minutes_hhmm(suggested_utc, duration_minutes)
        start_display = self._display_start_hhmm_from_utc(suggested_utc, show_local=self._show_local)
        end_display = self._display_start_hhmm_from_utc(end_utc, show_local=self._show_local)
        start_edit.setText(start_display)
        if isinstance(end_edit, QLineEdit):
            end_edit.setText(end_display)
        status_label = getattr(self, "conflict_workbench_status_label", None)
        if isinstance(status_label, QLabel):
            status_label.setText(f"Applied suggested start to row {target_row + 1}: {start_display}.")
        self._schedule_realtime_hf_conflict_check()

    def _update_conflict_workbench(
        self,
        analyses: List[Tuple[int, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]] | None,
        *,
        force: bool = False,
    ) -> None:
        box = getattr(self, "conflict_workbench_box", None)
        table = getattr(self, "conflict_workbench_table", None)
        status_label = getattr(self, "conflict_workbench_status_label", None)
        if not isinstance(box, QGroupBox) or not isinstance(table, QTableWidget) or not isinstance(status_label, QLabel):
            return
        if self._current_category() != self.CAT_HF:
            self._set_conflict_workbench_expanded(False, refresh=False)
            table.setRowCount(0)
            self._conflict_workbench_diag_cache = {}
            self._conflict_workbench_suggested_start_utc = {}
            self._conflict_workbench_suggest_queue = []
            try:
                self._conflict_workbench_suggest_timer.stop()
            except Exception:
                pass
            self._conflict_workbench_signature = None
            self._update_conflict_workbench_header_summary(total_rows=0, visible_rows=0, needs_time_rows=0)
            return

        rows_all: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
        for row_index, action, diag, _peers in list(analyses or []):
            if not isinstance(diag, dict) or not bool(diag.get("has_conflict")):
                continue
            rows_all.append((int(row_index), dict(action or {}), dict(diag or {})))
        rows: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
        for row_index, action_row, diag_row in rows_all:
            policy_for_filter = self._action_row_conflict_policy(int(row_index))
            if not self._workbench_row_matches_filter(diag=diag_row, policy=policy_for_filter):
                continue
            rows.append((int(row_index), dict(action_row), dict(diag_row)))

        pending_rows = max(0, int(self.actions_table.rowCount()) - len(list(analyses or [])))
        signature_rows: List[Tuple[Any, ...]] = []
        for row_index, action_for_sig, diag in rows:
            policy = self._action_row_conflict_policy(row_index)
            signature_rows.append(
                (
                    int(row_index),
                    policy,
                    str(action_for_sig.get("daily_start_utc") or ""),
                    str(action_for_sig.get("daily_end_utc") or ""),
                    str(diag.get("daily_summary") or ""),
                    str(diag.get("net_summary") or ""),
                    str(diag.get("sop_summary") or ""),
                    bool(diag.get("first_occurrence_conflict")),
                    bool(diag.get("daily_conflicts")),
                    bool(diag.get("net_conflicts")),
                    bool(diag.get("sop_conflicts")),
                )
            )
        signature = (
            self._normalize_conflict_workbench_filter_mode(getattr(self, "_conflict_workbench_filter_mode", self.WB_FILTER_ALL)),
            int(len(rows_all)),
            tuple(signature_rows),
            int(pending_rows),
        )
        if (not force) and signature == getattr(self, "_conflict_workbench_signature", None):
            return
        if signature != getattr(self, "_conflict_workbench_signature", None):
            self._conflict_workbench_suggested_start_utc = {}
            self._conflict_workbench_suggest_queue = []
            try:
                self._conflict_workbench_suggest_timer.stop()
            except Exception:
                pass
        self._conflict_workbench_signature = signature

        def _readonly_item(text: str) -> QTableWidgetItem:
            item = QTableWidgetItem(str(text or ""))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            return item

        self._conflict_workbench_updating = True
        try:
            table.setRowCount(len(rows))
            self._conflict_workbench_diag_cache = {int(row_index): dict(diag) for row_index, _action, diag in rows}
            needs_time_count = 0
            rows_needing_suggest: List[int] = []
            for t_row, (row_index, action, diag) in enumerate(rows):
                policy = self._action_row_conflict_policy(row_index)
                has_sop_conflict = bool(diag.get("sop_conflicts"))
                first_occurrence = bool(diag.get("first_occurrence_conflict"))
                needs_time = has_sop_conflict or (
                    policy in {self.manager.CONFLICT_POLICY_NET, self.manager.CONFLICT_POLICY_DAILY} and first_occurrence
                )
                if needs_time:
                    needs_time_count += 1
                next_step = "Use Suggest/Apply"
                if has_sop_conflict:
                    next_step = "Use Suggest/Apply (SOP overlap)"
                elif policy in {self.manager.CONFLICT_POLICY_NET, self.manager.CONFLICT_POLICY_DAILY} and first_occurrence:
                    next_step = "Use Suggest/Apply (first occurrence)"
                else:
                    next_step = "Policy ready"

                action_label = str(action.get("action_label") or action.get("action_key") or "Action").strip() or "Action"
                group_name = str(action.get("group_name") or "").strip().upper()
                table.setItem(t_row, self.WB_COL_ROW, _readonly_item(str(int(row_index) + 1)))
                table.setItem(t_row, self.WB_COL_ACTION, _readonly_item(action_label))
                table.setItem(t_row, self.WB_COL_GROUP, _readonly_item(group_name))
                table.setItem(t_row, self.WB_COL_HF, _readonly_item(str(diag.get("daily_summary") or "None")))
                table.setItem(t_row, self.WB_COL_NET, _readonly_item(str(diag.get("net_summary") or "None")))
                table.setItem(t_row, self.WB_COL_SOP, _readonly_item(str(diag.get("sop_summary") or "None")))
                table.setItem(t_row, self.WB_COL_NEXT, _readonly_item(next_step))

                policy_combo = QComboBox(table)
                policy_combo.addItem("SOP Priority", self.manager.CONFLICT_POLICY_SOP)
                policy_combo.addItem("Net Priority", self.manager.CONFLICT_POLICY_NET)
                policy_combo.addItem("Daily Priority", self.manager.CONFLICT_POLICY_DAILY)
                policy_combo.setProperty("sop_row_index", int(row_index))
                policy_combo.setCurrentIndex(self._conflict_policy_combo_index(policy_combo, policy))
                policy_combo.currentIndexChanged.connect(lambda _=0, c=policy_combo: self._workbench_policy_combo_changed(c))
                table.setCellWidget(t_row, self.WB_COL_POLICY, policy_combo)

                table.removeCellWidget(t_row, self.WB_COL_SUGGESTED)
                table.removeCellWidget(t_row, self.WB_COL_APPLY)
                if needs_time:
                    suggested_btn = QToolButton(table)
                    suggested_btn.setProperty("sop_row_index", int(row_index))
                    suggested_btn.clicked.connect(
                        lambda _=False, b=suggested_btn: self._request_conflict_workbench_suggested_start_for_button(b)
                    )
                    apply_btn = QPushButton("Apply", table)
                    apply_btn.setProperty("sop_row_index", int(row_index))
                    apply_btn.clicked.connect(
                        lambda _=False, b=apply_btn: self._apply_conflict_workbench_suggested_start_for_button(b)
                    )
                    suggested_utc = self.manager._normalize_hhmm(
                        self._conflict_workbench_suggested_start_utc.get(int(row_index)) or ""
                    )
                    if suggested_utc:
                        suggested_display = self._display_start_hhmm_from_utc(suggested_utc, show_local=self._show_local)
                        suggested_btn.setText(suggested_display)
                        suggested_btn.setToolTip("Recompute suggested non-conflicting start.")
                        apply_btn.setEnabled(True)
                        apply_btn.setToolTip("Apply the suggested start to the SOP action row.")
                    else:
                        rows_needing_suggest.append(int(row_index))
                        suggested_btn.setText("Computing...")
                        suggested_btn.setToolTip("Auto-computing suggested start. Click to compute immediately.")
                        apply_btn.setEnabled(False)
                        apply_btn.setToolTip("Waiting for suggested start.")
                    table.setCellWidget(t_row, self.WB_COL_SUGGESTED, suggested_btn)
                    table.setCellWidget(t_row, self.WB_COL_APPLY, apply_btn)
                else:
                    table.setItem(t_row, self.WB_COL_SUGGESTED, _readonly_item("Not needed"))
                    table.setItem(t_row, self.WB_COL_APPLY, _readonly_item(""))

                detail_btn = QToolButton(table)
                detail_btn.setText("Details")
                detail_btn.setProperty("sop_row_index", int(row_index))
                detail_btn.clicked.connect(lambda _=False, b=detail_btn: self._open_conflict_workbench_details_for_button(b))
                table.setCellWidget(t_row, self.WB_COL_DETAILS, detail_btn)

            if not rows:
                filter_mode = self._normalize_conflict_workbench_filter_mode(
                    getattr(self, "_conflict_workbench_filter_mode", self.WB_FILTER_ALL)
                )
                filter_labels = {
                    self.WB_FILTER_ALL: "All",
                    self.WB_FILTER_HF: "HF",
                    self.WB_FILTER_NET: "Net",
                    self.WB_FILTER_SOP: "SOP",
                    self.WB_FILTER_NEEDS_TIME: "Needs Time",
                }
                filter_label = filter_labels.get(filter_mode, "All")
                if rows_all and filter_mode != self.WB_FILTER_ALL:
                    status_label.setText(
                        f"No conflicts match filter '{filter_label}'. Showing 0 of {len(rows_all)} conflicting row(s)."
                    )
                elif pending_rows > 0:
                    status_label.setText(
                        "No actionable conflicts yet. Complete HF action row fields to evaluate conflicts."
                    )
                else:
                    status_label.setText("No HF conflicts detected.")
            else:
                status_text = (
                    f"Showing {len(rows)} of {len(rows_all)} conflicting row(s)"
                    if len(rows) != len(rows_all)
                    else f"{len(rows)} conflicting row(s)"
                )
                if needs_time_count > 0:
                    status_text += f" | {needs_time_count} still need timing changes at Save"
                state = self._conflict_workbench_action_state()
                if bool(state.get("all_timing_only")):
                    status_text += " | Timing-only conflicts: batch priority buttons do not apply"
                if pending_rows > 0:
                    status_text += f" | {pending_rows} row(s) pending validation"
                status_label.setText(status_text)

            self._update_conflict_workbench_header_summary(
                total_rows=len(rows_all),
                visible_rows=len(rows),
                needs_time_rows=needs_time_count,
            )
            self._update_conflict_workbench_batch_actions()
            self._queue_conflict_workbench_auto_suggestions(rows_needing_suggest)
        finally:
            self._conflict_workbench_updating = False

    def _schedule_realtime_hf_conflict_check(self, *_args) -> None:
        if getattr(self, "_loading_ui", False):
            return
        if getattr(self, "_suppress_realtime_conflict_checks", False):
            return
        if self._current_category() != self.CAT_HF:
            return
        timer = getattr(self, "_realtime_conflict_timer", None)
        if timer is None:
            return
        timer.start()

    def _collect_hf_action_for_realtime(self, row_index: int) -> Dict[str, Any] | None:
        if row_index < 0 or row_index >= self.actions_table.rowCount():
            return None
        if self._current_category() != self.CAT_HF:
            return None
        group_combo = self.actions_table.cellWidget(row_index, self.COL_GROUP)
        cond_widget = self.actions_table.cellWidget(row_index, self.COL_COND)
        resource_combo = self.actions_table.cellWidget(row_index, self.COL_RESOURCE)
        action_combo = self.actions_table.cellWidget(row_index, self.COL_ACTION)
        bandfreq_combo = self.actions_table.cellWidget(row_index, self.COL_BANDFREQ)
        start_edit = self._action_row_start_edit(row_index)
        duration_combo = self.actions_table.cellWidget(row_index, self.COL_DURATION)
        interval_combo = self.actions_table.cellWidget(row_index, self.COL_INTERVAL)
        contact_combo = self.actions_table.cellWidget(row_index, self.COL_CONTACT)
        target_combo = self.actions_table.cellWidget(row_index, self.COL_CONTACT_TARGET)
        desc_edit = self.actions_table.cellWidget(row_index, self.COL_DESC)
        if not isinstance(group_combo, QComboBox) or not isinstance(resource_combo, QComboBox):
            return None
        if not isinstance(action_combo, QComboBox) or not isinstance(bandfreq_combo, QComboBox):
            return None
        if not isinstance(start_edit, QLineEdit) or not isinstance(duration_combo, QComboBox):
            return None
        if not isinstance(interval_combo, QComboBox) or not isinstance(contact_combo, QComboBox):
            return None
        if not isinstance(target_combo, QComboBox) or not isinstance(desc_edit, QLineEdit):
            return None

        group_name = group_combo.currentText().strip().upper()
        resource = resource_combo.currentText().strip()
        action_key = str(action_combo.currentData() or "").strip()
        action_label = action_combo.currentText().strip()
        band, freq = self._split_band_freq(bandfreq_combo.currentText().strip())
        start_display = start_edit.text().strip()
        if not (group_name and resource and action_key and band and freq and self._is_valid_hhmm(start_display)):
            return None
        if not self._hf_group_uses_condition_levels(group_name):
            return None
        start_utc = self._utc_start_hhmm_from_display(start_display, show_local=self._show_local)
        duration_minutes = int(duration_combo.currentData() or 60)
        if duration_minutes not in {30, 60}:
            duration_minutes = 60
        end_utc = self._add_minutes_hhmm(start_utc, duration_minutes)
        interval_minutes, phase_minutes = self._parse_interval_spec(interval_combo.currentText())
        if interval_minutes <= 0:
            return None
        condition_levels = self._condition_levels_from_widget(cond_widget)
        contact_rule = str(contact_combo.currentData() or "none").strip()
        contact_target = str(target_combo.currentText() or "").strip().upper()
        if contact_target == "ANY (ROLE MATCH)":
            contact_target = self.ANY_ROLE_TOKEN
        return {
            "id": int(group_combo.property("action_id") or 0),
            "group_name": group_name,
            "condition_levels": condition_levels,
            "band": band,
            "frequency": freq,
            "software": resource,
            "action_key": action_key,
            "action_label": action_label or action_key,
            "enabled": True,
            "daily_start_utc": start_utc,
            "daily_end_utc": end_utc,
            "duration_minutes": duration_minutes,
            "interval_minutes": interval_minutes,
            "interval_phase_minutes": phase_minutes,
            "interval_hours": max(1, int((interval_minutes + 59) // 60)),
            "conflict_policy": self.manager._normalize_conflict_policy(group_combo.property("conflict_policy")),
            "schedule_applied": bool(group_combo.property("schedule_applied")),
            "description": desc_edit.text().strip(),
            "contact_rule": contact_rule,
            "contact_target": contact_target,
            "sort_order": row_index,
        }

    def _collect_hf_actions_for_realtime(self) -> List[Tuple[int, Dict[str, Any]]]:
        out: List[Tuple[int, Dict[str, Any]]] = []
        for r in range(self.actions_table.rowCount()):
            action = self._collect_hf_action_for_realtime(r)
            if not action:
                continue
            out.append((r, action))
        return out

    def _set_inline_conflict_badge(self, row_index: int, status: str, tooltip: str = "") -> None:
        badge = self.actions_table.cellWidget(row_index, self.COL_CONFLICT)
        if not isinstance(badge, (QLabel, QToolButton)):
            return
        theme = resolve_theme(self.settings)
        status_key = str(status or "").strip().lower()
        if status_key == "conflict":
            bg = theme.get("warning", "#c28c18")
            fg = theme.get("button_text", "#ffffff")
            label = "Conflict"
        elif status_key == "ok":
            bg = theme.get("success", "#17863a")
            fg = theme.get("button_text", "#ffffff")
            label = "OK"
        elif status_key == "local":
            bg = theme.get("panel_alt", "#334155")
            fg = theme.get("text_muted", "#b9c4d2")
            label = "Local"
        else:
            bg = theme.get("panel_alt", "#334155")
            fg = theme.get("text_muted", "#b9c4d2")
            label = "Pending"
        badge.setText(label)
        badge.setToolTip(str(tooltip or "").strip())
        if isinstance(badge, QLabel):
            badge.setAlignment(Qt.AlignCenter)
        if isinstance(badge, QToolButton):
            badge.setEnabled(status_key == "conflict")
            try:
                badge.setCursor(Qt.PointingHandCursor if status_key == "conflict" else Qt.ArrowCursor)
            except Exception:
                pass
        badge.setStyleSheet(
            (
                "QLabel, QToolButton {"
                f"background: {bg};"
                f"color: {fg};"
                "padding: 2px 8px;"
                "border-radius: 10px;"
                "font-weight: 600;"
                "border: none;"
                "}"
                "QToolButton:disabled {"
                f"background: {bg};"
                f"color: {fg};"
                "border: none;"
                "}"
            )
        )
        if isinstance(badge, QToolButton):
            badge.setProperty("conflict_status", status_key)
        cards = getattr(self, "sop_action_cards_container", None)
        if cards is not None:
            for card in cards.findChildren(QGroupBox, "sopActionCard"):
                card_index = card.property("sop_action_index")
                if card_index is None or int(card_index) != row_index:
                    continue
                card_badge = card.findChild(QLabel, "sopCardConflict")
                if card_badge is not None:
                    card_badge.setText(label)
                    card_badge.setToolTip(str(tooltip or "").strip())
                break

    def _show_inline_conflict_details_for_button(self, btn: QToolButton) -> None:
        for r in range(self.actions_table.rowCount()):
            if self.actions_table.cellWidget(r, self.COL_CONFLICT) is btn:
                self._show_inline_conflict_details_for_row(r)
                return

    def _format_conflict_overlap_span(self, start_iso: str, end_iso: str) -> str:
        try:
            start_dt = datetime.datetime.fromisoformat(str(start_iso or "").strip())
            end_dt = datetime.datetime.fromisoformat(str(end_iso or "").strip())
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)
            else:
                start_dt = start_dt.astimezone(datetime.timezone.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=datetime.timezone.utc)
            else:
                end_dt = end_dt.astimezone(datetime.timezone.utc)
            if self._show_local:
                tz_name = self.settings.get("timezone", "UTC") or "UTC"
                tz = get_timezone(tz_name)
                start_dt = start_dt.astimezone(tz)
                end_dt = end_dt.astimezone(tz)
                tz_tag = self._tz_short_name()
            else:
                tz_tag = "UTC"
            if start_dt.date() == end_dt.date():
                return f"{start_dt.strftime('%a %H:%M')}-{end_dt.strftime('%H:%M')} {tz_tag}"
            return f"{start_dt.strftime('%a %H:%M')}-{end_dt.strftime('%a %H:%M')} {tz_tag}"
        except Exception:
            return f"{str(start_iso or '').strip()} to {str(end_iso or '').strip()}".strip()

    def _build_conflict_detail_lines(
        self,
        title: str,
        rows: List[Dict[str, Any]],
        overflow_count: int = 0,
    ) -> List[str]:
        if not rows:
            return [f"{title}: none"]
        lines: List[str] = [f"{title}:"]
        for row in rows[:10]:
            span = self._format_conflict_overlap_span(
                str(row.get("overlap_start_utc") or ""),
                str(row.get("overlap_end_utc") or ""),
            )
            other_label = str(row.get("other_label") or "").strip() or "Schedule row"
            other_group = str(row.get("other_group") or "").strip().upper()
            other_band = str(row.get("other_band") or "").strip().upper()
            other_freq = str(row.get("other_frequency") or "").strip()
            freq_text = " ".join([t for t in [other_band, other_freq] if t]).strip()
            label_text = other_label
            if other_group and other_group not in label_text.upper():
                label_text = f"{other_group}: {other_label}"
            if freq_text:
                label_text = f"{label_text} [{freq_text}]"
            lines.append(f"  - {span} | {label_text}")
        hidden = max(0, len(rows) - 10) + max(0, int(overflow_count or 0))
        if hidden > 0:
            lines.append(f"  - ...and {hidden} more")
        return lines

    def _show_inline_conflict_details_for_row(self, row_index: int) -> None:
        badge = self.actions_table.cellWidget(row_index, self.COL_CONFLICT)
        if isinstance(badge, QToolButton):
            if str(badge.property("conflict_status") or "").strip().lower() != "conflict":
                return

        if self._current_category() != self.CAT_HF:
            QMessageBox.information(self, "SOP Conflict Details", "Local Comms rows do not use HF conflict checks.")
            return

        action_rows = self._collect_hf_actions_for_realtime()
        action_map = {r: a for r, a in action_rows}
        action = action_map.get(int(row_index))
        if not isinstance(action, dict):
            QMessageBox.information(
                self,
                "SOP Conflict Details",
                "Complete Group, Resource, Action, Band-Freq, Start, Duration, and Interval to review conflicts.",
            )
            return
        peers = [dict(other_action) for other_row, other_action in action_rows if int(other_row) != int(row_index)]
        diag = self.manager.detect_action_conflicts(
            action=action,
            operating_group=str(action.get("group_name") or "").strip().upper(),
            horizon_days=7,
            check_all_groups=True,
            peer_actions=peers,
            include_details=True,
        )
        if not bool(diag.get("has_conflict")):
            QMessageBox.information(self, "SOP Conflict Details", "No Daily/Net/SOP conflicts detected.")
            self._set_inline_conflict_badge(row_index, "ok", "No Daily/Net/SOP conflicts detected.")
            return

        action_label = str(action.get("action_label") or action.get("action_key") or "Action").strip() or "Action"
        group_name = str(action.get("group_name") or "").strip().upper()
        band = str(action.get("band") or "").strip().upper()
        freq = str(action.get("frequency") or "").strip()
        policy = self.manager._normalize_conflict_policy(action.get("conflict_policy"))

        daily_details = list(diag.get("daily_details") or [])
        net_details = list(diag.get("net_details") or [])
        sop_details = list(diag.get("sop_details") or [])
        daily_over = int(diag.get("daily_detail_overflow") or 0)
        net_over = int(diag.get("net_detail_overflow") or 0)
        sop_over = int(diag.get("sop_detail_overflow") or 0)

        lines: List[str] = []
        lines.extend(self._build_conflict_detail_lines("HF Schedule", daily_details, daily_over))
        lines.append("")
        lines.extend(self._build_conflict_detail_lines("Net Schedule", net_details, net_over))
        lines.append("")
        lines.extend(self._build_conflict_detail_lines("SOP Actions", sop_details, sop_over))
        lines.append("")
        lines.append("Recommended actions:")
        if daily_details:
            lines.append("  - HF Schedule: adjust SOP Start/Duration, or change band/frequency.")
        if net_details:
            lines.append("  - Net Schedule: adjust SOP timing/frequency, or choose Net/SOP Priority for intentional overlaps.")
        if sop_details:
            lines.append("  - SOP Actions: move one action time, or intentionally align to the same band/frequency.")
        if policy in {self.manager.CONFLICT_POLICY_NET, self.manager.CONFLICT_POLICY_DAILY} and bool(diag.get("first_occurrence_conflict")):
            policy_name = "Net Priority" if policy == self.manager.CONFLICT_POLICY_NET else "Daily Priority"
            lines.append(f"  - Current policy is {policy_name}; first-occurrence conflicts can block Save.")
        lines.append("  - Same-frequency overlaps are allowed and are not shown here.")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("SOP Conflict Details")
        heading = f"Row {int(row_index) + 1}: {action_label}"
        context = " ".join([t for t in [band, freq] if t]).strip()
        if group_name:
            heading += f" ({group_name})"
        if context:
            heading += f" [{context}]"
        box.setText(heading)
        box.setInformativeText("\n".join(lines))
        box.addButton(QMessageBox.Close)
        box.exec()

    def _refresh_inline_conflict_badges(
        self,
    ) -> List[Tuple[int, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]]:
        if self._current_category() != self.CAT_HF:
            self._last_realtime_hf_analyses_cache = []
            for r in range(self.actions_table.rowCount()):
                self._set_inline_conflict_badge(r, "local", "Local Comms actions do not use HF conflict checks.")
            self._update_activation_conflict_summary([])
            self._update_conflict_workbench([])
            return []

        action_rows = self._collect_hf_actions_for_realtime()
        action_map = {r: a for r, a in action_rows}
        out: List[Tuple[int, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]] = []

        for row_index in range(self.actions_table.rowCount()):
            action = action_map.get(row_index)
            if not action:
                self._set_inline_conflict_badge(
                    row_index,
                    "pending",
                    "Complete Group, Resource, Action, Band-Freq, Start, Duration, and Interval to validate conflicts.",
                )
                continue
            peers = [dict(other_action) for other_row, other_action in action_rows if other_row != row_index]
            diag = self.manager.detect_action_conflicts(
                action=action,
                operating_group=str(action.get("group_name") or "").strip().upper(),
                horizon_days=7,
                check_all_groups=True,
                peer_actions=peers,
            )
            if bool(diag.get("has_conflict")):
                tooltip_lines = [
                    f"HF Schedule: {diag.get('daily_summary') or 'None'}",
                    f"Net Schedule: {diag.get('net_summary') or 'None'}",
                    f"SOP Actions: {diag.get('sop_summary') or 'None'}",
                    "Click Conflict for details.",
                ]
                self._set_inline_conflict_badge(row_index, "conflict", "\n".join(tooltip_lines))
            else:
                self._set_inline_conflict_badge(row_index, "ok", "No Daily/Net/SOP conflicts detected.")
            out.append((row_index, action, diag, peers))
        self._last_realtime_hf_analyses_cache = list(out)
        self._update_activation_conflict_summary(out)
        self._update_conflict_workbench(out)
        return out

    def _run_realtime_hf_conflict_check(self) -> None:
        if getattr(self, "_loading_ui", False):
            return
        if getattr(self, "_suppress_realtime_conflict_checks", False):
            return
        with perf_span(
            "sop.realtime_conflict_check",
            settings=self.settings,
            min_ms=10.0,
            meta={"rows": int(self.actions_table.rowCount())},
        ):
            analyses = self._refresh_inline_conflict_badges()
            if not analyses:
                self._last_realtime_conflict_signature = None
                return
            conflict_signatures: List[Tuple[Any, ...]] = []
            for row_index, action, diag, _peers in analyses:
                if not bool(diag.get("has_conflict")):
                    continue
                conflict_signatures.append(
                    (
                        int(row_index),
                        str(action.get("action_key") or ""),
                        str(action.get("group_name") or ""),
                        str(action.get("daily_start_utc") or ""),
                        str(action.get("daily_end_utc") or ""),
                        str(diag.get("daily_summary") or ""),
                        str(diag.get("net_summary") or ""),
                        str(diag.get("sop_summary") or ""),
                    )
                )
            self._last_realtime_conflict_signature = tuple(conflict_signatures) if conflict_signatures else None

    def _apply_accessibility_width_guards(self) -> None:
        buttons = [
            self.time_toggle_btn,
            self.new_btn,
            self.save_btn,
            self.delete_btn,
            self.export_pdf_btn,
            self.export_import_btn,
            self.add_row_btn,
            getattr(self, "workbench_set_sop_btn", None),
            getattr(self, "workbench_set_net_btn", None),
            getattr(self, "workbench_set_daily_btn", None),
            getattr(self, "workbench_apply_defaults_btn", None),
            getattr(self, "activation_defaults_toggle_btn", None),
            getattr(self, "conflict_workbench_toggle_btn", None),
        ]
        for _mode_key, _btn in dict(getattr(self, "_conflict_workbench_filter_buttons", {}) or {}).items():
            buttons.append(_btn)
        for btn in buttons:
            if btn is None:
                continue
            txt = str(btn.text() or "").replace("&", "").strip()
            if not txt:
                continue
            try:
                needed = int(btn.fontMetrics().horizontalAdvance(txt) + 30)
            except Exception:
                continue
            try:
                btn.setMinimumWidth(max(100, min(360, needed)))
            except Exception:
                pass
        apply_text_size_accessibility_guards(self, include_widths=False)

    def _schedule_layer_sync_refresh(self) -> None:
        return

    def _refresh_layer_sync_hint(self) -> None:
        return

    def _update_clock_labels(self) -> None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        now_local = now_utc.astimezone(tz)
        self.utc_label.setText(f"UTC: {now_utc.strftime('%Y-%m-%d %H:%M')}")
        self.local_label.setText(f"Local: {now_local.strftime('%Y-%m-%d %H:%M')}")
        self.time_toggle_btn.setText("Times: Local" if self._show_local else "Times: UTC")
        self._update_action_time_headers()
        self._update_time_toggle_style()

    def _update_action_time_headers(self) -> None:
        tz_short = "UTC" if not self._show_local else self._tz_short_name()
        start_item = self.actions_table.horizontalHeaderItem(self.COL_START)
        if start_item is None:
            start_item = QTableWidgetItem()
            self.actions_table.setHorizontalHeaderItem(self.COL_START, start_item)
        start_item.setText(f"Start ({tz_short})")
        end_item = self.actions_table.horizontalHeaderItem(self.COL_END)
        if end_item is None:
            end_item = QTableWidgetItem()
            self.actions_table.setHorizontalHeaderItem(self.COL_END, end_item)
        end_item.setText(f"End ({tz_short})")

    def _toggle_time_view(self) -> None:
        """Change presentation only; draft UTC timing and dirty state stay intact."""
        self._show_local = not self._show_local
        self.time_toggle_btn.setText("Times: Local" if self._show_local else "Times: UTC")
        self._update_time_toggle_style()
        if not getattr(self, "_summary_drafts_pending", False):
            for row, draft in enumerate(self._action_drafts.rows()):
                for widget, utc_value in ((self._action_row_start_edit(row), draft.daily_start_utc),
                                          (self.actions_table.cellWidget(row, self.COL_END), draft.daily_end_utc)):
                    if isinstance(widget, QLineEdit):
                        widget.blockSignals(True)
                        widget.setText(self._summary_display_start(utc_value))
                        widget.blockSignals(False)
        self._render_sop_action_cards()

    def _refresh_reference_data(self) -> None:
        data = self.settings.all()
        self._summary_timezone = get_timezone(str(data.get("timezone") or "UTC"))
        og = data.get("operating_groups", [])
        self._operating_groups = [g for g in og if isinstance(g, dict)]
        self._load_local_net_profiles_from_data(data)
        self._invalidate_dynamic_option_caches()
        self._refresh_all_rows_dynamic_options()

    def _hf_group_condition_meta(self) -> Dict[str, Tuple[bool, int | None]]:
        cached = self._hf_group_condition_meta_cache
        if cached is not None:
            return dict(cached)
        out: Dict[str, Tuple[bool, int | None]] = {}
        for row in (self._operating_groups or []):
            group = str(row.get("group", "")).strip().upper()
            if not group:
                continue
            prev_enabled, prev_level = out.get(group, (False, None))
            use_levels = bool(row.get("use_condition_levels", False))
            level_val = prev_level
            if use_levels:
                try:
                    parsed_level = int(row.get("condition_level", 0) or 0)
                except Exception:
                    parsed_level = 0
                if 1 <= parsed_level <= 5:
                    if level_val is None or parsed_level < level_val:
                        level_val = parsed_level
            out[group] = (bool(prev_enabled or use_levels), level_val)
        self._hf_group_condition_meta_cache = dict(out)
        return dict(out)

    def _hf_group_uses_condition_levels(self, group: str) -> bool:
        grp = str(group or "").strip().upper()
        if not grp:
            return False
        enabled, _level = self._hf_group_condition_meta().get(grp, (False, None))
        return bool(enabled)

    def _condition_level_selector_values(self) -> List[str]:
        cached = self._condition_level_selector_values_cache
        if cached is not None:
            return list(cached)
        values: List[str] = ["ALL", "1", "2", "3", "4", "5"]
        self._condition_level_selector_values_cache = list(values)
        return list(values)

    def _condition_levels_from_widget(self, widget: object) -> str:
        if isinstance(widget, _ConditionLevelsMultiCombo):
            return self.manager._normalize_condition_levels(widget.normalized_value())
        if isinstance(widget, QComboBox):
            raw = widget.currentData()
            if raw is None:
                raw = widget.currentText()
            return self.manager._normalize_condition_levels(str(raw or "").strip())
        if isinstance(widget, QLineEdit):
            return self.manager._normalize_condition_levels(widget.text().strip())
        return "ALL"

    def _refresh_action_row_condition_widget(
        self,
        *,
        category: str,
        group_name: str,
        cond_widget: object,
    ) -> None:
        if isinstance(cond_widget, _ConditionLevelsMultiCombo):
            normalized_current = self._condition_levels_from_widget(cond_widget)
            cond_widget.set_normalized_value(normalized_current, emit=False)
            if category != self.CAT_HF:
                cond_widget.set_normalized_value("ALL", emit=False)
                cond_widget.setEnabled(False)
                cond_widget.setToolTip("Condition levels apply to HF action rows only.")
            elif not self._hf_group_uses_condition_levels(group_name):
                cond_widget.set_normalized_value("ALL", emit=False)
                cond_widget.setEnabled(False)
                cond_widget.setToolTip(
                    "Group must have 'Use Condition Levels' enabled in Configuration for HF SOP actions."
                )
            else:
                cond_widget.setEnabled(True)
                cond_widget.setToolTip("Applies only when the group's current condition level matches this selection.")
            self._fit_combo_popup(cond_widget)
            return
        if isinstance(cond_widget, QLineEdit):
            if category == self.CAT_HF:
                if not cond_widget.text().strip():
                    cond_widget.setText("ALL")
                cond_widget.setEnabled(self._hf_group_uses_condition_levels(group_name))
                return
            cond_widget.setText("ALL")
            cond_widget.setEnabled(False)
            return
        if not isinstance(cond_widget, QComboBox):
            return

        normalized_current = self._condition_levels_from_widget(cond_widget)
        values = self._condition_level_selector_values()
        cond_widget.blockSignals(True)
        cond_widget.clear()
        for val in values:
            cond_widget.addItem(val, val)
        if normalized_current not in values:
            normalized_current = "ALL"
        idx = cond_widget.findData(normalized_current)
        cond_widget.setCurrentIndex(idx if idx >= 0 else 0)
        cond_widget.setEditable(False)

        if category != self.CAT_HF:
            idx_all = cond_widget.findData("ALL")
            cond_widget.setCurrentIndex(idx_all if idx_all >= 0 else 0)
            cond_widget.setEnabled(False)
            cond_widget.setToolTip("Condition levels apply to HF action rows only.")
        elif not self._hf_group_uses_condition_levels(group_name):
            idx_all = cond_widget.findData("ALL")
            cond_widget.setCurrentIndex(idx_all if idx_all >= 0 else 0)
            cond_widget.setEnabled(False)
            cond_widget.setToolTip(
                "Group must have 'Use Condition Levels' enabled in Configuration for HF SOP actions."
            )
        else:
            cond_widget.setEnabled(True)
            cond_widget.setToolTip("Applies only when the group's current condition level matches this selection.")
        self._fit_combo_popup(cond_widget)
        cond_widget.blockSignals(False)

    def _hf_group_names(self) -> List[str]:
        cached = self._hf_group_names_cache
        if cached is not None:
            return list(cached)
        names = sorted(
            {
                group
                for group, (use_levels, _lvl) in self._hf_group_condition_meta().items()
                if use_levels and group
            }
        )
        self._hf_group_names_cache = list(names)
        return names

    def _local_group_names(self) -> List[str]:
        cached = self._local_group_names_cache
        if cached is not None:
            return list(cached)
        names = sorted(
            {
                str(row.get("group", "")).strip().upper()
                for row in (self._local_net_profiles or [])
                if str(row.get("group", "")).strip()
            }
        )
        self._local_group_names_cache = list(names)
        return names

    def _local_resources_for_group(self, group: str) -> List[str]:
        grp = str(group or "").strip().upper()
        cached = self._local_resource_cache.get(grp)
        if cached is not None:
            return list(cached)
        out: Set[str] = set()
        for row in self._local_net_profiles or []:
            if str(row.get("group", "")).strip().upper() != grp:
                continue
            resource = str(row.get("resource", "")).strip()
            if resource:
                out.add(resource)
        if not out:
            out.update(self.LOCAL_RESOURCE_FALLBACK)
        resolved = sorted(out, key=lambda x: x.upper())
        self._local_resource_cache[grp] = list(resolved)
        return resolved

    def _local_modes_for_group_resource(self, group: str, resource: str) -> List[str]:
        grp = str(group or "").strip().upper()
        res = str(resource or "").strip().upper()
        cache_key = (grp, res)
        cached = self._local_mode_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        out: Set[str] = set()
        for row in self._local_net_profiles or []:
            if str(row.get("group", "")).strip().upper() != grp:
                continue
            if res and str(row.get("resource", "")).strip().upper() != res:
                continue
            mode = str(row.get("mode", "")).strip()
            if mode:
                out.add(mode)
        if not out:
            out.update(self.LOCAL_MODE_FALLBACK)
        resolved = sorted(out, key=lambda x: x.upper())
        self._local_mode_cache[cache_key] = list(resolved)
        return resolved

    def _hf_band_freq_options_for_group(self, group: str) -> List[str]:
        grp = str(group or "").strip().upper()
        cached = self._hf_bandfreq_cache.get(grp)
        if cached is not None:
            return list(cached)
        out: List[Tuple[str, str]] = []
        for row in self._operating_groups or []:
            if str(row.get("group", "")).strip().upper() != grp:
                continue
            band = str(row.get("band", "")).strip().upper()
            freq = str(row.get("frequency", "")).strip()
            if not band or not freq:
                continue
            if normalize_frequency_hz(freq) is not None:
                out.append((band, freq))
        out = sorted(set(out), key=lambda x: (x[0], normalize_frequency_hz(x[1]) or 0, x[1]))
        resolved = [f"{band} - {freq}" for band, freq in out]
        self._hf_bandfreq_cache[grp] = list(resolved)
        return resolved

    def _resource_options_for_category(self, category: str, group: str) -> List[str]:
        if category == self.CAT_LOCAL:
            return self._local_resources_for_group(group)
        return list(self.HF_RESOURCE_OPTIONS)

    def _action_catalog(self) -> Dict[str, List[Tuple[str, str]]]:
        spotter_forms = tuple(self._load_spotter_forms())
        if self._action_catalog_cache_key == spotter_forms and self._action_catalog_cache_value is not None:
            return {name: list(rows) for name, rows in self._action_catalog_cache_value.items()}

        catalog: Dict[str, List[Tuple[str, str]]] = {
            "JS8Call": [("js8_send_status", "Status"), ("js8_commstat", "CommStat")],
            "VarAC": [
                ("varac_send_broadcast", "Broadcast"),
                ("varac_direct_contact", "Direct Contact"),
                ("varac_send_sitrep", "SitRep"),
                ("varac_send_statrep", "StatRep"),
                ("varac_send_report", "General"),
            ],
            "FLDigi": [
                ("fldigi_send_sitrep", "SitRep"),
                ("fldigi_send_statrep", "StatRep"),
                ("fldigi_send_report", "General"),
            ],
            "SSB": [
                ("ssb_monitor", "Monitor"),
                ("ssb_checkin", "Check-in"),
                ("ssb_message", "Message"),
            ],
            "Local Net": [
                ("local_ncs", "NCS"),
                ("local_checkin", "Check-in"),
                ("local_message", "Message"),
                ("local_monitor", "Monitor"),
            ],
        }
        for key, label in spotter_forms:
            catalog.setdefault("JS8Call", []).append((key, label))
        self._action_catalog_cache_key = spotter_forms
        self._action_catalog_cache_value = {name: list(rows) for name, rows in catalog.items()}
        return catalog

    def _current_category(self) -> str:
        return str(self.category_combo.currentData() or self.CAT_HF)

    def _apply_category_table_view(self) -> None:
        cat = self._current_category()
        is_hf = cat == self.CAT_HF
        try:
            self.activation_defaults_toggle_btn.setVisible(is_hf)
            self.activation_defaults_summary_label.setVisible(is_hf)
            self.activation_defaults_box.setVisible(is_hf and bool(getattr(self, "_activation_defaults_expanded", False)))
        except Exception:
            pass
        try:
            self.conflict_workbench_toggle_btn.setVisible(is_hf)
            self.conflict_workbench_summary_label.setVisible(is_hf)
            self.conflict_workbench_filter_label.setVisible(is_hf)
            for _mode_key, _btn in dict(getattr(self, "_conflict_workbench_filter_buttons", {}) or {}).items():
                if isinstance(_btn, QToolButton):
                    _btn.setVisible(is_hf)
            self.conflict_workbench_box.setVisible(is_hf and bool(getattr(self, "_conflict_workbench_expanded", False)))
        except Exception:
            pass
        self.actions_table.setColumnHidden(self.COL_COND, not is_hf)
        self.actions_table.setColumnHidden(self.COL_BANDFREQ, not is_hf)
        self.actions_table.setColumnHidden(self.COL_MODE, is_hf)
        self.actions_table.setColumnHidden(self.COL_END, True)
        self.actions_table.setColumnHidden(self.COL_CONFLICT, not is_hf)
        self._update_activation_defaults_header_summary()
        self._apply_workflow_section_toggle_styles()
        self._apply_conflict_workbench_filter_button_styles()
        self._apply_action_table_visual_order()
        self._autosize_actions_table()
        self._refresh_inline_conflict_badges()

    def _apply_action_table_visual_order(self) -> None:
        header = self.actions_table.horizontalHeader()
        if header is None:
            return
        try:
            header.setSectionsMovable(True)
            header.moveSection(header.visualIndex(self.COL_CONFLICT), 0)
            header.moveSection(header.visualIndex(self.COL_REMOVE), 1)
        except Exception:
            pass

    def _profile_id_for_category(self, category: str) -> int:
        cat = self.manager._normalize_category(category)
        for p in self._profiles:
            if self.manager._normalize_category(p.get("category")) == cat:
                return int(p.get("id") or 0)
        return 0

    def _reload_profiles(self, select_id: int | None) -> None:
        try:
            self.manager.enforce_single_profile_per_category()
        except Exception:
            pass
        hf = self.manager.ensure_category_profile(self.CAT_HF)
        local = self.manager.ensure_category_profile(self.CAT_LOCAL)
        profiles = {int(p.get("id") or 0): p for p in self.manager.list_profiles()}
        self._profiles = [
            profiles.get(int(hf.get("id") or 0), hf),
            profiles.get(int(local.get("id") or 0), local),
        ]

        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("HF SOP", int(hf.get("id") or 0))
        self.profile_combo.addItem("Local Comms SOP", int(local.get("id") or 0))
        self.profile_combo.blockSignals(False)

        target_id = int(select_id or 0)
        if target_id <= 0:
            target_id = int(self._selected_profile_id or 0)
        if target_id <= 0:
            target_id = int(hf.get("id") or 0)
        for idx in range(self.profile_combo.count()):
            if int(self.profile_combo.itemData(idx) or 0) != target_id:
                continue
            self.profile_combo.setCurrentIndex(idx)
            self._on_profile_selected(idx)
            return
        if self.profile_combo.count() > 0:
            self.profile_combo.setCurrentIndex(0)
            self._on_profile_selected(0)

    def _on_category_changed(self) -> None:
        if getattr(self, "_loading_ui", False):
            return
        self._last_realtime_conflict_signature = None
        cat = self._current_category()
        pid = self._profile_id_for_category(cat)
        if pid > 0:
            for idx in range(self.profile_combo.count()):
                if int(self.profile_combo.itemData(idx) or 0) != pid:
                    continue
                if self.profile_combo.currentIndex() != idx:
                    self.profile_combo.setCurrentIndex(idx)
                break
        if pid > 0 and pid != self._selected_profile_id:
            return
        self._apply_category_table_view()
        self._refresh_all_rows_dynamic_options()
        self._schedule_realtime_hf_conflict_check()

    @staticmethod
    def _is_local_action_dict(action: Dict[str, Any]) -> bool:
        software = str(action.get("software", "")).strip().upper()
        action_key = str(action.get("action_key", "")).strip().lower()
        contact_rule = str(action.get("contact_rule", "")).strip().lower()
        return software == "LOCAL NET" or action_key.startswith("local_") or contact_rule in {"local_group", "local_profile"}

    def _on_profile_selected(self, idx: int) -> None:
        if idx < 0:
            return
        profile_id = int(self.profile_combo.itemData(idx) or 0)
        if profile_id <= 0:
            return
        if profile_id != self._selected_profile_id and self._dirty and not self._loading_ui:
            answer = QMessageBox.question(
                self, "Unsaved SOP", "Discard changes and switch SOP? Choose Cancel to keep editing or save first.",
                QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Discard:
                self.profile_combo.blockSignals(True)
                self.profile_combo.setCurrentIndex(self.profile_combo.findData(self._selected_profile_id))
                self.profile_combo.blockSignals(False)
                selected = next((p for p in self._profiles if int(p.get("id") or 0) == self._selected_profile_id), {})
                self.category_combo.blockSignals(True)
                self.category_combo.setCurrentIndex(0 if selected.get("category", self.CAT_HF) == self.CAT_HF else 1)
                self.category_combo.blockSignals(False)
                return
        profile = self.manager.get_profile(profile_id)
        if not profile:
            return
        self._selected_profile_id = profile_id
        category = self.manager._normalize_category(profile.get("category"))
        actions = list(profile.get("actions") or [])
        if category == self.CAT_HF:
            actions = [a for a in actions if not self._is_local_action_dict(a)]
        else:
            actions = [a for a in actions if self._is_local_action_dict(a)]
        self._loading_ui = True
        try:
            self.name_edit.setText(str(profile.get("name") or ("HF SOP" if category == self.CAT_HF else "Local Comms SOP")))
            self.active_cb.setChecked(bool(profile.get("active")))
            self.category_combo.setCurrentIndex(0 if category == self.CAT_HF else 1)
            self._populate_actions(actions)
        finally:
            self._loading_ui = False
        self._apply_category_table_view()
        self._set_save_dirty(False)
        self._update_profile_action_styles()

    def _new_profile(self) -> None:
        choices = ["HF SOP", "Local Comms SOP"]
        choice, ok = QInputDialog.getItem(self, "New SOP", "Select SOP category:", choices, 0, False)
        if not ok:
            return
        category = self.CAT_HF if choice == "HF SOP" else self.CAT_LOCAL
        profile = self.manager.ensure_category_profile(category)
        pid = int(profile.get("id") or 0)
        if pid <= 0:
            return
        for idx in range(self.profile_combo.count()):
            if int(self.profile_combo.itemData(idx) or 0) != pid:
                continue
            self.profile_combo.setCurrentIndex(idx)
            break
        if self.actions_table.rowCount() > 0:
            resp = QMessageBox.question(
                self,
                "New SOP Draft",
                "Clear current action rows and start a new draft for this category?",
            )
            if resp == QMessageBox.Yes:
                self._populate_actions([])
                self.name_edit.setText("HF SOP" if category == self.CAT_HF else "Local Comms SOP")
                self.active_cb.setChecked(False)
                self._set_save_dirty(True)

    def _category_display_label(self, category: str) -> str:
        cat = self.manager._normalize_category(category)
        return "HF SOP" if cat == self.CAT_HF else "Local Comms SOP"

    @staticmethod
    def _version_time_label(iso_text: str) -> str:
        txt = str(iso_text or "").strip()
        if not txt:
            return "Unknown time"
        return txt.replace("T", " ").replace("+00:00", "Z")

    def _pick_saved_version(self, *, purpose_title: str) -> Dict[str, Any] | None:
        category = self._current_category()
        versions = self.manager.list_profile_versions(category=category, limit=250)
        if not versions:
            QMessageBox.information(
                self,
                "SOP Versions",
                f"No saved versions for {self._category_display_label(category)}.",
            )
            return None
        labels: List[str] = []
        for row in versions:
            vid = int(row.get("id") or 0)
            label = str(row.get("label") or "").strip() or f"Version {vid}"
            created = self._version_time_label(str(row.get("created_utc") or ""))
            actions = int(row.get("action_count") or 0)
            note = str(row.get("note") or "").strip()
            display = f"#{vid} | {created} | {label} | {actions} action(s)"
            if note:
                display += f" | {note[:72]}"
            labels.append(display)
        choice, ok = QInputDialog.getItem(self, purpose_title, "Select version:", labels, 0, False)
        if not ok or not choice:
            return None
        try:
            idx = labels.index(choice)
        except Exception:
            idx = -1
        if idx < 0 or idx >= len(versions):
            return None
        return dict(versions[idx])

    def _save_current_version(self) -> None:
        category = self._current_category()
        if int(self._selected_profile_id or 0) <= 0:
            QMessageBox.warning(self, "SOP Versions", "Select a SOP profile first.")
            return
        default_label = self.manager.default_profile_version_label(category)
        label, ok = QInputDialog.getText(
            self,
            "Save SOP Version",
            f"Version name ({self._category_display_label(category)}):",
            text=default_label,
        )
        if not ok:
            return
        try:
            payload, actions, schedule_layer = self._collect_profile_payload()
        except Exception as e:
            QMessageBox.warning(
                self,
                "SOP Versions",
                f"Version save needs valid action rows.\n\n{e}",
            )
            return
        snapshot_profile = dict(payload)
        snapshot_profile["id"] = int(self._selected_profile_id or 0)
        snapshot = {
            "schema_version": 1,
            "captured_utc": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "profile": snapshot_profile,
            "actions": [dict(row) for row in (actions or []) if isinstance(row, dict)],
            "schedule_layer": [dict(row) for row in (schedule_layer or []) if isinstance(row, dict)],
        }
        try:
            version_id = int(
                self.manager.save_profile_version(
                    category=category,
                    snapshot=snapshot,
                    label=str(label or "").strip(),
                    note="",
                )
                or 0
            )
            if version_id <= 0:
                raise RuntimeError("Version save returned no version id.")
            QMessageBox.information(self, "SOP Versions", f"Saved version #{version_id}.")
        except Exception as e:
            QMessageBox.warning(self, "SOP Versions", f"Could not save version:\n{e}")

    def _load_saved_version(self) -> None:
        selected = self._pick_saved_version(purpose_title="Load SOP Version")
        if not selected:
            return
        if bool(self._dirty):
            resp = QMessageBox.question(
                self,
                "Load SOP Version",
                "Discard unsaved edits and load this saved version into SOP Builder?",
            )
            if resp != QMessageBox.Yes:
                return
        version_id = int(selected.get("id") or 0)
        if version_id <= 0:
            return
        loaded = self.manager.get_profile_version(version_id)
        if not loaded:
            QMessageBox.warning(self, "SOP Versions", "Selected version could not be loaded.")
            return
        snapshot = loaded.get("snapshot")
        if not isinstance(snapshot, dict):
            QMessageBox.warning(self, "SOP Versions", "Saved version snapshot is invalid.")
            return
        profile = snapshot.get("profile")
        if not isinstance(profile, dict):
            QMessageBox.warning(self, "SOP Versions", "Saved version is missing profile data.")
            return
        target_category = self._current_category()
        snap_category = self.manager._normalize_category(profile.get("category") or target_category)
        if snap_category != target_category:
            QMessageBox.warning(
                self,
                "SOP Versions",
                "Saved version category does not match current SOP category.",
            )
            return
        actions_raw = snapshot.get("actions")
        actions = [dict(row) for row in actions_raw if isinstance(row, dict)] if isinstance(actions_raw, list) else []
        if target_category == self.CAT_HF:
            actions = [row for row in actions if not self._is_local_action_dict(row)]
        else:
            actions = [row for row in actions if self._is_local_action_dict(row)]
        self._loading_ui = True
        try:
            self.name_edit.setText(
                str(profile.get("name") or self._category_display_label(target_category))
            )
            self.active_cb.setChecked(bool(profile.get("active")))
            self.category_combo.setCurrentIndex(0 if target_category == self.CAT_HF else 1)
            self._populate_actions(actions)
        finally:
            self._loading_ui = False
        self._apply_category_table_view()
        self._set_save_dirty(True)
        self._schedule_realtime_hf_conflict_check()
        QMessageBox.information(
            self,
            "SOP Versions",
            "Version loaded into SOP Builder. Click Save to apply it.",
        )

    def _delete_saved_version(self) -> None:
        selected = self._pick_saved_version(purpose_title="Delete SOP Version")
        if not selected:
            return
        version_id = int(selected.get("id") or 0)
        if version_id <= 0:
            return
        label = str(selected.get("label") or f"Version {version_id}").strip()
        resp = QMessageBox.question(
            self,
            "Delete SOP Version",
            f"Delete saved version '{label}'?",
        )
        if resp != QMessageBox.Yes:
            return
        try:
            if not self.manager.delete_profile_version(version_id):
                QMessageBox.warning(self, "SOP Versions", "Version could not be deleted.")
                return
            QMessageBox.information(self, "SOP Versions", f"Deleted version '{label}'.")
        except Exception as e:
            QMessageBox.warning(self, "SOP Versions", f"Could not delete version:\n{e}")

    def _update_profile_action_styles(self, theme: Dict[str, str] | None = None) -> None:
        try:
            if theme is None:
                theme = resolve_theme(self.settings)
            has_profile = int(self._selected_profile_id or 0) > 0
            self.new_btn.setStyleSheet(button_style("muted", theme))
            self.save_btn.setStyleSheet(button_style("eligible_success" if self._dirty else "muted", theme))
            self.save_btn.setEnabled(True)
            self.delete_btn.setEnabled(has_profile)
            self.delete_btn.setStyleSheet(button_style("eligible_danger" if has_profile else "muted", theme))
            self.versions_btn.setEnabled(has_profile)
            self.versions_btn.setStyleSheet(button_style("muted", theme))
            self.export_pdf_btn.setEnabled(True)
            self.export_pdf_btn.setStyleSheet(button_style("muted", theme))
            self.export_import_btn.setEnabled(True)
            self.export_import_btn.setStyleSheet(button_style("muted", theme))
            self.add_row_btn.setEnabled(True)
            self.add_row_btn.setStyleSheet(button_style("eligible_primary" if has_profile else "muted", theme))
        except Exception:
            pass

    def _refresh_all_rows_dynamic_options(self) -> None:
        for r in range(self.actions_table.rowCount()):
            self._refresh_row_dynamic_options(r, preserve_current=True)

    def _clear_row_dynamic_refresh_timers(self) -> None:
        timers = dict(getattr(self, "_row_dynamic_refresh_timers", {}) or {})
        for _row, timer in timers.items():
            if isinstance(timer, QTimer):
                try:
                    timer.stop()
                except Exception:
                    pass
                try:
                    timer.deleteLater()
                except Exception:
                    pass
        self._row_dynamic_refresh_timers = {}

    def _schedule_row_dynamic_options_refresh(self, row: int, *, delay_ms: int = 90) -> None:
        try:
            target_row = int(row)
        except Exception:
            return
        if target_row < 0:
            return
        timer = self._row_dynamic_refresh_timers.get(target_row)
        if not isinstance(timer, QTimer):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda r=target_row: self._refresh_row_dynamic_options(r, preserve_current=True)
            )
            self._row_dynamic_refresh_timers[target_row] = timer
        timer.setInterval(max(20, int(delay_ms or 90)))
        timer.start()

    def _combo_set_items(self, combo: QComboBox, values: List[str], current_text: str = "") -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("")
        for val in values:
            if val:
                combo.addItem(val)
        if current_text and combo.findText(current_text) < 0:
            combo.addItem(current_text)
        combo.setCurrentText(current_text)
        self._fit_combo_popup(combo)
        combo.blockSignals(False)

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return float(path.stat().st_mtime) if path.exists() else 0.0
        except Exception:
            return 0.0

    def _clear_hf_schedule_slot_cache(self) -> None:
        self._hf_schedule_slots_cache_token = None
        self._hf_schedule_slots_index = {}

    @staticmethod
    def _schedule_day_sort_rank(day_utc: str) -> int:
        order = {
            "ALL": 0,
            "MON": 1,
            "TUE": 2,
            "WED": 3,
            "THU": 4,
            "FRI": 5,
            "SAT": 6,
            "SUN": 7,
        }
        return int(order.get(str(day_utc or "").strip().upper(), 99))

    def _load_hf_schedule_slot_index(self) -> Dict[Tuple[str, str], List[Dict[str, str]]]:
        db_path = self.manager._settings_db_path()
        token = (str(db_path), self._safe_mtime(db_path))
        if self._hf_schedule_slots_cache_token == token and self._hf_schedule_slots_index:
            return {
                k: [dict(row) for row in rows]
                for k, rows in dict(self._hf_schedule_slots_index).items()
            }

        index: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
        with perf_span(
            "sop.slots.index_load",
            settings=self.settings,
            min_ms=5.0,
            meta={"db": str(db_path)},
        ):
            dedup: Dict[Tuple[str, str], Set[Tuple[str, str, str, str]]] = {}
            if db_path.exists():
                conn = sqlite3.connect(db_path)
                try:
                    rows = conn.execute(
                        """
                        SELECT
                            COALESCE(day_utc, 'ALL'),
                            COALESCE(start_utc, '00:00'),
                            COALESCE(end_utc, '23:59'),
                            COALESCE(group_name, ''),
                            COALESCE(band, ''),
                            COALESCE(frequency, '')
                        FROM daily_schedule_tab
                        """
                    ).fetchall()
                    for day_raw, start_raw, end_raw, group_raw, band_raw, freq_raw in rows:
                        band = str(band_raw or "").strip().upper()
                        freq = self.manager._normalize_frequency(freq_raw)
                        if not band or not freq:
                            continue
                        slot_key = (band, freq)
                        day_utc = self.manager._normalize_day_utc(day_raw)
                        start_utc = self.manager._normalize_hhmm(start_raw or "00:00")
                        end_utc = self.manager._normalize_hhmm(end_raw or "23:59")
                        group_name = str(group_raw or "").strip().upper()
                        dedup_key = (day_utc, start_utc, end_utc, group_name)
                        bucket = dedup.setdefault(slot_key, set())
                        if dedup_key in bucket:
                            continue
                        bucket.add(dedup_key)
                        index.setdefault(slot_key, []).append(
                            {
                                "day_utc": day_utc,
                                "start_utc": start_utc,
                                "end_utc": end_utc,
                                "group_name": group_name,
                            }
                        )
                except Exception as e:
                    log.debug("SOP Builder: failed loading HF schedule slots: %s", e)
                    index = {}
                finally:
                    conn.close()

            for key, rows in index.items():
                rows.sort(
                    key=lambda row: (
                        self._schedule_day_sort_rank(str(row.get("day_utc") or "")),
                        str(row.get("start_utc") or ""),
                        str(row.get("end_utc") or ""),
                        str(row.get("group_name") or ""),
                    )
                )
                index[key] = rows[:50]

        self._hf_schedule_slots_cache_token = token
        self._hf_schedule_slots_index = {
            k: [dict(row) for row in rows]
            for k, rows in index.items()
        }
        return {
            k: [dict(row) for row in rows]
            for k, rows in index.items()
        }

    def _hf_schedule_slots_for_band_freq(self, band: str, freq: str) -> List[Dict[str, str]]:
        band_uc = str(band or "").strip().upper()
        freq_norm = self.manager._normalize_frequency(freq)
        if not band_uc or not freq_norm:
            return []
        index = self._load_hf_schedule_slot_index()
        return [dict(row) for row in list(index.get((band_uc, freq_norm), []))]

    def _format_hf_schedule_slot_text(self, slot: Dict[str, Any]) -> str:
        day_utc = str(slot.get("day_utc") or "ALL").strip().upper()
        day_text = "Daily" if day_utc == "ALL" else day_utc
        start_utc = self.manager._normalize_hhmm(slot.get("start_utc") or "00:00")
        end_utc = self.manager._normalize_hhmm(slot.get("end_utc") or "23:59")
        start_display = self._display_start_hhmm_from_utc(start_utc, show_local=self._show_local)
        end_display = self._display_start_hhmm_from_utc(end_utc, show_local=self._show_local)
        group_name = str(slot.get("group_name") or "").strip().upper() or "-"
        return f"{day_text} {start_display}-{end_display} | {group_name}"

    def _action_row_start_edit(self, row: int) -> QLineEdit | None:
        widget = self.actions_table.cellWidget(int(row), self.COL_START)
        if isinstance(widget, QLineEdit):
            return widget
        if isinstance(widget, QWidget):
            named = widget.findChild(QLineEdit, "sop_action_start_edit")
            if isinstance(named, QLineEdit):
                return named
            fallback = widget.findChild(QLineEdit)
            if isinstance(fallback, QLineEdit):
                return fallback
        return None

    def _action_row_start_slots_button(self, row: int) -> QToolButton | None:
        widget = self.actions_table.cellWidget(int(row), self.COL_START)
        if isinstance(widget, QWidget):
            named = widget.findChild(QToolButton, "sop_action_start_slots_btn")
            if isinstance(named, QToolButton):
                return named
            fallback = widget.findChild(QToolButton)
            if isinstance(fallback, QToolButton):
                return fallback
        return None

    def _row_for_start_slots_button(self, btn: QToolButton) -> int:
        for row in range(self.actions_table.rowCount()):
            if self._action_row_start_slots_button(row) is btn:
                return int(row)
        return -1

    def _update_start_slots_button_for_row(self, row: int) -> None:
        btn = self._action_row_start_slots_button(row)
        if not isinstance(btn, QToolButton):
            return
        if self._current_category() != self.CAT_HF:
            btn.setEnabled(False)
            btn.setToolTip("HF schedule slot guidance applies to HF SOP rows.")
            return
        group_combo = self.actions_table.cellWidget(row, self.COL_GROUP)
        bandfreq_combo = self.actions_table.cellWidget(row, self.COL_BANDFREQ)
        group_name = group_combo.currentText().strip().upper() if isinstance(group_combo, QComboBox) else ""
        bandfreq_val = bandfreq_combo.currentText().strip() if isinstance(bandfreq_combo, QComboBox) else ""
        band, freq = self._split_band_freq(bandfreq_val)
        if group_name and band and freq:
            btn.setEnabled(True)
            btn.setToolTip("Pick Start from existing HF Schedule rows on this frequency.")
            return
        btn.setEnabled(False)
        btn.setToolTip("Select Group and Band-Freq to view HF Schedule time slots.")

    def _apply_hf_schedule_slot_start(self, row: int, start_utc: str) -> None:
        start_edit = self._action_row_start_edit(row)
        if not isinstance(start_edit, QLineEdit):
            return
        suggested_utc = self.manager._normalize_hhmm(start_utc or "")
        if not suggested_utc:
            return
        duration_combo = self.actions_table.cellWidget(row, self.COL_DURATION)
        end_edit = self.actions_table.cellWidget(row, self.COL_END)
        duration_minutes = 60
        if isinstance(duration_combo, QComboBox):
            try:
                duration_minutes = int(duration_combo.currentData() or 60)
            except Exception:
                duration_minutes = 60
        if duration_minutes not in {30, 60}:
            duration_minutes = 60
        end_utc = self._add_minutes_hhmm(suggested_utc, duration_minutes)
        start_display = self._display_start_hhmm_from_utc(suggested_utc, show_local=self._show_local)
        start_edit.setText(start_display)
        if isinstance(end_edit, QLineEdit):
            end_edit.setText(self._display_start_hhmm_from_utc(end_utc, show_local=self._show_local))

    def _show_hf_schedule_slots_for_button(self, btn: QToolButton) -> None:
        if not isinstance(btn, QToolButton):
            return
        row = self._row_for_start_slots_button(btn)
        if row < 0:
            return
        if self._current_category() != self.CAT_HF:
            return
        group_combo = self.actions_table.cellWidget(row, self.COL_GROUP)
        bandfreq_combo = self.actions_table.cellWidget(row, self.COL_BANDFREQ)
        group_name = group_combo.currentText().strip().upper() if isinstance(group_combo, QComboBox) else ""
        bandfreq_val = bandfreq_combo.currentText().strip() if isinstance(bandfreq_combo, QComboBox) else ""
        band, freq = self._split_band_freq(bandfreq_val)
        if not (group_name and band and freq):
            return

        slots = self._hf_schedule_slots_for_band_freq(band, freq)
        menu = QMenu(btn)
        if not slots:
            empty_action = menu.addAction("No HF schedule slots on this frequency")
            empty_action.setEnabled(False)
        else:
            primary = [slot for slot in slots if str(slot.get("group_name") or "").strip().upper() == group_name]
            secondary = [slot for slot in slots if slot not in primary]
            ordered_slots = list(primary) + list(secondary)
            if secondary and primary:
                top_note = menu.addAction("Selected group matches first")
                top_note.setEnabled(False)
                menu.addSeparator()
            for slot in ordered_slots:
                start_utc = self.manager._normalize_hhmm(slot.get("start_utc") or "")
                if not start_utc:
                    continue
                label = self._format_hf_schedule_slot_text(slot)
                action = menu.addAction(label)
                action.triggered.connect(
                    lambda _=False, target_row=int(row), start_val=str(start_utc): self._apply_hf_schedule_slot_start(
                        target_row, start_val
                    )
                )
        menu.popup(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _lookup_callsigns_for_contact_rule(self, group: str, rule: str) -> List[str]:
        grp = str(group or "").strip().upper()
        if not grp:
            return []
        normalized_rule = str(rule or "none").strip().lower()
        cache_key = (grp, normalized_rule)
        now = time.monotonic()
        cached = self._contact_lookup_cache.get(cache_key)
        if cached and (now - cached[0]) <= float(self._contact_lookup_cache_ttl_sec):
            return list(cached[1])

        resolved: List[str]
        contacts = self.manager.resolve_primary_contacts(grp, "")
        if normalized_rule == "hub_or_hub_alt":
            resolved = list(contacts.get("hub", []) or [])
        elif normalized_rule == "ncs_or_ancs":
            resolved = list(contacts.get("ncs", []) or [])
        elif normalized_rule == "peer":
            resolved = list(contacts.get("peer", []) or [])
        elif normalized_rule == "group":
            resolved = self.manager.resolve_group_callsigns(grp, "")
        else:
            resolved = self.manager.resolve_group_callsigns(grp, "")
        self._contact_lookup_cache[cache_key] = (now, list(resolved))
        return resolved

    def _refresh_row_dynamic_options(self, row: int, *, preserve_current: bool) -> None:
        cat = self._current_category()
        group_combo = self.actions_table.cellWidget(row, self.COL_GROUP)
        cond_widget = self.actions_table.cellWidget(row, self.COL_COND)
        resource_combo = self.actions_table.cellWidget(row, self.COL_RESOURCE)
        mode_combo = self.actions_table.cellWidget(row, self.COL_MODE)
        action_combo = self.actions_table.cellWidget(row, self.COL_ACTION)
        bandfreq_combo = self.actions_table.cellWidget(row, self.COL_BANDFREQ)
        contact_combo = self.actions_table.cellWidget(row, self.COL_CONTACT)
        target_combo = self.actions_table.cellWidget(row, self.COL_CONTACT_TARGET)
        if not isinstance(group_combo, QComboBox) or not isinstance(resource_combo, QComboBox):
            return
        if not isinstance(mode_combo, QComboBox) or not isinstance(action_combo, QComboBox):
            return
        if not isinstance(bandfreq_combo, QComboBox) or not isinstance(contact_combo, QComboBox):
            return
        if not isinstance(target_combo, QComboBox):
            return

        group_current = group_combo.currentText().strip().upper()
        resource_current = resource_combo.currentText().strip()
        mode_current = mode_combo.currentText().strip()
        action_key_current = str(action_combo.currentData() or "").strip()
        bandfreq_current = bandfreq_combo.currentText().strip()
        contact_current = str(contact_combo.currentData() or "none").strip()

        group_values = self._hf_group_names() if cat == self.CAT_HF else self._local_group_names()
        self._combo_set_items(group_combo, group_values, group_current if preserve_current else "")
        group_current = group_combo.currentText().strip().upper()

        resource_values = self._resource_options_for_category(cat, group_current)
        if cat == self.CAT_LOCAL and resource_current and resource_current not in resource_values:
            resource_values = resource_values + [resource_current]
        self._combo_set_items(resource_combo, resource_values, resource_current if preserve_current else "")
        resource_current = resource_combo.currentText().strip()

        mode_values: List[str] = []
        if cat == self.CAT_LOCAL:
            mode_values = self._local_modes_for_group_resource(group_current, resource_current)
        else:
            mode_values = self._mode_options_for_group_band(group_current, "")
            if not mode_values:
                mode_values = ["DIGI", "USB", "LSB"]
        self._combo_set_items(mode_combo, mode_values, mode_current if preserve_current else "")

        action_pairs = []
        if cat == self.CAT_LOCAL:
            action_pairs = self._action_catalog().get("Local Net", [])
        else:
            action_pairs = self._action_catalog().get(resource_current, [])
        action_combo.blockSignals(True)
        action_combo.clear()
        for key, label in action_pairs:
            action_combo.addItem(label, key)
        if action_key_current and action_combo.findData(action_key_current) < 0:
            action_combo.addItem(action_key_current, action_key_current)
        idx = action_combo.findData(action_key_current)
        action_combo.setCurrentIndex(idx if idx >= 0 else (0 if action_combo.count() else -1))
        self._fit_combo_popup(action_combo)
        action_combo.blockSignals(False)
        action_key_current = str(action_combo.currentData() or "").strip()

        if cat == self.CAT_HF:
            bandfreq_values = self._hf_band_freq_options_for_group(group_current)
            self._combo_set_items(bandfreq_combo, bandfreq_values, bandfreq_current if preserve_current else "")
            bandfreq_combo.setEnabled(True)
        else:
            self._combo_set_items(bandfreq_combo, [], "")
            bandfreq_combo.setEnabled(False)

        contact_opts = self.CONTACT_RULE_OPTIONS
        if cat == self.CAT_LOCAL:
            if action_key_current == "local_monitor":
                contact_opts = [("none", "None")]
            else:
                contact_opts = list(self.LOCAL_CONTACT_OPTIONS)
        contact_combo.blockSignals(True)
        contact_combo.clear()
        for code, label in contact_opts:
            contact_combo.addItem(label, code)
        idx_contact = contact_combo.findData(contact_current)
        if idx_contact < 0:
            idx_contact = 0
        contact_combo.setCurrentIndex(idx_contact)
        self._fit_combo_popup(contact_combo)
        contact_combo.blockSignals(False)

        self._refresh_action_row_condition_widget(
            category=cat,
            group_name=group_current,
            cond_widget=cond_widget,
        )

        selected_contact = str(contact_combo.currentData() or "none").strip()
        target_enabled = selected_contact != "none"
        if cat == self.CAT_LOCAL and action_key_current == "local_monitor":
            target_enabled = False
        existing_target = str(target_combo.currentText() or "").strip().upper()
        option_values = self._lookup_callsigns_for_contact_rule(group_current, selected_contact) if target_enabled else []
        if selected_contact in {"hub_or_hub_alt", "ncs_or_ancs"} and target_enabled:
            option_values = ["Any (Role Match)"] + option_values
        self._combo_set_items(target_combo, option_values, existing_target if preserve_current else "")
        target_combo.setEditable(True)
        target_combo.setEnabled(target_enabled)
        self._apply_typeahead(target_combo)
        self._update_start_slots_button_for_row(row)

    def _populate_actions(self, existing: List[Dict[str, Any]]) -> None:
        if hasattr(self, "_action_drafts"):
            self._action_drafts.replace(existing or [{}])
        self._summary_drafts_pending = False
        self._clear_row_dynamic_refresh_timers()
        self.actions_table.setRowCount(0)
        rows = [r for r in (existing or []) if isinstance(r, dict)]
        rows.sort(key=lambda x: int(x.get("sort_order") or 0))
        for row in rows:
            self._add_action_row(existing=row, mark_dirty=False)
        if self.actions_table.rowCount() == 0:
            self._add_action_row(existing=None, mark_dirty=False)
        self._apply_category_table_view()
        self._autosize_actions_table()
        self._refresh_sop_workbench_contracts()
        self._sync_drafts_from_advanced_table()
        self._render_sop_action_cards()

    def _add_action_row(self, existing: Dict[str, Any] | None, *, mark_dirty: bool = True) -> None:
        row = self.actions_table.rowCount()
        self.actions_table.insertRow(row)
        cat = self._current_category()

        group_combo = QComboBox()
        group_combo.setEditable(True)
        group_combo.setProperty("action_id", int((existing or {}).get("id") or 0))
        group_combo.setProperty("sort_order", int((existing or {}).get("sort_order") or row))
        group_combo.setProperty(
            "operating_group_frequency_key",
            str((existing or {}).get("operating_group_frequency_key") or "").strip(),
        )
        group_combo.setProperty(
            "conflict_policy",
            self.manager._normalize_conflict_policy((existing or {}).get("conflict_policy")),
        )
        group_combo.setProperty(
            "schedule_applied",
            bool((existing or {}).get("schedule_applied", True)),
        )
        self.actions_table.setCellWidget(row, self.COL_GROUP, group_combo)

        cond_combo = _ConditionLevelsMultiCombo()
        initial_cond = self.manager._normalize_condition_levels((existing or {}).get("condition_levels") or "ALL")
        self._fit_combo_popup(cond_combo)
        self.actions_table.setCellWidget(row, self.COL_COND, cond_combo)

        resource_combo = QComboBox()
        resource_combo.setEditable(True)
        self.actions_table.setCellWidget(row, self.COL_RESOURCE, resource_combo)

        mode_combo = QComboBox()
        mode_combo.setEditable(True)
        self.actions_table.setCellWidget(row, self.COL_MODE, mode_combo)

        action_combo = QComboBox()
        self.actions_table.setCellWidget(row, self.COL_ACTION, action_combo)

        bandfreq_combo = QComboBox()
        bandfreq_combo.setEditable(True)
        self.actions_table.setCellWidget(row, self.COL_BANDFREQ, bandfreq_combo)

        start_utc = str((existing or {}).get("daily_start_utc") or "00:00")
        end_utc = str((existing or {}).get("daily_end_utc") or "23:59")
        start_edit = QLineEdit(self._display_start_hhmm_from_utc(start_utc, show_local=self._show_local))
        end_edit = QLineEdit(self._display_start_hhmm_from_utc(end_utc, show_local=self._show_local))
        start_edit.setPlaceholderText("HH:MM")
        end_edit.setPlaceholderText("HH:MM")
        start_edit.setObjectName("sop_action_start_edit")
        start_slots_btn = QToolButton()
        start_slots_btn.setObjectName("sop_action_start_slots_btn")
        start_slots_btn.setText("Slots")
        start_slots_btn.clicked.connect(lambda _=False, b=start_slots_btn: self._show_hf_schedule_slots_for_button(b))
        start_cell = QWidget()
        start_layout = QHBoxLayout(start_cell)
        start_layout.setContentsMargins(0, 0, 0, 0)
        start_layout.setSpacing(4)
        start_layout.addWidget(start_edit, stretch=1)
        start_layout.addWidget(start_slots_btn)
        self.actions_table.setCellWidget(row, self.COL_START, start_cell)
        self.actions_table.setCellWidget(row, self.COL_END, end_edit)

        duration_combo = QComboBox()
        for label, minutes in self.DURATION_OPTIONS:
            duration_combo.addItem(label, minutes)
        duration_val = int((existing or {}).get("duration_minutes") or 60)
        idx_dur = duration_combo.findData(duration_val)
        duration_combo.setCurrentIndex(idx_dur if idx_dur >= 0 else 1)
        self.actions_table.setCellWidget(row, self.COL_DURATION, duration_combo)

        interval_combo = QComboBox()
        interval_combo.setEditable(True)
        for preset in self.INTERVAL_PRESETS:
            interval_combo.addItem(preset, preset)
        interval_minutes = int((existing or {}).get("interval_minutes") or 0)
        if interval_minutes <= 0:
            interval_minutes = int((existing or {}).get("interval_hours") or 24) * 60
        interval_phase = int((existing or {}).get("interval_phase_minutes") or 0)
        interval_text = self._format_interval_spec(interval_minutes, interval_phase)
        if interval_combo.findText(interval_text) < 0:
            interval_combo.addItem(interval_text, interval_text)
        interval_combo.setCurrentText(interval_text)
        interval_combo.setToolTip("Examples: Daily, 00:30, 01:00, 03:00@30m")
        self.actions_table.setCellWidget(row, self.COL_INTERVAL, interval_combo)

        contact_combo = QComboBox()
        self.actions_table.setCellWidget(row, self.COL_CONTACT, contact_combo)

        target_combo = QComboBox()
        target_combo.setEditable(True)
        target_combo.setInsertPolicy(QComboBox.NoInsert)
        target_combo.setCurrentText(str((existing or {}).get("contact_target") or "").strip().upper())
        target_combo.setProperty("saved_target", str((existing or {}).get("contact_target") or "").strip().upper())
        if target_combo.lineEdit() is not None:
            target_combo.lineEdit().setPlaceholderText("Optional (type or select)")
        self.actions_table.setCellWidget(row, self.COL_CONTACT_TARGET, target_combo)

        desc_edit = QLineEdit(str((existing or {}).get("description") or "").strip())
        desc_edit.setPlaceholderText("Optional description")
        self.actions_table.setCellWidget(row, self.COL_DESC, desc_edit)

        conflict_badge = QToolButton()
        conflict_badge.setText("Pending")
        conflict_badge.setAutoRaise(False)
        conflict_badge.clicked.connect(lambda _=False, b=conflict_badge: self._show_inline_conflict_details_for_button(b))
        self.actions_table.setCellWidget(row, self.COL_CONFLICT, conflict_badge)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(lambda _=False, b=remove_btn: self._remove_row_for_button(b))
        self.actions_table.setCellWidget(row, self.COL_REMOVE, remove_btn)

        existing_group = str((existing or {}).get("group_name") or "").strip().upper()
        if not existing_group:
            existing_group = str((existing or {}).get("operating_group") or "").strip().upper()
        existing_resource = str((existing or {}).get("software") or "").strip()
        existing_mode = str((existing or {}).get("mode") or "").strip()
        existing_action_key = str((existing or {}).get("action_key") or "").strip()
        existing_contact = str((existing or {}).get("contact_rule") or "").strip()
        if not existing_contact:
            existing_contact = "group" if cat == self.CAT_HF else "none"

        self._refresh_row_dynamic_options(row, preserve_current=False)
        group_combo.setCurrentText(existing_group)
        self._refresh_row_dynamic_options(row, preserve_current=True)
        if existing_resource:
            resource_combo.setCurrentText(existing_resource)
            self._refresh_row_dynamic_options(row, preserve_current=True)
        if existing_mode:
            mode_combo.setCurrentText(existing_mode)
        if cat == self.CAT_HF:
            band = str((existing or {}).get("band") or "").strip().upper()
            freq = str((existing or {}).get("frequency") or "").strip()
            if band and freq:
                bandfreq_combo.setCurrentText(f"{band} - {freq}")
        if existing_action_key:
            idx_action = action_combo.findData(existing_action_key)
            if idx_action >= 0:
                action_combo.setCurrentIndex(idx_action)
        idx_contact = contact_combo.findData(existing_contact)
        if idx_contact >= 0:
            contact_combo.setCurrentIndex(idx_contact)
        resolved_group = group_combo.currentText().strip().upper()
        initial_band, initial_frequency = self._split_band_freq(bandfreq_combo.currentText())
        group_combo.setProperty(
            "operating_group_frequency_original_identity",
            (resolved_group, initial_band, mode_combo.currentText().strip().upper(), initial_frequency),
        )
        if cat == self.CAT_HF and self._hf_group_uses_condition_levels(resolved_group):
            cond_combo.set_normalized_value(initial_cond, emit=False)
        else:
            cond_combo.set_normalized_value("ALL", emit=False)
        self._refresh_action_row_condition_widget(
            category=cat,
            group_name=resolved_group,
            cond_widget=cond_combo,
        )

        group_combo.currentIndexChanged.connect(lambda _=0, r=row: self._refresh_row_dynamic_options(r, preserve_current=True))
        group_combo.currentTextChanged.connect(lambda _=None, r=row: self._schedule_row_dynamic_options_refresh(r))
        group_combo.currentTextChanged.connect(lambda _=None, r=row: self._update_start_slots_button_for_row(r))
        resource_combo.currentIndexChanged.connect(lambda _=0, r=row: self._refresh_row_dynamic_options(r, preserve_current=True))
        resource_combo.currentTextChanged.connect(lambda _=None, r=row: self._schedule_row_dynamic_options_refresh(r))
        action_combo.currentIndexChanged.connect(lambda _=0, r=row: self._refresh_row_dynamic_options(r, preserve_current=True))
        contact_combo.currentIndexChanged.connect(lambda _=0, r=row: self._refresh_row_dynamic_options(r, preserve_current=True))
        bandfreq_combo.currentIndexChanged.connect(lambda _=0, r=row: self._update_start_slots_button_for_row(r))
        bandfreq_combo.currentTextChanged.connect(lambda _=None, r=row: self._update_start_slots_button_for_row(r))

        cond_combo.selectionChanged.connect(self._mark_dirty)
        cond_combo.selectionChanged.connect(self._schedule_realtime_hf_conflict_check)

        for widget in (
            group_combo,
            resource_combo,
            mode_combo,
            action_combo,
            bandfreq_combo,
            start_edit,
            end_edit,
            duration_combo,
            interval_combo,
            contact_combo,
            target_combo,
            desc_edit,
        ):
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._mark_dirty)
                widget.currentTextChanged.connect(self._mark_dirty)
                widget.currentIndexChanged.connect(self._schedule_realtime_hf_conflict_check)
                widget.currentTextChanged.connect(self._schedule_realtime_hf_conflict_check)
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._mark_dirty)
                widget.textChanged.connect(self._schedule_realtime_hf_conflict_check)
        self._autosize_actions_table()
        self._set_inline_conflict_badge(row, "pending")
        self._update_start_slots_button_for_row(row)
        if mark_dirty:
            if hasattr(self, "_action_drafts"):
                self._sync_drafts_from_advanced_table()
                self._render_sop_action_cards()
            self._mark_dirty()

    def _remove_row_for_button(self, btn: QPushButton) -> None:
        for r in range(self.actions_table.rowCount()):
            if self.actions_table.cellWidget(r, self.COL_REMOVE) is btn:
                if hasattr(self, "_action_drafts"):
                    self._action_drafts.remove(r)
                self.actions_table.removeRow(r)
                self._clear_row_dynamic_refresh_timers()
                if self.actions_table.rowCount() == 0:
                    self._add_action_row(existing=None, mark_dirty=False)
                    if hasattr(self, "_action_drafts") and not self._action_drafts.rows():
                        self._action_drafts.append({})
                self._autosize_actions_table()
                self._mark_dirty()
                self._refresh_sop_workbench_contracts()
                self._render_sop_action_cards()
                self._last_realtime_conflict_signature = None
                self._schedule_realtime_hf_conflict_check()
                return

    def _autosize_actions_table(self) -> None:
        try:
            for col in (
                self.COL_GROUP,
                self.COL_COND,
                self.COL_RESOURCE,
                self.COL_MODE,
                self.COL_ACTION,
                self.COL_BANDFREQ,
                self.COL_START,
                self.COL_END,
                self.COL_DURATION,
                self.COL_INTERVAL,
                self.COL_CONTACT,
                self.COL_CONTACT_TARGET,
                self.COL_CONFLICT,
                self.COL_REMOVE,
            ):
                self.actions_table.resizeColumnToContents(col)
            min_widths = {
                self.COL_GROUP: 150,
                self.COL_RESOURCE: 130,
                self.COL_ACTION: 140,
                self.COL_BANDFREQ: 180,
                self.COL_START: 170,
                self.COL_INTERVAL: 110,
                self.COL_CONTACT_TARGET: 170,
                self.COL_CONFLICT: 96,
            }
            for col, min_w in min_widths.items():
                if self.actions_table.columnWidth(col) < min_w:
                    self.actions_table.setColumnWidth(col, min_w)
            if self.actions_table.columnWidth(self.COL_DESC) < 240:
                self.actions_table.setColumnWidth(self.COL_DESC, 240)
        except Exception:
            pass

    @staticmethod
    def _split_band_freq(value: str) -> Tuple[str, str]:
        text = str(value or "").strip()
        if not text:
            return "", ""
        if "-" in text:
            band, freq = text.split("-", 1)
            return band.strip().upper(), freq.strip()
        parts = text.split()
        if len(parts) >= 2:
            return parts[0].strip().upper(), parts[-1].strip()
        return "", text

    @staticmethod
    def _add_minutes_hhmm(hhmm: str, minutes: int) -> str:
        try:
            parts = str(hhmm or "00:00").strip().split(":")
            h = max(0, min(23, int(parts[0])))
            m = max(0, min(59, int(parts[1])))
        except Exception:
            h, m = 0, 0
        base = (h * 60) + m
        total = max(0, min((24 * 60) - 1, base + int(minutes)))
        return f"{total // 60:02d}:{total % 60:02d}"

    def _collect_profile_payload(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]], None]:
        if hasattr(self, "_action_drafts"):
            return self._collect_profile_payload_from_drafts()
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("SOP name is required.")
        category = self._current_category()
        active = bool(self.active_cb.isChecked())
        profile_id = int(self._selected_profile_id or 0)

        actions: List[Dict[str, Any]] = []
        for r in range(self.actions_table.rowCount()):
            group_combo = self.actions_table.cellWidget(r, self.COL_GROUP)
            cond_widget = self.actions_table.cellWidget(r, self.COL_COND)
            resource_combo = self.actions_table.cellWidget(r, self.COL_RESOURCE)
            mode_combo = self.actions_table.cellWidget(r, self.COL_MODE)
            action_combo = self.actions_table.cellWidget(r, self.COL_ACTION)
            bandfreq_combo = self.actions_table.cellWidget(r, self.COL_BANDFREQ)
            start_edit = self._action_row_start_edit(r)
            end_edit = self.actions_table.cellWidget(r, self.COL_END)
            duration_combo = self.actions_table.cellWidget(r, self.COL_DURATION)
            interval_combo = self.actions_table.cellWidget(r, self.COL_INTERVAL)
            contact_combo = self.actions_table.cellWidget(r, self.COL_CONTACT)
            target_combo = self.actions_table.cellWidget(r, self.COL_CONTACT_TARGET)
            desc_edit = self.actions_table.cellWidget(r, self.COL_DESC)
            if not isinstance(group_combo, QComboBox) or not isinstance(resource_combo, QComboBox):
                continue
            if not isinstance(mode_combo, QComboBox) or not isinstance(action_combo, QComboBox):
                continue
            if not isinstance(bandfreq_combo, QComboBox) or not isinstance(start_edit, QLineEdit):
                continue
            if not isinstance(end_edit, QLineEdit) or not isinstance(duration_combo, QComboBox):
                continue
            if not isinstance(interval_combo, QComboBox) or not isinstance(contact_combo, QComboBox):
                continue
            if not isinstance(target_combo, QComboBox) or not isinstance(desc_edit, QLineEdit):
                continue

            group_name = group_combo.currentText().strip().upper()
            condition_levels = self._condition_levels_from_widget(cond_widget)
            resource = resource_combo.currentText().strip()
            mode = mode_combo.currentText().strip().upper()
            action_key = str(action_combo.currentData() or "").strip()
            action_label = action_combo.currentText().strip()
            band = ""
            freq = ""
            if category == self.CAT_HF:
                band, freq = self._split_band_freq(bandfreq_combo.currentText().strip())
            start_display = start_edit.text().strip()
            end_display = end_edit.text().strip() if isinstance(end_edit, QLineEdit) else ""
            description = desc_edit.text().strip()
            contact_rule = str(contact_combo.currentData() or "none").strip()
            contact_target = str(target_combo.currentText() or "").strip().upper()
            if contact_target == "ANY (ROLE MATCH)":
                contact_target = self.ANY_ROLE_TOKEN

            row_blank = not any([group_name, resource, action_key, action_label, description, contact_target, band, freq])
            if row_blank:
                continue

            if category == self.CAT_HF and not group_name:
                raise ValueError(f"Row {r + 1}: Group is required for HF SOP.")
            if category == self.CAT_LOCAL and not group_name:
                raise ValueError(f"Row {r + 1}: Group is required for Local Comms SOP.")
            if category == self.CAT_HF and not self._hf_group_uses_condition_levels(group_name):
                raise ValueError(
                    f"Row {r + 1}: Group '{group_name}' must have Use Condition Levels enabled in Configuration."
                )
            if not resource:
                raise ValueError(f"Row {r + 1}: Resource is required.")
            if not action_key:
                raise ValueError(f"Row {r + 1}: Action is required.")
            if category == self.CAT_HF and (not band or not freq):
                raise ValueError(f"Row {r + 1}: Band - Freq is required for HF SOP.")
            frequency_key = ""
            if category == self.CAT_HF:
                original_identity = group_combo.property("operating_group_frequency_original_identity")
                try:
                    frequency_key = self._resolve_sop_frequency_row_key(
                        group=group_name,
                        band=band,
                        mode=mode,
                        frequency=freq,
                        frequency_key=str(group_combo.property("operating_group_frequency_key") or ""),
                        original_identity=tuple(original_identity) if isinstance(original_identity, (tuple, list)) and len(original_identity) == 4 else None,
                    )
                except ValueError as exc:
                    raise ValueError(f"Row {r + 1}: {exc}") from exc
                group_combo.setProperty("operating_group_frequency_key", frequency_key)
                group_combo.setProperty("operating_group_frequency_original_identity", (group_name, band, mode, freq))
            if not self._is_valid_hhmm(start_display):
                raise ValueError(f"Row {r + 1}: Daily Start must be HH:MM.")
            start_utc = self._utc_start_hhmm_from_display(start_display, show_local=self._show_local)
            duration_minutes = int(duration_combo.currentData() or 60)
            if duration_minutes not in {30, 60}:
                duration_minutes = 60
            end_utc = self._add_minutes_hhmm(start_utc, duration_minutes)
            if isinstance(end_edit, QLineEdit):
                end_edit.setText(self._display_start_hhmm_from_utc(end_utc, show_local=self._show_local))
            interval_minutes, phase_minutes = self._parse_interval_spec(interval_combo.currentText())
            if category == self.CAT_LOCAL and action_key == "local_monitor":
                contact_rule = "none"
                contact_target = ""

            action_id = int(group_combo.property("action_id") or 0)
            conflict_policy = self.manager._normalize_conflict_policy(group_combo.property("conflict_policy"))
            schedule_applied = bool(group_combo.property("schedule_applied"))
            actions.append(
                {
                    "id": action_id,
                    "group_name": group_name,
                    "condition_levels": condition_levels if category == self.CAT_HF else "ALL",
                    "band": band if category == self.CAT_HF else "",
                    "frequency": freq if category == self.CAT_HF else "",
                    "operating_group_frequency_key": frequency_key,
                    "software": "Local Net" if category == self.CAT_LOCAL else resource,
                    "mode": mode,
                    "action_key": action_key,
                    "action_label": action_label or action_key,
                    "enabled": True,
                    "daily_start_utc": start_utc,
                    "daily_end_utc": end_utc,
                    "duration_minutes": duration_minutes,
                    "interval_minutes": interval_minutes,
                    "interval_phase_minutes": phase_minutes,
                    "interval_hours": max(1, int((interval_minutes + 59) // 60)),
                    "conflict_policy": conflict_policy,
                    "daily_conflict_summary": "",
                    "net_conflict_summary": "",
                    "schedule_applied": schedule_applied,
                    "description": description,
                    "contact_rule": contact_rule,
                    "contact_target": contact_target,
                    "sort_order": r,
                }
            )

        if not actions:
            raise ValueError("Add at least one action row.")

        profile_group = ""
        if category == self.CAT_HF:
            for action in actions:
                grp = str(action.get("group_name") or "").strip().upper()
                if grp:
                    profile_group = grp
                    break
            if not profile_group:
                raise ValueError("At least one HF action row must include a Group.")

        payload = {
            "id": profile_id,
            "name": name,
            "category": category,
            "operating_group": profile_group,
            "secondary_group": "",
            "frequency": "",
            "sop_start_utc": "00:00",
            "priority": 100,
            "active": active,
            "window_hours": 24,
        }
        return payload, actions, None

    def _collect_profile_payload_from_drafts(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]], None]:
        """Validate/persist the widget-independent draft collection."""
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("SOP name is required.")
        category = self._current_category()
        actions: List[Dict[str, Any]] = []
        for row, draft in enumerate(self._action_drafts.rows()):
            action = draft.to_payload(sort_order=row)
            group_name = str(action.get("group_name") or "").strip().upper()
            resource = str(action.get("software") or "").strip()
            action_key = str(action.get("action_key") or "").strip()
            band = str(action.get("band") or "").strip().upper()
            frequency = str(action.get("frequency") or "").strip()
            description = str(action.get("description") or "").strip()
            target = str(action.get("contact_target") or "").strip().upper()
            blank = not any((group_name, resource, action_key, description, target, band, frequency))
            if blank:
                continue
            if not group_name:
                raise ValueError(f"Row {row + 1}: Group is required for this SOP.")
            if category == self.CAT_HF and not self._hf_group_uses_condition_levels(group_name):
                raise ValueError(
                    f"Row {row + 1}: Group '{group_name}' must have Use Condition Levels enabled in Configuration."
                )
            if not resource:
                raise ValueError(f"Row {row + 1}: Resource is required.")
            if not action_key:
                raise ValueError(f"Row {row + 1}: Action is required.")
            if category == self.CAT_HF and (not band or not frequency):
                raise ValueError(f"Row {row + 1}: Band - Freq is required for HF SOP.")
            frequency_key = ""
            if category == self.CAT_HF:
                try:
                    frequency_key = self._resolve_sop_frequency_row_key(
                        group=group_name,
                        band=band,
                        mode=str(action.get("mode") or ""),
                        frequency=frequency,
                        frequency_key=str(action.get("operating_group_frequency_key") or ""),
                        original_identity=(group_name, band, str(action.get("mode") or ""), frequency),
                    )
                except ValueError as exc:
                    raise ValueError(f"Row {row + 1}: {exc}") from exc
            raw_start_utc = str(action.get("daily_start_utc") or "").strip()
            if not self._is_valid_hhmm(raw_start_utc):
                raise ValueError(f"Row {row + 1}: Daily Start must be HH:MM.")
            start_utc = self.manager._normalize_hhmm(raw_start_utc)
            try:
                duration = int(action.get("duration_minutes") or 60)
            except Exception:
                duration = 60
            if duration not in {30, 60}:
                raise ValueError(f"Row {row + 1}: Duration must be 30 or 60 minutes.")
            try:
                interval = max(1, int(action.get("interval_minutes") or 180))
            except Exception:
                interval = 180
            try:
                phase = max(0, int(action.get("interval_phase_minutes") or 0)) % interval
            except Exception:
                phase = 0
            action.update(
                {
                    "group_name": group_name,
                    "condition_levels": self.manager._normalize_condition_levels(action.get("condition_levels")) if category == self.CAT_HF else "ALL",
                    "band": band if category == self.CAT_HF else "",
                    "frequency": frequency if category == self.CAT_HF else "",
                    "operating_group_frequency_key": frequency_key,
                    "software": "Local Net" if category == self.CAT_LOCAL else resource,
                    "mode": str(action.get("mode") or "").strip().upper(),
                    "action_label": str(action.get("action_label") or action_key).strip(),
                    "enabled": bool(action.get("enabled", True)),
                    "daily_start_utc": start_utc,
                    "daily_end_utc": self._add_minutes_hhmm(start_utc, duration),
                    "duration_minutes": duration,
                    "interval_minutes": interval,
                    "interval_phase_minutes": phase,
                    "interval_hours": max(1, int((interval + 59) // 60)),
                    "conflict_policy": self.manager._normalize_conflict_policy(action.get("conflict_policy")),
                    "daily_conflict_summary": "",
                    "net_conflict_summary": "",
                    "schedule_applied": bool(action.get("schedule_applied", True)),
                    "description": description,
                    "contact_target": self.ANY_ROLE_TOKEN if target == "ANY (ROLE MATCH)" else target,
                    "sort_order": row,
                }
            )
            if category == self.CAT_LOCAL and action_key == "local_monitor":
                action["contact_rule"] = "none"
                action["contact_target"] = ""
            actions.append(action)
        if not actions:
            raise ValueError("Add at least one action row.")
        profile_group = next((str(a.get("group_name") or "").strip().upper() for a in actions if a.get("group_name")), "")
        payload = {
            "id": int(self._selected_profile_id or 0),
            "name": name,
            "category": category,
            "operating_group": profile_group if category == self.CAT_HF else "",
            "secondary_group": "",
            "frequency": "",
            "sop_start_utc": "00:00",
            "priority": 100,
            "active": bool(self.active_cb.isChecked()),
            "window_hours": 24,
        }
        return payload, actions, None

    def _activation_row_builder_default_policy(self, diag: Dict[str, Any]) -> str:
        defaults = self._activation_conflict_defaults()
        if bool(diag.get("sop_conflicts")):
            return self.manager.CONFLICT_POLICY_SOP
        if bool(diag.get("daily_conflicts")) and defaults.get("hf_mode") == self.HF_CONFLICT_MODE_AUTO_ADJUST:
            return self.manager.CONFLICT_POLICY_SOP
        if bool(diag.get("net_conflicts")) and defaults.get("net_mode") == self.NET_CONFLICT_MODE_SOP_PRIORITY_TEMP:
            return self.manager.CONFLICT_POLICY_SOP
        return self.manager.CONFLICT_POLICY_SOP

    def _activation_row_default_conflict_policy(self, diag: Dict[str, Any], current_policy: Any) -> str:
        policy = self.manager._normalize_conflict_policy(current_policy)
        if policy in {self.manager.CONFLICT_POLICY_NET, self.manager.CONFLICT_POLICY_DAILY}:
            # Preserve explicit row policy choices (including workbench edits).
            return policy
        return self._activation_row_builder_default_policy(diag)

    def _build_sop_priority_net_policy_decisions(
        self,
        conflicts: List[Dict[str, Any]],
        *,
        profile_id: int,
    ) -> List[Dict[str, Any]]:
        decisions: List[Dict[str, Any]] = []
        for row in conflicts:
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
                    "policy": self.manager.NET_SOP_POLICY_SOP,
                    "resolution_note": "SOP Builder activation default (temporary SOP session)",
                }
            )
        return decisions

    def _apply_post_save_hf_activation_defaults(
        self,
        *,
        profile_id: int,
        profile_name: str,
        actions: List[Dict[str, Any]] | None = None,
    ) -> Tuple[List[str], bool]:
        notes: List[str] = []
        should_emit_refresh = False
        defaults = self._activation_conflict_defaults()
        profile_ids = {int(profile_id or 0)} if int(profile_id or 0) > 0 else set()
        explicit_non_sop_policy = False
        for row in (actions or []):
            if not isinstance(row, dict):
                continue
            policy = self.manager._normalize_conflict_policy(row.get("conflict_policy"))
            if policy in {self.manager.CONFLICT_POLICY_NET, self.manager.CONFLICT_POLICY_DAILY}:
                explicit_non_sop_policy = True
                break

        active_hf_conflicts: List[Dict[str, Any]] | None = None
        active_net_conflicts: List[Dict[str, Any]] | None = None

        def _load_active_hf_conflicts() -> List[Dict[str, Any]]:
            nonlocal active_hf_conflicts
            if active_hf_conflicts is not None:
                return list(active_hf_conflicts)
            try:
                rows = self.manager.collect_active_hf_conflicts()
            except Exception as e:
                log.debug("SOP Builder: active HF conflict scan after save failed: %s", e)
                rows = []
            if profile_ids:
                rows = [r for r in rows if int(r.get("profile_id") or 0) in profile_ids]
            active_hf_conflicts = list(rows)
            return list(active_hf_conflicts)

        def _load_active_net_conflicts() -> List[Dict[str, Any]]:
            nonlocal active_net_conflicts
            if active_net_conflicts is not None:
                return list(active_net_conflicts)
            try:
                rows = self.manager.collect_active_net_sop_conflicts(horizon_days=7, include_profile_ids=profile_ids or None)
            except Exception as e:
                log.debug("SOP Builder: active Net/SOP conflict scan after save failed: %s", e)
                rows = []
            active_net_conflicts = list(rows)
            return list(active_net_conflicts)

        win = self.window()
        daily_tab = getattr(win, "daily_tab", None)

        if defaults.get("net_mode") == self.NET_CONFLICT_MODE_SOP_PRIORITY_TEMP and not explicit_non_sop_policy:
            pending_net = [r for r in _load_active_net_conflicts() if not bool(r.get("has_policy"))]
            if pending_net:
                decisions = self._build_sop_priority_net_policy_decisions(pending_net, profile_id=profile_id)
                saved = 0
                if decisions:
                    try:
                        if daily_tab is not None and hasattr(daily_tab, "save_net_sop_conflict_policies_with_session_tracking"):
                            saved = int(
                                daily_tab.save_net_sop_conflict_policies_with_session_tracking(
                                    decisions,
                                    origin="SOP Builder activation default (Net SOP Priority)",
                                    session_profile_hint={
                                        "id": int(profile_id or 0),
                                        "name": str(profile_name or ""),
                                        "active": True,
                                        "category": "HF",
                                    },
                                )
                                or 0
                            )
                    except Exception as e:
                        log.debug("SOP Builder: failed applying temporary Net/SOP priority defaults: %s", e)
                        saved = 0
                if saved > 0:
                    notes.append(f"Net Schedule: applied temporary SOP Priority to {saved} overlap window(s) for this SOP session.")
                    should_emit_refresh = True
                else:
                    notes.append(
                        f"Net Schedule: detected {len(pending_net)} overlap window(s), but temporary SOP Priority could not be applied automatically. Review in Net Schedule."
                    )
            else:
                notes.append("Net Schedule: no unresolved Net/SOP overlaps found for this SOP.")
        elif defaults.get("net_mode") == self.NET_CONFLICT_MODE_SOP_PRIORITY_TEMP and explicit_non_sop_policy:
            pending_net = [r for r in _load_active_net_conflicts() if not bool(r.get("has_policy"))]
            if pending_net:
                notes.append(
                    f"Net Schedule: builder auto-apply skipped because explicit row policies were selected; review {len(pending_net)} overlap window(s) in Net Schedule."
                )
        else:
            pending_net = [r for r in _load_active_net_conflicts() if not bool(r.get("has_policy"))]
            if pending_net:
                notes.append(f"Net Schedule: review {len(pending_net)} overlap window(s) in Net Schedule to choose priority.")

        if defaults.get("hf_mode") == self.HF_CONFLICT_MODE_AUTO_ADJUST and not explicit_non_sop_policy:
            applied = False
            if daily_tab is not None:
                try:
                    if hasattr(daily_tab, "_refresh_sop_overlay_rows_in_table"):
                        daily_tab._refresh_sop_overlay_rows_in_table()
                    if hasattr(daily_tab, "_collect_active_time_conflict_pairs") and hasattr(daily_tab, "_can_auto_adjust_hf_around_sop"):
                        active_pairs = list(daily_tab._collect_active_time_conflict_pairs() or [])
                        if active_pairs and bool(daily_tab._can_auto_adjust_hf_around_sop(active_pairs)):
                            changed, detail = daily_tab._auto_adjust_hf_around_sop(active_pairs)
                            notes.append(f"HF Schedule: {detail}")
                            applied = True
                        elif _load_active_hf_conflicts():
                            notes.append("HF Schedule: active SOP conflicts remain. Review in Daily Schedule.")
                            applied = True
                    elif _load_active_hf_conflicts():
                        notes.append("HF Schedule: review active SOP conflicts in Daily Schedule.")
                        applied = True
                except Exception as e:
                    log.debug("SOP Builder: Daily auto-adjust via activation default failed: %s", e)
            if not applied and _load_active_hf_conflicts():
                notes.append("HF Schedule: review active SOP conflicts in Daily Schedule.")
        elif defaults.get("hf_mode") == self.HF_CONFLICT_MODE_AUTO_ADJUST and explicit_non_sop_policy:
            hf_conflicts = _load_active_hf_conflicts()
            if hf_conflicts:
                notes.append(
                    f"HF Schedule: builder auto-adjust skipped because explicit row policies were selected; review {len(hf_conflicts)} active conflict row(s) in Daily Schedule."
                )
        else:
            hf_conflicts = _load_active_hf_conflicts()
            if hf_conflicts:
                notes.append(f"HF Schedule: review {len(hf_conflicts)} active conflict row(s) in Daily Schedule.")

        return notes, should_emit_refresh

    def _resolve_hf_activation_conflicts(self, actions: List[Dict[str, Any]]) -> bool:
        hf_peer_actions = [
            a
            for a in actions
            if isinstance(a, dict) and not self.manager._is_local_action(a) and bool(a.get("enabled", True))
        ]
        conflicts: List[Dict[str, Any]] = []
        manual_conflicts: List[Dict[str, Any]] = []
        for idx, action in enumerate(actions):
            group = str(action.get("group_name") or "").strip().upper()
            if not group:
                continue
            diag = self.manager.detect_action_conflicts(
                action=action,
                operating_group=group,
                horizon_days=7,
                check_all_groups=True,
                peer_actions=hf_peer_actions,
            )
            action["daily_conflict_summary"] = str(diag.get("daily_summary") or "")
            action["net_conflict_summary"] = str(diag.get("net_summary") or "")
            if not bool(diag.get("has_conflict")):
                action["conflict_policy"] = self.manager.CONFLICT_POLICY_SOP
                continue
            action["conflict_policy"] = self._activation_row_default_conflict_policy(diag, action.get("conflict_policy"))
            policy = self.manager._normalize_conflict_policy(action.get("conflict_policy"))
            entry = {
                "row_index": idx,
                "action": action,
                "group": group,
                "diag": diag,
            }
            conflicts.append(
                entry
            )
            needs_manual = bool(diag.get("sop_conflicts")) or (
                policy in {self.manager.CONFLICT_POLICY_NET, self.manager.CONFLICT_POLICY_DAILY}
                and bool(diag.get("first_occurrence_conflict"))
            )
            if needs_manual:
                manual_conflicts.append(entry)
        if not conflicts:
            return True
        if not manual_conflicts:
            return True
        try:
            # Manual-first UX while editing; force open only at Save when unresolved
            # timing conflicts remain and user attention is needed.
            self._set_conflict_workbench_expanded(True, refresh=False)
            self._update_conflict_workbench(self._last_realtime_hf_analyses_cache, force=True)
            status_label = getattr(self, "conflict_workbench_status_label", None)
            if isinstance(status_label, QLabel):
                status_label.setText(
                    f"Save requires conflict resolution: {len(manual_conflicts)} row(s) still need timing changes."
                )
        except Exception:
            pass
        return self._resolve_hf_conflicts_dialog(
            manual_conflicts,
            hf_peer_actions,
            total_conflict_count=len(conflicts),
        )

    def _resolve_hf_conflicts_dialog(
        self,
        conflicts: List[Dict[str, Any]],
        hf_peer_actions: List[Dict[str, Any]],
        *,
        total_conflict_count: int | None = None,
    ) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle("SOP Conflict Resolution")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        intro_lines = [
            "Review conflict policy and Daily Start for each row.",
            "SOP-vs-SOP overlaps, and first-occurrence conflicts under Net/Daily priority, must be resolved before Save.",
        ]
        try:
            total_count = int(total_conflict_count or 0)
        except Exception:
            total_count = 0
        if total_count > len(conflicts):
            intro_lines.append(
                f"Only {len(conflicts)} of {total_count} conflicting row(s) need manual changes; other rows will follow SOP Builder activation defaults."
            )
        intro = QLabel("\n".join(intro_lines))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        table = QTableWidget(len(conflicts), 8, dialog)
        table.setHorizontalHeaderLabels(
            [
                "Row",
                "Action",
                "Group",
                "Daily Conflicts",
                "Net Conflicts",
                "SOP Conflicts",
                "Policy",
                "Start",
            ]
        )
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeToContents)
        table.setColumnWidth(7, 100)

        def _set_readonly_text(row: int, col: int, text: str) -> None:
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            table.setItem(row, col, item)

        for row, entry in enumerate(conflicts):
            action = entry["action"]
            diag = entry["diag"]
            group = entry["group"]
            policy = self.manager._normalize_conflict_policy(action.get("conflict_policy"))
            action["conflict_policy"] = policy

            _set_readonly_text(row, 0, str(int(entry.get("row_index", row)) + 1))
            _set_readonly_text(row, 1, str(action.get("action_label") or "Action"))
            _set_readonly_text(row, 2, group)
            _set_readonly_text(row, 3, str(diag.get("daily_summary") or "None"))
            _set_readonly_text(row, 4, str(diag.get("net_summary") or "None"))
            _set_readonly_text(row, 5, str(diag.get("sop_summary") or "None"))

            policy_combo = QComboBox(table)
            policy_combo.addItem("SOP Priority", self.manager.CONFLICT_POLICY_SOP)
            policy_combo.addItem("Net Priority", self.manager.CONFLICT_POLICY_NET)
            policy_combo.addItem("Daily Priority", self.manager.CONFLICT_POLICY_DAILY)
            if policy == self.manager.CONFLICT_POLICY_NET:
                policy_combo.setCurrentIndex(1)
            elif policy == self.manager.CONFLICT_POLICY_DAILY:
                policy_combo.setCurrentIndex(2)
            else:
                policy_combo.setCurrentIndex(0)
            table.setCellWidget(row, 6, policy_combo)

            display_start = self._display_start_hhmm_from_utc(
                str(action.get("daily_start_utc") or "00:00"),
                show_local=self._show_local,
            )
            start_edit = QLineEdit(display_start, table)
            start_edit.setPlaceholderText("HH:MM")
            start_edit.setMaxLength(5)
            start_edit.setAlignment(Qt.AlignCenter)
            if bool(diag.get("sop_conflicts")) or (
                policy in {self.manager.CONFLICT_POLICY_NET, self.manager.CONFLICT_POLICY_DAILY}
                and bool(diag.get("first_occurrence_conflict"))
            ):
                suggested_utc = self.manager.suggest_non_conflicting_start(
                    action=action,
                    operating_group=group,
                    check_all_groups=True,
                    peer_actions=hf_peer_actions,
                )
                suggested_display = self._display_start_hhmm_from_utc(suggested_utc, show_local=self._show_local)
                start_edit.setText(suggested_display)
                start_edit.setToolTip(f"Suggested non-conflicting start: {suggested_display}")
            table.setCellWidget(row, 7, start_edit)

            entry["policy_combo"] = policy_combo
            entry["start_edit"] = start_edit

        layout.addWidget(table)
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        dialog.resize(1200, min(620, 220 + (len(conflicts) * 38)))

        while True:
            if dialog.exec() != QDialog.Accepted:
                return False

            invalid_rows: List[str] = []
            for entry in conflicts:
                action = entry["action"]
                policy_combo = entry.get("policy_combo")
                start_edit = entry.get("start_edit")
                policy = self.manager._normalize_conflict_policy(
                    policy_combo.currentData() if isinstance(policy_combo, QComboBox) else action.get("conflict_policy")
                )
                action["conflict_policy"] = policy
                candidate = ""
                if isinstance(start_edit, QLineEdit):
                    candidate = str(start_edit.text() or "").strip()
                if not self._is_valid_hhmm(candidate):
                    invalid_rows.append(str(int(entry.get("row_index", 0)) + 1))
                    continue
                new_start_utc = self._utc_start_hhmm_from_display(candidate, show_local=self._show_local)
                action["daily_start_utc"] = new_start_utc
                action["daily_end_utc"] = self._add_minutes_hhmm(new_start_utc, int(action.get("duration_minutes") or 60))

            if invalid_rows:
                QMessageBox.warning(
                    self,
                    "SOP Conflict Resolution",
                    "Start time must be HH:MM for row(s): " + ", ".join(invalid_rows),
                )
                continue

            unresolved: List[str] = []
            for entry in conflicts:
                action = entry["action"]
                group = entry["group"]
                policy = self.manager._normalize_conflict_policy(action.get("conflict_policy"))
                validate = self.manager.detect_action_conflicts(
                    action=action,
                    operating_group=group,
                    horizon_days=7,
                    check_all_groups=True,
                    peer_actions=hf_peer_actions,
                )
                action["daily_conflict_summary"] = str(validate.get("daily_summary") or "")
                action["net_conflict_summary"] = str(validate.get("net_summary") or "")

                has_sop_conflict = bool(validate.get("sop_conflicts"))
                has_first_occurrence_conflict = bool(validate.get("first_occurrence_conflict"))
                if not has_sop_conflict and not (
                    policy in {self.manager.CONFLICT_POLICY_NET, self.manager.CONFLICT_POLICY_DAILY}
                    and has_first_occurrence_conflict
                ):
                    continue

                suggested_utc = self.manager.suggest_non_conflicting_start(
                    action=action,
                    operating_group=group,
                    check_all_groups=True,
                    peer_actions=hf_peer_actions,
                )
                suggested_display = self._display_start_hhmm_from_utc(suggested_utc, show_local=self._show_local)
                start_edit = entry.get("start_edit")
                if isinstance(start_edit, QLineEdit):
                    start_edit.setText(suggested_display)

                row_num = int(entry.get("row_index", 0)) + 1
                action_name = str(action.get("action_label") or "Action")
                if has_sop_conflict:
                    reason = "overlaps another SOP action"
                else:
                    reason = "still conflicts for first occurrence under the selected policy"
                unresolved.append(f"Row {row_num} ({action_name}): {reason}. Suggested start {suggested_display}.")

            if unresolved:
                preview = unresolved[:8]
                extra = ""
                if len(unresolved) > len(preview):
                    extra = f"\n...and {len(unresolved) - len(preview)} more row(s)."
                QMessageBox.warning(
                    self,
                    "SOP Conflict Resolution",
                    "Some rows still need adjustment before Save:\n\n"
                    + "\n".join(preview)
                    + extra
                    + "\n\nUpdated suggestions have been applied in the Start column.",
                )
                continue
            return True

    def _pending_sop_schedule_layer_rows(
        self,
        profile_id: int,
        payload: Dict[str, Any],
        actions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        try:
            category = self.manager._normalize_category(payload.get("category"))
        except Exception:
            category = str(payload.get("category") or "").strip().upper()
        if category == self.CAT_LOCAL:
            return []

        rows: List[Dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            if not bool(action.get("enabled", True)):
                continue
            if self.manager._is_local_action(action):
                continue
            if not bool(action.get("schedule_applied", True)):
                continue
            band = str(action.get("band") or "").strip().upper()
            freq = self.manager._normalize_frequency(action.get("frequency"))
            if not band or not freq:
                continue
            group_name = str(action.get("group_name") or payload.get("operating_group") or "").strip().upper()
            rows.append(
                {
                    "source": "SOP",
                    "sop_profile_id": int(profile_id or 0),
                    "profile_id": int(profile_id or 0),
                    "day_utc": "ALL",
                    "recurrence": "Daily",
                    "biweekly_offset_weeks": 0,
                    "month_weeks": "",
                    "condition_levels": self.manager._normalize_condition_levels(action.get("condition_levels")),
                    "group_name": group_name,
                    "band": band,
                    "mode": str(action.get("mode") or "").strip().upper() or "DIGI",
                    "vfo": "A",
                    "frequency": freq,
                    "start_utc": self.manager._normalize_hhmm(action.get("daily_start_utc") or "00:00"),
                    "end_utc": self.manager._normalize_hhmm(action.get("daily_end_utc") or "23:59"),
                    "enabled": True,
                }
            )

        dedup: Dict[Tuple[str, str, str, str, str, str, str, str], Dict[str, Any]] = {}
        for row in rows:
            key = (
                str(row.get("day_utc") or "").upper(),
                str(row.get("band") or "").upper(),
                str(row.get("mode") or "").upper(),
                str(row.get("frequency") or ""),
                str(row.get("start_utc") or ""),
                str(row.get("end_utc") or ""),
                str(row.get("group_name") or "").upper(),
                self.manager._normalize_condition_levels(row.get("condition_levels")),
            )
            dedup[key] = row
        final_rows = list(dedup.values())
        final_rows.sort(
            key=lambda row: (
                str(row.get("day_utc") or ""),
                str(row.get("start_utc") or ""),
                str(row.get("band") or ""),
                str(row.get("frequency") or ""),
            )
        )
        for idx, row in enumerate(final_rows):
            row["sort_order"] = idx
        return final_rows

    def _format_rf_guard_sop_impacts(self, impacts: List[Dict[str, Any]]) -> List[str]:
        lines: List[str] = []
        for impact in impacts[:8]:
            device = impact.get("device") if isinstance(impact.get("device"), dict) else {}
            plan = impact.get("plan") if isinstance(impact.get("plan"), dict) else {}
            validation = impact.get("validation") if isinstance(impact.get("validation"), dict) else {}
            radio_name = str(device.get("name") or f"Radio {device.get('id') or ''}").strip() or "Radio"
            plan_name = str(plan.get("name") or f"Plan {plan.get('id') or ''}").strip() or "Frequency Plan"
            messages = [str(m).strip() for m in validation.get("messages", []) if str(m).strip()]
            detail = messages[0] if messages else "RF Guard reported an assignment issue."
            lines.append(f"- {radio_name} / {plan_name}: {detail}")
        if len(impacts) > len(lines):
            lines.append(f"- +{len(impacts) - len(lines)} more")
        return lines

    def _confirm_rf_guard_sop_update(
        self,
        profile_id: int,
        profile_name: str,
        pending_layer_rows: List[Dict[str, Any]],
    ) -> bool:
        try:
            impacts = assigned_plan_rf_guard_impacts_for_sop_update(profile_id, pending_layer_rows)
        except Exception as exc:
            log.debug("SOP Builder: RF Guard impact scan failed before save: %s", exc)
            response = QMessageBox.question(
                self,
                "RF Guard Check Unavailable",
                "RF Guard could not check assigned master schedules before updating this SOP.\n\n"
                f"{exc}\n\nSave this SOP anyway?",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            return response == QMessageBox.Save
        if not impacts:
            return True

        blocked = [
            impact
            for impact in impacts
            if str((impact.get("validation") or {}).get("state") or "").strip().lower() == "blocked"
        ]
        warning = [
            impact
            for impact in impacts
            if str((impact.get("validation") or {}).get("state") or "").strip().lower() == "warning"
        ]
        lines = self._format_rf_guard_sop_impacts(blocked or warning)
        name = str(profile_name or "this SOP").strip() or "this SOP"
        if blocked:
            QMessageBox.warning(
                self,
                "RF Guard Blocked Update",
                f"Updating '{name}' would create an RF Guard conflict in an assigned master schedule.\n\n"
                + "\n".join(lines)
                + "\n\nThe SOP was not saved.",
            )
            return False
        response = QMessageBox.question(
            self,
            "RF Guard Warning",
            f"Updating '{name}' affects an assigned master schedule and RF Guard found warning(s).\n\n"
            + "\n".join(lines)
            + "\n\nSave this SOP anyway?",
            QMessageBox.Save | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return response == QMessageBox.Save

    def _traffic_suggestion_rf_guard_impacts(self, decision: Any) -> List[Dict[str, Any]]:
        """Return assigned-plan RF Guard impacts for a traffic-suggested level change."""
        try:
            profile_id = int(str(getattr(decision, "sop_profile_id", "") or "0").strip() or "0")
        except Exception:
            profile_id = 0
        if profile_id <= 0:
            return []
        profile = self.manager.get_profile(profile_id)
        if not isinstance(profile, dict):
            return []
        rows = [dict(row) for row in schedule_layer_rows_for_condition_decision(profile, decision)]
        if not rows:
            return []
        return list(assigned_plan_rf_guard_impacts_for_sop_update(profile_id, rows) or [])

    def _save_profile(self) -> None:
        timer = getattr(self, "_realtime_conflict_timer", None)
        if timer is not None:
            timer.stop()
        prev_suppress = getattr(self, "_suppress_realtime_conflict_checks", False)
        self._suppress_realtime_conflict_checks = True
        try:
            prior_profile = self.manager.get_profile(int(self._selected_profile_id or 0)) or {}
            payload, actions, _schedule_layer = self._collect_profile_payload()
            if payload.get("category") == self.CAT_HF and bool(payload.get("active")):
                self._ensure_summary_compatibility_projection()
                if not self._resolve_hf_activation_conflicts(actions):
                    return
            if payload.get("category") == self.CAT_HF and int(payload.get("id") or 0) > 0:
                pending_layer_rows = self._pending_sop_schedule_layer_rows(
                    int(payload.get("id") or 0),
                    payload,
                    actions,
                )
                if not self._confirm_rf_guard_sop_update(
                    int(payload.get("id") or 0),
                    str(payload.get("name") or ""),
                    pending_layer_rows,
                ):
                    return
            profile_id = self.manager.save_profile(payload, actions, schedule_layer=None)
            self._reload_profiles(select_id=profile_id)
            self._set_save_dirty(False)
            self._emit_sop_data_changed()
            post_save_notes: List[str] = []
            try:
                category = self.manager._normalize_category(payload.get("category"))
            except Exception:
                category = str(payload.get("category") or "").strip().upper()
            prior_active = bool(prior_profile.get("active"))
            new_active = bool(payload.get("active"))
            if category == self.CAT_HF:
                win = self.window()
                daily_tab = getattr(win, "daily_tab", None)
                activated_now = (not prior_active) and new_active
                if activated_now and daily_tab is not None and hasattr(daily_tab, "register_sop_session_activation"):
                    try:
                        daily_tab.register_sop_session_activation(profile_id, str(payload.get("name") or ""))
                    except Exception:
                        pass
                if activated_now:
                    try:
                        notes, should_emit_refresh = self._apply_post_save_hf_activation_defaults(
                            profile_id=int(profile_id or 0),
                            profile_name=str(payload.get("name") or ""),
                            actions=actions,
                        )
                        post_save_notes.extend([str(n).strip() for n in notes if str(n).strip()])
                        if should_emit_refresh:
                            self._emit_sop_data_changed()
                    except Exception as e:
                        log.debug("SOP Builder: post-save activation defaults failed: %s", e)
                if prior_active and not new_active and daily_tab is not None and hasattr(daily_tab, "prompt_sop_return_to_normal_after_deactivation"):
                    try:
                        daily_tab.prompt_sop_return_to_normal_after_deactivation([int(profile_id or 0)], origin_label="SOP Builder")
                    except Exception:
                        pass
            message_lines = ["SOP saved."]
            if post_save_notes:
                message_lines.append("")
                message_lines.extend(post_save_notes)
            self.refresh_traffic_suggestions()
            QMessageBox.information(self, "SOP", "\n".join(message_lines))
        except Exception as e:
            QMessageBox.warning(self, "SOP", str(e))
        finally:
            self._suppress_realtime_conflict_checks = prev_suppress

    def _delete_profile(self) -> None:
        if int(self._selected_profile_id or 0) <= 0:
            return
        resp = QMessageBox.question(
            self,
            "Clear SOP",
            "Clear all action rows for this SOP category?",
        )
        if resp != QMessageBox.Yes:
            return
        profile = self.manager.get_profile(int(self._selected_profile_id or 0))
        if not profile:
            return
        payload = dict(profile)
        payload["active"] = False
        payload["id"] = int(profile.get("id") or 0)
        actions: List[Dict[str, Any]] = []
        if self.manager._normalize_category(profile.get("category")) == self.CAT_HF:
            if not self._confirm_rf_guard_sop_update(
                int(profile.get("id") or 0),
                str(profile.get("name") or ""),
                [],
            ):
                return
        self.manager.save_profile(payload, actions, schedule_layer=None)
        self._reload_profiles(select_id=int(profile.get("id") or 0))
        self._set_save_dirty(False)
        self._emit_sop_data_changed()
        if self.manager._normalize_category(profile.get("category")) == self.CAT_HF and bool(profile.get("active")):
            try:
                win = self.window()
                daily_tab = getattr(win, "daily_tab", None)
                if daily_tab is not None and hasattr(daily_tab, "prompt_sop_return_to_normal_after_deactivation"):
                    daily_tab.prompt_sop_return_to_normal_after_deactivation([int(profile.get("id") or 0)], origin_label="SOP Builder")
            except Exception:
                pass

    def refresh_upcoming(self) -> None:
        self._upcoming_rows = []

    def on_condition_levels_changed(self) -> None:
        try:
            self.settings.reload()
        except Exception:
            pass
        self._refresh_reference_data()
        self._refresh_all_rows_dynamic_options()
        self._refresh_inline_conflict_badges()
        self._schedule_realtime_hf_conflict_check()

    def _operating_plan_inputs_summary_text(self) -> str:
        try:
            context = self.plan_context_service.context_for_tab("sop", refresh=True)
        except Exception:
            context = None
        if context is None:
            return "Operating Plan Inputs: no active Frequency Plan context."
        ref_counts: List[str] = []
        if context.source_ref_count:
            ref_counts.append(f"{context.source_ref_count} source{'s' if context.source_ref_count != 1 else ''}")
        if context.schedule_ref_count:
            ref_counts.append(
                f"{context.schedule_ref_count} schedule ref{'s' if context.schedule_ref_count != 1 else ''}"
            )
        if context.frequency_ref_count:
            ref_counts.append(
                f"{context.frequency_ref_count} frequency ref{'s' if context.frequency_ref_count != 1 else ''}"
            )
        if context.group_ref_count:
            ref_counts.append(f"{context.group_ref_count} group ref{'s' if context.group_ref_count != 1 else ''}")
        source_text = ", ".join(ref_counts) if ref_counts else "no source refs yet"
        mode = "receive-only" if context.receive_only else "transmit-capable"
        return (
            f"Operating Plan Inputs: {context.plan_label} assigned to {context.radio_label}; "
            f"{mode}; {source_text}."
        )

    def _refresh_operating_plan_inputs_summary(self) -> None:
        if not hasattr(self, "operating_plan_inputs_label"):
            return
        self.operating_plan_inputs_label.setText(self._operating_plan_inputs_summary_text())

    def refresh_traffic_suggestions(self) -> None:
        label = getattr(self, "traffic_suggestions_label", None)
        if label is None:
            return
        self._traffic_suggestion_decisions = []
        apply_btn = getattr(self, "traffic_suggestions_apply_btn", None)
        if apply_btn is not None:
            apply_btn.setEnabled(False)
        focus_prefix = self._traffic_focus_prefix()
        try:
            db_path = self._condition_sop_db_path()
        except Exception:
            db_path = None
        if not db_path or not Path(db_path).exists():
            label.setText(f"{focus_prefix}Traffic Suggestions: no message activity database found yet.")
            return
        try:
            audit_text = self._condition_sop_audit_text(db_path)
            since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()
            snapshot = operational_activity_snapshot(
                db_path,
                ObservationQuery(source_family="condition_alert", since_utc=since, limit=25),
                limit=25,
            )
            alerts = tuple(getattr(snapshot, "condition_alerts", ()) or ())
            if not alerts:
                label.setText(self._traffic_suggestions_with_audit(f"{focus_prefix}Traffic Suggestions: no recent condition alerts in the last 24 hours.", audit_text))
                return
            profiles = self._condition_sop_profiles()
            if not profiles:
                label.setText(self._traffic_suggestions_with_audit(f"{focus_prefix}Traffic Suggestions: recent condition alerts found, but no SOP layers are configured.", audit_text))
                return
            auto_allowed = bool(self.settings.get(AUTO_SOP_INVOCATION_SETTING_KEY, False))
            decisions = evaluate_condition_sop_invocations(
                alerts[:5],
                sop_profiles=profiles,
                auto_apply_enabled=auto_allowed,
            )
            visible = [
                decision
                for decision in decisions
                if decision.operating_group or decision.condition_level is not None or decision.sop_profile_name
            ]
            if not visible:
                label.setText(self._traffic_suggestions_with_audit(f"{focus_prefix}Traffic Suggestions: recent condition alerts found, but none match an SOP layer.", audit_text))
                return
            actionable = [d for d in visible if not bool(getattr(d, "blocked", False))]
            self._traffic_suggestion_decisions = actionable
            if apply_btn is not None:
                apply_btn.setEnabled(bool(actionable))
            label.setText(
                self._traffic_suggestions_with_audit(
                    f"{focus_prefix}Traffic Suggestions: " + " | ".join(self._traffic_suggestion_text(d) for d in visible[:3]),
                    audit_text,
                )
            )
        except Exception as e:
            log.debug("SOP Builder traffic suggestions unavailable: %s", e)
            label.setText(f"{focus_prefix}Traffic Suggestions: unavailable.")

    def focus_traffic_context(
        self,
        *,
        group: str = "",
        topic: str = "",
        source_family: str = "",
    ) -> None:
        """Focus SOP review around a map/message traffic context."""
        self._traffic_focus_context = {
            "group": str(group or "").strip().upper().lstrip("@"),
            "topic": str(topic or "").strip(),
            "source_family": str(source_family or "").strip(),
        }
        try:
            box = getattr(self, "traffic_suggestions_box", None)
            if box is not None:
                box.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass
        self.refresh_traffic_suggestions()

    def _traffic_focus_prefix(self) -> str:
        context = getattr(self, "_traffic_focus_context", {}) or {}
        if not isinstance(context, Mapping):
            return ""
        parts = [
            str(context.get("group") or "").strip(),
            str(context.get("topic") or "").strip(),
            str(context.get("source_family") or "").strip(),
        ]
        text = " | ".join(part for part in parts if part)
        return f"Map context: {text}. " if text else ""

    def _condition_sop_db_path(self) -> Path | None:
        try:
            return Path(self.settings.config_dir) / "freqinout_nets.db"
        except Exception:
            return None

    def _condition_sop_audit_text(self, db_path: Path) -> str:
        try:
            summary = condition_sop_audit_summary(db_path, limit=10)
        except Exception:
            return ""
        display = condition_sop_audit_display(summary)
        if not display.text:
            return ""
        return display.text

    @staticmethod
    def _traffic_suggestions_with_audit(text: str, audit_text: str) -> str:
        base = str(text or "").strip()
        audit = str(audit_text or "").strip()
        if not audit:
            return base
        return f"{base} {audit}"

    def _apply_first_traffic_suggestion(self) -> None:
        decisions = list(getattr(self, "_traffic_suggestion_decisions", []) or [])
        decision = decisions[0] if decisions else None
        if decision is None:
            QMessageBox.information(self, "Traffic Suggestions", "No matching condition alert is ready to apply.")
            return
        group = str(getattr(decision, "operating_group", "") or "").strip().upper()
        level = getattr(decision, "condition_level", None)
        sop_name = str(getattr(decision, "sop_profile_name", "") or "").strip() or "matching SOP"
        if not group or level is None:
            QMessageBox.information(self, "Traffic Suggestions", "The selected condition alert is missing a group or level.")
            return
        response = QMessageBox.question(
            self,
            "Apply Condition Level",
            f"Apply {group} condition level {level} and refresh SOP projections?\n\n"
            f"Matching SOP: {sop_name}",
            QMessageBox.Apply | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if response != QMessageBox.Apply:
            return
        try:
            impacts = self._traffic_suggestion_rf_guard_impacts(decision)
            if impacts:
                blocked = [
                    impact
                    for impact in impacts
                    if str((impact.get("validation") or {}).get("state") or "").strip().lower() == "blocked"
                ]
                warning = [
                    impact
                    for impact in impacts
                    if str((impact.get("validation") or {}).get("state") or "").strip().lower() == "warning"
                ]
                lines = self._format_rf_guard_sop_impacts(blocked or warning or impacts)
                QMessageBox.warning(
                    self,
                    "RF Guard Blocks Condition Level",
                    "Applying this condition level would affect an assigned Frequency Plan with RF Guard issues.\n\n"
                    + "\n".join(lines)
                    + "\n\nThe condition level was not changed.",
                )
                return
            result = apply_operating_group_condition_level(
                {"operating_groups": self.settings.get("operating_groups", []) or []},
                operating_group=group,
                condition_level=int(level),
                create_if_missing=False,
            )
            if result.warnings:
                QMessageBox.warning(
                    self,
                    "Condition Level Not Applied",
                    "\n".join(result.warnings),
                )
                return
            self.settings.set("operating_groups", result.settings_data.get("operating_groups", []))
            self._refresh_after_condition_level_change()
            QMessageBox.information(
                self,
                "Condition Level Applied",
                f"{result.operating_group} condition level is now {result.condition_level}.",
            )
        except Exception as e:
            QMessageBox.warning(self, "Condition Level Not Applied", str(e))

    def _refresh_after_condition_level_change(self) -> None:
        try:
            self.settings.reload()
        except Exception:
            pass
        self._hf_group_condition_meta_cache = None
        self._condition_level_selector_values_cache = None
        self._refresh_reference_data()
        self._refresh_all_rows_dynamic_options()
        self._schedule_realtime_hf_conflict_check()
        win = self.window()
        if win is not None and hasattr(win, "notify_condition_levels_changed"):
            try:
                win.notify_condition_levels_changed()
            except Exception:
                pass
        self.refresh_traffic_suggestions()

    def _open_condition_sop_automation_review(self) -> None:
        db_path = self._condition_sop_db_path()
        if not db_path or not db_path.exists():
            QMessageBox.information(
                self,
                "SOP Automation Review",
                "No message activity database is available yet.",
            )
            return
        try:
            rows = list(list_condition_sop_invocation_audit(db_path, limit=50))
        except Exception as e:
            QMessageBox.warning(self, "SOP Automation Review", f"Could not load SOP automation history: {e}")
            return
        if not rows:
            QMessageBox.information(
                self,
                "SOP Automation Review",
                "No SOP automation history has been recorded yet.",
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("SOP Automation Review")
        layout = QVBoxLayout(dialog)
        intro = QLabel(
            "Recent condition-alert automation decisions. Rows created before revert tracking are review-only."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        table = QTableWidget(len(rows), 6, dialog)
        table.setHorizontalHeaderLabels(["When", "Status", "Group", "Level", "SOP", "Message"])
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        for row_idx, row in enumerate(rows):
            values = self._condition_sop_audit_table_values(row)
            for col_idx, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col_idx == 0:
                    item.setData(Qt.UserRole, int(row.get("id") or 0))
                table.setItem(row_idx, col_idx, item)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        table.setMinimumHeight(260)
        layout.addWidget(table)

        buttons = QHBoxLayout()
        revert_btn = QPushButton("Revert Latest Applied")
        revert_btn.setToolTip("Restore the previous condition level from the newest applied automation row that captured before-state.")
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        buttons.addStretch(1)
        buttons.addWidget(revert_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        def _revert() -> None:
            if self._revert_latest_condition_sop_audit(rows):
                dialog.accept()

        revert_btn.clicked.connect(_revert)
        dialog.resize(920, 420)
        dialog.exec()

    @staticmethod
    def _condition_sop_audit_table_values(row: Mapping[str, Any]) -> tuple[str, str, str, str, str, str]:
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        message = str(
            payload.get("message")
            or payload.get("summary")
            or payload.get("reason")
            or payload.get("revert_summary")
            or ""
        ).strip()
        if not message:
            reasons = payload.get("reasons")
            if isinstance(reasons, (list, tuple)):
                message = "; ".join(str(reason) for reason in reasons if str(reason).strip())
        message = " ".join(message.split())
        if len(message) > 110:
            message = f"{message[:107].rstrip()}..."
        level = row.get("condition_level")
        return (
            str(row.get("created_utc") or ""),
            str(row.get("status") or ""),
            str(row.get("operating_group") or ""),
            "" if level is None else str(level),
            str(row.get("sop_profile_name") or ""),
            message,
        )

    def _revert_latest_condition_sop_audit(self, rows: Sequence[Mapping[str, Any]] | None = None) -> bool:
        db_path = self._condition_sop_db_path()
        if not db_path or not db_path.exists():
            QMessageBox.information(self, "SOP Automation Review", "No message activity database is available yet.")
            return False
        if rows is None:
            try:
                rows = list_condition_sop_invocation_audit(db_path, limit=50)
            except Exception as e:
                QMessageBox.warning(self, "SOP Automation Review", f"Could not load SOP automation history: {e}")
                return False
        applied = next(
            (
                row
                for row in rows
                if str(row.get("status") or "").strip().lower() == "applied"
            ),
            None,
        )
        if applied is None:
            QMessageBox.information(
                self,
                "SOP Automation Review",
                "No applied SOP automation row is available to revert.",
            )
            return False
        current = {"operating_groups": self.settings.get("operating_groups", []) or []}
        result = revert_condition_sop_audit_row(current, applied)
        if result.warnings:
            QMessageBox.warning(self, "SOP Automation Review", "\n".join(result.warnings))
            return False
        response = QMessageBox.question(
            self,
            "Revert SOP Automation",
            f"Revert {result.operating_group} to the condition level saved before automation audit #{applied.get('id')}?",
            QMessageBox.Apply | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if response != QMessageBox.Apply:
            return False
        try:
            self.settings.set("operating_groups", result.settings_data.get("operating_groups", []))
            append_condition_sop_invocation_audit(
                db_path,
                {
                    "event": "condition_sop_revert",
                    "decision": "revert",
                    "operating_group": result.operating_group,
                    "condition_level": applied.get("condition_level"),
                    "reverted_audit_id": applied.get("id"),
                    "restored_rows": result.restored_rows,
                    "revert_summary": f"Restored {result.restored_rows} {result.operating_group} condition row(s).",
                },
                status="reverted",
            )
            self._refresh_after_condition_level_change()
            QMessageBox.information(
                self,
                "SOP Automation Reverted",
                f"Restored {result.restored_rows} {result.operating_group} condition row(s).",
            )
            return True
        except Exception as e:
            QMessageBox.warning(self, "SOP Automation Review", f"Could not revert SOP automation: {e}")
            return False

    def _condition_sop_profiles(self) -> List[Dict[str, Any]]:
        profiles: List[Dict[str, Any]] = []
        try:
            summaries = list(self.manager.list_profiles())
        except Exception:
            summaries = []
        for summary in summaries:
            try:
                profile_id = int(summary.get("id") or summary.get("profile_id") or 0)
            except Exception:
                profile_id = 0
            if profile_id <= 0:
                continue
            try:
                profile = self.manager.get_profile(profile_id)
            except Exception as e:
                log.debug("SOP Builder could not load SOP profile %s for traffic suggestions: %s", profile_id, e)
                continue
            if not isinstance(profile, dict):
                continue
            if profile.get("schedule_layer"):
                profiles.append(profile)
        return profiles

    @staticmethod
    def _traffic_suggestion_text(decision: Any) -> str:
        group = str(getattr(decision, "operating_group", "") or "").strip() or "group"
        level = getattr(decision, "condition_level", None)
        level_text = f"L{level}" if level is not None else "condition"
        sop_name = str(getattr(decision, "sop_profile_name", "") or "").strip() or "matching SOP"
        if bool(getattr(decision, "blocked", False)) or str(getattr(decision, "decision", "") or "") == "blocked":
            reason = "; ".join(str(r) for r in getattr(decision, "reasons", ()) if str(r).strip())
            return f"{group} {level_text} blocked: {reason or sop_name}"
        if bool(getattr(decision, "should_apply", False)):
            return f"{group} {level_text} ready: {sop_name}"
        if str(getattr(decision, "decision", "") or "") == "suggest":
            return f"{group} {level_text} suggested: {sop_name}"
        return f"{group} {level_text} review: {sop_name}"

    def on_hf_schedule_saved(self) -> None:
        self._clear_hf_schedule_slot_cache()

    def on_settings_saved(self) -> None:
        try:
            self.settings.reload()
        except Exception:
            pass
        try:
            self.plan_context_label.invalidate_context()
            self.plan_context_label.refresh_context(refresh=True)
            self._refresh_operating_plan_inputs_summary()
        except Exception:
            pass
        selected_id = int(self._selected_profile_id or 0)
        self._load_activation_conflict_defaults_ui()
        self._refresh_reference_data()
        self._reload_profiles(select_id=selected_id)
        self._update_clock_labels()
        self.refresh_traffic_suggestions()

    def on_local_net_profiles_updated(self) -> None:
        try:
            self.settings.reload()
        except Exception:
            pass
        self._refresh_reference_data()
        self._refresh_all_rows_dynamic_options()

    def on_tab_activated(self) -> None:
        with perf_span(
            "sop.on_tab_activated",
            settings=self.settings,
            meta={"rows": int(self.actions_table.rowCount())},
            min_ms=0.0,
        ):
            self._update_clock_labels()

    def open_local_net_context(self, intent: object) -> bool:
        """Focus a linked SOP with stable Local Net reminder context."""
        if not isinstance(intent, NavigationIntent) or intent.sop_id is None:
            return False
        self._local_net_navigation_intent = intent
        selected = self.select_profile(intent.sop_id)
        schedule_name = intent.local_net_schedule_id or "Local Net"
        group_name = "Community / Unassigned"
        try:
            from freqinout.core.known_operating_groups import net_resources_db_path
            from freqinout.core.local_net_store import LocalNetStore

            schedule = LocalNetStore(net_resources_db_path()).get_schedule(
                intent.local_net_schedule_id or ""
            )
            if schedule is not None:
                schedule_name = schedule.name
                group_name = schedule.operating_group_name or group_name
        except Exception as exc:
            log.debug("SOP Local Net context lookup unavailable: %s", exc)
        occurrence = str(intent.draft_snapshot.get("occurrence_key") or "")
        status = "Linked SOP selected" if selected else "Linked SOP is unavailable"
        self.local_net_context_label.setText(
            f"{schedule_name} · {group_name} · {occurrence}. {status}. "
            "Review is manual and does not change station or radio state."
        )
        self.local_net_context_bar.setVisible(True)
        self.local_net_context_bar.setFocus(Qt.OtherFocusReason)
        return selected

    def _return_from_local_net_context(self) -> None:
        intent = self._local_net_navigation_intent
        if intent is None:
            return
        self._local_net_navigation_intent = None
        self.local_net_context_bar.setVisible(False)
        self.local_net_return_requested.emit(intent)

    def on_sop_profiles_updated(self) -> None:
        self._reload_profiles(select_id=int(self._selected_profile_id or 0))

    def select_profile(self, profile_id: int) -> bool:
        try:
            target = int(profile_id or 0)
        except Exception:
            return False
        if target <= 0:
            return False
        self._reload_profiles(select_id=target)
        return int(self._selected_profile_id or 0) == target

    def focus_source_segment(self, segment: Any) -> bool:
        raw = getattr(segment, "raw", {}) if segment is not None else {}
        try:
            profile_id = int(raw.get("sop_profile_id") or raw.get("profile_id") or 0)
        except Exception:
            profile_id = 0
        if profile_id > 0:
            self.select_profile(profile_id)
        return False

    def _import_profile(self) -> None:
        timer = getattr(self, "_realtime_conflict_timer", None)
        if timer is not None:
            timer.stop()
        prev_suppress = getattr(self, "_suppress_realtime_conflict_checks", False)
        self._suppress_realtime_conflict_checks = True
        try:
            src, _ = QFileDialog.getOpenFileName(self, "Import SOP", "", "JSON Files (*.json)")
            if not src:
                return
            payload = json.loads(Path(src).read_text(encoding="utf-8"))
            profile = payload.get("profile")
            if not isinstance(profile, dict):
                raise ValueError("Invalid SOP import payload.")
            category = self.manager._normalize_category(profile.get("category"))
            target_profile = self.manager.ensure_category_profile(category)
            target_id = int(target_profile.get("id") or 0)
            if target_id <= 0:
                raise ValueError("Could not resolve target SOP category profile.")

            imported = dict(profile)
            imported["id"] = target_id
            imported["category"] = category
            raw_actions = imported.pop("actions", [])
            if not isinstance(raw_actions, list):
                raw_actions = []
            imported_actions: List[Dict[str, Any]] = []
            for row in raw_actions:
                if not isinstance(row, dict):
                    continue
                clean = dict(row)
                clean.pop("id", None)
                imported_actions.append(clean)

            if category == self.CAT_HF and bool(imported.get("active", True)):
                if not self._resolve_hf_activation_conflicts(imported_actions):
                    return
            self.manager.save_profile(imported, imported_actions, schedule_layer=None)
            self._reload_profiles(select_id=target_id)
            self._set_save_dirty(False)
            self._emit_sop_data_changed()
            QMessageBox.information(self, "Import SOP", "SOP imported.")
        except Exception as e:
            QMessageBox.warning(self, "Import SOP", str(e))
        finally:
            self._suppress_realtime_conflict_checks = prev_suppress

    def apply_theme(self) -> None:
        try:
            theme = resolve_theme(self.settings)
            self.terms_hint_label.setStyleSheet(label_style("muted", theme))
            if hasattr(self, "operating_plan_inputs_label"):
                self.operating_plan_inputs_label.setStyleSheet(label_style("muted", theme))
            if hasattr(self, "activation_defaults_hint_label"):
                self.activation_defaults_hint_label.setStyleSheet(label_style("muted", theme))
            if hasattr(self, "activation_conflict_summary_label"):
                self.activation_conflict_summary_label.setStyleSheet(
                    label_style("text", theme, weight=600)
                )
            if hasattr(self, "conflict_workbench_hint_label"):
                self.conflict_workbench_hint_label.setStyleSheet(label_style("muted", theme))
            if hasattr(self, "conflict_workbench_filter_label"):
                self.conflict_workbench_filter_label.setStyleSheet(label_style("muted", theme))
            if hasattr(self, "conflict_workbench_status_label"):
                self.conflict_workbench_status_label.setStyleSheet(
                    label_style("text", theme, weight=600)
                )
            if hasattr(self, "sop_workflow_status_label"):
                self.sop_workflow_status_label.setStyleSheet(
                    f"color: {theme.get('text', '#e5e7eb')}; font-weight: 600;"
                )
            for btn_name in (
                "workbench_set_sop_btn",
                "workbench_set_net_btn",
                "workbench_set_daily_btn",
                "workbench_apply_defaults_btn",
            ):
                btn = getattr(self, btn_name, None)
                if isinstance(btn, QPushButton):
                    btn.setStyleSheet(button_style("muted", theme))
            self._update_time_toggle_style(theme)
            self._update_profile_action_styles(theme)
            self._apply_accessibility_width_guards()
            self._refresh_inline_conflict_badges()
            self._apply_workflow_section_toggle_styles()
            self._apply_conflict_workbench_filter_button_styles()
            self._update_conflict_workbench_batch_actions()
        except Exception:
            pass
