"""Bounded Frequency Catalog workspace for Tools & Resources."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
import uuid

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QScrollArea,
)

from freqinout.core.resource_catalog_models import CatalogValidationError, FrequencyResource, ReadOnlyResourceError, ReferencedResourceError
from freqinout.core.resource_catalog_store import (
    MAX_RESULTS,
    STATION_MANUAL_SOURCE_KEY,
    ResourceCatalogStore,
)
from freqinout.gui.resource_catalog_snapshot import (
    ResourceCatalogSnapshot,
    ResourceCatalogSnapshotService,
    filter_frequencies,
    source_label,
    load_resource_catalog_snapshot,
)
from freqinout.gui.resource_picker import frequency_where_text, frequency_programming_text, resource_status_text
from freqinout.gui.theme import (
    active_app_theme,
    button_height_for_font,
    button_style,
    control_height_for_font,
    label_style,
    ThemedItemCheckDelegate,
)


def new_frequency_resource_key() -> str:
    return f"frequency_{uuid.uuid4()}"


def hz_from_text(value: str) -> int | None:
    """Parse an editor Hz field without accepting display-MHz comparison keys."""
    text = str(value or "").strip().replace(",", "")
    return int(text) if text else None


class FrequencyCatalogView(QWidget):
    """Frequency catalog browser/editor. All reads are bounded store calls."""

    review_export_requested = Signal(object)

    def __init__(self, store: ResourceCatalogStore, parent: QWidget | None = None, *, snapshot_service: ResourceCatalogSnapshotService | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self._selected: FrequencyResource | None = None
        self._editing_key: str | None = None
        self._editor_original: FrequencyResource | None = None
        self._export_selected_keys: set[str] = set()
        self._populating_results = False
        self._snapshot: ResourceCatalogSnapshot | None = None
        self._frequency_offset = 0
        self._default_snapshot_service = snapshot_service is None
        self._snapshot_service = snapshot_service or ResourceCatalogSnapshotService(store)
        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.setInterval(10)
        self._snapshot_timer.timeout.connect(self._take_snapshot)
        self._column_timer = QTimer(self)
        self._column_timer.setSingleShot(True)
        self._column_timer.timeout.connect(self._fit_columns)
        self._build_ui()
        self.refresh_results()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.title_label = QLabel("Frequency Catalog", self)
        self.title_label.hide()
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Search label, channel, frequency, locality, or coverage")
        self.search_edit.setAccessibleName("Frequency catalog search")
        self.search_edit.textChanged.connect(self._render_cached_results)
        self.search_edit.returnPressed.connect(self._search_catalog)
        self.search_edit.setToolTip("Typing filters the loaded page. Press Enter to search the full catalog. Reference guidance is advisory and does not assess transmit authorization.")
        layout.addWidget(self.search_edit)
        filters = QHBoxLayout()
        self.filters_row = filters
        self.service_filter = QComboBox(self)
        self.service_filter.addItem("Service: All", None)
        self.service_filter.addItem("Amateur", "AMATEUR")
        self.service_filter.addItem("GMRS", "GMRS")
        for service in ("FRS", "MURS", "OTHER"):
            self.service_filter.addItem(service, service)
        self.service_filter.currentIndexChanged.connect(self._render_cached_results)
        self.service_filter.currentIndexChanged.connect(self._search_catalog)
        filters.addWidget(self.service_filter)
        self.status_filter = QComboBox(self)
        self.status_filter.addItem("Listing: Listed", True)
        self.status_filter.addItem("Retired", False)
        self.status_filter.addItem("All listings", None)
        self.status_filter.currentIndexChanged.connect(self._render_cached_results)
        self.status_filter.currentIndexChanged.connect(self._search_catalog)
        filters.addWidget(self.status_filter)
        filters.addStretch(1)
        self.new_btn = QPushButton("New", self)
        self.details_btn = QPushButton("Details", self)
        self.import_radio_btn = QPushButton("Import radio files…", self)
        self.import_radio_btn.clicked.connect(self.import_radio_files)
        self.more_btn = QPushButton("More", self)
        self.more_menu = QMenu(self.more_btn)
        self.more_btn.setMenu(self.more_menu)
        for button in (self.new_btn, self.details_btn, self.import_radio_btn, self.more_btn):
            filters.addWidget(button)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(["Export", "Label", "Service", "Frequency", "Catalog source", "Listing"])
        self.table.setAccessibleName("Frequency catalog results")
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemChanged.connect(self._export_item_changed)
        self.table.installEventFilter(self)
        self.table.itemDoubleClicked.connect(lambda _item: self.show_details())
        self.table.setItemDelegateForColumn(0, ThemedItemCheckDelegate(self.table))
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        for column in (0, 2, 3, 5):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.previous_page_btn = QPushButton("Previous", self)
        self.next_page_btn = QPushButton("Next", self)
        self.previous_page_btn.clicked.connect(lambda: self._change_frequency_page(-1))
        self.next_page_btn.clicked.connect(lambda: self._change_frequency_page(1))
        footer.addWidget(self.previous_page_btn)
        footer.addWidget(self.next_page_btn)
        self.export_selection_label = QLabel("No frequencies selected for export.", self)
        self.export_selection_label.setAccessibleName("Frequency export selection summary")
        footer.addWidget(self.export_selection_label, 1)
        self.clear_export_selection_btn = QPushButton("Clear selection", self)
        self.clear_export_selection_btn.setAccessibleName("Clear selected frequencies for export")
        self.clear_export_selection_btn.clicked.connect(self.clear_export_selection)
        self.review_export_btn = QPushButton("Review export…", self)
        self.review_export_btn.setAccessibleName("Review selected frequencies for export")
        self.review_export_btn.setToolTip("Preview selected frequencies and included catalog dependencies before choosing a file.")
        self.review_export_btn.clicked.connect(self.request_export_review)
        footer.addWidget(self.clear_export_selection_btn)
        footer.addWidget(self.review_export_btn)
        layout.addLayout(footer)
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.details_dialog = QDialog(self)
        self.details_dialog.setWindowTitle("Frequency details")
        details_layout = QVBoxLayout(self.details_dialog)
        self.details_scroll = QScrollArea(self.details_dialog)
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        details_body = QWidget(self.details_scroll)
        self.details_scroll.setWidget(details_body)
        details_body_layout = QVBoxLayout(details_body)
        self.detail_label = QLabel("Select a frequency resource to review its catalog source and station impact.", self.details_dialog)
        self.detail_label.setTextFormat(Qt.PlainText)
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        details_body_layout.addWidget(self.detail_label)
        self.usage_label = QLabel("Used by: —", self.details_dialog)
        self.usage_label.setWordWrap(True)
        details_body_layout.addWidget(self.usage_label)
        details_body_layout.addStretch(1)
        details_layout.addWidget(self.details_scroll, 1)
        self.copy_details_btn = QPushButton("Copy details", self.details_dialog)
        self.copy_details_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.detail_label.text() + "\n" + self.usage_label.text()))
        details_layout.addWidget(self.copy_details_btn)
        self.details_btn.clicked.connect(self.show_details)

        self.edit_btn = QPushButton("Edit", self)
        self.clone_btn = QPushButton("Clone", self)
        self.retire_btn = QPushButton("Retire", self)
        self.delete_btn = QPushButton("Delete", self)
        for button in (self.edit_btn, self.clone_btn, self.retire_btn, self.delete_btn):
            button.hide()
            action = self.more_menu.addAction(button.text())
            action.triggered.connect(button.click)
            button._menu_action = action
        self.more_menu.addSeparator()
        refresh_action = self.more_menu.addAction("Refresh")
        refresh_action.triggered.connect(self.refresh_results)

        self.editor_dialog = QDialog(self)
        self.editor_dialog.setWindowModality(Qt.WindowModal)
        self.editor_dialog.setWindowTitle("Frequency resource")
        editor_dialog_layout = QVBoxLayout(self.editor_dialog)
        self.detail_scroll = QScrollArea(self.editor_dialog)
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        editor_dialog_layout.addWidget(self.detail_scroll)
        detail = QWidget(self.detail_scroll)
        self.detail_scroll.setWidget(detail)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        editor_frame = QFrame(detail)
        editor_frame.setFrameShape(QFrame.StyledPanel)
        editor_layout = QFormLayout(editor_frame)
        editor_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        editor_layout.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.key_edit = QLineEdit(editor_frame)
        self.source_edit = QComboBox(editor_frame)
        self.source_edit.setAccessibleName("Catalog source")
        self.label_edit = QLineEdit(editor_frame)
        self.kind_edit = QComboBox(editor_frame)
        self.kind_edit.addItems(["simplex", "repeater", "channel", "band_range"])
        self.editor_service = QComboBox(editor_frame)
        self.editor_service.addItems(["AMATEUR", "GMRS", "FRS", "MURS", "OTHER"])
        self.center_hz_edit = QLineEdit(editor_frame)
        self.lower_hz_edit = QLineEdit(editor_frame)
        self.upper_hz_edit = QLineEdit(editor_frame)
        self.receive_hz_edit = QLineEdit(editor_frame)
        self.transmit_hz_edit = QLineEdit(editor_frame)
        self.band_edit = QLineEdit(editor_frame)
        self.channel_edit = QLineEdit(editor_frame)
        self.mode_edit = QLineEdit(editor_frame)
        self.notes_edit = QLineEdit(editor_frame)
        self.key_edit.setVisible(False)
        for label, widget in (("Catalog source", self.source_edit), ("Label", self.label_edit), ("Kind", self.kind_edit), ("Service", self.editor_service), ("Center frequency (Hz)", self.center_hz_edit), ("Lower frequency (Hz)", self.lower_hz_edit), ("Upper frequency (Hz)", self.upper_hz_edit), ("Receive frequency (Hz)", self.receive_hz_edit), ("Transmit frequency (Hz)", self.transmit_hz_edit), ("Band", self.band_edit), ("Channel", self.channel_edit), ("Mode", self.mode_edit), ("Notes", self.notes_edit)):
            editor_layout.addRow(label, widget)
        editor_actions = QHBoxLayout()
        self.save_btn = QPushButton("Save", self.editor_dialog)
        self.cancel_btn = QPushButton("Cancel", self.editor_dialog)
        editor_actions.addWidget(self.save_btn)
        editor_actions.addWidget(self.cancel_btn)
        detail_layout.addWidget(editor_frame)
        self.editor_frame = editor_frame
        self.editor_status_label = QLabel("", self.editor_dialog)
        self.editor_status_label.setTextFormat(Qt.PlainText)
        self.editor_status_label.setWordWrap(True)
        self.editor_status_label.hide()
        editor_dialog_layout.addWidget(self.editor_status_label)
        editor_dialog_layout.addLayout(editor_actions)
        detail_layout.addStretch(1)
        self.new_btn.clicked.connect(self.begin_new)
        self.edit_btn.clicked.connect(self.begin_edit)
        self.clone_btn.clicked.connect(self.begin_clone)
        self.retire_btn.clicked.connect(self.retire_selected)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.save_btn.clicked.connect(self.save_editor)
        self.cancel_btn.clicked.connect(self.cancel_editor)
        self.editor_frame.setVisible(False)
        self.apply_theme()
        self._set_action_state()

    def apply_theme(self) -> None:
        theme = active_app_theme()
        self.title_label.setStyleSheet(label_style("text", theme, weight=700))
        self.table.setStyleSheet(f"QTableWidget {{ gridline-color: {theme['border']}; }}")
        self.detail_label.setStyleSheet(label_style("text", theme))
        self.usage_label.setStyleSheet(label_style("muted", theme))
        self.status_label.setStyleSheet(label_style("muted", theme))
        for button in (
            self.copy_details_btn,
            self.details_btn,
            self.more_btn,
            self.import_radio_btn,
            self.previous_page_btn,
            self.next_page_btn,
            self.clear_export_selection_btn,
            self.review_export_btn,
            self.new_btn,
            self.edit_btn,
            self.clone_btn,
            self.retire_btn,
            self.delete_btn,
            self.save_btn,
            self.cancel_btn,
        ):
            button.setMinimumHeight(button_height_for_font(button))
        for button, role in (
            (self.copy_details_btn, "secondary"),
            (self.previous_page_btn, "secondary"),
            (self.next_page_btn, "secondary"),
            (self.import_radio_btn, "secondary"),
            (self.details_btn, "secondary"),
            (self.more_btn, "secondary"),
            (self.clear_export_selection_btn, "muted"),
            (self.review_export_btn, "primary"),
            (self.new_btn, "secondary"),
            (self.edit_btn, "secondary"),
            (self.clone_btn, "secondary"),
            (self.retire_btn, "eligible_warning"),
            (self.delete_btn, "eligible_danger"),
            (self.save_btn, "primary"),
            (self.cancel_btn, "muted"),
        ):
            button.setStyleSheet(button_style(role, theme))
        self.search_edit.setMinimumHeight(control_height_for_font(self.search_edit))
        for combo in (self.service_filter, self.status_filter, self.source_edit, self.kind_edit, self.editor_service):
            combo.setMinimumHeight(control_height_for_font(combo))
        for field in (
            self.label_edit,
            self.center_hz_edit,
            self.lower_hz_edit,
            self.upper_hz_edit,
            self.receive_hz_edit,
            self.transmit_hz_edit,
            self.band_edit,
            self.channel_edit,
            self.mode_edit,
            self.notes_edit,
        ):
            field.setMinimumHeight(control_height_for_font(field))
        self.table.verticalHeader().setDefaultSectionSize(
            control_height_for_font(self.table, vertical_padding=10, floor=1)
        )
        self.table.horizontalHeader().setMinimumHeight(control_height_for_font(self.table.horizontalHeader(), vertical_padding=10, floor=1))
        self._column_timer.start(0)

    def _fit_columns(self) -> None:
        """Keep names and complete frequencies ahead of secondary listing metadata."""
        header = self.table.horizontalHeader()
        metrics = self.table.fontMetrics()
        padding = metrics.horizontalAdvance("MM")
        primary = sum(header.sectionSize(column) for column in (0, 2, 3))
        primary += metrics.horizontalAdvance("Frequency name and locality") + padding
        secondary = metrics.horizontalAdvance("Catalog source") + metrics.horizontalAdvance("Listing") + 2 * padding
        compact = primary + secondary > self.table.viewport().width()
        for column in (4, 5):
            self.table.setColumnHidden(column, compact)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_column_timer"):
            self._column_timer.start(0)

    def eventFilter(self, watched, event):
        if watched is self.table and event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Space, Qt.Key_Select) and event.modifiers() == Qt.NoModifier:
            item = self.table.item(self.table.currentRow(), 0)
            if self.table.isEnabled() and item is not None and item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)
                return True
        return super().eventFilter(watched, event)


    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.apply_theme()

    def refresh_results(self) -> None:
        """Request an off-thread snapshot; existing rows stay visible until it arrives."""
        if self._default_snapshot_service:
            loader = partial(load_resource_catalog_snapshot,
                frequency_search=self.search_edit.text(), frequency_service=self.service_filter.currentData(),
                frequency_active=self.status_filter.currentData(), frequency_offset=self._frequency_offset)
            self._snapshot_service.request(loader=loader)
        else:
            self._snapshot_service.request()
        self._snapshot_timer.start()

    def _search_catalog(self) -> None:
        self._frequency_offset = 0
        self.refresh_results()

    def _change_frequency_page(self, direction: int) -> None:
        self._frequency_offset = max(0, self._frequency_offset + direction * MAX_RESULTS)
        self.refresh_results()

    def _take_snapshot(self) -> None:
        completion = self._snapshot_service.take_latest()
        if completion is None:
            if not self._snapshot_service.has_pending():
                self._snapshot_timer.stop()
            return
        if completion.snapshot is not None:
            self._snapshot = completion.snapshot
            self._render_cached_results()
        elif completion.error is not None:
            self.status_label.setText(f"Catalog refresh unavailable; showing the last coherent results. {completion.error}")
        if not self._snapshot_service.has_pending():
            self._snapshot_timer.stop()

    def _render_cached_results(self, *_: object) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        selected_key = self._selected.frequency_resource_key if self._selected else None
        rows = filter_frequencies(
            snapshot, search=self.search_edit.text(), service=self.service_filter.currentData(),
            active=self.status_filter.currentData(),
        )
        self._populating_results = True
        try:
            self.table.setRowCount(len(rows))
            for row_index, resource in enumerate(rows):
                export_item = QTableWidgetItem()
                export_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                export_item.setData(Qt.UserRole, resource.frequency_resource_key)
                export_item.setCheckState(
                    Qt.Checked if resource.frequency_resource_key in self._export_selected_keys else Qt.Unchecked
                )
                self.table.setItem(row_index, 0, export_item)
                values = (
                    resource.label,
                    resource.service,
                    frequency_where_text(resource),
                    source_label(snapshot, resource.source_key),
                    resource_status_text(resource),
                )
                for column, value in enumerate(values, start=1):
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    if column == 1:
                        item.setData(Qt.UserRole, resource)
                    self.table.setItem(row_index, column, item)
        finally:
            self._populating_results = False
        self.status_label.setText(
            f"Showing {len(rows)} frequenc{'y' if len(rows) == 1 else 'ies'} · Page {self._frequency_offset // MAX_RESULTS + 1}"
            if rows else "No matching frequencies. Change the filters, import a file, or create a frequency.")
        self.previous_page_btn.setEnabled(self._frequency_offset > 0)
        self.next_page_btn.setEnabled(len(snapshot.frequencies) == MAX_RESULTS)
        self._update_export_selection_state()
        self._column_timer.start(0)
        if rows:
            selected_index = next((index for index, row in enumerate(rows) if row.frequency_resource_key == selected_key), 0)
            self.table.selectRow(selected_index)
            self._selection_changed()
        else:
            self._selected = None
            self.detail_label.setText("No matching frequency resources. Create a station-owned resource when a new local reference is needed.")
            self.usage_label.setText("Used by: —")
            self._set_action_state()

    def _selection_changed(self) -> None:
        selected = self.table.selectedItems()
        if not selected:
            return
        resource_item = self.table.item(selected[0].row(), 1)
        resource = resource_item.data(Qt.UserRole) if resource_item is not None else None
        if isinstance(resource, FrequencyResource):
            self._selected = resource
            snapshot = self._snapshot
            if snapshot is None:
                return
            usage = snapshot.frequency_usage.get(resource.frequency_resource_key)
            if usage is None:
                return
            fields = [
                f"{resource.label} · {resource.service} · {resource.resource_kind}",
                frequency_programming_text(resource),
                f"Catalog source: {source_label(snapshot, resource.source_key)} · {resource_status_text(resource)}",
            ]
            self.detail_label.setText("\n".join(fields))
            impacts = ", ".join(f"{name.replace('_', ' ')} {count}" for name, count in usage.by_kind.items()) or "none"
            self.usage_label.setText(f"Used by: {usage.total_references} ({impacts})")
            self._set_action_state()

    def import_radio_files(self) -> None:
        from freqinout.gui.radio_import_dialog import RadioImportDialog
        dialog = RadioImportDialog(self.store, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._search_catalog()

    def _export_item_changed(self, item: QTableWidgetItem) -> None:
        if self._populating_results or item.column() != 0:
            return
        key = str(item.data(Qt.UserRole) or "").strip()
        if not key:
            return
        if item.checkState() == Qt.Checked:
            self._export_selected_keys.add(key)
        else:
            self._export_selected_keys.discard(key)
        self._update_export_selection_state()

    def _update_export_selection_state(self) -> None:
        count = len(self._export_selected_keys)
        self.export_selection_label.setText(
            "No frequencies selected for export."
            if count == 0
            else f"{count} frequenc{'y' if count == 1 else 'ies'} selected for export."
        )
        self.review_export_btn.setEnabled(count > 0)
        self.clear_export_selection_btn.setEnabled(count > 0)

    def clear_export_selection(self) -> None:
        if not self._export_selected_keys:
            return
        self._export_selected_keys.clear()
        self._render_cached_results()

    def request_export_review(self) -> None:
        if self._export_selected_keys:
            self.review_export_requested.emit(tuple(sorted(self._export_selected_keys)))

    def _set_action_state(self) -> None:
        has_selection = self._selected is not None
        self.details_btn.setEnabled(has_selection)
        for button in (self.edit_btn, self.clone_btn, self.retire_btn, self.delete_btn):
            button.setEnabled(has_selection)
            button._menu_action.setEnabled(has_selection)

    def show_details(self) -> None:
        if self._selected:
            available = self.screen().availableGeometry()
            metrics = self.fontMetrics()
            self.details_dialog.resize(min(metrics.horizontalAdvance("M" * 54), int(available.width() * .9)),
                                       min(metrics.lineSpacing() * 18, int(available.height() * .85)))
            self.details_dialog.show()
            self.details_dialog.raise_()

    def _show_editor(self, title: str) -> None:
        self.editor_status_label.clear()
        self.editor_status_label.hide()
        self.editor_dialog.setWindowTitle(title)
        self.editor_frame.setVisible(True)
        screen = self.screen()
        available = screen.availableGeometry() if screen else None
        width = max(self.editor_dialog.sizeHint().width(), 480)
        self.editor_dialog.resize(min(width, available.width()) if available else width,
                                  min(600, int(available.height() * 0.85)) if available else 600)
        self.editor_dialog.show()
        self.editor_dialog.raise_()
        self.label_edit.setFocus()
        self.label_edit.setCursorPosition(0)
        self.notes_edit.setCursorPosition(0)

    def begin_new(self) -> None:
        self._editing_key = None
        self._fill_editor(None)
        self.key_edit.setText(new_frequency_resource_key())
        self._show_editor("New frequency")

    def begin_edit(self) -> None:
        if not self._selected:
            return
        self._editing_key = self._selected.frequency_resource_key
        self._fill_editor(self._selected)
        self._show_editor("Edit frequency")

    def begin_clone(self) -> None:
        if not self._selected:
            return
        self._editing_key = None
        self._fill_editor(self._selected)
        self.key_edit.setText(new_frequency_resource_key())
        self._populate_source_combo()
        self._show_editor("Clone frequency")
        self.editor_status_label.setText("Clone creates a new station-owned resource. The catalog source defaults to Station Resources.")
        self.editor_status_label.show()

    def _fill_editor(self, resource: FrequencyResource | None) -> None:
        self._editor_original = resource
        self.key_edit.setReadOnly(False)
        if resource is None:
            for field in (self.key_edit, self.label_edit, self.center_hz_edit, self.lower_hz_edit, self.upper_hz_edit, self.receive_hz_edit, self.transmit_hz_edit, self.band_edit, self.channel_edit, self.mode_edit, self.notes_edit):
                field.clear()
            self._populate_source_combo()
            self.kind_edit.setCurrentText("simplex")
            self.editor_service.setCurrentText("AMATEUR")
            return
        self.key_edit.setText(resource.frequency_resource_key)
        self.key_edit.setReadOnly(self._editing_key is not None)
        self._populate_source_combo(resource.source_key)
        self.label_edit.setText(resource.label)
        self.kind_edit.setCurrentText(resource.resource_kind)
        self.editor_service.setCurrentText(resource.service)
        self.center_hz_edit.setText(str(resource.center_hz or ""))
        self.lower_hz_edit.setText(str(resource.lower_hz or ""))
        self.upper_hz_edit.setText(str(resource.upper_hz or ""))
        self.receive_hz_edit.setText(str(resource.receive_hz or ""))
        self.transmit_hz_edit.setText(str(resource.transmit_hz or ""))
        self.band_edit.setText(resource.band or "")
        self.channel_edit.setText(resource.channel or "")
        self.mode_edit.setText(resource.mode or "")
        self.notes_edit.setText(resource.notes or "")

    def _populate_source_combo(self, selected_key: str | None = None) -> None:
        """Populate the editor from the current snapshot; opening an editor never reads SQLite."""
        key = str(selected_key or STATION_MANUAL_SOURCE_KEY).strip()
        sources = () if self._snapshot is None else tuple(
            source for source in self._snapshot.sources.values()
            if source.source_kind == "station" and not source.read_only
        )
        choices = [(source.label, source.source_key) for source in sources]
        if selected_key and key not in {choice_key for _label, choice_key in choices}:
            choices.append((source_label(self._snapshot, key) if self._snapshot else "Catalog source unavailable", key))
        if key == STATION_MANUAL_SOURCE_KEY and key not in {choice_key for _label, choice_key in choices}:
            choices.append(("Station Resources", key))
        prior = self.source_edit.blockSignals(True)
        try:
            self.source_edit.clear()
            for label, source_key in sorted(choices, key=lambda choice: (choice[0].casefold(), choice[1])):
                self.source_edit.addItem(label, source_key)
            self.source_edit.setCurrentIndex(self.source_edit.findData(key))
        finally:
            self.source_edit.blockSignals(prior)

    def _editor_resource(self) -> FrequencyResource:
        existing = self._editor_original if self._editing_key else None
        original = self._editor_original
        receive = hz_from_text(self.receive_hz_edit.text()) or hz_from_text(self.center_hz_edit.text())
        transmit = hz_from_text(self.transmit_hz_edit.text())
        return FrequencyResource(
            frequency_resource_key=self.key_edit.text(), source_key=str(self.source_edit.currentData() or ""), resource_kind=self.kind_edit.currentText(),
            service=self.editor_service.currentText(), label=self.label_edit.text(), center_hz=hz_from_text(self.center_hz_edit.text()),
            lower_hz=hz_from_text(self.lower_hz_edit.text()), upper_hz=hz_from_text(self.upper_hz_edit.text()),
            receive_hz=hz_from_text(self.receive_hz_edit.text()), transmit_hz=hz_from_text(self.transmit_hz_edit.text()),
            band=self.band_edit.text().strip() or None, channel=self.channel_edit.text().strip() or None,
            mode=self.mode_edit.text().strip() or None, notes=self.notes_edit.text().strip() or None,
            offset_hz=abs(transmit - receive) if transmit is not None and receive is not None and transmit != receive else None,
            bandwidth_hz=original.bandwidth_hz if original else None,
            tone=original.tone if original else None,
            locality=original.locality if original else None,
            grid=original.grid if original else None,
            coverage=original.coverage if original else None,
            provenance=original.provenance if original else None,
            active=existing.active if existing else True, retired=existing.retired if existing else False,
            replacement_frequency_resource_key=existing.replacement_frequency_resource_key if existing else None,
        )

    def save_editor(self) -> None:
        try:
            if self.source_edit.currentData() == STATION_MANUAL_SOURCE_KEY:
                self.store.ensure_station_source()
            resource = self._editor_resource()
            saved = self.store.update_frequency(resource) if self._editing_key else self.store.create_frequency(resource)
        except (CatalogValidationError, ReadOnlyResourceError, ValueError) as exc:
            self.editor_status_label.setText(f"Cannot save frequency resource: {exc}")
            self.editor_status_label.show()
            self.status_label.setText(f"Cannot save frequency resource: {exc}")
            return
        self.editor_frame.setVisible(False)
        self.editor_dialog.hide()
        self._selected = saved
        self.status_label.setText("Frequency resource saved.")
        self.refresh_results()

    def cancel_editor(self) -> None:
        self.editor_frame.setVisible(False)
        self.editor_dialog.hide()
        self._editing_key = None

    def retire_selected(self) -> None:
        if not self._selected:
            return
        try:
            retired = self.store.retire_frequency(self._selected.frequency_resource_key)
        except (CatalogValidationError, ReadOnlyResourceError) as exc:
            self.status_label.setText(f"Cannot retire frequency resource: {exc}")
            return
        self.status_label.setText(f"Retired {retired.label}; existing references remain intact.")
        self.refresh_results()

    def delete_selected(self) -> None:
        if not self._selected:
            return
        usage = self.store.frequency_usage(self._selected.frequency_resource_key)
        if usage.is_referenced:
            self.status_label.setText(f"Cannot delete: used by {usage.total_references} record(s). Retire it instead.")
            return
        answer = QMessageBox.question(self, "Delete frequency resource", f"Delete '{self._selected.label}'? This cannot be undone.")
        if answer != QMessageBox.Yes:
            return
        try:
            self.store.delete_frequency_if_unreferenced(self._selected.frequency_resource_key)
        except (ReferencedResourceError, ReadOnlyResourceError) as exc:
            self.status_label.setText(f"Cannot delete frequency resource: {exc}")
            return
        self._selected = None
        self.status_label.setText("Frequency resource deleted.")
        self.refresh_results()


__all__ = ["FrequencyCatalogView", "hz_from_text", "new_frequency_resource_key"]
