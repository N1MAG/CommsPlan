"""Reusable bounded resource picker and presentation helpers.

The picker is deliberately a catalog reader.  It does not create schema,
perform imports, or make runtime/radio decisions.
"""

from __future__ import annotations

from typing import Callable, Iterable, Mapping

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QVBoxLayout,
    QWidget,
    QBoxLayout,
)

from freqinout.core.resource_catalog_models import FrequencyResource, NetDirectorySession
from freqinout.core.frequency_reference import frequency_mhz_text, frequency_programming_text
from freqinout.core.resource_catalog_store import (
    MAX_RESULTS,
    STATION_MANUAL_SOURCE_KEY,
    ResourceCatalogStore,
)
from freqinout.gui.theme import (
    active_app_theme,
    button_height_for_font,
    button_style,
    control_height_for_font,
    label_style,
)



def source_display_labels(
    store: ResourceCatalogStore, source_keys: Iterable[str]
) -> Mapping[str, str]:
    """Resolve a bounded source-key collection without one database read per row."""
    keys = tuple(dict.fromkeys(str(key or "").strip() for key in source_keys if str(key or "").strip()))
    try:
        sources = store.sources_by_keys(keys)
    except Exception:
        sources = {}
    return {
        key: str(getattr(sources.get(key), "label", "") or "").strip()
        or "Catalog source unavailable"
        for key in keys
    }


def source_display_label(store: ResourceCatalogStore, source_key: str) -> str:
    """Return one operator-facing source name, never its opaque storage key."""
    return source_display_labels(store, (source_key,)).get(source_key, "Catalog source unavailable")


def populate_source_combo(
    combo: QComboBox, store: ResourceCatalogStore, selected_key: str | None = None
) -> None:
    """Populate a mutable-source selector with friendly labels and opaque data."""
    key = str(selected_key or STATION_MANUAL_SOURCE_KEY).strip()
    sources = tuple(
        source
        for source in store.list_sources(limit=MAX_RESULTS)
        if source.source_kind == "station" and not source.read_only
    )
    choices = [(source.label, source.source_key) for source in sources]
    choice_keys = {choice_key for _label, choice_key in choices}
    if key and key not in choice_keys and selected_key:
        selected = store.sources_by_keys((key,)).get(key)
        if selected is not None:
            choices.append((selected.label, selected.source_key))
        else:
            choices.append(("Catalog source unavailable", key))
    if key == STATION_MANUAL_SOURCE_KEY and key not in choice_keys:
        choices.append(("Station Resources", key))
    choices.sort(key=lambda item: (item[0].casefold(), item[1]))
    prior = combo.blockSignals(True)
    try:
        combo.clear()
        for label, source_key in choices:
            combo.addItem(label, source_key)
        if key:
            index = combo.findData(key)
            combo.setCurrentIndex(index)
    finally:
        combo.blockSignals(prior)


def populate_frequency_combo(
    combo: QComboBox, store: ResourceCatalogStore, selected_key: str | None = None
) -> None:
    """Populate a frequency selector with readable labels and opaque key data."""
    rows = store.list_frequencies(active=None, limit=MAX_RESULTS)
    prior = combo.blockSignals(True)
    try:
        combo.clear()
        combo.addItem("Choose a frequency", None)
        for resource in rows:
            combo.addItem(
                f"{resource.label} · {frequency_where_text(resource)}",
                resource.frequency_resource_key,
            )
        key = str(selected_key or "").strip()
        if key:
            index = combo.findData(key)
            if index < 0:
                combo.addItem("Frequency unavailable", key)
                index = combo.count() - 1
            combo.setCurrentIndex(index)
    finally:
        combo.blockSignals(prior)


def frequency_where_text(resource: FrequencyResource) -> str:
    """Return a concise display value; keys and Hz remain the comparison API."""
    if resource.resource_kind == "band_range":
        return f"{frequency_mhz_text(resource.lower_hz or 0)}–{frequency_mhz_text(resource.upper_hz or 0)}"
    receive = resource.receive_hz or resource.center_hz
    transmit = resource.transmit_hz
    if receive is None:
        return "Frequency unavailable"
    if transmit is not None and transmit != receive:
        return f"RX {frequency_mhz_text(receive)} · TX {frequency_mhz_text(transmit)}"
    return frequency_mhz_text(receive)



def resource_status_text(resource: FrequencyResource) -> str:
    if resource.retired:
        return "Retired"
    if not resource.active:
        return "Unlisted"
    return "Listed"


def session_when_text(session: NetDirectorySession) -> str:
    parts = [part for part in (getattr(session, "day_utc", None), session.recurrence, session.local_start_time, session.timezone) if part]
    return " · ".join(parts) or "Timing not published"


class ResourcePicker(QDialog):
    """Modal, bounded picker usable by Local Nets and HF subscription flows."""

    frequency_selected = Signal(object)

    def __init__(self, store: ResourceCatalogStore, parent: QWidget | None = None, *, service: str | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.service = service
        self.selected_resource: FrequencyResource | None = None
        self.setWindowTitle("Choose Frequency Resource")
        self._build_ui()
        self.refresh_results()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        heading = QLabel("Choose a reusable frequency resource")
        self.heading = heading
        layout.addWidget(heading)
        copy = QLabel("Search the station catalog. Selecting a resource does not tune a radio or evaluate transmit eligibility.")
        copy.setWordWrap(True)
        layout.addWidget(copy)
        search_row = QHBoxLayout()
        self.search_row = search_row
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Search label, channel, frequency, locality, or coverage")
        self.search_edit.setAccessibleName("Search frequency catalog")
        self.search_edit.returnPressed.connect(self.refresh_results)
        search_row.addWidget(self.search_edit, 1)
        refresh = QPushButton("Search", self)
        refresh.clicked.connect(self.refresh_results)
        search_row.addWidget(refresh)
        layout.addLayout(search_row)
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["Label", "Service", "Where", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.itemDoubleClicked.connect(lambda *_: self._accept_selected())
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel, self)
        choose = buttons.addButton("Choose", QDialogButtonBox.AcceptRole)
        choose.clicked.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.apply_theme()

    def apply_theme(self) -> None:
        theme = active_app_theme()
        self.heading.setStyleSheet(label_style("text", theme, weight=700))
        self.status_label.setStyleSheet(label_style("muted", theme))
        self.table.setStyleSheet(f"QTableWidget {{ gridline-color: {theme['border']}; }}")
        self.search_edit.setMinimumHeight(control_height_for_font(self.search_edit))
        self.table.verticalHeader().setDefaultSectionSize(
            control_height_for_font(self.table, vertical_padding=10, floor=1)
        )
        for button in self.findChildren(QPushButton):
            button.setMinimumHeight(button_height_for_font(button))
            if button.text() == "Search":
                button.setStyleSheet(button_style("primary", theme))
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        compact = self.width() > 0 and self.width() < max(700, self.fontMetrics().horizontalAdvance("Choose a reusable frequency resource") * 20)
        self.search_row.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.apply_theme()

    def refresh_results(self) -> None:
        rows = self.store.list_frequencies(search=self.search_edit.text(), service=self.service, limit=MAX_RESULTS)
        self.table.setRowCount(len(rows))
        for row_index, resource in enumerate(rows):
            values = (resource.label, resource.service, frequency_where_text(resource), resource_status_text(resource))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, resource)
                self.table.setItem(row_index, column, item)
        self.status_label.setText(f"Showing {len(rows)} result{'s' if len(rows) != 1 else ''}; catalog results are limited to {MAX_RESULTS}.")
        if rows:
            self.table.selectRow(0)

    def _accept_selected(self) -> None:
        selected = self.table.selectedItems()
        if not selected:
            self.status_label.setText("Choose one frequency resource first.")
            return
        resource = self.table.item(selected[0].row(), 0).data(Qt.UserRole)
        if not isinstance(resource, FrequencyResource):
            return
        self.selected_resource = resource
        self.frequency_selected.emit(resource)
        self.accept()


def choose_frequency_resource(store: ResourceCatalogStore, parent: QWidget | None = None, *, service: str | None = None) -> FrequencyResource | None:
    picker = ResourcePicker(store, parent, service=service)
    return picker.selected_resource if picker.exec() == QDialog.Accepted else None


__all__ = [
    "ResourcePicker",
    "choose_frequency_resource",
    "frequency_mhz_text",
    "frequency_programming_text",
    "frequency_where_text",
    "populate_frequency_combo",
    "populate_source_combo",
    "resource_status_text",
    "session_when_text",
    "source_display_label",
    "source_display_labels",
]
