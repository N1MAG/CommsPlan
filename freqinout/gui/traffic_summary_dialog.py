"""Offline review dialog for station traffic summaries.

The dialog deliberately treats the traffic-summary core as an immutable,
worker-owned boundary.  The GUI receives a snapshot and a rendered narrative;
it never opens the database, reads a report file, or renders a snapshot on the
GUI thread.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from PySide6.QtCore import QCoreApplication, QDateTime, QEvent, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QCheckBox,
    QToolButton,
)

from freqinout.gui.dropdown_checklist import DropdownChecklist

from freqinout.gui.theme import (
    active_app_theme,
    apply_text_size_accessibility_guards,
    button_height_for_font,
    button_style,
    control_height_for_font,
    fit_child_combo_boxes,
    fit_text_action_button,
    item_view_height_for_rows,
    multiline_height_for_font,
    horizontal_layout_breakpoint,
    label_style,
)


_UTC = _dt.timezone.utc
_MAX_RECORDS = 50_000
_EVIDENCE_PAGE_SIZE = 200
_DETACHED_JOBS: list["_JobHandle"] = []


def _release_detached_job(handle: "_JobHandle") -> None:
    """Release a close-detached job only after its thread has stopped."""

    if handle in _DETACHED_JOBS:
        _DETACHED_JOBS.remove(handle)


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_UTC).replace(microsecond=0)


def _normal_values(value: str) -> tuple[str, ...]:
    """Normalize the small explicit group/source entry fields."""

    result: list[str] = []
    seen: set[str] = set()
    for piece in str(value or "").replace(";", ",").split(","):
        item = " ".join(piece.strip().split())
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return tuple(result)


def _qdatetime_to_utc(value: QDateTime) -> _dt.datetime:
    try:
        py_value = value.toUTC().toPython()
    except Exception:
        py_value = None
    if isinstance(py_value, _dt.datetime):
        if py_value.tzinfo is None:
            return py_value.replace(tzinfo=_UTC)
        return py_value.astimezone(_UTC).replace(microsecond=0)
    text = value.toUTC().toString("yyyy-MM-dd HH:mm:ss")
    try:
        return _dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC)
    except ValueError:
        return _now_utc()


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_UTC)
        return value.astimezone(_UTC).strftime("%Y-%m-%d %H:%MZ")
    return str(value)


def _timestamp_text(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return _dt.datetime.fromtimestamp(float(value), _UTC).strftime("%Y-%m-%d %H:%MZ")
        except (OverflowError, OSError, ValueError):
            pass
    return _as_text(value)


def _api(module_name: str):
    return importlib.import_module(module_name)


def _summary_api() -> Any:
    return _api("freqinout.core.traffic_summary")


def _store_api() -> Any:
    return _api("freqinout.core.traffic_summary_store")


class _Worker(QObject):
    completed = Signal(int, object, object)

    def __init__(self, operation: Callable[[Callable[[], bool]], object], generation: int) -> None:
        super().__init__()
        self._operation = operation
        self._generation = generation
        self._cancel = threading.Event()

    @Slot()
    def run(self) -> None:
        result: object = None
        error: object = None
        try:
            result = self._operation(self._cancel.is_set)
        except Exception as exc:  # surfaced as operator-facing status
            error = exc
        self.completed.emit(self._generation, result, error)

    def cancel(self) -> None:
        self._cancel.set()


class _ResultRelay(QObject):
    """Keep a GUI-thread result receiver alive after a worker quits directly."""

    forwarded = Signal(int, object, object)

    @Slot(int, object, object)
    def accept(self, generation: int, result: object, error: object) -> None:
        self.forwarded.emit(generation, result, error)


@dataclass
class _JobHandle:
    thread: QThread
    worker: _Worker
    kind: str
    requested_scope: object | None = None
    result_received: bool = False
    thread_stopped: bool = False
    relay: _ResultRelay | None = None


class TrafficSummaryDialog(QDialog):
    """Generate, review, save and stage one frozen traffic-summary snapshot."""

    stageRequested = Signal(object, str)

    _COL_INCLUDE = 0
    _COL_ID = 1
    _COL_SOURCE = 2
    _COL_FROM = 3
    _COL_GROUP = 4
    _COL_STATE = 5
    _COL_GRID = 6
    _COL_RECEIVED = 7
    _COL_SEVERITY = 8
    _COL_ORIGIN = 9
    _COL_EXCERPT = 10

    def __init__(
        self,
        db_path: str,
        report_root: Path,
        parent: QWidget | None = None,
        *,
        station_callsign: str = "",
        reference_root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.db_path = str(db_path)
        self.report_root = Path(report_root)
        self.reference_root = Path(reference_root) if reference_root is not None else None
        self._configured_callsign = self._normalize_callsign(station_callsign)
        self._snapshot: object | None = None
        self._excluded_ids: set[str] = set()
        self._draft_texts: dict[str, str] = {"rf": "", "detailed": "", "legacy": ""}
        self._draft_dirty: dict[str, bool] = {"rf": False, "detailed": False, "legacy": False}
        self._active_report_kind = "rf"
        self._target_minutes = 3
        self._snapshot_target_minutes = 3
        self._render_failed = False
        self._narrative_dirty = False
        self._applying_narrative = False
        self._generation = 0
        self._render_generation = 0
        self._closing = False
        self._close_requested = False
        self._scope_dirty = False
        self._suppress_scope_invalidation = False
        self._jobs: list[_JobHandle] = []
        self._row_checks: dict[str, QCheckBox] = {}
        self._previous_excluded: set[str] = set()
        self._evidence_entries: tuple[object, ...] = ()
        self._evidence_page = 0
        self._filter_catalog = {"groups": (), "sources": ()}
        self._filter_values: dict[str, tuple[str, ...] | None] = {"groups": None, "sources": None}
        self._catalog_generation = 0
        self._catalog_loading = False
        self._last_saved_path: object | None = None
        self._count_snapshot: object | None = None
        self._count_total = 0
        self._count_raw: object = 0
        self._count_core_exclusions = 0
        self._estimate_timer = QTimer(self)
        self._estimate_timer.setSingleShot(True)
        self._estimate_timer.setInterval(150)
        self._estimate_timer.timeout.connect(self._update_narrative_bytes)

        self.setWindowTitle("Station Traffic Summary — offline review")
        self.setModal(False)
        self.resize(1040, 760)
        self._build_ui()
        self._apply_theme()
        self._set_default_window()
        self._set_busy(False)
        QTimer.singleShot(0, self._load_filter_options)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._cancel_background_jobs)

    @property
    def snapshot(self) -> object | None:
        """Current immutable core snapshot, for the parent staging workflow."""

        return self._snapshot

    @property
    def excluded_ids(self) -> tuple[str, ...]:
        """Stable excluded-ID view used by save/stage hosts."""

        return tuple(sorted(self._excluded_ids))

    @property
    def report_kind(self) -> str:
        if hasattr(self, "report_kind_combo"):
            return str(self.report_kind_combo.currentData() or self._active_report_kind)
        return self._active_report_kind

    @property
    def companion_text(self) -> str:
        kind = self.report_kind
        return self._draft_texts.get("detailed" if kind == "rf" else "rf", "")

    @property
    def target_minutes(self) -> int:
        return int(self._target_minutes)

    def _any_draft_dirty(self) -> bool:
        active = self.report_kind
        return self._narrative_dirty or any(
            dirty for kind, dirty in self._draft_dirty.items() if kind != active
        )

    @staticmethod
    def _normalize_callsign(value: object) -> str:
        return str(value or "").strip().upper()

    @classmethod
    def _callsign_error(cls, value: object) -> str | None:
        callsign = cls._normalize_callsign(value)
        if not callsign:
            return None
        if not re.fullmatch(r"(?=[A-Z0-9/]*[A-Z])(?=[A-Z0-9/]*[0-9])[A-Z0-9/]{3,32}", callsign):
            return "Enter a valid station callsign with letters and a number; portable prefixes/suffixes may use /."
        return None

    @classmethod
    def _has_actual_callsign(cls, value: object) -> bool:
        callsign = cls._normalize_callsign(value)
        return bool(callsign and cls._callsign_error(callsign) is None)

    # ----- UI ---------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        self._page = QWidget(self)
        page_layout = QVBoxLayout(self._page)

        heading = QHBoxLayout()
        self.title_label = QLabel("Station Traffic Summary")
        self.title_label.setObjectName("trafficSummaryTitle")
        self.title_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        heading.addWidget(self.title_label)
        heading.addStretch(1)
        self.status_label = QLabel("Ready to generate an offline review.")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        heading.addWidget(self.status_label, stretch=1)
        page_layout.addLayout(heading)

        scope_box = QGroupBox("Scope")
        scope_layout = QGridLayout(scope_box)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Last 24 hours", "24h")
        self.preset_combo.addItem("Last 7 days", "7d")
        self.preset_combo.addItem("Custom UTC window", "custom")
        self.preset_combo.addItem("Latest stored data (worker)", "latest")
        self.start_edit = QDateTimeEdit()
        self.end_edit = QDateTimeEdit()
        for editor in (self.start_edit, self.end_edit):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd HH:mm:ss 'UTC'")
            editor.setTimeSpec(Qt.UTC)
        self.group_filter = DropdownChecklist("")
        self.source_filter = DropdownChecklist("")
        self.group_filter.setAccessibleName("Groups filter")
        self.source_filter.setAccessibleName("Source families filter")
        self._filter_summary = QLabel()
        self._filter_summary.setWordWrap(True)
        self._catalog_status = QLabel("Loading filter choices…")
        self._catalog_status.setWordWrap(True)
        self.callsign_edit = QLineEdit(self._configured_callsign)
        self.callsign_edit.setPlaceholderText("Station callsign (optional for draft)")
        self._period_label = QLabel("Period")
        self._start_label = QLabel("Start")
        self._end_label = QLabel("End")
        self._groups_label = QLabel("Groups")
        self._sources_label = QLabel("Source families")
        self._station_label = QLabel("Station")
        self._filters_row = QWidget()
        self._filters_layout = QBoxLayout(QBoxLayout.LeftToRight, self._filters_row)
        self._filters_layout.setContentsMargins(0, 0, 0, 0)
        for label, control in ((self._groups_label, self.group_filter), (self._sources_label, self.source_filter)):
            field = QWidget()
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.addWidget(label)
            field_layout.addWidget(control)
            self._filters_layout.addWidget(field, 1)
        self._scope_layout = scope_layout
        self._scope_compact: bool | None = None
        self._layout_scope_controls(compact=False)
        page_layout.addWidget(scope_box)
        page_layout.addWidget(self._filter_summary)
        page_layout.addWidget(self._catalog_status)
        self._refresh_filter_display()

        self.coverage_label = QLabel(
            "Stored time may be authored/event time or file mtime, not actual receipt. "
            "Review source paths and reported locations before distribution."
        )
        self.coverage_label.setWordWrap(True)
        page_layout.addWidget(self.coverage_label)

        self.counts_label = QLabel("Traffic entries: —   Stored entries: —   Excluded: —")
        self.counts_label.setWordWrap(True)
        page_layout.addWidget(self.counts_label)

        self.evidence_box = QWidget()
        evidence_box = self.evidence_box
        evidence_layout = QVBoxLayout(evidence_box)
        evidence_layout.setContentsMargins(0, 0, 0, 0)
        self.evidence_toggle = QToolButton()
        self.evidence_toggle.setCheckable(True)
        self.evidence_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.evidence_toggle.setAccessibleName("Review source reports")
        self.evidence_toggle.setToolTip("Show received reports and choose which reports to include in both outputs.")
        evidence_layout.addWidget(self.evidence_toggle, 0, Qt.AlignLeft)
        self.evidence_table = QTableWidget(0, 11)
        self.evidence_table.setHorizontalHeaderLabels(
            [
                "Include", "Report ID", "Source", "From", "Group", "State", "Grid",
                "Received UTC", "Severity", "Sender origin / event places", "Evidence",
            ]
        )
        self.evidence_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.evidence_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.evidence_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.evidence_table.setAlternatingRowColors(True)
        self.evidence_table.setWordWrap(False)
        self.evidence_table.verticalHeader().setVisible(False)
        header = self.evidence_table.horizontalHeader()
        for column in range(self._COL_ID, self._COL_EXCERPT):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_EXCERPT, QHeaderView.Stretch)
        self._evidence_content = QWidget()
        evidence_content_layout = QVBoxLayout(self._evidence_content)
        evidence_content_layout.setContentsMargins(0, 0, 0, 0)
        evidence_help = QLabel("Clear Include to exclude a source report from both outputs. Changing inclusion regenerates both drafts.")
        evidence_help.setWordWrap(True)
        evidence_content_layout.addWidget(evidence_help)
        evidence_content_layout.addWidget(self.evidence_table)
        evidence_nav = QHBoxLayout()
        self.evidence_previous_button = QPushButton("Previous")
        self.evidence_next_button = QPushButton("Next")
        self.evidence_page_label = QLabel("No evidence rows")
        evidence_nav.addWidget(self.evidence_previous_button)
        evidence_nav.addWidget(self.evidence_page_label)
        evidence_nav.addWidget(self.evidence_next_button)
        evidence_nav.addStretch(1)
        evidence_content_layout.addLayout(evidence_nav)
        evidence_layout.addWidget(self._evidence_content)
        self.evidence_toggle.toggled.connect(self._set_evidence_visibility)
        self._set_evidence_visibility(False)
        page_layout.addWidget(evidence_box)

        preview_box = QGroupBox("Report — edit narrative below computed counts")
        preview_layout = QVBoxLayout(preview_box)
        self._selector_layout = selector_layout = QBoxLayout(QBoxLayout.LeftToRight)
        selector_layout.addWidget(QLabel("Report"))
        self.report_kind_combo = QComboBox()
        self.report_kind_combo.addItem("RF Brief", "rf")
        self.report_kind_combo.addItem("Detailed Report", "detailed")
        self.report_kind_combo.setToolTip("Switch cached report drafts without regenerating or reading files.")
        selector_layout.addWidget(self.report_kind_combo)
        selector_layout.addWidget(QLabel("RF length"))
        self.target_minutes_combo = QComboBox()
        for minutes in (3, 5, 10):
            self.target_minutes_combo.addItem(f"{minutes} minutes", minutes)
        self.target_minutes_combo.setCurrentIndex(0)
        selector_layout.addWidget(self.target_minutes_combo)
        self.copy_export_button = QToolButton()
        self.copy_export_button.setText("Copy / export")
        self.copy_export_button.setToolTip("Copy or export the selected report draft.")
        self.copy_export_button.setPopupMode(QToolButton.InstantPopup)
        self.copy_export_menu = QMenu(self.copy_export_button)
        self.copy_action = QAction("Copy report", self.copy_export_menu)
        self.export_action = QAction("Export report…", self.copy_export_menu)
        self.copy_export_menu.addAction(self.copy_action)
        self.copy_export_menu.addAction(self.export_action)
        self.copy_export_button.setMenu(self.copy_export_menu)
        selector_layout.addWidget(self.copy_export_button)
        selector_layout.addStretch(1)
        preview_layout.addLayout(selector_layout)
        self.report_legend = QLabel("Priority / significant impacts first, then routine topic rollups. R references identify source reports; sender origin may differ from event location. Add operator notes in the draft, labeled Operator note.")
        self.report_legend.setWordWrap(True)
        preview_layout.addWidget(self.report_legend)
        self.narrative_edit = QPlainTextEdit()
        self.narrative_edit.setPlaceholderText("The generated offline narrative will appear here for operator review.")
        self.narrative_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        preview_layout.addWidget(self.narrative_edit)
        self.narrative_bytes_label = QLabel("Narrative size: 0 bytes")
        self.narrative_bytes_label.setWordWrap(True)
        preview_layout.addWidget(self.narrative_bytes_label)
        self.delivery_estimate_label = QLabel("MFSK32 body-only planning estimate: 20s")
        self.delivery_estimate_label.setWordWrap(True)
        preview_layout.addWidget(self.delivery_estimate_label)
        page_layout.addWidget(preview_box, stretch=1)

        self._footer_layout = QHBoxLayout()
        self.generate_button = QPushButton("Generate reports")
        self.save_button = QPushButton("Save drafts")
        self.load_button = QPushButton("Load saved draft…")
        self.stage_button = QPushButton("Review in Compose…")
        self.close_button = QPushButton("Close")
        self._footer_layout.addWidget(self.generate_button)
        self._footer_layout.addWidget(self.save_button)
        self._footer_layout.addWidget(self.load_button)
        self._footer_layout.addStretch(1)
        self._footer_layout.addWidget(self.stage_button)
        self._footer_layout.addWidget(self.close_button)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._page)
        self._scroll.viewport().installEventFilter(self)
        outer.addWidget(self._scroll, stretch=1)
        outer.addLayout(self._footer_layout)

        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        self.preset_combo.currentIndexChanged.connect(self._scope_edited)
        self.start_edit.dateTimeChanged.connect(self._scope_edited)
        self.end_edit.dateTimeChanged.connect(self._scope_edited)
        self.group_filter.selectionChanged.connect(lambda: self._filter_changed("groups"))
        self.source_filter.selectionChanged.connect(lambda: self._filter_changed("sources"))
        self.callsign_edit.textChanged.connect(self._callsign_edited)
        self.report_kind_combo.currentIndexChanged.connect(self._report_kind_changed)
        self.target_minutes_combo.currentIndexChanged.connect(self._target_minutes_changed)
        self.evidence_previous_button.clicked.connect(self._previous_evidence_page)
        self.evidence_next_button.clicked.connect(self._next_evidence_page)
        self.generate_button.clicked.connect(self.generate)
        self.save_button.clicked.connect(self.save_draft)
        self.load_button.clicked.connect(self.load_draft)
        self.copy_action.triggered.connect(self.copy_report)
        self.export_action.triggered.connect(self.export_report)
        self.copy_action.setEnabled(False)
        self.export_action.setEnabled(False)
        self.stage_button.clicked.connect(self._stage)
        self.close_button.clicked.connect(self.close)
        self.narrative_edit.textChanged.connect(self._narrative_changed)

    def _set_status(self, text: str) -> None:
        lowered = text.lower()
        if "failed" in lowered or "blocked" in lowered:
            self._status_role = "danger"
        elif any(value in lowered for value in ("changed", "generate again", "unavailable")):
            self._status_role = "warning"
        elif any(value in lowered for value in ("saved:", "exported:", "copied ")):
            self._status_role = "success"
        else:
            self._status_role = "info"
        self.status_label.setText(text)
        self.status_label.setStyleSheet(label_style(self._status_role, active_app_theme()))

    def _apply_theme(self) -> None:
        theme = active_app_theme()
        self.title_label.setStyleSheet(label_style("text", theme, weight=700))
        self.status_label.setStyleSheet(label_style(getattr(self, "_status_role", "info"), theme))
        self.coverage_label.setStyleSheet(label_style("muted", theme))
        self.counts_label.setStyleSheet(label_style("text", theme, weight=600))
        self.generate_button.setStyleSheet(button_style("primary", theme))
        self.stage_button.setStyleSheet(button_style("eligible_success", theme))
        self.save_button.setStyleSheet(button_style("secondary", theme))
        self.load_button.setStyleSheet(button_style("secondary", theme))
        self.copy_export_button.setStyleSheet(button_style("secondary", theme))
        self.evidence_toggle.setStyleSheet(button_style("secondary", theme))
        self.close_button.setStyleSheet(button_style("muted", theme))
        for button in (
            self.generate_button,
            self.save_button,
            self.load_button,
            self.stage_button,
            self.close_button,
            self.copy_export_button,
            self.evidence_toggle,
        ):
            button.setMinimumHeight(button_height_for_font(button))
        for editor in (
            self.preset_combo,
            self.report_kind_combo,
            self.target_minutes_combo,
            self.start_edit,
            self.end_edit,
            self.group_filter,
            self.source_filter,
            self.callsign_edit,
        ):
            editor.setMinimumHeight(control_height_for_font(editor))
        for button in (self.evidence_toggle, self.copy_export_button, self.group_filter, self.source_filter):
            button.setStyleSheet(button_style("secondary", theme))
            fit_text_action_button(button)
        fit_child_combo_boxes(self)
        row_height = control_height_for_font(self.evidence_table)
        self.evidence_table.verticalHeader().setDefaultSectionSize(row_height)
        header_height = control_height_for_font(self.evidence_table.horizontalHeader())
        self.evidence_table.horizontalHeader().setMinimumHeight(header_height)
        self.evidence_table.setMinimumHeight(item_view_height_for_rows(self.evidence_table, visible_rows=4))
        self.evidence_table.setMaximumHeight(item_view_height_for_rows(self.evidence_table, visible_rows=10))
        self.narrative_edit.setMinimumHeight(multiline_height_for_font(self.narrative_edit, visible_lines=10))
        for button in (self.evidence_previous_button, self.evidence_next_button):
            button.setMinimumHeight(button_height_for_font(button))
        apply_text_size_accessibility_guards(self)
        self._apply_responsive_layout()

    def apply_theme(self) -> None:
        """Republish shared theme geometry for hosts that change settings."""

        self._apply_theme()

    def _apply_responsive_layout(self) -> None:
        available_width = self._scroll.viewport().width() if hasattr(self, "_scroll") else self.width()
        scope_controls = (self._period_label, self.preset_combo, self._start_label,
                          self.start_edit, self._end_label, self.end_edit)
        scope_width = sum(max(widget.minimumWidth(), widget.minimumSizeHint().width()) for widget in scope_controls)
        scope_width += max(0, self._scope_layout.horizontalSpacing()) * (len(scope_controls) + 1)
        # Include both containers' live layout margins and one font-derived
        # reserve so an almost-fitting row stacks before it widens the page.
        for layout in (self._page.layout(), self._scope_layout):
            margins = layout.contentsMargins()
            scope_width += margins.left() + margins.right()
        scope_width += control_height_for_font(self.preset_combo)
        self._layout_scope_controls(compact=available_width > 0 and available_width < scope_width)
        self._selector_layout.setDirection(
            QBoxLayout.TopToBottom if available_width < horizontal_layout_breakpoint(self._selector_layout)
            else QBoxLayout.LeftToRight
        )
        compact = self.width() > 0 and self.width() < horizontal_layout_breakpoint(
            self._footer_layout, reserve_controls=1
        )
        if compact:
            self._footer_layout.setDirection(QBoxLayout.TopToBottom)
        else:
            self._footer_layout.setDirection(QBoxLayout.LeftToRight)

    def _layout_scope_controls(self, *, compact: bool) -> None:
        if not hasattr(self, "_scope_layout") or compact == self._scope_compact:
            return
        layout = self._scope_layout
        self._scope_compact = compact
        for column in range(6):
            layout.setColumnStretch(column, 0)
        if compact:
            rows = (
                (self._period_label, self.preset_combo),
                (self._start_label, self.start_edit),
                (self._end_label, self.end_edit),
                (self._station_label, self.callsign_edit),
            )
            for row, (label, control) in enumerate(rows):
                layout.addWidget(label, row, 0)
                layout.addWidget(control, row, 1, 1, 5)
            layout.addWidget(self._filters_row, 4, 0, 1, 6)
            self._filters_layout.setDirection(QBoxLayout.TopToBottom)
            layout.setColumnStretch(1, 1)
        else:
            layout.addWidget(self._period_label, 0, 0)
            layout.addWidget(self.preset_combo, 0, 1)
            layout.addWidget(self._start_label, 0, 2)
            layout.addWidget(self.start_edit, 0, 3)
            layout.addWidget(self._end_label, 0, 4)
            layout.addWidget(self.end_edit, 0, 5)
            layout.addWidget(self._filters_row, 1, 0, 1, 6)
            self._filters_layout.setDirection(QBoxLayout.LeftToRight)
            layout.addWidget(self._station_label, 2, 0)
            layout.addWidget(self.callsign_edit, 2, 1, 1, 5)
            for column in (1, 3, 5):
                layout.setColumnStretch(column, 1)

    def _set_evidence_visibility(self, visible: bool) -> None:
        self.evidence_toggle.setText("Hide source reports" if visible else "Review source reports")
        self.evidence_toggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
        self.evidence_toggle.setAccessibleDescription("Expanded" if visible else "Collapsed")
        if hasattr(self, "_evidence_content"):
            self._evidence_content.setVisible(bool(visible))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def eventFilter(self, watched, event) -> bool:
        if hasattr(self, "_scroll") and watched is self._scroll.viewport() and event.type() == QEvent.Resize:
            self._apply_responsive_layout()
        return super().eventFilter(watched, event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.PaletteChange):
            self._apply_theme()

    # ----- scope and workers -----------------------------------------

    def _set_default_window(self) -> None:
        end = _now_utc()
        self.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(end.timestamp()), Qt.UTC))
        self.start_edit.setDateTime(
            QDateTime.fromSecsSinceEpoch(
                int((end - _dt.timedelta(hours=24)).timestamp()), Qt.UTC
            )
        )

    def _scope_edited(self, *_args: object) -> None:
        if self._suppress_scope_invalidation or self._snapshot is None:
            return
        self._scope_dirty = True
        self.stage_button.setEnabled(False)
        self._set_status("Scope changed; generate again before staging this report.")

    def _report_kind_changed(self, _index: int) -> None:
        selected = str(self.report_kind_combo.currentData() or "rf")
        previous = self._active_report_kind
        if selected == previous:
            return
        self._draft_texts[previous] = self.narrative_edit.toPlainText()
        self._draft_dirty[previous] = self._narrative_dirty
        self._active_report_kind = selected
        self._set_narrative(self._draft_texts.get(selected, ""))
        self._narrative_dirty = self._draft_dirty.get(selected, False)
        self._update_narrative_bytes()
        self._set_status(f"Showing {self._report_kind_label(selected)} draft.")

    def _target_minutes_changed(self, _index: int) -> None:
        try:
            target = int(self.target_minutes_combo.currentData() or 3)
        except (TypeError, ValueError):
            target = 3
        if target == self._target_minutes:
            return
        self._target_minutes = target
        if self._snapshot is not None:
            self._scope_dirty = True
            self.stage_button.setEnabled(False)
            self._set_status("Report length changed; generate again before staging.")
        self._update_narrative_bytes()

    @staticmethod
    def _report_kind_label(kind: str) -> str:
        return {"rf": "RF Brief", "detailed": "Detailed Report", "legacy": "Legacy report"}.get(kind, kind)

    @staticmethod
    def _set_editor_window(
        start: _dt.datetime,
        end: _dt.datetime,
        start_edit: QDateTimeEdit,
        end_edit: QDateTimeEdit,
    ) -> None:
        start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(start.timestamp()), Qt.UTC))
        end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(end.timestamp()), Qt.UTC))

    def _preset_changed(self, _index: int) -> None:
        kind = str(self.preset_combo.currentData() or "24h")
        self.start_edit.setEnabled(kind != "latest")
        self.end_edit.setEnabled(kind != "latest")
        if kind in {"24h", "7d"}:
            end = _now_utc()
            amount = _dt.timedelta(hours=24 if kind == "24h" else 24 * 7)
            self._set_editor_window(end - amount, end, self.start_edit, self.end_edit)
        if kind == "latest":
            self._set_status("Latest stored bounds will be resolved by the generation worker.")

    def _scope_inputs(
        self,
    ) -> tuple[str, _dt.datetime, _dt.datetime, tuple[str, ...], tuple[str, ...], str]:
        kind = str(self.preset_combo.currentData() or "24h")
        start = _qdatetime_to_utc(self.start_edit.dateTime())
        end = _qdatetime_to_utc(self.end_edit.dateTime())
        return (
            kind,
            start,
            end,
            self._filter_values["groups"] or (),
            self._filter_values["sources"] or (),
            self._normalize_callsign(self.callsign_edit.text()),
        )

    def _load_filter_options(self) -> None:
        if self._closing or self._close_requested or self._catalog_loading:
            return
        self._catalog_loading = True
        self._catalog_generation += 1
        db_path = self.db_path
        def operation(cancelled):
            reader = getattr(_summary_api(), "summary_filter_options", None)
            return reader(db_path, cancelled=cancelled) if reader is not None else ((), ())
        self._start_job("filter_options", operation, catalog_generation=self._catalog_generation)

    @staticmethod
    def _canonical_filter(key: str, values) -> tuple[str, ...]:
        return tuple(sorted({str(value).strip().lstrip("@").upper() if key == "groups"
                             else str(value).strip().lower() for value in values if str(value).strip()}))

    def _restore_filter(self, key: str, values) -> None:
        canonical = self._canonical_filter(key, values)
        self._filter_values[key] = canonical or None

    def _apply_filter_choices(self) -> None:
        for key, control in (("groups", self.group_filter), ("sources", self.source_filter)):
            selected = self._filter_values[key]
            choices = sorted(set(self._filter_catalog[key]) | set(selected or ()))
            # Retain restored selections missing from this catalog, rather than
            # silently broadening a saved report's scope.
            control.blockSignals(True)
            control.set_options([(value, value) for value in choices],
                                selected_values=choices if selected is None else selected,
                                select_all_when_empty=False)
            control.blockSignals(False)
        self._refresh_filter_display()

    def _filter_changed(self, key: str) -> None:
        control = self.group_filter if key == "groups" else self.source_filter
        values = None if control.all_selected() else self._canonical_filter(key, control.selected_values())
        if values == self._filter_values[key]:
            return
        self._filter_values[key] = values
        self._refresh_filter_display()
        self._scope_edited()

    def _refresh_filter_display(self) -> None:
        summaries = []
        for key, label, control in (("groups", "Groups", self.group_filter),
                                    ("sources", "Source families", self.source_filter)):
            values = self._filter_values[key]
            if values is None:
                control.setText("All")
                control.setToolTip(f"All {label.lower()}, including unclassified traffic")
            else:
                summary = ", ".join(values) if values else "None — choose values or All"
                summaries.append(f"{label}: {summary}")
                control.setToolTip(summary)
            fit_text_action_button(control)
        self._filter_summary.setText("; ".join(summaries))
        self._filter_summary.setVisible(bool(summaries))

    def _callsign_edited(self, value: str) -> None:
        normalized = self._normalize_callsign(value)
        if value != normalized:
            self.callsign_edit.blockSignals(True)
            self.callsign_edit.setText(normalized)
            self.callsign_edit.blockSignals(False)
        self._scope_edited()

    def _start_job(
        self,
        kind: str,
        operation: Callable[[Callable[[], bool]], object],
        **metadata: object,
    ) -> _JobHandle:
        if kind != "filter_options":
            self._generation += 1
        generation = self._generation
        thread = QThread()
        worker = _Worker(operation, generation)
        worker.moveToThread(thread)
        relay = _ResultRelay(self)
        handle = _JobHandle(thread, worker, kind, relay=relay)
        for key, value in metadata.items():
            setattr(handle, key, value)
        self._jobs.append(handle)
        thread.started.connect(worker.run)
        # Connect through a QObject slot on the dialog so result handling is
        # queued back to the GUI thread.  A plain Python lambda can otherwise
        # run in the worker's emitter thread in some PySide versions.
        worker.completed.connect(relay.accept)
        relay.forwarded.connect(self._job_completed)
        worker.completed.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(lambda h=handle: _release_detached_job(h))
        thread.start()
        return handle

    @Slot(int, object, object)
    def _job_completed(self, generation: int, result: object, error: object) -> None:
        relay = self.sender()
        handle = next((item for item in self._jobs if item.relay is relay), None)
        if handle is not None:
            handle.result_received = True
            self._job_done(handle, generation, result, error)
            if handle.thread_stopped:
                self._job_finished(handle)

    @Slot()
    def _thread_finished(self) -> None:
        thread = self.sender()
        handle = next((item for item in self._jobs if item.thread is thread), None)
        if handle is not None:
            handle.thread_stopped = True
            if handle.result_received:
                self._job_finished(handle)

    def _job_finished(self, handle: _JobHandle) -> None:
        if handle in self._jobs:
            self._jobs.remove(handle)
        if handle in _DETACHED_JOBS:
            _DETACHED_JOBS.remove(handle)
        handle.thread.deleteLater()
        if self._close_requested and not self._jobs:
            self._closing = True
            super().close()

    def _job_done(self, handle: _JobHandle, generation: int, result: object, error: object) -> None:
        if self._closing or handle not in self._jobs:
            return
        if generation != self._generation and handle.kind in {"generate", "render", "load"}:
            return
        if handle.kind == "filter_options":
            if getattr(handle, "catalog_generation", -1) != self._catalog_generation:
                return
            self._catalog_loading = False
            if error is not None:
                self._catalog_status.setText("Could not load filter choices. Existing selections and All traffic remain available.")
                self._catalog_status.setToolTip(str(error))
            else:
                groups, sources = result
                self._filter_catalog = {"groups": self._canonical_filter("groups", groups),
                                        "sources": self._canonical_filter("sources", sources)}
                self._apply_filter_choices()
                self._catalog_status.setVisible(False)
            return
        if error is not None:
            if handle.kind == "render":
                self._render_failed = True
                self.stage_button.setEnabled(False)
            if isinstance(error, Exception):
                detail = str(error).strip() or error.__class__.__name__
                self._set_status(f"{handle.kind.title()} failed: {detail}")
            else:
                self._set_status(f"{handle.kind.title()} failed.")
            self._set_busy(False)
            return
        if handle.kind == "generate":
            snapshot, narratives, excluded = result
            self._accept_snapshot(
                snapshot,
                narratives,
                reason="generation",
                excluded_ids=excluded,
                requested_scope=getattr(handle, "requested_scope", None),
                requested_target_minutes=getattr(handle, "requested_target_minutes", None),
            )
            self._set_busy(False)
        elif handle.kind == "render":
            self._accept_render(result)
            self._set_busy(False)
        elif handle.kind == "save":
            self._last_saved_path = result
            saved_snapshot = getattr(handle, "saved_snapshot", None)
            saved_excluded = tuple(getattr(handle, "saved_excluded", ()))
            saved_target = getattr(handle, "saved_target_minutes", None)
            saved_texts = dict(getattr(handle, "saved_texts", {}) or {})
            if result is not None and (
                self._snapshot is saved_snapshot
                and tuple(sorted(self._excluded_ids)) == saved_excluded
                and self._snapshot_target_minutes == saved_target
            ):
                for report_kind, saved_text in saved_texts.items():
                    if self._draft_texts.get(report_kind, "") == saved_text:
                        self._draft_dirty[report_kind] = False
                        if report_kind == self.report_kind:
                            self._narrative_dirty = False
            self._set_status(f"Draft saved: {_as_text(result)}")
            self._set_busy(False)
        elif handle.kind == "export":
            self._set_status(f"Report exported: {_as_text(result)}")
            self._set_busy(False)
        elif handle.kind == "load":
            snapshot, narratives, excluded, target_minutes, active_kind = result
            self._accept_snapshot(
                snapshot,
                narratives,
                reason="load",
                excluded_ids=excluded,
                bundle_target_minutes=target_minutes,
                bundle_active_kind=active_kind,
            )
            self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        # Inclusion changes launch paired renders; prevent a second checkbox
        # click from creating an overlapping job before publication completes.
        self.evidence_toggle.setEnabled(not busy)
        self.evidence_table.setEnabled(not busy)
        page_count = max(1, (len(self._evidence_entries) + _EVIDENCE_PAGE_SIZE - 1) // _EVIDENCE_PAGE_SIZE)
        self.evidence_previous_button.setEnabled(not busy and self._evidence_page > 0)
        self.evidence_next_button.setEnabled(not busy and self._evidence_page + 1 < page_count)
        self.generate_button.setEnabled(not busy)
        self.load_button.setEnabled(not busy)
        self.save_button.setEnabled(
            not busy and self._snapshot is not None and not self._render_failed
        )
        can_transfer = not busy and self._snapshot is not None and not self._render_failed
        self.copy_export_button.setEnabled(can_transfer)
        self.copy_action.setEnabled(can_transfer)
        self.export_action.setEnabled(can_transfer)
        self.stage_button.setEnabled(
            not busy
            and self._snapshot is not None
            and not self._scope_dirty
            and not self._render_failed
        )
        self.narrative_edit.setReadOnly(bool(busy))
        if busy:
            self._set_status("Working offline…")

    # ----- generation, review, persistence --------------------------

    @staticmethod
    def _render_report(api: object, snapshot: object, kind: str, target_minutes: int, excluded: tuple[str, ...]) -> str:
        try:
            return str(
                api.render_summary(
                    snapshot,
                    report_kind=kind,
                    target_minutes=target_minutes,
                    excluded_ids=excluded,
                )
                or ""
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            return str(
                api.render_summary(
                    snapshot,
                    compact=kind == "rf",
                    notes="",
                    excluded_ids=excluded,
                )
                or ""
            )

    def generate(self) -> None:
        if any(values == () for values in self._filter_values.values()):
            self._set_status("Generate blocked: choose at least one value in each filter, or choose All.")
            return
        kind, start, end, groups, sources, station_callsign = self._scope_inputs()
        callsign_error = self._callsign_error(station_callsign)
        if callsign_error:
            self._set_status(f"Generate blocked: {callsign_error}")
            return
        prior_excluded = tuple(sorted(self._excluded_ids))
        db_path = self.db_path
        reference_root = self.reference_root
        target_minutes = self._target_minutes
        requested_scope = (kind, start, end, groups, sources, station_callsign)
        self._set_busy(True)

        def operation(cancelled: Callable[[], bool]):
            api = _summary_api()
            scope = api.SummaryScope(
                start_ts=start.timestamp(),
                end_ts=end.timestamp(),
                groups=groups,
                sources=sources,
                radio_ids=(),
                states=(),
                max_records=_MAX_RECORDS,
            )
            if kind == "latest":
                resolver = (
                    getattr(api, "resolve_latest_scope", None)
                    or getattr(api, "latest_stored_scope", None)
                )
                if resolver is None:
                    raise ValueError(
                        "Latest stored data is not available in this build; "
                        "choose an explicit UTC window."
                    )
                scope = resolver(db_path, scope)
            try:
                snapshot = api.generate_summary(
                    db_path,
                    scope,
                    cancelled=cancelled,
                    station_callsign=station_callsign,
                    reference_root=reference_root,
                )
            except TypeError as exc:
                # Keep this checkout runnable until the schema-2 core lands;
                # never hide a TypeError raised by the actual query body.
                if "unexpected keyword argument" not in str(exc):
                    raise
                snapshot = api.generate_summary(db_path, scope, cancelled=cancelled)
            if cancelled():
                return snapshot, {}, ()
            valid_ids = {str(entry.message_id) for entry in snapshot.entries}
            effective_excluded = tuple(sorted(set(prior_excluded) & valid_ids))
            narratives = {
                kind: self._render_report(api, snapshot, kind, target_minutes, effective_excluded)
                for kind in ("rf", "detailed")
            }
            return snapshot, narratives, effective_excluded

        self._start_job(
            "generate",
            operation,
            requested_scope=requested_scope,
            requested_target_minutes=target_minutes,
        )

    def _accept_snapshot(
        self,
        snapshot: object,
        narrative: str,
        *,
        reason: str,
        excluded_ids: Iterable[object] = (),
        requested_scope: object | None = None,
        requested_target_minutes: int | None = None,
        bundle_target_minutes: int | None = None,
        bundle_active_kind: str | None = None,
    ) -> bool:
        if self._any_draft_dirty() and self._snapshot is not None:
            answer = QMessageBox.question(
                self,
                "Replace edited narrative?",
                "The generated narrative has operator edits. Replace those edits with the new snapshot rendering?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self._set_status("New snapshot ready; edited narrative was preserved.")
                return False
        valid_ids = {str(entry.message_id) for entry in (getattr(snapshot, "entries", ()) or ())}
        self._snapshot = snapshot
        self._render_failed = False
        self._excluded_ids = {str(item) for item in excluded_ids if str(item) in valid_ids}
        self._set_dual_report_choices_enabled(
            not (reason == "load" and bundle_active_kind == "legacy")
        )
        if bundle_target_minutes in (3, 5, 10):
            self._target_minutes = int(bundle_target_minutes)
            self._snapshot_target_minutes = int(bundle_target_minutes)
            target_index = self.target_minutes_combo.findData(self._target_minutes)
            if target_index >= 0:
                self.target_minutes_combo.blockSignals(True)
                self.target_minutes_combo.setCurrentIndex(target_index)
                self.target_minutes_combo.blockSignals(False)
        if bundle_active_kind:
            self._select_report_kind(bundle_active_kind)
        elif reason == "generation" and self.report_kind == "legacy":
            self._select_report_kind("rf")
        self._sync_scope_controls(snapshot, reason=reason)
        self._populate_evidence(snapshot)
        if isinstance(narrative, dict):
            self._draft_texts["rf"] = str(narrative.get("rf", "") or "")
            self._draft_texts["detailed"] = str(narrative.get("detailed", "") or "")
            self._draft_texts["legacy"] = str(narrative.get("legacy", "") or "")
        else:
            self._draft_texts["rf"] = str(narrative or "")
            self._draft_texts["detailed"] = ""
        self._draft_dirty.update({"rf": False, "detailed": False, "legacy": False})
        self._set_narrative(self._draft_texts.get(self.report_kind, ""))
        self._narrative_dirty = False
        self._scope_dirty = False
        if reason == "generation" and requested_scope is not None:
            if requested_target_minutes in (3, 5, 10):
                self._snapshot_target_minutes = int(requested_target_minutes)
            current_scope = self._scope_inputs()
            if requested_scope[0] == "latest":
                # Latest resolution intentionally replaces the provisional
                # editor bounds with the worker's actual stored bounds.
                self._scope_dirty = (
                    current_scope[0] != requested_scope[0]
                    or current_scope[3:] != requested_scope[3:]
                )
            else:
                self._scope_dirty = current_scope != requested_scope
            if requested_target_minutes is not None and self._target_minutes != requested_target_minutes:
                self._scope_dirty = True
        report_id = _as_text(getattr(snapshot, "report_id", ""), "pending")
        generated = _as_text(getattr(snapshot, "generated_utc", ""), "")
        self._set_status(
            f"Snapshot {report_id} generated {generated}. "
            "Review evidence and narrative before staging."
        )
        self._set_busy(False)
        return True

    def _select_report_kind(self, kind: str) -> None:
        if kind == "legacy" and self.report_kind_combo.findData("legacy") < 0:
            self.report_kind_combo.addItem("Legacy report", "legacy")
        index = self.report_kind_combo.findData(kind)
        if index < 0:
            kind = "rf"
            index = self.report_kind_combo.findData(kind)
        self.report_kind_combo.blockSignals(True)
        if kind != "legacy":
            legacy_index = self.report_kind_combo.findData("legacy")
            if legacy_index >= 0:
                self.report_kind_combo.removeItem(legacy_index)
                index = self.report_kind_combo.findData(kind)
        self.report_kind_combo.setCurrentIndex(max(0, index))
        self.report_kind_combo.blockSignals(False)
        self._active_report_kind = kind

    def _set_dual_report_choices_enabled(self, enabled: bool) -> None:
        model = self.report_kind_combo.model()
        for kind in ("rf", "detailed"):
            index = self.report_kind_combo.findData(kind)
            item = model.item(index) if index >= 0 and hasattr(model, "item") else None
            if item is not None:
                item.setEnabled(bool(enabled))

    def _sync_scope_controls(self, snapshot: object, *, reason: str) -> None:
        """Show the immutable snapshot bounds after generation or draft load."""

        scope = getattr(snapshot, "scope", None)
        if scope is None:
            return
        try:
            start = _dt.datetime.fromtimestamp(float(scope.start_ts), _UTC)
            end = _dt.datetime.fromtimestamp(float(scope.end_ts), _UTC)
        except (AttributeError, TypeError, ValueError, OverflowError, OSError):
            return
        self._suppress_scope_invalidation = True
        try:
            if reason == "load":
                custom_index = self.preset_combo.findData("custom")
                if custom_index >= 0:
                    self.preset_combo.setCurrentIndex(custom_index)
            self._set_editor_window(start, end, self.start_edit, self.end_edit)
            if reason == "load":
                self._restore_filter("groups", getattr(scope, "groups", ()) or ())
                self._restore_filter("sources", getattr(scope, "sources", ()) or ())
                self._apply_filter_choices()
            snapshot_callsign = getattr(snapshot, "station_callsign", None)
            schema_version = int(getattr(snapshot, "schema_version", 1) or 1)
            if snapshot_callsign is not None and (schema_version >= 2 or snapshot_callsign):
                self.callsign_edit.setText(self._normalize_callsign(snapshot_callsign))
        finally:
            self._suppress_scope_invalidation = False

    def _set_narrative(self, text: str) -> None:
        self._applying_narrative = True
        try:
            self.narrative_edit.setPlainText(text)
        finally:
            self._applying_narrative = False
            self._draft_texts[self.report_kind] = text
            self._update_narrative_bytes()

    def _narrative_changed(self) -> None:
        if not self._applying_narrative:
            self._estimate_timer.start()
            if self._snapshot is not None:
                self._narrative_dirty = True
                self._draft_dirty[self.report_kind] = True

    def _update_narrative_bytes(self) -> None:
        self._estimate_timer.stop()
        text = self.narrative_edit.toPlainText()
        self._draft_texts[self.report_kind] = text
        size = len(text.encode("utf-8"))
        self.narrative_bytes_label.setText(f"Narrative size: {size} bytes")
        words = len(text.split())
        # Pure cached-text planning calculation: never import core or perform
        # I/O from editing, font/theme, or report-selection paths.
        seconds = 20.0 + max(words / 2.0, size / 12.0)
        if self.report_kind == "rf":
            guidance = (
                f" · above {self.target_minutes}-minute RF Brief guidance; editing is optional"
                if seconds > self.target_minutes * 60
                else f" · within {self.target_minutes}-minute RF Brief guidance"
            )
        else:
            guidance = " · Detailed Report estimate; RF Brief target does not apply"
        self.delivery_estimate_label.setText(
            "MFSK32 body-only planning estimate: "
            f"{seconds:.1f}s ({words} words; 120 wpm nominal + 20s overhead; "
            "FLMsg/signing/retries extra)" + guidance
        )

    def _populate_evidence(self, snapshot: object) -> None:
        self._evidence_entries = tuple(getattr(snapshot, "entries", ()) or ())
        self._evidence_page = 0
        self._render_evidence_page()
        self._update_counts(snapshot)

    def _render_evidence_page(self) -> None:
        entries = self._evidence_entries
        page_count = max(1, (len(entries) + _EVIDENCE_PAGE_SIZE - 1) // _EVIDENCE_PAGE_SIZE)
        self._evidence_page = max(0, min(self._evidence_page, page_count - 1))
        start = self._evidence_page * _EVIDENCE_PAGE_SIZE
        page_entries = entries[start : start + _EVIDENCE_PAGE_SIZE]
        self.evidence_table.setRowCount(0)
        self._row_checks.clear()
        for row, entry in enumerate(page_entries):
            report_number = start + row + 1
            report_ref = f"R{report_number:03d}"
            message_id = _as_text(getattr(entry, "message_id", ""), str(row + 1))
            body_text = str(getattr(entry, "body_text", None) or getattr(entry, "excerpt", ""))
            source_refs = tuple(str(value) for value in (getattr(entry, "source_refs", ()) or ()))
            self.evidence_table.insertRow(row)
            checkbox = QCheckBox("Include")
            checkbox.setChecked(message_id not in self._excluded_ids)
            checkbox.setAccessibleName(f"Include {report_ref} {message_id}")
            checkbox.toggled.connect(
                lambda checked, mid=message_id, box=checkbox: self._evidence_toggled(
                    mid, checked, box
                )
            )
            self.evidence_table.setCellWidget(row, self._COL_INCLUDE, checkbox)
            self._row_checks[message_id] = checkbox
            values = (
                f"{report_ref} · {message_id}",
                getattr(entry, "source_family", ""),
                getattr(entry, "from_call", ""),
                getattr(entry, "group_name", ""),
                getattr(entry, "reported_state", ""),
                getattr(entry, "grid", ""),
                _timestamp_text(getattr(entry, "received_ts", "")),
                getattr(entry, "severity", ""),
                self._entry_origin_text(entry),
                " ".join(body_text.split())[:420],
            )
            for column, value in enumerate(values, start=self._COL_ID):
                item = QTableWidgetItem(_as_text(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == self._COL_ID:
                    item.setData(Qt.UserRole, message_id)
                if column == self._COL_EXCERPT:
                    item.setToolTip(
                        "Source references:\n"
                        + ("\n".join(source_refs) if source_refs else "None recorded")
                        + "\n\nFull body:\n"
                        + (body_text or "No body text recorded")
                    )
                self.evidence_table.setItem(row, column, item)
        end = min(start + len(page_entries), len(entries))
        self.evidence_page_label.setText(
            f"Page {self._evidence_page + 1} of {page_count} · "
            f"Rows {start + 1 if entries else 0}–{end}"
        )
        self.evidence_previous_button.setEnabled(self._evidence_page > 0)
        self.evidence_next_button.setEnabled(self._evidence_page + 1 < page_count)

    @staticmethod
    def _entry_origin_text(entry: object) -> str:
        sender = " / ".join(
            str(value).strip()
            for value in (
                getattr(entry, "origin_source", ""),
                getattr(entry, "origin_state", ""),
                getattr(entry, "origin_grid", ""),
            )
            if str(value or "").strip()
        )
        places = getattr(entry, "event_places", ()) or ()
        place_text = ", ".join(str(place).strip() for place in places if str(place).strip())
        if sender and place_text:
            return f"Sender: {sender}; Events: {place_text}"
        if sender:
            return f"Sender: {sender}"
        if place_text:
            return f"Events: {place_text}"
        return ""

    def _previous_evidence_page(self) -> None:
        if self._evidence_page <= 0:
            return
        self._evidence_page -= 1
        self._render_evidence_page()

    def _next_evidence_page(self) -> None:
        page_count = max(1, (len(self._evidence_entries) + _EVIDENCE_PAGE_SIZE - 1) // _EVIDENCE_PAGE_SIZE)
        if self._evidence_page + 1 >= page_count:
            return
        self._evidence_page += 1
        self._render_evidence_page()

    def _update_counts(self, snapshot: object) -> None:
        if self._count_snapshot is not snapshot:
            self._count_snapshot = snapshot
            self._count_total = len(tuple(getattr(snapshot, "entries", ()) or ()))
            self._count_raw = getattr(snapshot, "raw_count", None)
            if self._count_raw is None:
                self._count_raw = self._count_total
            self._count_core_exclusions = len(tuple(getattr(snapshot, "exclusions", ()) or ()))
        selected_count = self._count_total - len(self._excluded_ids)
        self.counts_label.setText(
            f"Traffic entries: {selected_count}   Stored entries: {_as_text(self._count_raw)}   "
            f"Excluded: {len(self._excluded_ids)}   "
            f"Core exclusions: {self._count_core_exclusions}"
        )

    def _evidence_toggled(self, message_id: str, included: bool, checkbox: QCheckBox) -> None:
        prior = set(self._excluded_ids)
        if included:
            self._excluded_ids.discard(message_id)
        else:
            self._excluded_ids.add(message_id)
        if self._any_draft_dirty():
            answer = QMessageBox.question(
                self,
                "Replace edited narrative?",
                "Changing inclusion recalculates both report drafts and would replace edited prose. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self._excluded_ids = prior
                checkbox.blockSignals(True)
                checkbox.setChecked(message_id not in prior)
                checkbox.blockSignals(False)
                return
        self._previous_excluded = prior
        self._update_counts(self._snapshot)
        self._request_render()

    def _request_render(self) -> None:
        if self._snapshot is None:
            return
        self._set_busy(True)
        self._render_generation = self._generation + 1
        self._generation = self._render_generation
        snapshot = self._snapshot
        excluded = tuple(sorted(self._excluded_ids))
        target_minutes = self._snapshot_target_minutes
        render_kinds = ("legacy",) if self.report_kind == "legacy" else ("rf", "detailed")

        def operation(cancelled: Callable[[], bool]):
            if cancelled():
                return {}
            api = _summary_api()
            return {
                kind: self._render_report(api, snapshot, kind, target_minutes, excluded)
                for kind in render_kinds
            }

        self._start_job("render", operation)

    def _accept_render(self, narratives: object) -> None:
        if isinstance(narratives, dict):
            if self.report_kind == "legacy":
                self._draft_texts["legacy"] = str(narratives.get("legacy", "") or "")
            else:
                self._draft_texts["rf"] = str(narratives.get("rf", "") or "")
                self._draft_texts["detailed"] = str(narratives.get("detailed", "") or "")
        else:
            self._draft_texts[self.report_kind] = str(narratives or "")
        if self.report_kind == "legacy":
            self._draft_dirty["legacy"] = False
        else:
            self._draft_dirty.update({"rf": False, "detailed": False})
        self._render_failed = False
        self._set_narrative(self._draft_texts.get(self.report_kind, ""))
        self._narrative_dirty = False
        self._update_counts(self._snapshot)
        self._set_status("Inclusion changed; review the regenerated narrative before staging.")

    def save_draft(self) -> None:
        if self._snapshot is None:
            self._set_status("Generate a summary before saving a draft.")
            return
        snapshot = self._snapshot
        self._draft_texts[self.report_kind] = self.narrative_edit.toPlainText()
        rf_text = self._draft_texts.get("rf", "")
        detailed_text = self._draft_texts.get("detailed", "")
        if self.report_kind == "legacy":
            rf_text = self._draft_texts.get("legacy", "")
        excluded = tuple(sorted(self._excluded_ids))
        report_root = self.report_root
        active_kind = self.report_kind
        target_minutes = self._snapshot_target_minutes
        saved_texts = dict(self._draft_texts)
        self._set_busy(True)

        def operation(cancelled: Callable[[], bool]):
            if cancelled():
                return None
            store = _store_api()
            saver = getattr(store, "save_summary_bundle", None)
            if saver is not None and active_kind != "legacy":
                return saver(
                    report_root,
                    snapshot,
                    rf_text,
                    detailed_text,
                    target_minutes=target_minutes,
                    excluded_ids=excluded,
                    active_kind=active_kind,
                )
            # Version-1 drafts load as a Legacy report. Keep their original
            # revision format because the version-2 bundle schema has no
            # legacy active kind.
            legacy_text = self._render_report(
                _summary_api(), snapshot, "legacy", target_minutes, excluded
            ) if active_kind != "legacy" else rf_text
            return store.save_summary_revision(
                report_root,
                snapshot,
                legacy_text,
                excluded_ids=excluded,
            )

        self._start_job(
            "save",
            operation,
            saved_snapshot=snapshot,
            saved_excluded=excluded,
            saved_target_minutes=target_minutes,
            saved_texts=saved_texts,
        )

    def load_draft(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load saved traffic-summary draft", str(self.report_root))
        if not path:
            return
        self._set_busy(True)

        def operation(cancelled: Callable[[], bool]):
            if cancelled():
                return None
            store = _store_api()
            loader = getattr(store, "load_summary_bundle", None)
            if loader is None:
                snapshot, text, excluded = store.load_summary_revision(Path(path))
                return snapshot, {"legacy": str(text or "")}, excluded, 3, "legacy"
            bundle = loader(Path(path))
            snapshot = getattr(bundle, "snapshot")
            active_kind = str(getattr(bundle, "active_kind", "rf") or "rf")
            legacy_text = getattr(bundle, "legacy_text", None)
            if active_kind == "legacy" and not legacy_text:
                # Version-1 bundles expose their preserved draft through
                # rf_text/detailed_text; SummaryBundle has no legacy_text
                # field in the new schema.
                legacy_text = getattr(bundle, "rf_text", "")
            narratives = {
                "rf": str(getattr(bundle, "rf_text", "") or ""),
                "detailed": str(getattr(bundle, "detailed_text", "") or ""),
                "legacy": str(legacy_text or ""),
            }
            return (
                snapshot,
                narratives,
                tuple(getattr(bundle, "excluded_ids", ()) or ()),
                int(getattr(bundle, "target_minutes", 3) or 3),
                active_kind,
            )

        self._start_job("load", operation)

    def copy_report(self) -> None:
        if self._snapshot is None:
            self._set_status("Generate a report before copying.")
            return
        QGuiApplication.clipboard().setText(self.narrative_edit.toPlainText())
        self._set_status(f"Copied {self._report_kind_label(self.report_kind)} to the clipboard.")

    def export_report(self) -> None:
        if self._snapshot is None:
            self._set_status("Generate a report before exporting.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export traffic summary report",
            str(self.report_root / f"{self.report_kind}-traffic-summary.txt"),
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        snapshot = self._snapshot
        text = self.narrative_edit.toPlainText()
        kind = self.report_kind
        target_minutes = self._snapshot_target_minutes
        excluded = tuple(sorted(self._excluded_ids))
        self._set_busy(True)

        def operation(cancelled: Callable[[], bool]):
            if cancelled():
                return None
            api = _summary_api()
            exporter = getattr(api, "export_summary_text", None)
            if exporter is None:
                exporter = getattr(_store_api(), "export_summary_text", None)
            if exporter is not None:
                return exporter(
                    Path(path),
                    snapshot,
                    text,
                    report_kind=kind,
                    target_minutes=target_minutes,
                    excluded_ids=excluded,
                )
            raise RuntimeError("Report export is unavailable in this build.")

        self._start_job("export", operation)

    def _stage(self) -> None:
        if self._snapshot is None:
            self._set_status("Generate and review a summary before staging.")
            return
        if self._scope_dirty:
            self._set_status("Generate again after changing scope before staging.")
            return
        schema_version = int(getattr(self._snapshot, "schema_version", 1) or 1)
        if schema_version >= 2:
            snapshot_callsign = getattr(self._snapshot, "station_callsign", None)
            callsign = self._normalize_callsign(
                self.callsign_edit.text() if snapshot_callsign is None else snapshot_callsign
            )
            if not self._has_actual_callsign(callsign):
                self._set_status("Stage blocked: enter a station callsign for this report.")
                return
        text = self.narrative_edit.toPlainText()
        try:
            _summary_api().protected_counts(text)
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            self._set_status(f"Stage blocked: {detail}")
            return
        self.stageRequested.emit(self._snapshot, text)
        self._set_status("Reviewed report handed to the parent workflow for staging.")

    @Slot()
    def _cancel_background_jobs(self) -> None:
        """Request cooperative cancellation before application teardown."""

        self._closing = True
        self._estimate_timer.stop()
        for handle in tuple(self._jobs):
            handle.worker.cancel()
            if handle.thread.isRunning() and handle not in _DETACHED_JOBS:
                _DETACHED_JOBS.append(handle)

    # ----- close/lifecycle -------------------------------------------

    def closeEvent(self, event) -> None:
        if self._any_draft_dirty() and not self._closing:
            answer = QMessageBox.question(
                self,
                "Discard edited narrative?",
                "The narrative has operator edits. Close and discard those edits?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self._estimate_timer.stop()
        if self._jobs:
            self._close_requested = True
            self._closing = True
            for handle in tuple(self._jobs):
                handle.worker.cancel()
                # The worker owns blocking I/O.  Quitting the event loop is
                # insufficient, so retain the thread until its operation ends.
                if handle.thread.isRunning():
                    if handle not in _DETACHED_JOBS:
                        _DETACHED_JOBS.append(handle)
            event.accept()
            return
        self._closing = True
        event.accept()


__all__ = ["TrafficSummaryDialog"]
