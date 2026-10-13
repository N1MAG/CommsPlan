"""Lazy, read-mostly Shortwave Explore and Data Sources workspace.

All catalogue reads, provider parsing, downloads, previews, promotion, and
rollback run in serialized daemon task lanes. This module deliberately contains
no radio, scheduler, launcher, or receiver-control action.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QTimer, Qt, Signal, QEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QGridLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QScrollArea,
    QBoxLayout,
)

from freqinout.core.known_operating_groups import net_resources_db_path
from freqinout.core.shortwave_eibi_provider import (
    EIBI_DEFAULT_CSV_URL,
    EiBiDownloadConfig,
    EiBiProviderAdapter,
    bundled_eibi_seed_paths,
)
from freqinout.core.shortwave_import import (
    apply_shortwave_import,
    preview_shortwave_import,
    rollback_shortwave_dataset,
)
from freqinout.core.shortwave_models import (
    ShortwaveImportCancelled,
    ShortwaveImportPreview,
    StaleShortwavePreviewError,
)
from freqinout.core.shortwave_query import ShortwaveListingView, ShortwaveQuery, ShortwaveQueryResult, query_shortwave
from freqinout.core.shortwave_store import ShortwaveStore
from freqinout.core.shortwave_listening import ShortwaveListeningReminder, ShortwaveListeningStore
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.logger import log
from freqinout.core.perf_metrics import emit_span
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor
from freqinout.gui.help_registry import resolve_help_host
from freqinout.gui.theme import active_app_theme, button_height_for_font, button_style, control_height_for_font, font_derived_widget_height, label_style, style_splitter_handles


_DISPLAY_COLUMNS = (
    "Station / service", "Frequency", "UTC / local", "Days", "Language", "Target", "Listing state",
)

class _TaskBridge(QObject):
    """Deliver daemon-lane results to the owning GUI thread."""

    completed = Signal(int, object, object)
    failed = Signal(int, str)
    settled = Signal(int)


_RETIRED_TASK_BRIDGES: set[_TaskBridge] = set()


class _TaskHandle:
    """Small compatibility handle with the prior QThread inspection surface."""

    def __init__(self, cancelled: threading.Event) -> None:
        self.cancelled = cancelled
        self.future: object | None = None

    def cancel(self) -> None:
        self.cancelled.set()

    def isRunning(self) -> bool:  # noqa: N802 - compatibility with QThread
        future = self.future
        return bool(future is not None and not future.done())


def _init_task_lane(owner: object, name: str) -> None:
    bridge = _TaskBridge(owner)
    bridge.completed.connect(owner._task_completed)
    bridge.failed.connect(owner._task_failed)
    bridge.settled.connect(owner._task_settled_signal)
    owner._task_bridge = bridge
    owner._task_executor = DaemonSerialExecutor(max_workers=1, thread_name_prefix=name)


def _start_task_lane(
    owner: object,
    generation: int,
    operation: Callable[[Callable[[], bool]], object],
    done: Callable[[object], None],
) -> None:
    handle = getattr(owner, "_task_thread", None)
    if handle is not None and handle.isRunning():
        handle.cancel()
        owner._pending_task = (generation, operation, done)
        return
    cancelled = threading.Event()
    handle = _TaskHandle(cancelled)
    owner._task_thread = handle
    owner._task_worker = handle
    bridge = owner._task_bridge

    def run() -> None:
        started = time.monotonic()
        try:
            result = operation(cancelled.is_set)
            if not cancelled.is_set():
                bridge.completed.emit(generation, result, done)
        except ShortwaveImportCancelled:
            pass
        except Exception as exc:
            log.exception("Shortwave background operation failed")
            bridge.failed.emit(generation, str(exc))
        finally:
            elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
            emit_span(
                "shortwave.worker",
                elapsed_ms,
                meta={"generation": generation, "cancelled": cancelled.is_set()},
                level="warning" if elapsed_ms >= 500.0 else "debug",
            )
            bridge.settled.emit(generation)

    handle.future = owner._task_executor.submit(run)


def _shutdown_task_lane(owner: object) -> None:
    handle = getattr(owner, "_task_thread", None)
    if handle is not None:
        handle.cancel()
    owner._pending_task = None
    future = getattr(handle, "future", None)
    if future is not None and not future.done():
        try:
            future.result(timeout=0.05)
        except FutureTimeoutError:
            # The operation did not honor cancellation within the small UI
            # grace. Keep its signal source alive but disconnect the closing
            # page; the daemon task may finish safely in the background.
            bridge = getattr(owner, "_task_bridge", None)
            if isinstance(bridge, _TaskBridge):
                for signal in (bridge.completed, bridge.failed, bridge.settled):
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                bridge.setParent(None)
                _RETIRED_TASK_BRIDGES.add(bridge)
        except Exception:
            pass
    executor = getattr(owner, "_task_executor", None)
    owner._task_executor = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
    owner._task_thread = None
    owner._task_worker = None


class ShortwaveListingTableModel(QAbstractTableModel):
    """A bounded data model: it does not create a widget for every listing."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.rows: tuple[ShortwaveListingView, ...] = ()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(_DISPLAY_COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> object:  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(_DISPLAY_COLUMNS):
            return _DISPLAY_COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> object:  # noqa: N802
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        item = self.rows[index.row()]
        candidate = item.entry.candidate
        if role == Qt.AccessibleTextRole:
            return " | ".join(self._display_values(item))
        if role != Qt.DisplayRole:
            return None
        return self._display_values(item)[index.column()]

    @staticmethod
    def _display_values(item: ShortwaveListingView) -> tuple[str, ...]:
        candidate = item.entry.candidate
        return (
            candidate.station_name or "Unnamed listing",
            _frequency_text(candidate.frequency_hz),
            _window_text(
                candidate.start_minute_utc,
                candidate.end_minute_utc,
                candidate.crosses_midnight,
                reference_utc=item.next_start_utc,
            ),
            candidate.raw_days or "Daily",
            ", ".join(candidate.language_labels) or candidate.signal_type or "—",
            ", ".join(candidate.target_labels) or candidate.target_raw or "—",
            item.listing_state,
        )

    def set_rows(self, rows: tuple[ShortwaveListingView, ...]) -> None:
        self.beginResetModel()
        self.rows = tuple(rows[:200])
        self.endResetModel()

    def listing_at(self, row: int) -> ShortwaveListingView | None:
        return self.rows[row] if 0 <= row < len(self.rows) else None


class ShortwaveExploreView(QWidget):
    """Search/browse page; all query execution is worker-owned and generation-safe."""

    def __init__(self, db_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db_path = Path(db_path)
        self._active = False
        self._query_generation = 0
        self._task_thread: _TaskHandle | None = None
        self._task_worker: _TaskHandle | None = None
        self._pending_task: tuple[int, Callable[[Callable[[], bool]], object], Callable[[object], None]] | None = None
        _init_task_lane(self, "fio-shortwave-explore")
        self._current_result: ShortwaveQueryResult | None = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.refresh_results)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)
        copy = QLabel("Browse source listings. A listing is schedule intelligence, not proof of reception or transmitter activity.")
        copy.setWordWrap(True)
        copy.setAccessibleName("Shortwave listing guidance")
        outer.addWidget(copy)

        filters = QWidget(self)
        filters.setObjectName("shortwaveExploreFilters")
        filter_layout = QHBoxLayout(filters)
        self.filter_layout = filter_layout
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(6)
        self.search = QLineEdit(filters)
        self.search.setPlaceholderText("Search station, 9955 kHz, language, target, home, or site")
        self.search.setAccessibleName("Search Shortwave listings")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._schedule_refresh)
        filter_layout.addWidget(self.search, 2)
        self.timing = QComboBox(filters)
        self.timing.addItem("Scheduled now", "scheduled_now")
        self.timing.addItem("Starting soon", "starting_soon")
        self.timing.addItem("All listings", "all_listings")
        self.timing.setAccessibleName("Listing time filter")
        self.timing.currentIndexChanged.connect(self._schedule_refresh)
        filter_layout.addWidget(self.timing)
        self.soon = QComboBox(filters)
        self.soon.addItem("Next 30 min", 30)
        self.soon.addItem("Next 2 hr", 120)
        self.soon.addItem("Next 6 hr", 360)
        self.soon.setAccessibleName("Starting soon window")
        self.soon.currentIndexChanged.connect(self._schedule_refresh)
        filter_layout.addWidget(self.soon)
        self.band = QComboBox(filters)
        self.band.addItem("Any band", (None, None))
        self.band.addItem("120 m", (2_300_000, 2_600_000))
        self.band.addItem("90 m", (3_200_000, 3_500_000))
        self.band.addItem("75 m", (3_900_000, 4_100_000))
        self.band.addItem("60 m", (4_700_000, 5_100_000))
        self.band.addItem("49 m", (5_700_000, 6_300_000))
        self.band.addItem("41 m", (6_900_000, 7_600_000))
        self.band.addItem("31 m", (9_200_000, 10_000_000))
        self.band.addItem("25 m", (11_400_000, 12_300_000))
        self.band.addItem("22 m", (13_300_000, 13_900_000))
        self.band.addItem("19 m", (15_000_000, 15_900_000))
        self.band.addItem("16 m", (17_200_000, 18_000_000))
        self.band.addItem("13 m", (21_300_000, 21_900_000))
        self.band.setAccessibleName("Shortwave band filter")
        self.band.currentIndexChanged.connect(self._schedule_refresh)
        filter_layout.addWidget(self.band)
        self.refresh_btn = QPushButton("Refresh", filters)
        self.refresh_btn.setAccessibleName("Refresh Shortwave results")
        self.refresh_btn.clicked.connect(self.refresh_results)
        filter_layout.addWidget(self.refresh_btn)
        outer.addWidget(filters)

        detail_filters = QWidget(self)
        detail_filter_layout = QHBoxLayout(detail_filters)
        self.detail_filter_layout = detail_filter_layout
        detail_filter_layout.setContentsMargins(0, 0, 0, 0)
        detail_filter_layout.setSpacing(6)
        self.language = QLineEdit(detail_filters)
        self.language.setPlaceholderText("Language (optional)")
        self.language.setClearButtonEnabled(True)
        self.language.setAccessibleName("Shortwave language filter")
        self.language.textChanged.connect(self._schedule_refresh)
        detail_filter_layout.addWidget(self.language)
        self.target = QLineEdit(detail_filters)
        self.target.setPlaceholderText("Target region (optional)")
        self.target.setClearButtonEnabled(True)
        self.target.setAccessibleName("Shortwave target region filter")
        self.target.textChanged.connect(self._schedule_refresh)
        detail_filter_layout.addWidget(self.target)
        self.season = QComboBox(detail_filters)
        self.season.addItem("Current source season", None)
        self.season.setAccessibleName("Current or historical source season")
        self.season.currentIndexChanged.connect(self._schedule_refresh)
        detail_filter_layout.addWidget(self.season)
        outer.addWidget(detail_filters)

        chips = QWidget(self)
        chips.setObjectName("shortwaveExploreChips")
        chip_layout = QHBoxLayout(chips)
        chip_layout.setContentsMargins(0, 0, 0, 0)
        chip_layout.setSpacing(5)
        self.classification_chips: dict[str, QToolButton] = {}
        for label, value, checked in (
            ("Broadcast", "broadcast", True), ("Utility", "utility", False),
            ("Time / standard", "time_standard", False), ("Other / review", "other", False),
        ):
            button = QToolButton(chips)
            button.setText(label)
            button.setCheckable(True)
            button.setChecked(checked)
            button.setAccessibleName(f"Include {label} listings")
            button.setToolTip("Filters the source-owned listing classification.")
            button.toggled.connect(self._schedule_refresh)
            chip_layout.addWidget(button)
            self.classification_chips[value] = button
        chip_layout.addStretch(1)
        outer.addWidget(chips)

        self.status = QLabel("Open Explore to query the installed Shortwave dataset.")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Shortwave result status")
        outer.addWidget(self.status)
        self.splitter = QSplitter(Qt.Horizontal, self)
        split = self.splitter
        self.model = ShortwaveListingTableModel(split)
        self.table = QTableView(split)
        self.table.setObjectName("shortwaveListingTable")
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setAccessibleName("Bounded Shortwave listing results")
        self.table.selectionModel().currentRowChanged.connect(self._show_detail)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        split.addWidget(self.table)
        self.detail_scroll = QScrollArea(split)
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.detail_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        detail_panel = QWidget(self.detail_scroll)
        self.detail_scroll.setWidget(detail_panel)
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(4)
        self.technical_toggle = QToolButton(detail_panel)
        self.technical_toggle.setText("Show technical details")
        self.technical_toggle.setCheckable(True)
        self.technical_toggle.setAccessibleName("Show raw Shortwave provider codes and diagnostics")
        self.technical_toggle.setToolTip("Show raw provider codes and row diagnostics for the selected listing.")
        self.technical_toggle.toggled.connect(self._refresh_selected_detail)
        detail_layout.addWidget(self.technical_toggle, 0, Qt.AlignLeft)
        self.detail = QPlainTextEdit(detail_panel)
        self.detail.setObjectName("shortwaveListingDetail")
        self.detail.setReadOnly(True)
        self.detail.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.detail.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.detail.setPlaceholderText("Select a listing for source, schedule, and provenance details.")
        self.detail.setAccessibleName("Shortwave listing details")
        detail_layout.addWidget(self.detail, 1)
        self.add_listening_btn = QPushButton("Add to Listening", detail_panel)
        self.add_listening_btn.setEnabled(False)
        self.add_listening_btn.setAccessibleName("Add selected Shortwave listing to Listening reminders")
        self.add_listening_btn.setToolTip("Save this complete source listing as a manual listening reminder. It does not tune or control a receiver.")
        self.add_listening_btn.clicked.connect(self._add_selected_to_listening)
        detail_layout.addWidget(self.add_listening_btn, 0, Qt.AlignLeft)
        split.addWidget(detail_panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([700, 420])
        outer.addWidget(split, 1)
        self.apply_theme()

    def apply_theme(self) -> None:
        theme = active_app_theme()
        self.status.setStyleSheet(label_style("muted", theme))
        self.detail.setStyleSheet(f"QPlainTextEdit {{ border: 1px solid {theme['border']}; }}")
        for control in (self.search, self.timing, self.soon, self.band, self.language, self.target, self.season):
            control.setMinimumHeight(control_height_for_font(control))
        for button in (self.refresh_btn, self.add_listening_btn, *self.classification_chips.values()):
            button.setMinimumHeight(button_height_for_font(button))
        self.refresh_btn.setStyleSheet(button_style("primary", theme))
        self.add_listening_btn.setStyleSheet(button_style("primary", theme))
        self.table.verticalHeader().setDefaultSectionSize(font_derived_widget_height(self.table))
        style_splitter_handles(self.splitter, theme, width=12)
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        compact = self.width() > 0 and self.width() < max(900, self.fontMetrics().horizontalAdvance("Shortwave listings") * 24)
        self.filter_layout.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)
        self.detail_filter_layout.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)
        self.splitter.setOrientation(Qt.Vertical if compact else Qt.Horizontal)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.apply_theme()

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if self._active == active:
            return
        self._active = active
        if not self._active:
            self._debounce.stop()
            self._cancel_workers()
            return
        self.refresh_results()

    def _schedule_refresh(self, *_: object) -> None:
        if self._active:
            self._debounce.start()

    @property
    def _workers(self) -> dict[int, tuple[_TaskHandle, _TaskHandle]]:
        """Compatibility/introspection view of the sole active worker lane."""
        if self._task_thread is None or self._task_worker is None:
            return {}
        return {self._query_generation: (self._task_thread, self._task_worker)}

    def _query(self) -> ShortwaveQuery:
        classifications = tuple(key for key, button in self.classification_chips.items() if button.isChecked())
        band_min, band_max = self.band.currentData()
        return ShortwaveQuery(
            search=self.search.text(), timing=str(self.timing.currentData()), classifications=classifications,
            language=self.language.text(), target=self.target.text(), dataset_key=self.season.currentData(),
            frequency_min_hz=band_min, frequency_max_hz=band_max, soon_minutes=int(self.soon.currentData()), limit=200,
        )

    def refresh_results(self) -> None:
        if not self._active:
            return
        self._query_generation += 1
        generation = self._query_generation
        query = self._query()
        self.status.setText("Loading a bounded Shortwave result set…")
        self.refresh_btn.setEnabled(False)
        def operation(cancelled: Callable[[], bool]) -> object:
            store = ShortwaveStore(self._db_path)
            return query_shortwave(store, query), store.list_datasets(limit=50)
        self._start_task(generation, operation, self._query_ready)

    def _query_ready(self, result: object) -> None:
        self.refresh_btn.setEnabled(True)
        if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[0], ShortwaveQueryResult):
            self.status.setText("Shortwave query returned an unexpected result.")
            return
        result, datasets = result
        self._populate_seasons(tuple(datasets), result.dataset.dataset_key if result.dataset else None)
        self._current_result = result
        rows = result.rows
        self.model.set_rows(rows)
        self.detail.clear()
        self.add_listening_btn.setEnabled(False)
        if result.dataset is None:
            self.status.setText("No current Shortwave dataset is installed. Open Data Sources to review the bundled EiBi snapshot.")
        else:
            self.status.setText(
                f"{len(rows)} listing{'s' if len(rows) != 1 else ''} shown (bounded to {result.bounded_limit}) "
                f"from {result.dataset.provider_label}, {result.dataset.season_code}; "
                f"imported {_age_text(result.dataset.imported_utc)}."
            )

    def _populate_seasons(self, datasets: tuple[object, ...], current_key: str | None) -> None:
        selected = self.season.currentData()
        self.season.blockSignals(True)
        try:
            self.season.clear()
            self.season.addItem("Current source season", None)
            for dataset in datasets:
                if getattr(dataset, "state", "") not in {"current", "superseded"}:
                    continue
                text = f"{getattr(dataset, 'season_code', 'Unknown season')} ({getattr(dataset, 'state', 'retained')})"
                self.season.addItem(text, getattr(dataset, "dataset_key", None))
            preferred = selected if selected else None
            idx = self.season.findData(preferred)
            self.season.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self.season.blockSignals(False)

    def _show_detail(self, current: QModelIndex, _previous: QModelIndex) -> None:
        item = self.model.listing_at(current.row())
        if item is None or self._current_result is None:
            self.detail.clear()
            self.add_listening_btn.setEnabled(False)
            return
        entry = item.entry.candidate
        dataset = self._current_result.dataset
        countries = self._current_result.dictionaries.get("countries", {})
        home_country = countries.get(entry.station_home_code, entry.station_home_code)
        lines = [
            entry.station_name or "Unnamed listing",
            "",
            f"Frequency: {_frequency_text(entry.frequency_hz)}",
            f"Listing state: {item.listing_state} — not a reception claim.",
            f"Time: {_window_text(entry.start_minute_utc, entry.end_minute_utc, entry.crosses_midnight, reference_utc=item.next_start_utc)}; days: {entry.raw_days or 'Daily'}",
            f"Station home country: {home_country or 'Not supplied'}",
            f"Transmitter site: {', '.join(entry.transmitter_labels) or entry.transmitter_raw or 'Not supplied'}",
            f"Target audience / region: {', '.join(entry.target_labels) or entry.target_raw or 'Not supplied'}",
            f"Language / signal: {', '.join(entry.language_labels) or entry.signal_type or entry.language_raw or 'Not supplied'}",
            f"Classification: {entry.classification or 'Other / review'}; interpretation: {entry.parse_state.replace('_', ' ').title()}",
            "",
            f"Why this result: {item.why}",
        ]
        if dataset is not None:
            lines += [
                "", "Provenance",
                f"Provider: {dataset.provider_label}", f"Season: {dataset.season_code} ({dataset.season_effective_from_utc} to {dataset.season_effective_to_utc})",
                f"Provider update: {dataset.publisher_updated_utc or 'Not supplied'}", f"Imported: {dataset.imported_utc}",
                str(dataset.metadata.get("schedule_accuracy_caveat") or "A listing is not proof of reception or transmitter activity."),
            ]
        if self.technical_toggle.isChecked():
            technical = [f"Raw provider codes — home: {entry.station_home_code}; target: {entry.target_raw}; transmitter: {entry.transmitter_raw}."]
            if entry.diagnostics:
                technical.append("Row diagnostics: " + "; ".join(f"{d.severity}: {d.message}" for d in entry.diagnostics[:5]))
            lines += ["", "Technical details", *technical]
        self.detail.setPlainText("\n".join(lines))
        self.add_listening_btn.setEnabled(entry.parse_state == "complete")

    def _add_selected_to_listening(self) -> None:
        item = self.model.listing_at(self.table.currentIndex().row())
        if item is None or item.entry.candidate.parse_state != "complete":
            return
        parent = self.parent()
        while parent is not None and not hasattr(parent, "add_listing_to_listening"):
            parent = parent.parent()
        if parent is not None:
            parent.add_listing_to_listening(item)

    def _refresh_selected_detail(self, *_: object) -> None:
        index = self.table.currentIndex()
        self._show_detail(index, QModelIndex())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.table.parentWidget().setOrientation(Qt.Vertical if self.width() < 960 else Qt.Horizontal)
        super().resizeEvent(event)

    def _start_task(self, generation: int, operation: Callable[[Callable[[], bool]], object], done: Callable[[object], None]) -> None:
        _start_task_lane(self, generation, operation, done)

    def _task_completed(self, generation: int, result: object, done: object) -> None:
        self._finish_task(generation, result, done)

    def _task_failed(self, generation: int, message: str) -> None:
        self._query_failed(generation, message)

    def _task_settled_signal(self, _generation: int) -> None:
        self._task_finished()

    def _finish_task(self, generation: int, result: object, done: Callable[[object], None]) -> None:
        if self._active and generation == self._query_generation:
            done(result)

    def _query_failed(self, generation: int, message: str) -> None:
        if self._active and generation == self._query_generation:
            self.refresh_btn.setEnabled(True)
            self.status.setText(f"Shortwave query failed: {message}")

    def _cancel_workers(self) -> None:
        self._pending_task = None
        if self._task_worker is not None:
            self._task_worker.cancel()

    def _task_finished(self) -> None:
        self._task_thread = None
        self._task_worker = None
        pending = self._pending_task
        self._pending_task = None
        if pending is not None and self._active:
            self._start_task(*pending)

    def shutdown(self) -> None:
        self._active = False
        self._cancel_workers()
        _shutdown_task_lane(self)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.shutdown()
        super().closeEvent(event)


class ShortwaveListeningTableModel(QAbstractTableModel):
    """A small, bounded model for station-owned listening reminders."""

    _columns = ("Listening reminder", "Station / frequency", "UTC / local", "Source health", "Receiver")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.rows: tuple[ShortwaveListeningReminder, ...] = ()
        self.receivers: dict[int, str] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> object:  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self._columns):
            return self._columns[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> object:  # noqa: N802
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        reminder = self.rows[index.row()]
        snapshot = reminder.snapshot
        values = (
            reminder.operator_label or snapshot.station_name,
            f"{snapshot.station_name} · {_frequency_text(snapshot.frequency_hz)}",
            _window_text(snapshot.start_minute_utc, snapshot.end_minute_utc, snapshot.crosses_midnight),
            _source_health_label(reminder.source_review_state),
            self.receivers.get(
                reminder.receiver_profile_id or -1,
                f"Saved receiver #{reminder.receiver_profile_id} (not configured)"
                if reminder.receiver_profile_id is not None else "No receiver selected",
            ),
        )
        if role == Qt.AccessibleTextRole:
            return " | ".join(values)
        return values[index.column()] if role == Qt.DisplayRole else None

    def set_rows(self, rows: tuple[ShortwaveListeningReminder, ...], receivers: dict[int, str]) -> None:
        self.beginResetModel()
        self.rows = tuple(rows[:200])
        self.receivers = dict(receivers)
        self.endResetModel()

    def reminder_at(self, row: int) -> ShortwaveListeningReminder | None:
        return self.rows[row] if 0 <= row < len(self.rows) else None


class ShortwaveListeningView(QWidget):
    """Manual, receive-only reminder editor with one bounded worker lane."""

    def __init__(self, db_path: Path, parent: QWidget | None = None, *, receiver_profiles_provider: Callable[[], object] | None = None) -> None:
        super().__init__(parent)
        self._db_path = Path(db_path)
        self._receiver_profiles_provider = receiver_profiles_provider
        self._active = False
        self._generation = 0
        self._task_thread: _TaskHandle | None = None
        self._task_worker: _TaskHandle | None = None
        self._pending_task: tuple[int, Callable[[Callable[[], bool]], object], Callable[[object], None]] | None = None
        _init_task_lane(self, "fio-shortwave-listening")
        self._receivers: dict[int, str] = {}
        self._current_review: object | None = None
        self._pending_focus_key: str | None = None
        self._editing_key: str | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        intro = QLabel("Saved Shortwave reminders are manual listening cues. CommsPlan never tunes, launches, schedules, or transmits from this page.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.status = QLabel("Open Listening to load saved reminders.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.splitter = QSplitter(Qt.Vertical, self)
        split = self.splitter
        self.model = ShortwaveListeningTableModel(split)
        self.table = QTableView(split)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAccessibleName("Saved Shortwave listening reminders")
        self.table.selectionModel().currentRowChanged.connect(self._select_reminder)
        split.addWidget(self.table)
        self.editor_scroll = QScrollArea(split)
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.editor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        editor = QWidget(self.editor_scroll)
        self.editor_scroll.setWidget(editor)
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(5)
        editor_title = QLabel("Selected reminder")
        editor_title.setStyleSheet("font-weight: 700;")
        editor_layout.addWidget(editor_title)
        self.snapshot_summary = QLabel("Select a saved reminder to review its accepted listing snapshot.")
        self.snapshot_summary.setWordWrap(True)
        self.snapshot_summary.setAccessibleName("Accepted Shortwave listing snapshot")
        editor_layout.addWidget(self.snapshot_summary)
        form = QFormLayout()
        self.label_edit = QLineEdit(editor)
        self.label_edit.setPlaceholderText("Optional reminder label")
        self.label_edit.setAccessibleName("Listening reminder label")
        self.notes_edit = QLineEdit(editor)
        self.notes_edit.setPlaceholderText("Optional operator note")
        self.notes_edit.setAccessibleName("Listening reminder note")
        self.lead_combo = QComboBox(editor)
        for minutes in (0, 5, 10, 15, 30, 60):
            self.lead_combo.addItem("At start" if minutes == 0 else f"{minutes} minutes early", minutes)
        self.lead_combo.setAccessibleName("Listening reminder lead time")
        self.enabled_check = QCheckBox("Reminder enabled", editor)
        self.enabled_check.setChecked(True)
        self.enabled_check.setAccessibleName("Enable this listening reminder")
        self.receiver_combo = QComboBox(editor)
        self.receiver_combo.addItem("No receiver selected", None)
        self.receiver_combo.setAccessibleName("Optional configured receiver identity")
        self.receiver_combo.setToolTip("An informational station reference only. CommsPlan will not tune or control it from a Shortwave reminder.")
        form.addRow("Label", self.label_edit)
        form.addRow("Note", self.notes_edit)
        form.addRow("Remind", self.lead_combo)
        form.addRow("Receiver", self.receiver_combo)
        form.addRow("", self.enabled_check)
        editor_layout.addLayout(form)
        actions = QGridLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setHorizontalSpacing(6)
        actions.setVerticalSpacing(4)
        self.save_btn = QPushButton("Save reminder", editor)
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.save_selected)
        actions.addWidget(self.save_btn, 0, 0)
        self.remove_btn = QPushButton("Remove", editor)
        self.remove_btn.setEnabled(False)
        self.remove_btn.setToolTip("Remove this CommsPlan reminder only. The source dataset and listing remain unchanged.")
        self.remove_btn.clicked.connect(self.remove_selected)
        actions.addWidget(self.remove_btn, 0, 1)
        self.apply_btn = QPushButton("Apply listing update", editor)
        self.apply_btn.setEnabled(False)
        self.apply_btn.setToolTip("Replace the accepted snapshot only after reviewing the current source listing.")
        self.apply_btn.clicked.connect(self.apply_selected)
        actions.addWidget(self.apply_btn, 1, 0)
        self.keep_btn = QPushButton("Keep my reminder", editor)
        self.keep_btn.setEnabled(False)
        self.keep_btn.setToolTip("Keep the accepted listing snapshot when the source changed or no longer contains it.")
        self.keep_btn.clicked.connect(self.keep_selected)
        actions.addWidget(self.keep_btn, 1, 1)
        actions.setColumnStretch(2, 1)
        self.refresh_btn = QPushButton("Refresh", editor)
        self.refresh_btn.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_btn, 0, 3)
        editor_layout.addLayout(actions)
        split.addWidget(editor)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([300, 260])
        layout.addWidget(split, 1)
        self.apply_theme()

    def apply_theme(self) -> None:
        theme = active_app_theme()
        self.status.setStyleSheet(label_style("muted", theme))
        self.snapshot_summary.setStyleSheet(label_style("muted", theme))
        for control in (self.label_edit, self.notes_edit, self.lead_combo, self.receiver_combo):
            control.setMinimumHeight(control_height_for_font(control))
        for button in (self.save_btn, self.remove_btn, self.apply_btn, self.keep_btn, self.refresh_btn):
            button.setMinimumHeight(button_height_for_font(button))
        self.save_btn.setStyleSheet(button_style("primary", theme))
        self.apply_btn.setStyleSheet(button_style("primary", theme))
        self.remove_btn.setStyleSheet(button_style("eligible_danger", theme))
        self.keep_btn.setStyleSheet(button_style("secondary", theme))
        self.refresh_btn.setStyleSheet(button_style("muted", theme))
        self.table.verticalHeader().setDefaultSectionSize(font_derived_widget_height(self.table))
        style_splitter_handles(self.splitter, theme, width=12)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.apply_theme()

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if self._active == active:
            return
        self._active = active
        if active:
            self.refresh()
        else:
            self._cancel_workers()

    def refresh(self) -> None:
        if not self._active:
            return
        self.status.setText("Refreshing bounded listening reminders…")
        self._run(self._load_data, self._loaded)

    def _load_data(self, cancelled: Callable[[], bool]) -> object:
        store = ShortwaveListeningStore(self._db_path)
        store.refresh_source_review_states()
        if cancelled():
            raise ShortwaveImportCancelled()
        receivers: dict[int, str] = {}
        try:
            raw_profiles = self._receiver_profiles_provider() if self._receiver_profiles_provider else MultiRadioStore().list_device_profiles()
            for profile in raw_profiles:
                profile_id = profile.get("id")
                if profile_id is None:
                    continue
                name = str(profile.get("name") or profile.get("label") or f"Device {profile_id}").strip()
                device_class = str(profile.get("device_class") or "").strip().lower()
                receivers[int(profile_id)] = f"{name}{' · receive-only' if device_class == 'observer' else ''}"
        except Exception:
            # A missing/nonstandard settings profile must not prevent reminder use.
            receivers = {}
        return store.list_reminders(limit=200), receivers

    def _loaded(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            self.status.setText("Listening reminders were not available.")
            return
        reminders, receivers = result
        self._receivers = dict(receivers)
        current_key = self._pending_focus_key or self._selected_key()
        self.receiver_combo.blockSignals(True)
        try:
            self.receiver_combo.clear()
            self.receiver_combo.addItem("No receiver selected", None)
            for receiver_id, label in sorted(self._receivers.items(), key=lambda item: item[1].casefold()):
                self.receiver_combo.addItem(label, receiver_id)
        finally:
            self.receiver_combo.blockSignals(False)
        self.model.set_rows(tuple(reminders), self._receivers)
        self.status.setText(f"{len(reminders)} saved listening reminder{'s' if len(reminders) != 1 else ''}. Manual tuning only.")
        if self._select_key(current_key):
            self._pending_focus_key = None

    def add_listing(self, item: ShortwaveListingView) -> None:
        """Persist a selected complete listing on the worker lane, then expose its editor."""
        if item.entry.candidate.parse_state != "complete":
            return
        self._active = True
        self.status.setText("Saving accepted listing snapshot…")
        entry_key, dataset_key = item.entry.entry_key, item.entry.dataset_key
        def operation(cancelled: Callable[[], bool]) -> object:
            if cancelled():
                raise ShortwaveImportCancelled()
            return ShortwaveListeningStore(self._db_path).add_from_entry(entry_key, dataset_key=dataset_key)
        self._run(operation, self._added)

    def _added(self, result: object) -> None:
        if isinstance(result, ShortwaveListeningReminder):
            self.status.setText("Saved accepted listing snapshot. Review the reminder fields and select Save when ready.")
            key = result.reminder_key
            self._run(self._load_data, lambda loaded: self._loaded_and_select(loaded, key))

    def _loaded_and_select(self, result: object, key: str) -> None:
        self._loaded(result)
        self._select_key(key)

    def _selected_key(self) -> str | None:
        reminder = self.model.reminder_at(self.table.currentIndex().row())
        return reminder.reminder_key if reminder else self._editing_key

    def focus_reminder(self, reminder_key: str) -> None:
        """Select a reminder now or after the current bounded refresh completes."""
        self._pending_focus_key = str(reminder_key)
        if self._select_key(self._pending_focus_key):
            self._pending_focus_key = None

    def _select_key(self, reminder_key: str | None) -> bool:
        if not reminder_key:
            return False
        for index, reminder in enumerate(self.model.rows):
            if reminder.reminder_key == reminder_key:
                self.table.selectRow(index)
                self.table.setCurrentIndex(self.model.index(index, 0))
                return True
        return False

    def _select_reminder(self, current: QModelIndex, _previous: QModelIndex) -> None:
        reminder = self.model.reminder_at(current.row())
        has = reminder is not None
        self._editing_key = reminder.reminder_key if reminder else None
        self.save_btn.setEnabled(has)
        self.remove_btn.setEnabled(has)
        self.apply_btn.setEnabled(False)
        self.keep_btn.setEnabled(False)
        self._current_review = None
        if not reminder:
            self.snapshot_summary.setText("Select a saved reminder to review its accepted listing snapshot.")
            return
        snapshot = reminder.snapshot
        self.label_edit.setText(reminder.operator_label)
        self.notes_edit.setText(reminder.notes)
        lead_index = self.lead_combo.findData(reminder.lead_minutes)
        if lead_index < 0:
            self.lead_combo.addItem(f"{reminder.lead_minutes} minutes early (saved)", reminder.lead_minutes)
            lead_index = self.lead_combo.findData(reminder.lead_minutes)
        self.lead_combo.setCurrentIndex(lead_index)
        receiver_index = self.receiver_combo.findData(reminder.receiver_profile_id)
        if reminder.receiver_profile_id is not None and receiver_index < 0:
            self.receiver_combo.addItem(
                f"Saved receiver #{reminder.receiver_profile_id} (not currently configured)",
                reminder.receiver_profile_id,
            )
            receiver_index = self.receiver_combo.findData(reminder.receiver_profile_id)
        self.receiver_combo.setCurrentIndex(receiver_index if receiver_index >= 0 else 0)
        self.enabled_check.setChecked(reminder.enabled)
        lines = [
            f"Accepted listing: {snapshot.station_name} · {_frequency_text(snapshot.frequency_hz)} · {snapshot.signal_type or 'mode not supplied'}",
            f"UTC / local: {_window_text(snapshot.start_minute_utc, snapshot.end_minute_utc, snapshot.crosses_midnight)} · days: {snapshot.raw_days or 'Daily'}",
            f"Source: {snapshot.provider_label} · {snapshot.season_code} · {_source_health_label(reminder.source_review_state)}",
            "This is a saved source snapshot; a source update never changes it until you choose Apply listing update.",
        ]
        self.snapshot_summary.setText("\n".join(lines))
        selected_key = reminder.reminder_key
        self._run(
            lambda cancelled: ShortwaveListeningStore(self._db_path).source_review(selected_key),
            lambda review, key=selected_key: self._review_loaded(key, review),
        )

    def _review_loaded(self, reminder_key: str, review: object) -> None:
        if self._selected_key() != reminder_key:
            return
        self._current_review = review
        state = str(getattr(review, "state", "current") or "current")
        changed_fields = tuple(getattr(review, "changed_fields", ()) or ())
        current = getattr(review, "current", None)
        self.apply_btn.setEnabled(state == "changed" and current is not None)
        self.keep_btn.setEnabled(state in {"changed", "missing"})
        if state == "changed":
            detail = ", ".join(changed_fields) or "provider content"
            current_summary = ""
            if current is not None:
                current_summary = (
                    f"\nCurrent source: {getattr(current, 'station_name', 'Listing')} · "
                    f"{_frequency_text(int(getattr(current, 'frequency_hz', 0) or 0))} · "
                    f"{_window_text(getattr(current, 'start_minute_utc', None), getattr(current, 'end_minute_utc', None), bool(getattr(current, 'crosses_midnight', False)))} · "
                    f"days: {getattr(current, 'raw_days', '') or 'Daily'} · "
                    f"{getattr(current, 'signal_type', '') or 'mode not supplied'}."
                )
            self.snapshot_summary.setText(
                self.snapshot_summary.text()
                + f"\nSource update available. Changed: {detail}.{current_summary} Review before choosing Apply or Keep."
            )
        elif state == "missing":
            self.snapshot_summary.setText(
                self.snapshot_summary.text()
                + "\nThe current source no longer contains this listing. Keep preserves your accepted snapshot."
            )
        elif state == "kept":
            self.snapshot_summary.setText(
                self.snapshot_summary.text()
                + "\nYou chose to keep this accepted snapshot; no source update was applied."
            )

    def save_selected(self) -> None:
        key = self._selected_key()
        if not key:
            return
        self.status.setText("Saving reminder…")
        data = (self.label_edit.text(), self.notes_edit.text(), self.receiver_combo.currentData(), int(self.lead_combo.currentData()), self.enabled_check.isChecked())
        self._run(lambda cancelled: ShortwaveListeningStore(self._db_path).update(key, operator_label=data[0], notes=data[1], receiver_profile_id=data[2], lead_minutes=data[3], enabled=data[4]), self._mutated)

    def remove_selected(self) -> None:
        key = self._selected_key()
        if key and QMessageBox.question(
            self,
            "Remove listening reminder",
            "Remove this CommsPlan listening reminder? The source listing is not deleted.",
            QMessageBox.Remove | QMessageBox.Cancel,
            QMessageBox.Cancel,
        ) == QMessageBox.Remove:
            self._run(lambda cancelled: ShortwaveListeningStore(self._db_path).remove(key), self._mutated)

    def apply_selected(self) -> None:
        key = self._selected_key()
        if key:
            self._run(lambda cancelled: ShortwaveListeningStore(self._db_path).apply_current_listing(key), self._mutated)

    def keep_selected(self) -> None:
        key = self._selected_key()
        if key:
            self._run(lambda cancelled: ShortwaveListeningStore(self._db_path).keep_accepted_snapshot(key), self._mutated)

    def _mutated(self, _result: object) -> None:
        self.status.setText("Reminder saved. Refreshing source health…")
        self._run(self._load_data, self._loaded)

    def _run(self, operation: Callable[[Callable[[], bool]], object], done: Callable[[object], None]) -> None:
        self._generation += 1
        generation = self._generation
        _start_task_lane(self, generation, operation, done)

    def _task_completed(self, generation: int, result: object, done: object) -> None:
        self._finished(generation, result, done)

    def _task_failed(self, generation: int, message: str) -> None:
        self._failed(generation, message)

    def _task_settled_signal(self, _generation: int) -> None:
        self._task_settled()

    def _finished(self, generation: int, result: object, done: Callable[[object], None]) -> None:
        if self._active and generation == self._generation:
            done(result)

    def _failed(self, generation: int, message: str) -> None:
        if self._active and generation == self._generation:
            self.status.setText(f"Listening reminder action failed: {message}")

    def _cancel_workers(self) -> None:
        self._pending_task = None
        if self._task_worker:
            self._task_worker.cancel()

    def _task_settled(self) -> None:
        self._task_thread = None
        self._task_worker = None
        pending, self._pending_task = self._pending_task, None
        if pending is not None and self._active:
            _generation, operation, done = pending
            self._run(operation, done)

    def shutdown(self) -> None:
        self._active = False
        self._cancel_workers()
        _shutdown_task_lane(self)


class ShortwaveDataSourcesView(QWidget):
    """Preview-first source lifecycle surface with explicit Apply and rollback."""

    def __init__(self, db_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db_path = Path(db_path)
        self._active = False
        self._generation = 0
        self._task_thread: _TaskHandle | None = None
        self._task_worker: _TaskHandle | None = None
        self._pending_task: tuple[int, Callable[[Callable[[], bool]], object], Callable[[object], None]] | None = None
        _init_task_lane(self, "fio-shortwave-sources")
        self._preview: ShortwaveImportPreview | None = None
        self._datasets: tuple[object, ...] = ()
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        intro = QLabel("EiBi / Eike Bierwirth source data is versioned and immutable. Reviewing a source makes no change; Apply promotes only the reviewed preview.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.status = QLabel("Open Data Sources to inspect installed Shortwave datasets.")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Shortwave source status")
        layout.addWidget(self.status)
        source_row = QHBoxLayout()
        self.source_row = source_row
        self.refresh_btn = QPushButton("Refresh source status", self)
        self.refresh_btn.clicked.connect(self.refresh_status)
        source_row.addWidget(self.refresh_btn)
        self.review_bundled_btn = QPushButton("Review bundled snapshot", self)
        self.review_bundled_btn.setToolTip("Parse the packaged offline EiBi snapshot in the background and show an import preview.")
        self.review_bundled_btn.clicked.connect(self.review_bundled)
        source_row.addWidget(self.review_bundled_btn)
        self.review_official_btn = QPushButton("Review official update", self)
        self.review_official_btn.setToolTip(f"Download only the reviewed EiBi source at {EIBI_DEFAULT_CSV_URL}; no arbitrary URL is accepted.")
        self.review_official_btn.clicked.connect(self.review_official)
        source_row.addWidget(self.review_official_btn)
        self.review_file_btn = QPushButton("Review local files…", self)
        self.review_file_btn.clicked.connect(self.review_files)
        source_row.addWidget(self.review_file_btn)
        source_row.addStretch(1)
        layout.addLayout(source_row)
        metadata = QWidget(self)
        form = QFormLayout(metadata)
        self.season_code = QLineEdit("A26", metadata)
        self.season_code.setAccessibleName("Provider season code")
        self.season_from = QLineEdit("2026-03-29T00:00:00Z", metadata)
        self.season_from.setAccessibleName("Provider season effective from UTC")
        self.season_to = QLineEdit("2026-10-25T23:59:59Z", metadata)
        self.season_to.setAccessibleName("Provider season effective to UTC")
        self.publisher_updated = QLineEdit("2026-08-31T00:00:00Z", metadata)
        self.publisher_updated.setAccessibleName("Provider update date UTC")
        form.addRow("Season", self.season_code)
        form.addRow("Effective from UTC", self.season_from)
        form.addRow("Effective to UTC", self.season_to)
        form.addRow("Provider update UTC", self.publisher_updated)
        layout.addWidget(metadata)
        actions = QHBoxLayout()
        self.actions_row = actions
        self.apply_btn = QPushButton("Apply reviewed import", self)
        self.apply_btn.setEnabled(False)
        self.apply_btn.setAccessibleName("Apply reviewed Shortwave import")
        self.apply_btn.clicked.connect(self.apply_preview)
        actions.addWidget(self.apply_btn)
        self.rollback_selector = QComboBox(self)
        self.rollback_selector.setAccessibleName("Retained Shortwave dataset to restore")
        actions.addWidget(self.rollback_selector, 1)
        self.rollback_btn = QPushButton("Restore selected dataset", self)
        self.rollback_btn.setEnabled(False)
        self.rollback_btn.clicked.connect(self.rollback_selected)
        actions.addWidget(self.rollback_btn)
        self.export_diagnostics_btn = QPushButton("Export diagnostics…", self)
        self.export_diagnostics_btn.clicked.connect(self.export_diagnostics)
        actions.addWidget(self.export_diagnostics_btn)
        layout.addLayout(actions)
        self.preview_text = QPlainTextEdit(self)
        self.preview_text.setReadOnly(True)
        self.preview_text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.preview_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.preview_text.setPlaceholderText("Import/update preview and diagnostics appear here. Nothing is changed until Apply.")
        self.preview_text.setAccessibleName("Shortwave import preview and diagnostics")
        layout.addWidget(self.preview_text, 1)
        self.apply_theme()

    def apply_theme(self) -> None:
        theme = active_app_theme()
        self.status.setStyleSheet(label_style("muted", theme))
        self.preview_text.setStyleSheet(f"QPlainTextEdit {{ border: 1px solid {theme['border']}; }}")
        for control in (self.season_code, self.season_from, self.season_to, self.publisher_updated, self.rollback_selector):
            control.setMinimumHeight(control_height_for_font(control))
        for button in (self.refresh_btn, self.review_bundled_btn, self.review_official_btn, self.review_file_btn, self.apply_btn, self.rollback_btn, self.export_diagnostics_btn):
            button.setMinimumHeight(button_height_for_font(button))
        self.refresh_btn.setStyleSheet(button_style("muted", theme))
        self.apply_btn.setStyleSheet(button_style("primary", theme))
        self.preview_text.setMinimumHeight(
            max(control_height_for_font(self.preview_text, vertical_padding=18, floor=48) * 3, self.preview_text.fontMetrics().lineSpacing() * 4 + 20)
        )
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        compact = self.width() > 0 and self.width() < max(900, self.fontMetrics().horizontalAdvance("Shortwave source data") * 24)
        direction = QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight
        self.source_row.setDirection(direction)
        self.actions_row.setDirection(direction)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.apply_theme()

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if self._active == active:
            return
        self._active = active
        if not self._active:
            self._cancel_workers()
            return
        self.refresh_status()

    def refresh_status(self) -> None:
        if not self._active:
            return
        self._run(lambda cancelled: (ShortwaveStore(self._db_path).schema_available(), ShortwaveStore(self._db_path).list_datasets(limit=50)), self._status_ready)

    def _status_ready(self, result: object) -> None:
        available, datasets = result if isinstance(result, tuple) and len(result) == 2 else (False, ())
        self._datasets = tuple(datasets)
        self.rollback_selector.clear()
        current = None
        for dataset in self._datasets:
            label = f"{dataset.provider_label} — {dataset.season_code} — {dataset.state}"
            self.rollback_selector.addItem(label, dataset.dataset_key)
            if dataset.state == "current":
                current = dataset
        self.rollback_btn.setEnabled(bool(self._datasets))
        if not available:
            self.status.setText("Shortwave schema is not available in this profile. Resources remains unavailable until startup authority/migration completes.")
        elif current is None:
            self.status.setText("Shortwave schema is ready; no current dataset is installed. Review the bundled snapshot to begin.")
        else:
            age = _age_text(current.imported_utc)
            self.status.setText(f"Current: {current.provider_label}, {current.season_code}; {current.record_count:,} source rows; imported {age}. Source: {current.source_uri}")

    def _parse_args(self) -> dict[str, str]:
        return {
            "season_code": self.season_code.text().strip(),
            "season_effective_from_utc": self.season_from.text().strip(),
            "season_effective_to_utc": self.season_to.text().strip(),
            "publisher_updated_utc": self.publisher_updated.text().strip() or None,
        }

    def review_bundled(self) -> None:
        csv_path, readme_path = bundled_eibi_seed_paths()
        self._review_source(csv_path, readme_path, source_uri="bundled EiBi snapshot")

    def review_files(self) -> None:
        csv_file, _ = QFileDialog.getOpenFileName(self, "Choose EiBi CSV", "", "EiBi CSV (*.csv);;All files (*)")
        if not csv_file:
            return
        readme_file, _ = QFileDialog.getOpenFileName(self, "Choose matching EiBi README", str(Path(csv_file).parent), "Text files (*.txt *.TXT);;All files (*)")
        if readme_file:
            self._review_source(Path(csv_file), Path(readme_file), source_uri="operator-selected local EiBi files")

    def review_official(self) -> None:
        args = self._parse_args()
        self.status.setText("Downloading and parsing the reviewed EiBi source in the background…")
        def operation(cancelled: Callable[[], bool]) -> object:
            downloaded = EiBiProviderAdapter().download_official(
                EiBiDownloadConfig(timeout_seconds=5.0),
                cancellation_probe=cancelled,
            )
            if cancelled():
                raise ShortwaveImportCancelled()
            candidate = EiBiProviderAdapter().parse(
                downloaded.csv_bytes, downloaded.readme_bytes, source_uri=downloaded.csv_url,
                source_filename=Path(downloaded.csv_url).name, cancellation_probe=cancelled, **args,
            )
            return preview_shortwave_import(ShortwaveStore(self._db_path), candidate)
        self._run(operation, self._preview_ready)

    def _review_source(self, csv_source: Path, readme_source: Path, *, source_uri: str) -> None:
        args = self._parse_args()
        self.status.setText("Parsing source files in the background…")
        def operation(cancelled: Callable[[], bool]) -> object:
            candidate = EiBiProviderAdapter().parse(csv_source, readme_source, source_uri=source_uri, cancellation_probe=cancelled, **args)
            return preview_shortwave_import(ShortwaveStore(self._db_path), candidate)
        self._run(operation, self._preview_ready)

    def _preview_ready(self, result: object) -> None:
        if not isinstance(result, ShortwaveImportPreview):
            self.preview_text.setPlainText("Source preview was not available.")
            return
        self._preview = result
        candidate = result.candidate
        diagnostics = candidate.diagnostics[:40]
        lines = [
            f"Review: {candidate.provider_label} / {candidate.season_code}",
            f"New {result.new_count:,}; changed {result.changed_count:,}; unchanged {result.unchanged_count:,}; removed {result.removed_count:,}.",
            f"Exact duplicates {result.duplicate_count:,}; invalid {result.invalid_count:,}; special/review {result.special_count:,}; inactive {result.inactive_count:,}.",
            f"Saved Listening reminders affected: {result.reminder_changed_count:,} changed; {result.reminder_missing_count:,} missing from this source.",
            f"Source: {candidate.source_uri}",
            "No data has changed. Apply promotes this reviewed dataset transactionally.",
        ]
        if result.fatal_errors:
            lines += ["", "Cannot apply:", *result.fatal_errors]
        if diagnostics:
            lines += ["", "Diagnostics (first 40):", *(f"line {d.line_number or '—'} {d.severity}: {d.message}" for d in diagnostics)]
        self.preview_text.setPlainText("\n".join(lines))
        self.apply_btn.setEnabled(result.actionable)
        self.status.setText("Review ready. Confirm Apply to change the current dataset.")

    def apply_preview(self) -> None:
        preview = self._preview
        if preview is None:
            return
        answer = QMessageBox.question(self, "Apply reviewed Shortwave import", "Promote the reviewed dataset? The current dataset remains retained for rollback.", QMessageBox.Apply | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Apply:
            return
        self.apply_btn.setEnabled(False)
        self.status.setText("Applying reviewed dataset in the background…")
        self._run(lambda cancelled: apply_shortwave_import(ShortwaveStore(self._db_path), preview, cancel_check=cancelled), self._apply_ready)

    def _apply_ready(self, result: object) -> None:
        self._preview = None
        self.preview_text.appendPlainText(f"\nApply result: {getattr(result, 'status', 'complete')}. Current dataset: {getattr(result, 'dataset_key', '—')}")
        self.status.setText("Shortwave import completed. Refreshing source status…")
        self._notify_source_changed()
        self.refresh_status()

    def rollback_selected(self) -> None:
        dataset_key = str(self.rollback_selector.currentData() or "")
        if not dataset_key:
            return
        answer = QMessageBox.question(self, "Restore retained dataset", "Restore this immutable retained dataset as current? No rows are deleted.", QMessageBox.RestoreDefaults | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.RestoreDefaults:
            return
        self.status.setText("Restoring the selected dataset in the background…")
        self._run(lambda cancelled: rollback_shortwave_dataset(ShortwaveStore(self._db_path), dataset_key), self._rollback_ready)

    def _rollback_ready(self, result: object) -> None:
        self.preview_text.appendPlainText(f"\nRollback result: {getattr(result, 'status', 'complete')}. Current dataset: {getattr(result, 'dataset_key', '—')}")
        self._notify_source_changed()
        self.refresh_status()

    def _notify_source_changed(self) -> None:
        parent = self.parent()
        while parent is not None and not hasattr(parent, "on_shortwave_source_changed"):
            parent = parent.parent()
        if parent is not None:
            parent.on_shortwave_source_changed()

    def export_diagnostics(self) -> None:
        current = next((item for item in self._datasets if item.state == "current"), None)
        if current is None:
            self.preview_text.appendPlainText("\nNo current dataset diagnostics are available to export.")
            return
        target, _ = QFileDialog.getSaveFileName(self, "Export Shortwave diagnostics", f"shortwave-{current.season_code}-diagnostics.json", "JSON files (*.json)")
        if not target:
            return
        def operation(cancelled: Callable[[], bool]) -> object:
            diagnostics = ShortwaveStore(self._db_path).dataset_diagnostics(current.dataset_key, limit=10_000)
            if cancelled():
                raise ShortwaveImportCancelled()
            payload = {
                "provider": current.provider_label, "season": current.season_code, "state": current.state,
                "imported_utc": current.imported_utc, "diagnostic_counts": dict(current.diagnostic_counts),
                "source_uri": current.source_uri,
                "diagnostics": [
                    {"line_number": item.line_number, "severity": item.severity, "code": item.code,
                     "message": item.message, "raw_value": item.raw_value}
                    for item in diagnostics
                ],
            }
            Path(target).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            return target
        self.status.setText("Exporting bounded diagnostics in the background…")
        self._run(operation, lambda result: self.preview_text.appendPlainText(f"\nDiagnostics exported to {result}."))

    def _run(self, operation: Callable[[Callable[[], bool]], object], done: Callable[[object], None]) -> None:
        self._generation += 1
        generation = self._generation
        _start_task_lane(self, generation, operation, done)

    def _task_completed(self, generation: int, result: object, done: object) -> None:
        self._finish(generation, result, done)

    def _task_failed(self, generation: int, message: str) -> None:
        self._failed(generation, message)

    def _task_settled_signal(self, _generation: int) -> None:
        self._task_finished()

    def _finish(self, generation: int, result: object, done: Callable[[object], None]) -> None:
        if self._active and generation == self._generation:
            done(result)

    def _failed(self, generation: int, message: str) -> None:
        if self._active and generation == self._generation:
            self.apply_btn.setEnabled(self._preview is not None and self._preview.actionable)
            self.status.setText(f"Shortwave source action failed: {message}")
            self.preview_text.appendPlainText(f"\nAction failed: {message}")

    def _cancel_workers(self) -> None:
        self._pending_task = None
        if self._task_worker is not None:
            self._task_worker.cancel()

    def _task_finished(self) -> None:
        self._task_thread = None
        self._task_worker = None
        pending = self._pending_task
        self._pending_task = None
        if pending is not None and self._active:
            _generation, operation, done = pending
            self._run(operation, done)

    def shutdown(self) -> None:
        self._active = False
        self._cancel_workers()
        _shutdown_task_lane(self)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.shutdown()
        super().closeEvent(event)


class ShortwaveWorkspace(QWidget):
    """Lazy Shortwave workspace: browse/source administration/manual listening."""

    def __init__(self, parent: QWidget | None = None, *, db_path: str | Path | None = None, receiver_profiles_provider: Callable[[], object] | None = None) -> None:
        super().__init__(parent)
        self._db_path = Path(db_path) if db_path is not None else net_resources_db_path()
        self._receiver_profiles_provider = receiver_profiles_provider
        self._active = False
        self._pages: dict[int, QWidget] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        title_row = QHBoxLayout()
        self.title_label = QLabel("Shortwave")
        self.title_label.setAccessibleName("Shortwave Resources workspace")
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        self.help_btn = QPushButton("Help", self)
        self.help_btn.setAccessibleName("Open Shortwave help")
        self.help_btn.setToolTip("Open Shortwave listing and source-data help.")
        self.help_btn.clicked.connect(self._open_context_help)
        title_row.addWidget(self.help_btn)
        layout.addLayout(title_row)
        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("shortwaveWorkspaceTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setAccessibleName("Shortwave workspace tabs")
        for label in ("Explore", "Listening", "Data Sources"):
            placeholder = QLabel("Opening bounded Shortwave workspace…", self.tabs)
            placeholder.setWordWrap(True)
            self.tabs.addTab(placeholder, label)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)
        self._ensure_page(0)
        self.apply_theme()

    def apply_theme(self) -> None:
        theme = active_app_theme()
        self.title_label.setStyleSheet(label_style("text", theme, weight=700))
        self.help_btn.setStyleSheet(button_style("muted", theme))
        self.help_btn.setMinimumHeight(button_height_for_font(self.help_btn))
        for page in self._pages.values():
            apply_theme = getattr(page, "apply_theme", None)
            if callable(apply_theme):
                apply_theme()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.apply_theme()

    def _open_context_help(self) -> None:
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_context_help"):
            try:
                host.open_context_help("tab.shortwave")
            except Exception:
                pass

    def _ensure_page(self, index: int) -> None:
        if index in self._pages or index not in (0, 1, 2):
            return
        if index == 0:
            page: QWidget = ShortwaveExploreView(self._db_path, self.tabs)
        elif index == 1:
            page = ShortwaveListeningView(self._db_path, self.tabs, receiver_profiles_provider=self._receiver_profiles_provider)
        else:
            page = ShortwaveDataSourcesView(self._db_path, self.tabs)
        old = self.tabs.widget(index)
        self._pages[index] = page
        self.tabs.removeTab(index)
        old.deleteLater()
        self.tabs.insertTab(index, page, ("Explore", "Listening", "Data Sources")[index])
        self.tabs.setCurrentIndex(index)
        if self._active and hasattr(page, "set_active"):
            page.set_active(True)

    def _on_tab_changed(self, index: int) -> None:
        self._ensure_page(index)
        if self._active:
            for page_index, page in self._pages.items():
                setter = getattr(page, "set_active", None)
                if callable(setter):
                    setter(page_index == index)

    def set_tab_active(self, active: bool) -> None:
        active = bool(active)
        if self._active == active:
            return
        self._active = active
        for index, page in self._pages.items():
            if hasattr(page, "set_active"):
                page.set_active(self._active and index == self.tabs.currentIndex())

    def on_tab_activated(self) -> None:
        self.set_tab_active(True)

    def add_listing_to_listening(self, item: ShortwaveListingView) -> None:
        """Open the lazy manual reminder editor for one accepted source listing."""
        self._ensure_page(1)
        self.tabs.setCurrentIndex(1)
        page = self._pages.get(1)
        if isinstance(page, ShortwaveListeningView):
            page.set_active(True)
            page.add_listing(item)

    def focus_reminder(self, reminder_key: str) -> None:
        self._ensure_page(1)
        self.tabs.setCurrentIndex(1)
        page = self._pages.get(1)
        if isinstance(page, ShortwaveListeningView):
            page.set_active(True)
            page.focus_reminder(str(reminder_key))

    def on_shortwave_source_changed(self) -> None:
        page = self._pages.get(1)
        if isinstance(page, ShortwaveListeningView) and self._active and self.tabs.currentIndex() == 1:
            page.refresh()

    def shutdown(self) -> None:
        self.set_tab_active(False)
        for page in self._pages.values():
            close = getattr(page, "shutdown", None)
            if callable(close):
                close()


def _frequency_text(hz: int) -> str:
    return f"{hz / 1_000_000:.3f} MHz ({hz / 1_000:.1f} kHz)"


def _window_text(
    start: int | None,
    end: int | None,
    crosses_midnight: bool,
    *,
    reference_utc: datetime | None = None,
) -> str:
    if start is None or end is None:
        return "Special / not evaluated"
    suffix = " (+1 day)" if crosses_midnight or end == 1440 else ""
    utc_text = f"{start // 60:02d}{start % 60:02d}–{(end % 1440) // 60:02d}{(end % 1440) % 60:02d} UTC{suffix}"
    service_date = (reference_utc or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    utc_start = datetime.combine(service_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(minutes=start)
    utc_end = datetime.combine(service_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(minutes=end)
    if crosses_midnight and utc_end <= utc_start:
        utc_end += timedelta(days=1)
    local_start = utc_start.astimezone()
    local_end = utc_end.astimezone()
    local_zone = local_start.tzname() or "local"
    local_text = f"{local_start:%H%M}–{local_end:%H%M} {local_zone}"
    return f"{utc_text} · {local_text}"


def _age_text(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
        return f"{max(0, delta.days)} day{'s' if max(0, delta.days) != 1 else ''} ago"
    except (TypeError, ValueError):
        return str(value or "at an unknown time")


def _source_health_label(state: str) -> str:
    return {
        "current": "Current source listing",
        "changed": "Source changed — review",
        "missing": "Source no longer lists it",
        "kept": "Keeping accepted snapshot",
    }.get(str(state), "Source status needs review")


__all__ = [
    "ShortwaveDataSourcesView", "ShortwaveExploreView", "ShortwaveListeningTableModel",
    "ShortwaveListeningView", "ShortwaveListingTableModel", "ShortwaveWorkspace",
]
