"""Offline FCC Call Finder workspace.

The widget deliberately owns presentation only.  The controller owns archive
parsing, SQLite snapshots, and all background work; result objects published by
the controller are immutable value snapshots.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from freqinout.core.zip_grid import ESTIMATED_GRID_DESCRIPTION

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QBoxLayout,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from freqinout.gui.theme import (
    active_app_theme,
    apply_text_size_accessibility_guards,
    button_height_for_font,
    button_style,
    contrast_text_for_background,
    control_height_for_font,
    horizontal_layout_breakpoint,
    label_style,
)


_CALLSIGN_RE = re.compile(r"^[A-Za-z0-9]+$")
_STATES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA",
    "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX",
    "UT", "VT", "VA", "WA", "WV", "WI", "WY", "AS", "GU", "MP", "PR",
    "VI", "UM", "AA", "AE", "AP",
)
_CANADA_REGIONS = ("AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT")
_POSTAL_GRID_DESCRIPTION = "Grid estimated from the mailing postal code. May differ from self-reported location."
_GRID_RE = re.compile(r"[A-R]{2}[0-9]{2}(?:[A-X]{2})?\Z", re.ASCII)


def _plain_label(text: str = "", parent: QWidget | None = None) -> QLabel:
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _value(obj: object, name: str, default: str = "") -> str:
    value = getattr(obj, name, default)
    if value is None:
        return ""
    return str(value)


def _local_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%b %d, %Y %H:%M %Z")
    except ValueError:
        return value or "Unknown"


def _source_table_style(theme: dict[str, str]) -> str:
    """Keep the selected source readable even after focus moves to an action."""

    active_text = contrast_text_for_background(theme["accent"], theme)
    inactive_text = contrast_text_for_background(theme["accent_active"], theme)
    selector = "QTableWidget#callFinderSourceTable"
    return (
        f"{selector} {{ background-color: {theme['surface']}; color: {theme['text']};"
        f" border: 1px solid {theme['border']}; }}"
        f" {selector}::item:selected {{ background-color: {theme['accent']}; color: {active_text};"
        f" border: 1px solid {theme['accent_active']}; }}"
        f" {selector}::item:selected:!active {{ background-color: {theme['accent_active']}; color: {inactive_text};"
        f" border: 1px solid {theme['accent']}; }}"
    )


class CallFinderTab(QWidget):
    """Bounded, offline callsign lookup and source maintenance surface."""

    selected_license = Signal(object)
    add_operator_requested = Signal(object)

    _COLUMNS = ("Amateur", "Class / Qualification", "GMRS", "Licensee", "Region", "Est. Grid", "Type", "Country")

    def __init__(self, parent: QWidget | None = None, store: object | None = None, *, picker: bool = False) -> None:
        super().__init__(parent)
        from freqinout.gui.call_finder_controller import CallFinderController

        self._picker = bool(picker)
        self._active = False
        self._selected: object | None = None
        self._source_paths: dict[str, Path] = {}
        self._source_status: dict[str, object] = {}
        self._grid_status: object | None = None
        self._canada_grid_status: object | None = None
        self._last_error = ""
        self._refresh_feedback = ""
        self._source_dialog: QDialog | None = None
        self._theme_applying = False
        self._controller = CallFinderController(parent=self, store=store)
        self.controller = self._controller
        self._build_ui()
        self._connect_controller()
        self.apply_theme()
        self._show_setup("Enter at least 3 letters or numbers to search the offline callsign references.")

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self.header_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.title_label = _plain_label("Call Finder", self)
        self.header_row.addWidget(self.title_label)
        self.header_row.addStretch(1)
        self.source_toggle = QPushButton("Sources…", self)
        self.source_toggle.setAccessibleName("Manage offline reference sources")
        self.source_toggle.clicked.connect(self._open_sources_dialog)
        self.header_row.addWidget(self.source_toggle)
        root.addLayout(self.header_row)
        self.why_label = _plain_label(
            "Search loaded offline callsign references. License records may include an estimated grid from a mailing postal code.",
            self,
        )
        root.addWidget(self.why_label)

        self.filters_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.filters_row.setSpacing(6)
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Callsign (3+ characters)")
        self.search_edit.setAccessibleName("Offline callsign search")
        self.search_edit.textChanged.connect(self._schedule_search)
        self.filters_row.addWidget(self.search_edit, 1)
        self.service_combo = QComboBox(self)
        self.service_combo.setAccessibleName("FCC license service filter")
        self.service_combo.addItem("Service: All", "")
        self.service_combo.addItem("Amateur", "Amateur")
        self.service_combo.addItem("GMRS", "GMRS")
        self.service_combo.currentIndexChanged.connect(self._schedule_search)
        # Keep the internal filter contract for existing callers; ordinary
        # lookup compares both services in separate columns.
        self.service_combo.hide()
        self.country_filter_widget = QWidget(self)
        country_filter_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self.country_filter_widget)
        country_filter_layout.setContentsMargins(0, 0, 0, 0)
        country_filter_layout.setSpacing(6)
        self.country_filter_label = _plain_label("Country:", self.country_filter_widget)
        self.country_combo = QComboBox(self.country_filter_widget)
        self.country_combo.setAccessibleName("Licensing country filter")
        self.country_combo.setToolTip("Filter by licensing jurisdiction.")
        self.country_filter_label.setBuddy(self.country_combo)
        self.country_combo.addItem("All", "")
        self.country_combo.addItem("US", "US")
        self.country_combo.addItem("CA", "CA")
        country_filter_layout.addWidget(self.country_filter_label)
        country_filter_layout.addWidget(self.country_combo, 1)
        self.filters_row.addWidget(self.country_filter_widget)
        self.state_filter_widget = QWidget(self)
        state_filter_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self.state_filter_widget)
        state_filter_layout.setContentsMargins(0, 0, 0, 0)
        state_filter_layout.setSpacing(6)
        self.state_filter_label = _plain_label("Region:", self.state_filter_widget)
        self.state_combo = QComboBox(self.state_filter_widget)
        self.state_combo.setAccessibleName("Licensing region filter")
        self.state_combo.setToolTip("Filter by the mailing address region in the selected licensing country.")
        self.state_filter_label.setBuddy(self.state_combo)
        state_filter_layout.addWidget(self.state_filter_label)
        state_filter_layout.addWidget(self.state_combo, 1)
        self._populate_regions()
        self.country_combo.currentIndexChanged.connect(self._country_changed)
        self.state_combo.currentIndexChanged.connect(self._schedule_search)
        self.filters_row.addWidget(self.state_filter_widget)
        root.addLayout(self.filters_row)

        self.status_label = _plain_label("", self)
        self.status_label.setAccessibleName("Call Finder status")
        root.addWidget(self.status_label)

        self.table = QTableWidget(0, len(self._COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(self._COLUMNS))
        self.table.horizontalHeaderItem(5).setToolTip(_POSTAL_GRID_DESCRIPTION)
        self.table.setAccessibleName("Callsign search results")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in range(len(self._COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch if column == 3
                                        else QHeaderView.ResizeMode.ResizeToContents)
        self.table.currentCellChanged.connect(self._selection_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.table, 1)

        self.details_frame = QFrame(self)
        self.details_frame.setFrameShape(QFrame.Shape.StyledPanel)
        details_layout = QVBoxLayout(self.details_frame)
        details_layout.setContentsMargins(8, 8, 8, 8)
        details_layout.setSpacing(6)
        self.details_heading = _plain_label("Select a result to review its license details.", self.details_frame)
        details_layout.addWidget(self.details_heading)
        self.details_body = _plain_label("", self.details_frame)
        details_layout.addWidget(self.details_body)

        self.actions_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.details_btn = QPushButton("Details", self.details_frame)
        self.details_btn.setCheckable(True)
        self.details_btn.toggled.connect(self.details_body.setVisible)
        self.details_body.hide()
        self.copy_btn = QPushButton("Copy Callsign", self.details_frame)
        self.related_btn = QPushButton("Related licenses", self.details_frame)
        self.add_btn = QPushButton("Add to Operators…", self.details_frame)
        self.select_btn = QPushButton("Use selected callsign", self.details_frame)
        self.select_btn.setVisible(self._picker)
        self.add_btn.setVisible(not self._picker)
        self.copy_btn.clicked.connect(self._copy_selected)
        self.related_btn.clicked.connect(self._load_related)
        self.add_btn.clicked.connect(self._add_selected)
        self.select_btn.clicked.connect(self._select_for_picker)
        self.actions_row.addWidget(self.copy_btn)
        self.actions_row.addWidget(self.details_btn)
        self.actions_row.addWidget(self.related_btn)
        if self._picker:
            self.actions_row.addWidget(self.select_btn)
        else:
            self.actions_row.addWidget(self.add_btn)
        self.actions_row.addStretch(1)
        details_layout.addLayout(self.actions_row)

        self.related_heading = _plain_label("", self.details_frame)
        details_layout.addWidget(self.related_heading)
        self.details_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        root.addWidget(self.details_frame)

        self.source_panel = QWidget()
        source_root = QVBoxLayout(self.source_panel)
        source_root.setContentsMargins(8, 8, 8, 8)
        source_root.setSpacing(6)
        self.source_heading = _plain_label("Offline references", self.source_panel)
        source_root.addWidget(self.source_heading)
        self.source_table = QTableWidget(5, 5, self.source_panel)
        self.source_table.setHorizontalHeaderLabels(
            ("Country", "Reference", "Status", "Records", "Last refreshed")
        )
        self.source_table.setObjectName("callFinderSourceTable")
        self.source_table.setAccessibleName("Offline reference sources")
        self.source_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.source_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.source_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.source_table.setWordWrap(False)
        self.source_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.source_table.verticalHeader().setVisible(False)
        self.source_table.horizontalHeader().setStretchLastSection(True)
        for column in range(4):
            self.source_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.source_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.source_table.currentCellChanged.connect(self._source_row_selected)
        self.source_table.setMinimumHeight(0)
        source_root.addWidget(self.source_table)
        self._selected_source_service = "Amateur"
        self._source_services = ("Amateur", "GMRS", "ZIP grids", "Canada", "Canada postal grids")
        self._source_row_for_service = {service: index for index, service in enumerate(self._source_services)}
        self.source_feedback_label = _plain_label("", self.source_panel)
        self._source_rows: dict[str, tuple[QBoxLayout, QLabel, QLabel, QPushButton, QPushButton, QPushButton, QPushButton]] = {}
        for service in self._source_services:
            # Compatibility controls retain the historical tuple shape for
            # callers, while the dialog has exactly one visible action owner.
            source_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
            source_label = _plain_label("No source loaded.", self.source_panel)
            source_path_label = _plain_label("No local source selected.", self.source_panel)
            download_btn = QPushButton("Update from internet", self.source_panel)
            archive_btn = QPushButton("From file…", self.source_panel)
            folder_btn = QPushButton("From folder…", self.source_panel)
            refresh_btn = QPushButton("Import selected source", self.source_panel)
            download_btn.clicked.connect(lambda _checked=False, s=service: self._download_refresh(s))
            archive_btn.clicked.connect(lambda _checked=False, s=service: self._choose_archive(s))
            folder_btn.clicked.connect(lambda _checked=False, s=service: self._choose_folder(s))
            refresh_btn.clicked.connect(lambda _checked=False, s=service: self._refresh_source(s))
            for widget in (source_label, source_path_label, download_btn, archive_btn, folder_btn, refresh_btn):
                widget.hide()
            self._source_rows[service] = (source_row, source_label, source_path_label, download_btn, archive_btn, folder_btn, refresh_btn)
        self.selected_source_label = _plain_label("Selected: US · Amateur licenses", self.source_panel)
        source_root.addWidget(self.selected_source_label)
        self.source_actions_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.source_update_btn = QPushButton("Update from internet", self.source_panel)
        self.source_update_all_btn = QPushButton("Update all", self.source_panel)
        self.source_update_all_btn.setToolTip("Download and import every dataset one at a time. Stops on failure or cancellation.")
        self.source_update_all_btn.clicked.connect(self._update_all_sources)
        self.source_import_btn = QPushButton("Import…", self.source_panel)
        self.source_import_selected_btn = QPushButton("Import selected source", self.source_panel)
        self.source_details_btn = QPushButton("Details", self.source_panel)
        self.source_details_btn.setCheckable(True)
        self.source_details_btn.toggled.connect(lambda visible: self.source_details_body.setVisible(visible))
        self.source_update_btn.clicked.connect(self._update_selected_source)
        self.source_import_btn.clicked.connect(self._show_import_menu)
        self.source_import_selected_btn.clicked.connect(self._import_selected_source)
        for button in (self.source_update_btn, self.source_update_all_btn, self.source_import_btn, self.source_import_selected_btn, self.source_details_btn):
            self.source_actions_row.addWidget(button)
        self.source_actions_row.addStretch(1)
        source_root.addLayout(self.source_actions_row)
        self.source_details_body = _plain_label("", self.source_panel)
        self.source_details_body.hide()
        source_root.addWidget(self.source_details_body)
        self._canada_accept_smaller_btn = QPushButton("Review smaller Canada snapshot…", self.source_panel)
        self._canada_accept_smaller_btn.setToolTip(
            "Explicitly retry one Canada refresh after reviewing a count-drop warning. "
            "It never relaxes source format or integrity checks."
        )
        self._canada_accept_smaller_btn.clicked.connect(self._accept_smaller_canada_snapshot)
        self._canada_accept_smaller_btn.hide()
        source_root.addWidget(self._canada_accept_smaller_btn)
        self.cancel_refresh_btn = QPushButton("Cancel refresh", self.source_panel)
        self.cancel_refresh_btn.clicked.connect(self._cancel_refresh)
        source_root.addWidget(self.source_feedback_label)
        source_root.addWidget(self.cancel_refresh_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self._refresh_source_manager()

        self._set_detail_actions_enabled(False)
        self._set_sources_button_state()

    def _connect_controller(self) -> None:
        for name, slot in (
            ("result_ready", self._result_ready),
            ("error_ready", self._error_ready),
            ("refresh_progress", self._refresh_progress),
            ("refresh_source_started", self._refresh_source_started),
            ("refresh_finished", self._refresh_finished),
        ):
            signal = getattr(self._controller, name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(slot)

    @property
    def refreshing(self) -> bool:
        return bool(getattr(self._controller, "refreshing", False))

    def set_tab_active(self, active: bool) -> None:
        active = bool(active)
        if self._active == active:
            return
        self._active = active
        setter = getattr(self._controller, "set_active", None)
        if callable(setter):
            setter(active)
        if active:
            self._set_sources_button_state()
            loader = getattr(self._controller, "load_status", None)
            if callable(loader):
                loader()
            self._request_current_search()

    def on_tab_activated(self) -> None:
        self.set_tab_active(True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # A lazily-created workspace can be shown after the application theme
        # changed; this is presentation-only and preserves the existing model.
        self.apply_theme()
        self.set_tab_active(True)

    def hideEvent(self, event) -> None:
        self.set_tab_active(False)
        if self._source_dialog is not None:
            self._source_dialog.hide()
        super().hideEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange):
            self.apply_theme()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def apply_theme(self) -> None:
        if self._theme_applying:
            return
        self._theme_applying = True
        theme = active_app_theme()
        try:
            self.title_label.setStyleSheet(label_style("text", theme, weight=700))
            self.why_label.setStyleSheet(label_style("muted", theme))
            self.country_filter_label.setStyleSheet(label_style("muted", theme))
            self.state_filter_label.setStyleSheet(label_style("muted", theme))
            self.status_label.setStyleSheet(label_style("muted", theme))
            self.source_feedback_label.setStyleSheet(label_style("muted", theme))
            self.source_heading.setStyleSheet(label_style("text", theme, weight=700))
            self.source_table.setStyleSheet(_source_table_style(theme))
            self.selected_source_label.setStyleSheet(label_style("text", theme, weight=600))
            self.source_details_body.setStyleSheet(label_style("muted", theme))
            self.details_heading.setStyleSheet(label_style("text", theme, weight=700))
            self.details_body.setStyleSheet(label_style("text", theme))
            self.related_heading.setStyleSheet(label_style("info", theme, weight=600))
            for button, role in (
                (self.copy_btn, "muted"),
                (self.details_btn, "muted"),
                (self.related_btn, "eligible_info"),
                (self.add_btn, "eligible_success"),
                (self.select_btn, "eligible_success"),
                (self.source_toggle, "muted"),
                (self.source_update_btn, "eligible_info"),
                (self.source_update_all_btn, "eligible_info"),
                (self.source_import_btn, "muted"),
                (self.source_import_selected_btn, "eligible_info"),
                (self.source_details_btn, "muted"),
                (self.cancel_refresh_btn, "eligible_warning"),
            ):
                button.setStyleSheet(button_style(role, theme))
            for service in ("Amateur", "GMRS", "Canada", "ZIP grids", "Canada postal grids"):
                _, _, _, download_btn, archive_btn, folder_btn, refresh_btn = self._source_rows[service]
                download_btn.setStyleSheet(button_style("eligible_info", theme))
                for button in (archive_btn, folder_btn):
                    button.setStyleSheet(button_style("muted", theme))
                refresh_btn.setStyleSheet(button_style("eligible_info", theme))
            self._canada_accept_smaller_btn.setStyleSheet(button_style("eligible_warning", theme))
            for button in self._all_action_buttons():
                button.setMinimumHeight(button_height_for_font(button))
            for widget in (self.search_edit, self.service_combo, self.country_combo, self.state_combo):
                widget.setMinimumHeight(control_height_for_font(widget))
            self.source_toggle.setMinimumHeight(button_height_for_font(self.source_toggle))
            self.table.verticalHeader().setDefaultSectionSize(control_height_for_font(self.table, vertical_padding=10, floor=1))
            source_row_height = max(
                control_height_for_font(self.source_table, vertical_padding=6, floor=1),
                self.source_table.fontMetrics().lineSpacing() + 12,
            )
            self.source_table.verticalHeader().setDefaultSectionSize(source_row_height)
            source_table_height = (
                self.source_table.horizontalHeader().sizeHint().height()
                + source_row_height * len(self._source_services)
                + self.source_table.frameWidth() * 2
            )
            self.source_table.setMinimumHeight(source_table_height)
            self.source_table.setMaximumHeight(source_table_height)
            apply_text_size_accessibility_guards(self, include_widths=False)
            apply_text_size_accessibility_guards(self.source_panel, include_widths=False)
            close_btn = getattr(self, "_source_dialog_close_btn", None)
            if close_btn is not None:
                close_btn.setStyleSheet(button_style("muted", theme))
                close_btn.setMinimumHeight(button_height_for_font(close_btn))
            self._apply_responsive_layout()
        finally:
            self._theme_applying = False

    def _all_action_buttons(self) -> list[QPushButton]:
        buttons = [self.copy_btn, self.details_btn, self.related_btn, self.add_btn, self.select_btn,
                   self.source_update_btn, self.source_update_all_btn, self.source_import_btn, self.source_import_selected_btn,
                   self.source_details_btn, self.cancel_refresh_btn, self._canada_accept_smaller_btn]
        for _, _, _, download_btn, archive_btn, folder_btn, refresh_btn in self._source_rows.values():
            buttons.extend((download_btn, archive_btn, folder_btn, refresh_btn))
        return buttons

    def _apply_responsive_layout(self) -> None:
        compact = self.width() > 0 and self.width() < max(
            horizontal_layout_breakpoint(self.filters_row, reserve_controls=1),
            horizontal_layout_breakpoint(self.actions_row, reserve_controls=1),
        )
        direction = QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
        self.filters_row.setDirection(direction)
        self.actions_row.setDirection(direction)
        source_compact = self._source_dialog is not None and self._source_dialog.width() < max(
            500, horizontal_layout_breakpoint(self.source_actions_row, reserve_controls=1) + 40
        )
        self.source_actions_row.setDirection(
            QBoxLayout.Direction.TopToBottom if source_compact else QBoxLayout.Direction.LeftToRight
        )

    def _populate_regions(self) -> None:
        """Keep the compact region filter scoped to its licensing jurisdiction."""
        country = self.country_combo.currentData() if hasattr(self, "country_combo") else ""
        if country == "US":
            label, regions = "State:", tuple((region, region) for region in _STATES)
        elif country == "CA":
            label, regions = "Province / territory:", tuple((region, region) for region in _CANADA_REGIONS)
        else:
            label = "Region:"
            regions = tuple((f"US · {region}", ("US", region)) for region in _STATES) + tuple(
                (f"CA · {region}", ("CA", region)) for region in _CANADA_REGIONS
            )
        selected = self.state_combo.currentData() if hasattr(self, "state_combo") else ""
        self.state_filter_label.setText(label)
        self.state_combo.blockSignals(True)
        self.state_combo.clear()
        self.state_combo.addItem("All", "")
        for text, value in regions:
            self.state_combo.addItem(text, value)
        index = self.state_combo.findData(selected)
        self.state_combo.setCurrentIndex(index if index >= 0 else 0)
        self.state_combo.blockSignals(False)

    def _country_changed(self, *_args) -> None:
        self._populate_regions()
        self._schedule_search()

    def _schedule_search(self, *_args) -> None:
        prefix = self.search_edit.text().strip()
        self._last_error = ""
        self._refresh_feedback = ""
        self._request_search(prefix)
        if len(prefix) < 3 or not _CALLSIGN_RE.fullmatch(prefix):
            self._show_setup("Enter at least 3 ASCII letters or numbers to search the offline callsign references.")
            return
        self.status_label.setText("Searching offline cache…")

    def _request_search(self, prefix: str | None = None) -> None:
        prefix = self.search_edit.text().strip() if prefix is None else str(prefix).strip()
        country = self.country_combo.currentData() or ""
        state = self.state_combo.currentData() or ""
        if isinstance(state, tuple) and len(state) == 2:
            country, state = state
        try:
            self._controller.search(
                prefix,
                service=self.service_combo.currentData() or "",
                state=state,
                country=country,
            )
        except Exception as exc:
            self._error_ready(str(exc))

    def _request_current_search(self) -> None:
        prefix = self.search_edit.text().strip()
        if len(prefix) >= 3 and _CALLSIGN_RE.fullmatch(prefix):
            self._request_search(prefix)
        else:
            self._show_setup("Enter at least 3 letters or numbers to search the offline callsign references.")

    def _show_setup(self, message: str) -> None:
        self._selected = None
        self.table.clearSpans()
        self.table.clearContents()
        self.table.setRowCount(0)
        self._clear_details()
        self.status_label.setText(message)

    def _result_ready(self, result: object) -> None:
        kind = _value(result, "kind")
        self._update_sources(
            getattr(result, "sources", ()) or (),
            complete=bool(getattr(result, "sources_complete", True)),
        )
        # Grid references publish separately from callsign snapshots.  This
        # avoids reopening either database during table painting or selection.
        grid_status = getattr(result,"grid_status",None)
        if grid_status is not None or bool(getattr(result,"sources_complete",True)):
            self._update_grid_status(grid_status)
        canada_grid_status = getattr(result, "canada_grid_status", None)
        if canada_grid_status is not None or bool(getattr(result, "sources_complete", True)):
            self._update_canada_grid_status(canada_grid_status)
        errors = tuple(getattr(result, "errors", ()) or ())
        self._last_error = "; ".join(str(item) for item in errors)
        if kind == "status":
            if errors:
                self.status_label.setText("Error: " + self._last_error)
            if not errors and not self._refresh_feedback:
                self.status_label.setText(self._source_summary())
            return
        if kind == "related":
            self._render_related(result)
            return
        prefix = self.search_edit.text().strip()
        if len(prefix) < 3 or not _CALLSIGN_RE.fullmatch(prefix):
            self._show_setup("Enter at least 3 ASCII letters or numbers to search the offline callsign references.")
            return
        self._render_search(result)

    def _render_search(self, result: object) -> None:
        rows = tuple(getattr(result, "rows", ()) or ())
        links = tuple(getattr(result, "links", ()) or ())
        linked = {self._record_key(record): pair
                  for pair in links for record in pair}
        groups, seen = [], set()
        for record in rows:
            key = self._record_key(record)
            if key in seen:
                continue
            group = linked.get(key, (record,))
            seen.update(self._record_key(item) for item in group)
            groups.append((record, group))
        has_more = bool(getattr(result, "has_more", False))
        selected_key = self._record_key(self._selected) if self._selected is not None else None
        scroll = self.table.verticalScrollBar().value()
        self.table.blockSignals(True)
        self._selected = None
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.clearSpans()
        self.table.clearContents()
        count = len(groups) + (1 if has_more else 0)
        self.table.setRowCount(count)
        restore = None
        for row_index, (record, group) in enumerate(groups):
            amateur = next((item for item in group if _value(item, "service") == "Amateur"), None)
            gmrs = next((item for item in group if _value(item, "service") == "GMRS"), None)
            values = self._record_values(record, amateur, gmrs)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                target = amateur if column in (0, 1) else gmrs if column == 2 else record
                item.setData(Qt.ItemDataRole.UserRole, target or record)
                item.setToolTip(value)
                if column in (0, 2) and target is not None:
                    font = item.font()
                    font.setUnderline(True)
                    item.setFont(font)
                    item.setToolTip(f"Select {_value(target, 'service')} callsign {_value(target, 'callsign')}")
                elif column == 5:
                    item.setToolTip(self._grid_cell_tooltip(record, group))
                self.table.setItem(row_index, column, item)
            for candidate in group:
                if self._record_key(candidate) == selected_key:
                    restore = (row_index, 0 if _value(candidate, "service") == "Amateur" else 2)
        if has_more:
            sentinel = QTableWidgetItem("More matches available — refine callsign or choose a region.")
            sentinel.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(count - 1, 0, sentinel)
            self.table.setSpan(count - 1, 0, 1, len(self._COLUMNS))
        self.table.resizeRowsToContents()
        self.table.blockSignals(False)
        prefix = f"Error: {self._last_error} " if self._last_error else ""
        if rows:
            self.status_label.setText(prefix +
                f"Showing {len(groups)} result{'s' if len(groups) != 1 else ''}." +
                (" More matches are available; refine callsign or choose a region." if has_more else "")
            )
        else:
            self.status_label.setText(prefix + "No matching active callsign licenses in the offline references.")
        self._clear_details()
        if restore is not None:
            self.table.setCurrentCell(*restore)
            self._selection_changed()
            self.table.verticalScrollBar().setValue(scroll)

    def _record_values(self, record: object, amateur=None, gmrs=None) -> tuple[str, ...]:
        return (
            _value(amateur, "callsign") or "—",
            self._class_or_qualification(amateur),
            _value(gmrs, "callsign") or "—",
            self._record_name(record),
            _value(record, "state") or "Unknown",
            self._grid_cell_value(record, tuple(item for item in (amateur, gmrs) if item is not None)),
            _value(record, "kind") or "Unknown",
            _value(record, "jurisdiction") or ("CA" if _value(record, "provider") == "ised_amateur" else "US"),
        )

    @staticmethod
    def _is_ised(record: object | None) -> bool:
        return _value(record, "provider") == "ised_amateur"

    @staticmethod
    def _record_key(record: object | None) -> tuple[str, str]:
        if record is None:
            return ("", "")
        from freqinout.core.call_finder import reference_key

        try:
            return tuple(str(value) for value in reference_key(record))
        except AttributeError:
            return (_value(record, "provider") or f"fcc_{_value(record, 'service').lower()}",
                    _value(record, "source_key") or _value(record, "fcc_id") or _value(record, "callsign"))

    def _class_or_qualification(self, record: object | None) -> str:
        if record is None or _value(record, "kind") != "Individual":
            return "—"
        if self._is_ised(record):
            return self._ised_qualification_summary(record) or "—"
        return _value(record, "license_class") or "—"

    @staticmethod
    def _ised_qualification_summary(record: object) -> str:
        flags = _value(record, "qualifications").upper()
        return " · ".join(label for flag, label in (("A", "Basic"), ("E", "Honours"), ("D", "Advanced")) if flag in flags)

    def _grid_value_reason(self, record: object) -> tuple[str, str]:
        value = _value(record, "estimated_grid6").strip().upper()
        reason = (_value(record, "grid_reason").strip()
                  or _value(record, "zip_reason").strip()
                  or "No mailing postal-code estimate is available.")
        if value and not _GRID_RE.fullmatch(value):
            return "", "Cached mailing postal-code grid is invalid; refresh the grid reference."
        return value, reason

    @staticmethod
    def _grid_precision(value: str) -> str:
        return "Grid4" if len(value) == 4 else "Grid6"

    def _grid_cell_value(self, primary: object, pair: tuple[object, ...] = ()) -> str:
        records = pair or (primary,)
        values = [self._grid_value_reason(record)[0] for record in records]
        known = [value for value in values if value]
        if len(records) > 1 and len(set(values)) > 1:
            return "Varies"
        if known:
            return known[0]
        return "—"

    def _grid_cell_tooltip(self, primary: object, pair: tuple[object, ...] = ()) -> str:
        records = pair or (primary,)
        if len(records) == 1:
            value, reason = self._grid_value_reason(records[0])
            extra = f"\nReason: {reason}" if reason not in {ESTIMATED_GRID_DESCRIPTION, _POSTAL_GRID_DESCRIPTION} else ""
            label = self._grid_precision(value) if value else "Grid"
            return f"Estimated {label}: {value or 'Unknown'}\n{_POSTAL_GRID_DESCRIPTION}{extra}"
        lines = []
        for record in records:
            value, reason = self._grid_value_reason(record)
            service = _value(record, "service") or "License"
            call = _value(record, "callsign") or "Unknown callsign"
            extra = f"\nReason: {reason}" if reason not in {ESTIMATED_GRID_DESCRIPTION, _POSTAL_GRID_DESCRIPTION} else ""
            label = self._grid_precision(value) if value else "Grid"
            lines.append(f"{service} {call}: {label} {value or 'Unknown'}{extra}")
        return _POSTAL_GRID_DESCRIPTION + "\n" + "\n".join(lines)

    def _record_name(self, record: object) -> str:
        name = _value(record, "name").strip()
        if name:
            return name
        parts = [_value(record, key).strip() for key in ("first_name", "last_name")]
        return " ".join(part for part in parts if part) or "Unknown"

    def _selection_changed(self, *_args) -> None:
        selected = self.table.currentItem()
        record = selected.data(Qt.ItemDataRole.UserRole) if selected is not None and selected.isSelected() else None
        if record is None:
            self._selected = None
            self._clear_details()
            return
        self._selected = record
        self._render_details(record)

    def _render_details(self, record: object) -> None:
        invalidate = getattr(self._controller, "invalidate", None)
        if callable(invalidate):
            invalidate()
        self._selected = record
        self.details_heading.setText(f"{_value(record, 'callsign')} · {_value(record, 'service')} · {self._record_name(record)}")
        service = _value(record, "service")
        kind = _value(record, "kind")
        if self._is_ised(record):
            fields = self._ised_detail_fields(record)
        else:
            license_class = "—" if service == "GMRS" or kind in {"Club", "Military Recreation"} else (_value(record, "license_class") or "Unknown")
            fields = (
                f"Service: {service or 'Unknown'} · Type: {kind or 'Unknown'} · Region: {_value(record, 'state') or 'Unknown'}",
                f"Class: {license_class} · Expiration: {_value(record, 'expires') or 'Unknown'}",
                f"Source: {_value(record, 'source_date') or 'Unknown'} · Refreshed: {_local_time(_value(record, 'refreshed_at'))}",
                f"ZIP: {_value(record, 'mail_zip') or 'Unknown'}",
                self._grid_detail_line(record),
                self._grid_source_details(record),
                "Mailing ZIP/region describe FCC mailing information, not the station's operating location.",
            )
        self.details_body.setText("\n".join(fields))
        if self._is_ised(record):
            self.related_heading.setText("Related licenses: not provided by this source.")
            self.related_heading.show()
        else:
            self.related_heading.setText("")
            self.related_heading.hide()
        self._set_detail_actions_enabled(True)

    def _ised_detail_fields(self, record: object) -> tuple[str, ...]:
        flags = _value(record, "qualifications").upper()
        qualifications = self._ised_qualification_summary(record) or "None published"
        morse = " · ".join(label for flag, label in (("B", "5 WPM Morse"), ("C", "12 WPM Morse")) if flag in flags)
        fields = [
            f"Service: Amateur · Type: {_value(record, 'kind') or 'Unknown'} · Region: {_value(record, 'state') or 'Unknown'} · Country: CA",
            f"Source: {_value(record, 'source_date') or 'Unknown'} · Refreshed: {_local_time(_value(record, 'refreshed_at'))}",
            f"Postal code: {_value(record, 'postal_code') or 'Unknown'}",
            self._grid_detail_line(record),
            self._grid_source_details(record),
            "Mailing postal code/region may differ from the station's operating location.",
            "ISED does not provide a published expiration date in this call sign list.",
        ]
        if _value(record, "kind") == "Club":
            fields.insert(1, "Qualification: not provided for club callsigns by this source.")
        else:
            fields.insert(1, f"Qualification: {qualifications}" + (f" · Morse: {morse}" if morse else ""))
        sponsor = _value(record, "sponsor_name")
        if sponsor:
            fields.append(f"Sponsor: {sponsor}")
        notice = _value(record, "source_notice")
        if notice:
            fields.append(f"Source notice: {notice}")
        return tuple(fields)

    def _grid_detail_line(self, record: object) -> str:
        value, reason = self._grid_value_reason(record)
        label = self._grid_precision(value) if value else "Grid"
        return f"Estimated {label}: {value or 'Unknown'} · {reason}"

    def _grid_source_details(self, record: object) -> str:
        source = _value(record, "grid_source")
        vintage = _value(record, "grid_vintage")
        status = self._canada_grid_status if self._is_ised(record) else self._grid_status
        source = source or _value(status, "source_name") or (
            "GeoNames Canadian postal-code data" if self._is_ised(record) else "Census ZIP representative points"
        )
        if self._is_ised(record):
            retrieved = _local_time(_value(status, "refreshed_at")) if status is not None else (vintage or "Unknown")
            return f"Grid source: {source} · snapshot retrieved {retrieved}"
        vintage = vintage or _value(status, "vintage") or "Unknown"
        return f"Grid source: {source} · vintage {vintage}"

    def _clear_details(self) -> None:
        self.details_heading.setText("Select a result to review its license details.")
        self.details_body.setText("")
        self.related_heading.setText("")
        self.related_heading.hide()
        self._set_detail_actions_enabled(False)

    def _set_detail_actions_enabled(self, enabled: bool) -> None:
        for button in (self.copy_btn, self.details_btn, self.add_btn, self.select_btn):
            button.setEnabled(bool(enabled))
        is_ised = self._is_ised(self._selected)
        self.related_btn.setEnabled(bool(enabled) and not is_ised)
        self.related_btn.setToolTip("Related licenses are not provided by the ISED source." if is_ised else "")

    def _copy_selected(self) -> None:
        if self._selected is not None:
            QApplication.clipboard().setText(_value(self._selected, "callsign"))

    def _load_related(self) -> None:
        if self._selected is None:
            return
        self.related_heading.setText("Loading related licenses…")
        self.related_heading.show()
        self.related_btn.setEnabled(False)
        try:
            self._controller.related(self._selected)
        except Exception as exc:
            self._error_ready(str(exc))

    def _add_selected(self) -> None:
        if self._selected is not None:
            self.add_operator_requested.emit(self._selected)

    def _select_for_picker(self) -> None:
        if self._selected is not None:
            self.selected_license.emit(self._selected)

    def _render_related(self, result: object) -> None:
        from freqinout.core.call_finder import SearchResult

        selected = getattr(result, "anchor", None) or self._selected
        if selected is None:
            return
        rows = tuple(getattr(result, "rows", ()) or ())
        has_more = bool(getattr(result, "has_more", False))
        # Use the newly published version of the selected reference when its
        # compatible pair is present; estimates may have changed on refresh.
        key = self._record_key(selected)
        links = tuple(getattr(result, "links", ()) or ())
        selected = next((row for pair in links for row in pair
                         if self._record_key(row) == key), selected)
        related_fields = {
            "rows": (selected,) + rows,
            "has_more": has_more,
            "links": links,
            "grid_status": getattr(result, "grid_status", None),
        }
        if "canada_grid_status" in getattr(SearchResult, "__dataclass_fields__", {}):
            related_fields["canada_grid_status"] = getattr(result, "canada_grid_status", None)
        related_result = SearchResult(**related_fields)
        self._render_search(related_result)
        related_text = (
            "Related licenses: " + ("none found." if not rows else f"{len(rows)} found.") +
            (" More were omitted." if has_more else "")
        )
        if self._last_error:
            related_text = "Error: " + self._last_error + " " + related_text
        self.related_heading.setText(related_text)
        self.related_heading.show()
        if rows:
            self.related_heading.setText(self.related_heading.text() + " Select a callsign in the results table.")
    def _update_sources(self, statuses, *, complete: bool = True) -> None:
        # Country-filtered requests intentionally publish only participating
        # providers.  Keep the last complete snapshot for the other panels.
        if complete:
            self._source_status = {}
        for status in statuses:
            service = str(getattr(status, "service", ""))
            if service:
                self._source_status[service] = status
        for service, (_, label, path_label, *_buttons) in self._source_rows.items():
            if service in {"ZIP grids", "Canada postal grids"}:
                continue  # Dedicated complete snapshot is rendered below.
            status = self._source_status.get(service)
            if status is None:
                label.setText("ISED Amateur call sign list source is not loaded." if service == "Canada" else "No source loaded.")
                continue
            source = _value(status, "source_name") or "source"
            date = _value(status, "source_date") or _value(status, "vintage") or "unknown source date"
            refreshed = _local_time(_value(status, "refreshed_at"))
            active = _value(status, "active_rows") or "0"
            skipped = _value(status, "skipped_rows") or "0"
            hint = (" · Refresh FCC source to load mailing ZIPs"
                    if service in {"Amateur", "GMRS"} and not bool(getattr(status, "zip_ready", False)) else "")
            notice = "ISED Amateur call sign list · " if service == "Canada" else ""
            qualifier = "assigned callsigns" if service == "Canada" else "active licenses"
            label.setText(f"{notice}{source} · source {date} · refreshed {refreshed} · {active} {qualifier} · {skipped} skipped{hint}")
            if service not in self._source_paths:
                path_label.setText(("Official ISED source: " if service == "Canada" else "Source archive: ") + source)
        if statuses:
            self.status_label.setText(self._source_summary())
        self._refresh_source_manager()

    def _update_grid_status(self, status: object | None) -> None:
        self._grid_status = status
        row = self._source_rows.get("ZIP grids")
        if row is None:
            return
        _, label, path_label, *_buttons = row
        status = self._grid_status
        if status is None:
            label.setText("No Census ZIP-grid source loaded. Refresh required for estimated Grid6.")
            self._refresh_source_manager()
            return
        source = _value(status, "source_name") or "Census"
        vintage = _value(status, "vintage") or "Unknown"
        areas = _value(status, "rows") or _value(status, "area_count") or "0"
        refreshed = _local_time(_value(status, "refreshed_at"))
        label.setText(f"U.S. Census Bureau · {source} · vintage {vintage} · {areas} areas · refreshed {refreshed}")
        if "ZIP grids" not in self._source_paths:
            path_label.setText(f"Source archive: {source}")
        self._refresh_source_manager()

    def _update_canada_grid_status(self, status: object | None) -> None:
        self._canada_grid_status = status
        row = self._source_rows.get("Canada postal grids")
        if row is None:
            return
        _, label, path_label, *_buttons = row
        if status is None:
            label.setText("No Canadian postal-grid source loaded. Refresh required for estimated grids.")
            self._refresh_source_manager()
            return
        source = _value(status, "source_name") or "Canadian postal-code source"
        rows = _value(status, "rows") or "0"
        refreshed = _local_time(_value(status, "refreshed_at"))
        source_url = _value(status, "source_url")
        label.setText(
            f"GeoNames postal-code data · CC BY 4.0 attribution required · {source} · "
            f"{rows} postal codes · snapshot retrieved {refreshed}"
        )
        if "Canada postal grids" not in self._source_paths:
            path_label.setText(f"GeoNames source: {source}" + (f" · {source_url}" if source_url else ""))
        self._refresh_source_manager()

    def _source_summary(self) -> str:
        if not self._source_status:
            return "No callsign source cache is loaded."
        parts = []
        for service in ("Amateur", "GMRS", "Canada"):
            status = self._source_status.get(service)
            if status is not None:
                qualifier = "assigned" if service == "Canada" else "active"
                parts.append(f"{service}: {_value(status, 'active_rows') or '0'} {qualifier}")
        return " · ".join(parts) or "No callsign source cache is loaded."

    @staticmethod
    def _source_identity(service: str) -> tuple[str, str]:
        return {
            "Amateur": ("US", "Amateur licenses"),
            "GMRS": ("US", "GMRS licenses"),
            "ZIP grids": ("US", "Postal grid points"),
            "Canada": ("CA", "Amateur callsigns"),
            "Canada postal grids": ("CA", "Postal grid points"),
        }[service]

    def _source_status_for(self, service: str) -> object | None:
        if service == "ZIP grids":
            return self._grid_status
        if service == "Canada postal grids":
            return self._canada_grid_status
        return self._source_status.get(service)

    @staticmethod
    def _source_count(status: object | None, service: str) -> str:
        if status is None:
            return "—"
        raw = _value(status, "rows") or _value(status, "area_count") or _value(status, "active_rows") or "0"
        try:
            return f"{int(raw):,}"
        except ValueError:
            return raw

    def _source_state(self, service: str) -> str:
        status = self._source_status_for(service)
        if self.refreshing and service == getattr(self, "_active_source_service", ""):
            return "Updating"
        if status is None:
            return "Missing"
        return "Ready"

    def _refresh_source_manager(self) -> None:
        """Refresh only retained widget values; source state remains controller-owned."""
        if not hasattr(self, "source_table"):
            return
        selected = getattr(self, "_selected_source_service", "Amateur")
        for service, row in self._source_row_for_service.items():
            country, reference = self._source_identity(service)
            status = self._source_status_for(service)
            refreshed = _local_time(_value(status, "refreshed_at")) if status is not None else "—"
            values = (country, reference, self._source_state(service), self._source_count(status, service), refreshed)
            for column, value in enumerate(values):
                item = self.source_table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.source_table.setItem(row, column, item)
                item.setText(value)
                item.setData(Qt.ItemDataRole.UserRole, service)
                item.setToolTip(
                    "Licensing jurisdiction; it does not establish the station's operating location."
                    if column == 0 else ""
                )
        row = self._source_row_for_service.get(selected, 0)
        self.source_table.blockSignals(True)
        self.source_table.selectRow(row)
        self.source_table.setCurrentCell(row, 0)
        self.source_table.blockSignals(False)
        self._update_selected_source_presentation()

    def _source_row_selected(self, row: int, _column: int, *_unused: int) -> None:
        service = next((name for name, index in self._source_row_for_service.items() if index == row), "")
        if service:
            self._selected_source_service = service
            self._update_selected_source_presentation()
            self._set_sources_button_state()

    def _source_details_text(self, service: str) -> str:
        country, reference = self._source_identity(service)
        status = self._source_status_for(service)
        lines = [f"{country} · {reference}", f"Status: {self._source_state(service)}"]
        if service == "Canada":
            lines.append("Provider: ISED Amateur call sign list. Assigned callsigns are not a statement of active licensing.")
            lines.append("ISED notice: Amateur call sign list — Innovation, Science and Economic Development Canada.")
            lines.append("Source: https://apc-cap.ic.gc.ca/datafiles/amateur_delim.zip")
        elif service == "Canada postal grids":
            lines.append("Provider: GeoNames Canadian postal-code data · CC BY 4.0 attribution required.")
            lines.append("License: https://creativecommons.org/licenses/by/4.0/")
            lines.append("Attribution: https://www.geonames.org/")
        elif service == "ZIP grids":
            lines.append("Provider: U.S. Census ZIP representative points.")
        else:
            lines.append("Provider: FCC licensing reference.")
        if status is not None:
            for label, field in (
                ("Source", "source_name"), ("Effective date", "source_date"),
                ("Vintage", "vintage"), ("Retrieved", "refreshed_at"),
                ("Hash", "hash"), ("Hash", "checksum"), ("Hash", "sha256"),
                ("Notice", "source_notice"),
                ("URL", "source_url"),
            ):
                value = _value(status, field)
                if value:
                    lines.append(f"{label}: {_local_time(value) if field == 'refreshed_at' else value}")
        path = self._source_paths.get(service)
        if path is not None:
            lines.append(f"Selected local source: {path}")
        return "\n".join(lines)

    def _update_selected_source_presentation(self) -> None:
        service = getattr(self, "_selected_source_service", "Amateur")
        country, reference = self._source_identity(service)
        self.selected_source_label.setText(f"Selected: {country} · {reference}")
        self.source_details_body.setText(self._source_details_text(service))
        self.source_import_selected_btn.setVisible(service in self._source_paths)

    def _update_selected_source(self) -> None:
        self._download_refresh(getattr(self, "_selected_source_service", "Amateur"))

    def _update_all_sources(self) -> None:
        if self.refreshing:
            return
        self._controller.download_refresh_all(self._source_services)
        self._set_sources_button_state()

    def _refresh_source_started(self, service: str) -> None:
        self._active_source_service = service
        if service == "Canada":
            self._last_canada_source = None
            self._canada_accept_smaller_btn.hide()
        self._refresh_progress(f"Downloading and refreshing {service} source…")
        self._set_sources_button_state()

    def _show_import_menu(self) -> None:
        service = getattr(self, "_selected_source_service", "Amateur")
        menu = QMenu(self.source_import_btn)
        menu.addAction("From file…", lambda: self._choose_archive(service))
        menu.addAction("From folder…", lambda: self._choose_folder(service))
        if service in self._source_paths:
            menu.addSeparator()
            menu.addAction("Reimport last local source", lambda: self._refresh_source(service))
        menu.exec(self.source_import_btn.mapToGlobal(self.source_import_btn.rect().bottomLeft()))

    def _import_selected_source(self) -> None:
        self._refresh_source(getattr(self, "_selected_source_service", "Amateur"))

    def _open_sources_dialog(self) -> None:
        if self._source_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("Call Finder data sources")
            dialog.setModal(False)
            layout = QVBoxLayout(dialog)
            scroll = QScrollArea(dialog)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(self.source_panel)
            layout.addWidget(scroll, 1)
            close_btn = QPushButton("Close", dialog)
            close_btn.clicked.connect(dialog.hide)
            layout.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignRight)
            self._source_dialog = dialog
            self._source_dialog_close_btn = close_btn
            self.apply_theme()
            screen = dialog.screen() or QApplication.primaryScreen()
            available = screen.availableGeometry() if screen is not None else dialog.geometry()
            metrics = dialog.fontMetrics()
            font_width = max(1, metrics.horizontalAdvance("M"))
            line_height = max(1, metrics.lineSpacing())
            natural_width = max(dialog.sizeHint().width(), font_width * 60)
            natural_height = max(dialog.sizeHint().height(), line_height * 18)
            dialog.resize(
                min(natural_width, max(1, int(available.width() * 0.9))),
                min(natural_height, max(1, int(available.height() * 0.8))),
            )
            self._apply_responsive_layout()
        self._set_sources_button_state()
        self._source_dialog.show()
        self._apply_responsive_layout()
        self._source_dialog.raise_()
        self._source_dialog.activateWindow()

    def _choose_archive(self, service: str) -> None:
        title = f"Choose {service} source archive"
        file_filter = ("GeoNames source (CA_full.csv.zip CA_full.txt)" if service == "Canada postal grids" else
                       "ZIP-grid archive (*.zip)" if service == "ZIP grids" else
                       "ISED source (amateur_delim.zip amateur_delim.txt)" if service == "Canada" else
                       "FCC archive (*.zip)")
        path, _ = QFileDialog.getOpenFileName(self, title, "", file_filter)
        if path:
            self._source_selected(service, Path(path))

    def _choose_folder(self, service: str) -> None:
        path = QFileDialog.getExistingDirectory(self, f"Choose {service} source folder")
        if path:
            self._source_selected(service, Path(path))

    def _source_selected(self, service: str, path: Path) -> None:
        self._source_paths[service] = path
        self._selected_source_service = service
        if service == "Canada":
            self._canada_accept_smaller_btn.hide()
        row = self._source_rows.get(service)
        if row is not None:
            row[2].setText(f"Selected local source: {path}")
        self._refresh_source_manager()
        self._set_sources_button_state()

    def _download_refresh(self, service: str) -> None:
        if self.refreshing:
            return
        try:
            self._active_source_service = service
            if service == "Canada":
                self._last_canada_source = None
                self._canada_accept_smaller_btn.hide()
            self._controller.download_refresh(service)
            self.status_label.setText(f"Downloading and refreshing {service} source…")
            self._set_sources_button_state()
        except Exception as exc:
            self._active_source_service = ""
            self._error_ready(str(exc))

    def _refresh_source(self, service: str) -> None:
        path = self._source_paths.get(service)
        if path is None:
            self.status_label.setText(f"Choose a {service} archive or folder before refreshing.")
            return
        try:
            self._active_source_service = service
            if service == "Canada":
                self._last_canada_source = path
                self._canada_accept_smaller_btn.hide()
            self._controller.refresh(service, path)
            self._set_sources_button_state()
            self.status_label.setText(f"Refreshing {service} source…")
        except Exception as exc:
            self._active_source_service = ""
            self._error_ready(str(exc))

    def _accept_smaller_canada_snapshot(self) -> None:
        if self.refreshing:
            return
        answer = QMessageBox.question(
            self,
            "Review smaller Canada snapshot",
            "Use this only after reviewing the Canada count-drop warning. Continue with one refresh that accepts a smaller ISED snapshot? Source format, uniqueness, and integrity checks still apply.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        path = getattr(self,"_last_canada_source",None)
        try:
            if path is None:
                self._controller.download_refresh("Canada", accept_count_drop=True)
                self.status_label.setText("Downloading and reviewing smaller Canada source snapshot…")
            else:
                self._controller.refresh("Canada", path, accept_count_drop=True)
                self.status_label.setText("Refreshing reviewed smaller Canada source snapshot…")
            self._set_sources_button_state()
        except Exception as exc:
            self._error_ready(str(exc))

    def _cancel_refresh(self) -> None:
        cancel = getattr(self._controller, "cancel_refresh", None)
        if callable(cancel):
            cancel()

    def _refresh_progress(self, message: str) -> None:
        self._refresh_feedback = str(message)
        self.source_feedback_label.setText(self._refresh_feedback)
        self.status_label.setText(self._refresh_feedback)
        self._refresh_source_manager()

    def _refresh_finished(self, message: str) -> None:
        if str(message).startswith("Canada"):
            self._canada_accept_smaller_btn.setVisible("ISED count fell" in str(message))
        self._refresh_feedback = str(message)
        self._active_source_service = ""
        self.source_feedback_label.setText(self._refresh_feedback)
        self.status_label.setText(self._refresh_feedback)
        self._set_sources_button_state()
        loader = getattr(self._controller, "load_status", None)
        if callable(loader):
            loader()
        self._request_current_search()

    def _set_sources_button_state(self) -> None:
        busy = self.refreshing
        self.cancel_refresh_btn.setEnabled(busy)
        self.cancel_refresh_btn.setVisible(busy)
        selected = getattr(self, "_selected_source_service", "Amateur")
        self.source_update_btn.setEnabled(not busy)
        self.source_update_all_btn.setEnabled(not busy)
        self.source_import_btn.setEnabled(not busy)
        self.source_import_selected_btn.setEnabled(not busy and selected in self._source_paths)
        for service, (_, _, _, download, archive, folder, refresh) in self._source_rows.items():
            download.setEnabled(not busy)
            archive.setEnabled(not busy)
            folder.setEnabled(not busy)
            refresh.setEnabled(not busy and service in self._source_paths)
        self._canada_accept_smaller_btn.setEnabled(not busy)
        self._refresh_source_manager()

    def _error_ready(self, message: str) -> None:
        self._last_error = str(message)
        self.status_label.setText("Error: " + self._last_error)
        self.related_btn.setEnabled(self._selected is not None)
        self._set_sources_button_state()

    def closeEvent(self, event) -> None:
        self.set_tab_active(False)
        super().closeEvent(event)

    def shutdown(self) -> None:
        self._controller.dispose()

    def __del__(self) -> None:
        try:
            dispose = getattr(self._controller, "dispose", None)
            if callable(dispose):
                dispose()
        except Exception:
            pass
