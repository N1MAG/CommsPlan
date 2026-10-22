"""Passive license-reference presentation for the Map workspace.

The owning Map tab supplies snapshots and performs every read, query, and
review action.  This widget only presents those values and emits intent.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from freqinout.gui.theme import active_app_theme, button_style


class MapLicenseReferencePanel(QWidget):
    """Map-owned license-reference controls with no storage or query behavior."""

    build_requested = Signal()
    area_requested = Signal()
    callsign_requested = Signal(str)
    show_requested = Signal(object)
    add_requested = Signal(object)
    overlay_toggled = Signal(bool)
    cancel_requested = Signal()

    _MAX_ROWS = 200

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(300)
        self.setMaximumWidth(400)
        self._rows: tuple[object, ...] = ()
        self._area_available = False
        self._build_ui()
        self.apply_theme()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.overlay_checkbox = QCheckBox("License references", self)
        self.overlay_checkbox.setChecked(False)
        self.overlay_checkbox.setToolTip("Show or hide reference search results on the map.")
        self.overlay_checkbox.toggled.connect(self.overlay_toggled)
        root.addWidget(self.overlay_checkbox)

        filters = QWidget(self)
        form = QFormLayout(filters)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(6)
        form.setVerticalSpacing(4)
        self.country_combo = QComboBox(filters)
        self.country_combo.addItem("All", "")
        self.country_combo.addItem("US", "US")
        self.country_combo.addItem("CA", "CA")
        self.country_combo.setToolTip(
            "Licensing jurisdiction only. It does not establish a station's current operating location."
        )
        self.service_combo = QComboBox(filters)
        self.service_combo.addItem("All", "")
        self.service_combo.addItem("Amateur", "Amateur")
        self.service_combo.addItem("GMRS", "GMRS")
        self.region_edit = QLineEdit(filters)
        self.region_edit.setPlaceholderText("Optional")
        self.region_edit.setToolTip("Mailing-address region from the license reference; not an operating location.")
        form.addRow("License country", self.country_combo)
        form.addRow("Service", self.service_combo)
        form.addRow("Region", self.region_edit)
        root.addWidget(filters)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        self.callsign_edit = QLineEdit(self)
        self.callsign_edit.setPlaceholderText("Callsign")
        self.callsign_edit.setToolTip("Enter a callsign, then press Enter or Search.")
        self.search_button = QPushButton("Search", self)
        self.callsign_edit.returnPressed.connect(self._request_callsign)
        self.search_button.clicked.connect(self._request_callsign)
        search_row.addWidget(self.callsign_edit, 1)
        search_row.addWidget(self.search_button)
        root.addLayout(search_row)

        actions = QGridLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setHorizontalSpacing(6)
        actions.setVerticalSpacing(4)
        self.build_button = QPushButton("Prepare map references", self)
        self.area_button = QPushButton("Search this area", self)
        self.cancel_button = QPushButton("Cancel", self)
        self.area_button.setEnabled(False)
        self.build_button.clicked.connect(self.build_requested)
        self.area_button.clicked.connect(self.area_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        actions.addWidget(self.build_button, 0, 0, 1, 2)
        actions.addWidget(self.area_button, 1, 0)
        actions.addWidget(self.cancel_button, 1, 1)
        root.addLayout(actions)

        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("License reference status")
        root.addWidget(self.status_label)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(("Callsign", "Name", "Grid"))
        self.table.horizontalHeaderItem(0).setToolTip("Callsign. Service and provider appear in the cell tooltip and selected details.")
        self.table.horizontalHeaderItem(2).setToolTip("Estimated grid from postal/reference data; it is not a current operating location.")
        self.table.setAccessibleName("License reference results")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch if column == 1 else QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.setVisible(False)
        root.addWidget(self.table, 1)

        self.details = QFrame(self)
        self.details.setFrameShape(QFrame.StyledPanel)
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(7, 7, 7, 7)
        details_layout.setSpacing(4)
        self.details_label = QLabel("", self.details)
        self.details_label.setWordWrap(True)
        self.details_label.setTextFormat(Qt.PlainText)
        details_layout.addWidget(self.details_label)
        self.details_body = QLabel("", self.details)
        self.details_body.setWordWrap(True)
        self.details_body.setTextFormat(Qt.PlainText)
        self.details_body.setVisible(False)
        details_layout.addWidget(self.details_body)
        details_actions = QGridLayout()
        details_actions.setContentsMargins(0, 0, 0, 0)
        details_actions.setHorizontalSpacing(6)
        details_actions.setVerticalSpacing(4)
        self.copy_button = QPushButton("Copy callsign", self.details)
        self.show_button = QPushButton("Show on map", self.details)
        self.details_button = QPushButton("Details", self.details)
        self.details_button.setCheckable(True)
        self.add_button = QPushButton("Add to Operators…", self.details)
        self.copy_button.clicked.connect(self._copy_callsign)
        self.show_button.clicked.connect(self._show_selected)
        self.add_button.clicked.connect(self._add_selected)
        self.details_button.toggled.connect(self.details_body.setVisible)
        for button in (self.copy_button, self.show_button, self.details_button, self.add_button):
            button.setEnabled(False)
        details_actions.addWidget(self.copy_button, 0, 0)
        details_actions.addWidget(self.show_button, 0, 1)
        details_actions.addWidget(self.details_button, 1, 0)
        details_actions.addWidget(self.add_button, 2, 0, 1, 2)
        details_layout.addLayout(details_actions)
        self.details.setVisible(False)
        root.addWidget(self.details)

    def filters(self) -> dict[str, str]:
        return {
            "country": str(self.country_combo.currentData() or ""),
            "service": str(self.service_combo.currentData() or ""),
            "state": self.region_edit.text().strip(),
        }

    def set_busy(self, busy: bool) -> None:
        busy = bool(busy)
        self.build_button.setEnabled(not busy)
        self.search_button.setEnabled(not busy)
        self.callsign_edit.setEnabled(not busy)
        self.area_button.setEnabled(self._area_available and not busy)
        self.cancel_button.setEnabled(busy)

    def set_status(self, text: str) -> None:
        self.status_label.setText(str(text or ""))

    def set_area_available(self, available: bool, changed: bool = False) -> None:
        self._area_available = bool(available)
        self.area_button.setEnabled(self._area_available and self.build_button.isEnabled())
        self.area_button.setToolTip(
            "Search the current map viewport." if available else "Set a map viewport before searching this area."
        )
        if changed and available:
            self.set_status("Map area changed. Search this area when ready.")

    def set_results(self, rows: Iterable[object], has_more: bool = False, message: str = "") -> None:
        self._rows = tuple(rows)[: self._MAX_ROWS]
        self.table.setRowCount(0)
        for index, record in enumerate(self._rows):
            self.table.insertRow(index)
            reference_key = self._reference_key(record)
            values = (
                str(getattr(record, "callsign", "") or ""),
                str(getattr(record, "name", "") or ""),
                str(getattr(record, "estimated_grid6", "") or ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, reference_key)
                if column == 0:
                    item.setToolTip(
                        f"{getattr(record, 'service', '') or 'Service unavailable'} · "
                        f"{getattr(record, 'provider', '') or 'Provider unavailable'}"
                    )
                elif column == 2:
                    item.setToolTip("Estimated grid from postal/reference data; not proof of current operating location.")
                self.table.setItem(index, column, item)
        self.table.setVisible(bool(self._rows))
        if self._rows:
            row_height = max(self.table.fontMetrics().lineSpacing() + 6, self.table.verticalHeader().defaultSectionSize())
            header_height = self.table.horizontalHeader().sizeHint().height()
            self.table.setMinimumHeight(header_height + row_height * min(4, len(self._rows)) + 4)
            self.table.setMaximumHeight(header_height + row_height * min(8, len(self._rows)) + 4)
        suffix = " More results are available; refine the reference filters." if has_more else ""
        self.set_status(str(message or f"{len(self._rows)} reference result(s).") + suffix)
        self._selection_changed()

    def apply_theme(self, theme: Mapping[str, str] | None = None) -> None:
        theme = dict(theme or active_app_theme())
        self.overlay_checkbox.setStyleSheet(button_style("muted", theme))
        self.build_button.setStyleSheet(button_style("muted", theme))
        self.area_button.setStyleSheet(button_style("secondary", theme))
        self.cancel_button.setStyleSheet(button_style("muted", theme))
        self.copy_button.setStyleSheet(button_style("muted", theme))
        self.show_button.setStyleSheet(button_style("secondary", theme))
        self.details_button.setStyleSheet(button_style("muted", theme))
        self.add_button.setStyleSheet(button_style("primary", theme))

    def _request_callsign(self) -> None:
        callsign = self.callsign_edit.text().strip().upper()
        if callsign:
            self.callsign_requested.emit(callsign)

    def _selected_record(self) -> object | None:
        row = self.table.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _selection_changed(self) -> None:
        record = self._selected_record()
        if record is None:
            self.details.setVisible(False)
            self.details_label.setText("")
            self.details_body.setText("")
            for button in (self.copy_button, self.show_button, self.details_button, self.add_button):
                button.setEnabled(False)
            return
        callsign = str(getattr(record, "callsign", "") or "")
        grid = str(getattr(record, "estimated_grid6", "") or "")
        self.details.setVisible(True)
        self.details_label.setText(
            f"{callsign} · {getattr(record, 'name', '') or 'Name unavailable'} · "
            f"{getattr(record, 'service', '') or 'Unspecified'} · {grid or 'No qualified grid estimate'}"
        )
        detail_lines = [
            f"{callsign} · {getattr(record, 'name', '') or 'Name unavailable'}",
            f"License reference: {getattr(record, 'jurisdiction', '') or 'US'} · {getattr(record, 'service', '') or 'Unspecified'} · {getattr(record, 'license_class', '') or '—'}",
            f"Postal estimate: {getattr(record, 'state', '') or '—'} {grid or 'No qualified grid estimate'}",
            f"Provider: {getattr(record, 'provider', '') or ('FCC ' + str(getattr(record, 'service', '') or ''))} · Expires: {getattr(record, 'expires', '') or 'Unavailable'} · Source date: {getattr(record, 'source_date', '') or 'Unavailable'} · Refreshed: {getattr(record, 'refreshed_at', '') or 'Unavailable'}",
            f"Grid source: {getattr(record, 'grid_source', '') or 'Unavailable'} · {getattr(record, 'grid_vintage', '') or 'Unknown vintage'}",
            str(getattr(record, 'grid_reason', '') or 'No qualified postal estimate.'),
            str(getattr(record, 'source_notice', '') or ''),
            "Grid estimated from the mailing postal code. May differ from self-reported location.",
            "License reference only; no activity, trust, or check-in is implied.",
        ]
        self.details_body.setText("\n".join(detail_lines))
        self.copy_button.setEnabled(bool(callsign))
        self.show_button.setEnabled(self._qualified_grid(grid))
        self.details_button.setEnabled(True)
        self.add_button.setEnabled(True)

    def _copy_callsign(self) -> None:
        record = self._selected_record()
        if record is not None:
            QApplication.clipboard().setText(str(getattr(record, "callsign", "") or ""))

    def _show_selected(self) -> None:
        record = self._selected_record()
        if record is not None and self._qualified_grid(str(getattr(record, "estimated_grid6", "") or "")):
            self.show_requested.emit(record)

    def _add_selected(self) -> None:
        record = self._selected_record()
        if record is not None:
            self.add_requested.emit(record)

    @staticmethod
    def _reference_key(record: object) -> str:
        import json
        from freqinout.core.call_finder import reference_key
        return json.dumps(reference_key(record),separators=(',',':'))

    @staticmethod
    def _qualified_grid(value: str) -> bool:
        return bool(re.fullmatch(r"[A-R]{2}\d{2}(?:[A-X]{2})?", str(value or "").strip().upper()))


__all__ = ["MapLicenseReferencePanel"]
