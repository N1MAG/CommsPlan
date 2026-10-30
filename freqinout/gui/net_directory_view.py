"""Bounded Net Directory workspace for Tools & Resources."""

from __future__ import annotations

from dataclasses import replace
import uuid

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QScrollArea,
    QBoxLayout,
)

from freqinout.core.resource_catalog_models import CatalogValidationError, NetDirectoryEntry, NetDirectorySession, ReadOnlyResourceError, ReferencedResourceError
from freqinout.core.resource_catalog_store import (
    MAX_RESULTS,
    STATION_MANUAL_SOURCE_KEY,
    ResourceCatalogStore,
)
from freqinout.gui.resource_catalog_snapshot import (
    ResourceCatalogSnapshot,
    ResourceCatalogSnapshotService,
    filter_entries,
    source_label,
)
from freqinout.gui.resource_picker import frequency_where_text, session_when_text
from freqinout.gui.theme import (
    active_app_theme,
    button_height_for_font,
    button_style,
    control_height_for_font,
    label_style,
    style_splitter_handles,
)


def new_net_entry_key() -> str:
    return f"net_{uuid.uuid4()}"


def new_net_session_key() -> str:
    return f"session_{uuid.uuid4()}"


class NetDirectoryView(QWidget):
    """Directory identities and published sessions; never a station scheduler."""

    add_to_hf_nets_requested = Signal(object)
    open_hf_schedule_requested = Signal(object)

    def __init__(self, store: ResourceCatalogStore, parent: QWidget | None = None, *, snapshot_service: ResourceCatalogSnapshotService | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self._selected_entry: NetDirectoryEntry | None = None
        self._selected_session: NetDirectorySession | None = None
        self._editing_entry_key: str | None = None
        self._editing_session_key: str | None = None
        self._snapshot: ResourceCatalogSnapshot | None = None
        self._snapshot_service = snapshot_service or ResourceCatalogSnapshotService(store)
        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.setInterval(10)
        self._snapshot_timer.timeout.connect(self._take_snapshot)
        self._build_ui()
        self.refresh_results()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.title_label = QLabel("Net Directory")
        layout.addWidget(self.title_label)
        why = QLabel("Directory records describe reusable nets and published net meetings. They do not activate an HF or Local schedule.")
        why.setWordWrap(True)
        layout.addWidget(why)
        filters = QHBoxLayout()
        self.filters_row = filters
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Search net name, purpose, source region, or public contact")
        self.search_edit.setAccessibleName("Net directory search")
        self.search_edit.textChanged.connect(self._render_cached_results)
        self.search_edit.returnPressed.connect(self.refresh_results)
        filters.addWidget(self.search_edit, 1)
        self.status_filter = QComboBox(self)
        self.status_filter.addItem("Listing: Listed", True)
        self.status_filter.addItem("Retired", False)
        self.status_filter.addItem("All listings", None)
        self.status_filter.currentIndexChanged.connect(self._render_cached_results)
        filters.addWidget(self.status_filter)
        refresh = QPushButton("Refresh", self)
        refresh.clicked.connect(self.refresh_results)
        filters.addWidget(refresh)
        layout.addLayout(filters)
        self.splitter = QSplitter(Qt.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)
        self.entry_table = QTableWidget(0, 4, self.splitter)
        self.entry_table.setHorizontalHeaderLabels(["Net", "Region", "Catalog source", "Listing"])
        self.entry_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.entry_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.entry_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.entry_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.entry_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.entry_table.itemSelectionChanged.connect(self._entry_selection_changed)
        self.detail_scroll = QScrollArea(self.splitter)
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.detail_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        right = QWidget(self.detail_scroll)
        self.detail_scroll.setWidget(right)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.detail_label = QLabel("Select a directory entry to inspect published net meetings and station use.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right_layout.addWidget(self.detail_label)
        self.entry_usage_label = QLabel("Used by: —")
        self.entry_usage_label.setWordWrap(True)
        right_layout.addWidget(self.entry_usage_label)
        entry_actions = QGridLayout()
        entry_actions.setHorizontalSpacing(6)
        entry_actions.setVerticalSpacing(6)
        self.new_entry_btn = QPushButton("New Net", right)
        self.edit_entry_btn = QPushButton("Edit", right)
        self.clone_entry_btn = QPushButton("Clone", right)
        self.retire_entry_btn = QPushButton("Retire", right)
        self.delete_entry_btn = QPushButton("Delete", right)
        for index, button in enumerate((self.new_entry_btn, self.edit_entry_btn, self.clone_entry_btn, self.retire_entry_btn, self.delete_entry_btn)):
            entry_actions.addWidget(button, index // 3, index % 3)
        for column in range(3):
            entry_actions.setColumnStretch(column, 1)
        right_layout.addLayout(entry_actions)
        self.session_table = QTableWidget(0, 4, right)
        self.session_table.setHorizontalHeaderLabels(["When", "Service", "Frequency", "Listing"])
        self.session_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.session_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.session_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.session_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.session_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.session_table.itemSelectionChanged.connect(self._session_selection_changed)
        right_layout.addWidget(self.session_table, 1)
        session_actions = QGridLayout()
        session_actions.setHorizontalSpacing(6)
        session_actions.setVerticalSpacing(6)
        self.new_session_btn = QPushButton("Add Meeting", right)
        self.edit_session_btn = QPushButton("Edit Meeting", right)
        self.clone_session_btn = QPushButton("Clone", right)
        self.retire_session_btn = QPushButton("Retire", right)
        self.delete_session_btn = QPushButton("Delete", right)
        self.add_hf_net_btn = QPushButton("Add to HF Nets", right)
        for button, action in (
            (self.new_session_btn, "Add a published net meeting"),
            (self.edit_session_btn, "Edit the selected net meeting"),
            (self.clone_session_btn, "Clone the selected net meeting"),
            (self.retire_session_btn, "Retire the selected net meeting"),
            (self.delete_session_btn, "Delete the selected net meeting"),
            (self.add_hf_net_btn, "Add selected net meetings to HF Nets"),
        ):
            button.setAccessibleName(action)
            button.setToolTip(action)
        for index, button in enumerate((self.new_session_btn, self.edit_session_btn, self.clone_session_btn, self.retire_session_btn, self.delete_session_btn, self.add_hf_net_btn)):
            session_actions.addWidget(button, index // 3, index % 3)
        for column in range(3):
            session_actions.setColumnStretch(column, 1)
        right_layout.addLayout(session_actions)
        self.entry_editor = self._build_entry_editor(right)
        self.session_editor = self._build_session_editor(right)
        right_layout.addWidget(self.entry_editor)
        right_layout.addWidget(self.session_editor)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        right_layout.addWidget(self.status_label)
        self.new_entry_btn.clicked.connect(self.begin_new_entry)
        self.edit_entry_btn.clicked.connect(self.begin_edit_entry)
        self.clone_entry_btn.clicked.connect(self.begin_clone_entry)
        self.retire_entry_btn.clicked.connect(self.retire_entry)
        self.delete_entry_btn.clicked.connect(self.delete_entry)
        self.new_session_btn.clicked.connect(self.begin_new_session)
        self.edit_session_btn.clicked.connect(self.begin_edit_session)
        self.clone_session_btn.clicked.connect(self.begin_clone_session)
        self.retire_session_btn.clicked.connect(self.retire_session)
        self.delete_session_btn.clicked.connect(self.delete_session)
        self.add_hf_net_btn.clicked.connect(self.request_add_to_hf_nets)
        self.entry_editor.setVisible(False)
        self.session_editor.setVisible(False)
        self.apply_theme()
        self._set_action_state()

    def apply_theme(self) -> None:
        theme = active_app_theme()
        self.title_label.setStyleSheet(label_style("text", theme, weight=700))
        self.table_style = f"QTableWidget {{ gridline-color: {theme['border']}; }}"
        self.entry_table.setStyleSheet(self.table_style)
        self.session_table.setStyleSheet(self.table_style)
        for label in (self.detail_label, self.entry_usage_label, self.status_label):
            label.setStyleSheet(label_style("muted" if label is not self.detail_label else "text", theme))
        buttons = (
            self.new_entry_btn, self.edit_entry_btn, self.clone_entry_btn,
            self.retire_entry_btn, self.delete_entry_btn, self.new_session_btn,
            self.edit_session_btn, self.clone_session_btn, self.retire_session_btn,
            self.delete_session_btn, self.add_hf_net_btn,
        )
        for button in buttons:
            button.setMinimumHeight(button_height_for_font(button))
        for button, role in (
            (self.new_entry_btn, "primary"),
            (self.edit_entry_btn, "secondary"),
            (self.clone_entry_btn, "secondary"),
            (self.retire_entry_btn, "eligible_warning"),
            (self.delete_entry_btn, "eligible_danger"),
            (self.new_session_btn, "primary"),
            (self.edit_session_btn, "secondary"),
            (self.clone_session_btn, "secondary"),
            (self.retire_session_btn, "eligible_warning"),
            (self.delete_session_btn, "eligible_danger"),
            (self.add_hf_net_btn, "eligible_info"),
        ):
            button.setStyleSheet(button_style(role, theme))
        for control in (
            self.search_edit, self.status_filter, self.entry_source_edit,
            self.session_source_edit, self.session_frequency_combo,
            self.session_service_edit,
        ):
            control.setMinimumHeight(control_height_for_font(control))
        self.entry_table.verticalHeader().setDefaultSectionSize(
            control_height_for_font(self.entry_table, vertical_padding=10, floor=1)
        )
        self.session_table.verticalHeader().setDefaultSectionSize(
            control_height_for_font(self.session_table, vertical_padding=10, floor=1)
        )
        style_splitter_handles(self.splitter, theme, width=12)
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        compact = self.width() > 0 and self.width() < max(900, self.fontMetrics().horizontalAdvance("Net Directory") * 25)
        self.filters_row.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)
        self.splitter.setOrientation(Qt.Vertical if compact else Qt.Horizontal)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.apply_theme()

    def _build_entry_editor(self, parent: QWidget) -> QFrame:
        frame = QFrame(parent)
        frame.setFrameShape(QFrame.StyledPanel)
        form = QFormLayout(frame)
        self.entry_key_edit = QLineEdit(frame)
        self.entry_source_edit = QComboBox(frame)
        self.entry_source_edit.setAccessibleName("Catalog source")
        self.entry_name_edit = QLineEdit(frame)
        self.entry_scope_edit = QLineEdit(frame)
        self.entry_description_edit = QLineEdit(frame)
        self.entry_key_edit.setVisible(False)
        for label, widget in (("Catalog source", self.entry_source_edit), ("Name", self.entry_name_edit), ("Source region", self.entry_scope_edit), ("Purpose", self.entry_description_edit)):
            form.addRow(label, widget)
        actions = QHBoxLayout()
        save = QPushButton("Save Net", frame)
        cancel = QPushButton("Cancel", frame)
        save.clicked.connect(self.save_entry)
        cancel.clicked.connect(lambda: frame.setVisible(False))
        actions.addWidget(save); actions.addWidget(cancel)
        form.addRow(actions)
        return frame

    def _build_session_editor(self, parent: QWidget) -> QFrame:
        frame = QFrame(parent)
        frame.setFrameShape(QFrame.StyledPanel)
        form = QFormLayout(frame)
        self.session_key_edit = QLineEdit(frame)
        self.session_source_edit = QComboBox(frame)
        self.session_source_edit.setAccessibleName("Catalog source")
        # Keep the canonical key as hidden internal state; the operator chooses
        # the same resource through its readable label and decimal-MHz value.
        self.session_frequency_edit = QLineEdit(frame)
        self.session_frequency_edit.setVisible(False)
        self.session_frequency_combo = QComboBox(frame)
        self.session_frequency_combo.setAccessibleName("Frequency")
        self.session_service_edit = QComboBox(frame)
        self.session_service_edit.addItems(["AMATEUR", "GMRS"])
        self.session_recurrence_edit = QLineEdit(frame)
        self.session_day_edit = QLineEdit(frame)
        self.session_start_edit = QLineEdit(frame)
        self.session_timezone_edit = QLineEdit(frame)
        self.session_duration_edit = QLineEdit(frame)
        self.session_day_edit.setPlaceholderText("Monday, Tuesday, or ALL")
        self.session_key_edit.setVisible(False)
        for label, widget in (("Catalog source", self.session_source_edit), ("Frequency", self.session_frequency_combo), ("Service", self.session_service_edit), ("Recurrence", self.session_recurrence_edit), ("Day (UTC)", self.session_day_edit), ("Local start", self.session_start_edit), ("Timezone", self.session_timezone_edit), ("Duration minutes", self.session_duration_edit)):
            form.addRow(label, widget)
        actions = QHBoxLayout()
        save = QPushButton("Save Net Meeting", frame)
        cancel = QPushButton("Cancel", frame)
        save.clicked.connect(self.save_session)
        cancel.clicked.connect(lambda: frame.setVisible(False))
        actions.addWidget(save); actions.addWidget(cancel)
        form.addRow(actions)
        return frame

    def refresh_results(self) -> None:
        self._snapshot_service.request()
        self._snapshot_timer.start()

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
            self.status_label.setText(f"Directory refresh unavailable; showing the last coherent results. {completion.error}")
        if not self._snapshot_service.has_pending():
            self._snapshot_timer.stop()

    def _render_cached_results(self, *_: object) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        selected_key = self._selected_entry.net_entry_key if self._selected_entry else None
        rows = filter_entries(snapshot, search=self.search_edit.text(), active=self.status_filter.currentData())
        self.entry_table.setRowCount(len(rows))
        for row_index, entry in enumerate(rows):
            values = (
                entry.name,
                entry.scope or "—",
                source_label(snapshot, entry.source_key),
                self._listing_text(entry.retired, entry.active),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, entry)
                self.entry_table.setItem(row_index, column, item)
        self.status_label.setText(f"Showing {len(rows)} bounded directory result{'s' if len(rows) != 1 else ''} (maximum {MAX_RESULTS}).")
        if rows:
            selected_index = next((index for index, row in enumerate(rows) if row.net_entry_key == selected_key), 0)
            self.entry_table.selectRow(selected_index)
            # Selecting the same row need not emit selectionChanged after its
            # items were replaced. Refresh details from the new snapshot too.
            self._entry_selection_changed()
        else:
            self._selected_entry = None
            self.session_table.setRowCount(0)
            self._set_action_state()

    def _entry_selection_changed(self) -> None:
        items = self.entry_table.selectedItems()
        if not items:
            return
        entry = self.entry_table.item(items[0].row(), 0).data(Qt.UserRole)
        if not isinstance(entry, NetDirectoryEntry):
            return
        self._selected_entry = entry
        snapshot = self._snapshot
        if snapshot is None:
            return
        usage = snapshot.entry_usage.get(entry.net_entry_key)
        if usage is None:
            return
        self.detail_label.setText("\n".join((
            f"{entry.name} · {self._listing_text(entry.retired, entry.active)}",
            f"Catalog source: {source_label(snapshot, entry.source_key)}",
            f"Source region: {entry.scope or '—'}",
            f"Purpose: {entry.description or '—'}",
        )))
        self.entry_usage_label.setText(f"Used by: {usage.total_references}")
        self._refresh_sessions()
        self._set_action_state()

    def _refresh_sessions(self) -> None:
        if not self._selected_entry:
            self.session_table.setRowCount(0)
            return
        snapshot = self._snapshot
        if snapshot is None:
            return
        rows = snapshot.sessions_by_entry.get(self._selected_entry.net_entry_key, ())
        self.session_table.setRowCount(len(rows))
        frequencies = {item.frequency_resource_key: item for item in snapshot.frequencies}
        for row_index, session in enumerate(rows):
            frequency = frequencies.get(session.frequency_resource_key or "")
            frequency_text = (
                f"{frequency.label} · {frequency_where_text(frequency)}"
                if frequency is not None
                else "Frequency unavailable"
            )
            values = (
                session_when_text(session),
                session.service,
                frequency_text,
                self._listing_text(session.retired, session.active),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, session)
                self.session_table.setItem(row_index, column, item)
        self._selected_session = None

    def _session_selection_changed(self) -> None:
        items = self.session_table.selectedItems()
        if not items:
            return
        session = self.session_table.item(items[0].row(), 0).data(Qt.UserRole)
        if isinstance(session, NetDirectorySession):
            self._selected_session = session
            snapshot = self._snapshot
            if snapshot is None:
                return
            usage = snapshot.session_usage.get(session.net_session_key)
            if usage is None:
                return
            if usage.is_referenced:
                self.status_label.setText(f"Scheduled: this net meeting is used by {usage.total_references} HF schedule record(s). Open Schedule to review it.")
            else:
                self.status_label.setText("Not scheduled by this station.")
            self._set_action_state()

    def _selected_session_keys(self) -> tuple[str, ...]:
        keys = []
        for item in self.session_table.selectedItems():
            if item.column() != 0:
                continue
            session = item.data(Qt.UserRole)
            if isinstance(session, NetDirectorySession):
                keys.append(session.net_session_key)
        return tuple(dict.fromkeys(keys))

    def request_add_to_hf_nets(self) -> None:
        """Emit canonical session keys; HF Nets owns the destination and save."""
        keys = self._selected_session_keys()
        if not keys:
            self.status_label.setText("Select one or more published net meetings before adding to HF Nets.")
            return
        if len(keys) == 1 and self._session_is_scheduled(keys[0]):
            self.open_hf_schedule_requested.emit(keys[0])
            return
        self.add_to_hf_nets_requested.emit(keys)

    def _set_action_state(self) -> None:
        entry = self._selected_entry is not None
        session = self._selected_session is not None
        for button in (self.edit_entry_btn, self.clone_entry_btn, self.retire_entry_btn, self.delete_entry_btn, self.new_session_btn):
            button.setEnabled(entry)
        for button in (self.edit_session_btn, self.clone_session_btn, self.retire_session_btn, self.delete_session_btn):
            button.setEnabled(session)
        keys = self._selected_session_keys()
        scheduled = len(keys) == 1 and self._session_is_scheduled(keys[0])
        self.add_hf_net_btn.setText("Open Schedule" if scheduled else "Add to HF Nets")
        self.add_hf_net_btn.setEnabled(bool(keys))

    def _session_is_scheduled(self, session_key: str) -> bool:
        snapshot = self._snapshot
        usage = snapshot.session_usage.get(session_key) if snapshot is not None else None
        return bool(usage and usage.is_referenced)

    def _populate_source_combo(self, combo: QComboBox, selected_key: str | None = None) -> None:
        """Use the already loaded catalog metadata when editing a selected item."""
        key = str(selected_key or STATION_MANUAL_SOURCE_KEY).strip()
        sources = () if self._snapshot is None else tuple(
            source for source in self._snapshot.sources.values()
            if source.source_kind == "station" and not source.read_only
        )
        choices = [(source.label, source.source_key) for source in sources]
        known = {choice_key for _label, choice_key in choices}
        if selected_key and key not in known:
            choices.append((source_label(self._snapshot, key) if self._snapshot else "Catalog source unavailable", key))
        if key == STATION_MANUAL_SOURCE_KEY and key not in known:
            choices.append(("Station Resources", key))
        prior = combo.blockSignals(True)
        try:
            combo.clear()
            for label, source_key in sorted(choices, key=lambda choice: (choice[0].casefold(), choice[1])):
                combo.addItem(label, source_key)
            combo.setCurrentIndex(combo.findData(key))
        finally:
            combo.blockSignals(prior)

    def _populate_frequency_combo(self, selected_key: str | None = None) -> None:
        key = str(selected_key or "").strip()
        rows = () if self._snapshot is None else self._snapshot.frequencies
        prior = self.session_frequency_combo.blockSignals(True)
        try:
            self.session_frequency_combo.clear()
            self.session_frequency_combo.addItem("Choose a frequency", None)
            for resource in rows:
                self.session_frequency_combo.addItem(
                    f"{resource.label} · {frequency_where_text(resource)}", resource.frequency_resource_key
                )
            if key:
                index = self.session_frequency_combo.findData(key)
                if index < 0:
                    self.session_frequency_combo.addItem("Frequency unavailable", key)
                    index = self.session_frequency_combo.count() - 1
                self.session_frequency_combo.setCurrentIndex(index)
        finally:
            self.session_frequency_combo.blockSignals(prior)

    def begin_new_entry(self) -> None:
        self._editing_entry_key = None
        self.entry_key_edit.setReadOnly(False)
        for field in (self.entry_name_edit, self.entry_scope_edit, self.entry_description_edit): field.clear()
        self._populate_source_combo(self.entry_source_edit)
        self.entry_key_edit.setText(new_net_entry_key())
        self.entry_editor.setVisible(True)

    def begin_edit_entry(self) -> None:
        if not self._selected_entry: return
        entry = self._selected_entry; self._editing_entry_key = entry.net_entry_key
        self.entry_key_edit.setText(entry.net_entry_key); self.entry_key_edit.setReadOnly(True)
        self._populate_source_combo(self.entry_source_edit, entry.source_key); self.entry_name_edit.setText(entry.name)
        self.entry_scope_edit.setText(entry.scope or ""); self.entry_description_edit.setText(entry.description or "")
        self.entry_editor.setVisible(True)

    def begin_clone_entry(self) -> None:
        if not self._selected_entry: return
        self._editing_entry_key = None
        self.entry_key_edit.setText(new_net_entry_key()); self.entry_key_edit.setReadOnly(False)
        self._populate_source_combo(self.entry_source_edit); self.entry_name_edit.setText(f"{self._selected_entry.name} copy")
        self.entry_scope_edit.setText(self._selected_entry.scope or ""); self.entry_description_edit.setText(self._selected_entry.description or "")
        self.entry_editor.setVisible(True)

    def save_entry(self) -> None:
        prior = self._selected_entry if self._editing_entry_key else None
        try:
            if self.entry_source_edit.currentData() == STATION_MANUAL_SOURCE_KEY:
                self.store.ensure_station_source()
            entry = NetDirectoryEntry(self.entry_key_edit.text(), str(self.entry_source_edit.currentData() or ""), self.entry_name_edit.text(), description=self.entry_description_edit.text().strip() or None, scope=self.entry_scope_edit.text().strip() or None, active=prior.active if prior else True, retired=prior.retired if prior else False, replacement_net_entry_key=prior.replacement_net_entry_key if prior else None)
            self.store.update_net_entry(entry) if self._editing_entry_key else self.store.create_net_entry(entry)
        except (CatalogValidationError, ReadOnlyResourceError, ValueError) as exc:
            self.status_label.setText(f"Cannot save directory entry: {exc}"); return
        self.entry_editor.setVisible(False); self.status_label.setText("Directory entry saved."); self.refresh_results()

    def retire_entry(self) -> None:
        if not self._selected_entry: return
        try: self.store.retire_net_entry(self._selected_entry.net_entry_key)
        except (CatalogValidationError, ReadOnlyResourceError) as exc: self.status_label.setText(f"Cannot retire directory entry: {exc}"); return
        self.status_label.setText("Directory entry retired; historical net-meeting references remain."); self.refresh_results()

    def delete_entry(self) -> None:
        if not self._selected_entry: return
        usage = self.store.net_entry_usage(self._selected_entry.net_entry_key)
        if usage.is_referenced: self.status_label.setText(f"Cannot delete: used by {usage.total_references} record(s). Retire it instead."); return
        if QMessageBox.question(self, "Delete directory entry", f"Delete '{self._selected_entry.name}'? This cannot be undone.") != QMessageBox.Yes: return
        try: self.store.delete_net_entry_if_unreferenced(self._selected_entry.net_entry_key)
        except (ReferencedResourceError, ReadOnlyResourceError) as exc: self.status_label.setText(f"Cannot delete directory entry: {exc}"); return
        self._selected_entry = None; self.status_label.setText("Directory entry deleted."); self.refresh_results()

    def begin_new_session(self) -> None:
        if not self._selected_entry: return
        self._editing_session_key = None; self.session_key_edit.setReadOnly(False); self.session_key_edit.setText(new_net_session_key())
        for field in (self.session_recurrence_edit, self.session_day_edit, self.session_start_edit, self.session_timezone_edit, self.session_duration_edit): field.clear()
        self._populate_source_combo(self.session_source_edit); self.session_service_edit.setCurrentText("AMATEUR"); self.session_editor.setVisible(True)
        self.session_frequency_edit.clear()
        self._populate_frequency_combo()

    def begin_edit_session(self) -> None:
        if not self._selected_session: return
        session = self._selected_session; self._editing_session_key = session.net_session_key
        self.session_key_edit.setText(session.net_session_key); self.session_key_edit.setReadOnly(True); self._populate_source_combo(self.session_source_edit, session.source_key)
        self.session_frequency_edit.setText(session.frequency_resource_key or "")
        self._populate_frequency_combo(session.frequency_resource_key); self.session_service_edit.setCurrentText(session.service); self.session_recurrence_edit.setText(session.recurrence or "")
        self.session_day_edit.setText(getattr(session, "day_utc", None) or "")
        self.session_start_edit.setText(session.local_start_time or ""); self.session_timezone_edit.setText(session.timezone or ""); self.session_duration_edit.setText(str(session.duration_minutes or "")); self.session_editor.setVisible(True)

    def begin_clone_session(self) -> None:
        if not self._selected_session: return
        self.begin_edit_session(); self._editing_session_key = None; self.session_key_edit.setReadOnly(False); self.session_key_edit.setText(new_net_session_key())
        self._populate_source_combo(self.session_source_edit)

    def save_session(self) -> None:
        if not self._selected_entry: return
        prior = self._selected_session if self._editing_session_key else None
        try:
            duration = int(self.session_duration_edit.text()) if self.session_duration_edit.text().strip() else None
            if self.session_source_edit.currentData() == STATION_MANUAL_SOURCE_KEY:
                self.store.ensure_station_source()
            frequency_key = str(self.session_frequency_combo.currentData() or "") or None
            self.session_frequency_edit.setText(frequency_key or "")
            session = NetDirectorySession(self.session_key_edit.text(), self._selected_entry.net_entry_key, str(self.session_source_edit.currentData() or ""), self.session_service_edit.currentText(), frequency_key, recurrence=self.session_recurrence_edit.text().strip() or None, day_utc=self.session_day_edit.text().strip() or None, local_start_time=self.session_start_edit.text().strip() or None, timezone=self.session_timezone_edit.text().strip() or None, duration_minutes=duration, active=prior.active if prior else True, retired=prior.retired if prior else False, replacement_net_session_key=prior.replacement_net_session_key if prior else None, fldigi_tuning_json=prior.fldigi_tuning_json if prior else "{}")
            self.store.update_session(session) if self._editing_session_key else self.store.create_session(session)
        except (CatalogValidationError, ReadOnlyResourceError, ValueError) as exc:
            self.status_label.setText(f"Cannot save net meeting: {exc}"); return
        self.session_editor.setVisible(False); self.status_label.setText("Published net meeting saved."); self._refresh_sessions()

    def retire_session(self) -> None:
        if not self._selected_session: return
        try: self.store.retire_session(self._selected_session.net_session_key)
        except (CatalogValidationError, ReadOnlyResourceError) as exc: self.status_label.setText(f"Cannot retire net meeting: {exc}"); return
        self.status_label.setText("Published net meeting retired; existing schedules remain reviewable."); self._refresh_sessions()

    def delete_session(self) -> None:
        if not self._selected_session: return
        usage = self.store.session_usage(self._selected_session.net_session_key)
        if usage.is_referenced: self.status_label.setText(f"Cannot delete: used by {usage.total_references} record(s). Retire it instead."); return
        if QMessageBox.question(self, "Delete published net meeting", "Delete this unreferenced net meeting? This cannot be undone.") != QMessageBox.Yes: return
        try: self.store.delete_session_if_unreferenced(self._selected_session.net_session_key)
        except (ReferencedResourceError, ReadOnlyResourceError) as exc: self.status_label.setText(f"Cannot delete net meeting: {exc}"); return
        self._selected_session = None; self.status_label.setText("Published net meeting deleted."); self._refresh_sessions()

    @staticmethod
    def _listing_text(retired: bool, active: bool) -> str:
        if retired:
            return "Retired"
        return "Listed" if active else "Unlisted"


__all__ = ["NetDirectoryView", "new_net_entry_key", "new_net_session_key"]
