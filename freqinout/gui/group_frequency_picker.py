"""Choose catalog frequencies for one operating group without changing settings."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHeaderView,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout)

from freqinout.core.frequency_reference import frequency_programming_text
from freqinout.core.local_group_frequency import catalog_reference_state, snapshot_for_profile
from freqinout.gui.radio_import_dialog import RadioRecordTable
from freqinout.gui.theme import (ThemedItemCheckDelegate, active_app_theme,
    button_height_for_font, button_style, control_height_for_font, multiline_height_for_font)

_PAGE = 200
_MAX_ASSIGNED = 2000
_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="group-frequency")


def _frequency(hz):
    return (f"{hz / 1000000:.6f}".rstrip("0").rstrip(".") + " MHz") if hz is not None else "—"


def _matches(resource, query):
    needle = query.strip().casefold()
    if not needle:
        return True
    values = [resource.label, resource.locality, resource.band, resource.channel,
              resource.coverage, resource.notes]
    for hz in (resource.receive_hz, resource.transmit_hz, resource.center_hz,
               resource.lower_hz, resource.upper_hz):
        if hz is not None:
            values.extend((str(hz), f"{hz / 1000000:.6f}", _frequency(hz)))
    return any(needle in str(value or "").casefold() for value in values)


def _load_page(store, query, service, offset):
    return tuple(store.list_frequencies(search=query, service=service, active=True,
                                        limit=_PAGE, offset=offset))


def _load_review(store, profiles):
    keys = tuple(dict.fromkeys(str(row.get("frequency_resource_key", "")).strip() for row in profiles
                               if str(row.get("frequency_resource_key", "")).strip()))
    if len(keys) > _MAX_ASSIGNED:
        raise ValueError(f"Review supports at most {_MAX_ASSIGNED} assigned frequencies at once.")
    return dict(store.frequencies_by_keys(keys)) if keys else {}


class GroupFrequencyPickerDialog(QDialog):
    """Select new resources or explicitly accept current versions of assignments."""

    def __init__(self, store, group_name, profiles, *, review_only=False, parent=None):
        super().__init__(parent)
        self._store = store
        self.group_name = str(group_name).strip()
        self.review_only = review_only
        self._profiles = tuple(dict(p) for p in profiles if str(p.get("group", "")).strip().casefold() == self.group_name.casefold() and p.get("frequency_resource_key"))
        self._assigned = {str(p["frequency_resource_key"]).strip(): p for p in self._profiles}
        self._selected = {}
        self.selected_resources = ()
        self._rows = ()
        self._offset = 0
        self._query = ""
        self._generation = 0
        self._future = None
        self._populating = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self.search)
        self._column_timer = QTimer(self)
        self._column_timer.setSingleShot(True)
        self._column_timer.timeout.connect(self._fit_columns)
        self.setWindowTitle("Review group frequencies" if review_only else "Add group frequencies")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        context = QLabel(("Review assigned frequencies for " if review_only else "Choose frequencies for ") + self.group_name)
        context.setWordWrap(True)
        context.setTextFormat(Qt.PlainText)
        layout.addWidget(context)
        controls = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Name, frequency, or locality")
        self.search_edit.textChanged.connect(self._schedule_search)
        self.search_edit.returnPressed.connect(self.search)
        controls.addWidget(self.search_edit, 1)
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.search)
        controls.addWidget(self.search_button)
        controls.addWidget(QLabel("Service"))
        self.service_combo = QComboBox()
        self.service_combo.addItem("All services", None)
        for service in ("AMATEUR", "GMRS", "FRS", "MURS", "OTHER"):
            self.service_combo.addItem(service, service)
        self.service_combo.currentIndexChanged.connect(self.search)
        controls.addWidget(self.service_combo)
        layout.addLayout(controls)
        self.status_label = QLabel("Loading frequencies…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.table = RadioRecordTable(0, 7)
        self.table.setAccessibleName("Group frequencies")
        self.table.setItemDelegateForColumn(0, ThemedItemCheckDelegate(self.table))
        self.table.setHorizontalHeaderLabels(["Select", "Name / locality", "RX", "TX", "Mode", "Tone", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for col in (0, 2, 3, 4):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        for col in (5, 6):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.Interactive)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._show_details)
        layout.addWidget(self.table, 1)
        paging = QHBoxLayout()
        self.selection_label = QLabel()
        paging.addWidget(self.selection_label)
        paging.addStretch()
        self.previous_button = QPushButton("Previous")
        self.previous_button.clicked.connect(lambda: self._page(-1))
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(lambda: self._page(1))
        paging.addWidget(self.previous_button)
        paging.addWidget(self.next_button)
        layout.addLayout(paging)
        self.details_toggle = QToolButton()
        self.details_toggle.setText("Frequency details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.RightArrow)
        self.details_toggle.toggled.connect(self._toggle_details)
        layout.addWidget(self.details_toggle)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.hide()
        layout.addWidget(self.details)
        footer = QHBoxLayout()
        footer.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.commit_button = QPushButton()
        self.commit_button.clicked.connect(self._commit)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.commit_button)
        layout.addLayout(footer)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._poll)
        self.apply_theme()
        self._update_actions()
        self._request()

    def apply_theme(self):
        theme = active_app_theme()
        for button, role in ((self.search_button, "secondary"), (self.previous_button, "secondary"),
                             (self.next_button, "secondary"), (self.cancel_button, "muted"),
                             (self.commit_button, "primary")):
            button.setStyleSheet(button_style(role, theme))
            button.setMinimumHeight(button_height_for_font(button))
        for control in (self.search_edit, self.service_combo):
            control.setMinimumHeight(control_height_for_font(control))
        self.details_toggle.setMinimumHeight(button_height_for_font(self.details_toggle))
        self.table.verticalHeader().setDefaultSectionSize(control_height_for_font(self.table, vertical_padding=10, floor=1))
        self.table.horizontalHeader().setMinimumHeight(control_height_for_font(self.table.horizontalHeader(), vertical_padding=10, floor=1))
        self.details.setMaximumHeight(multiline_height_for_font(self.details, visible_lines=4, vertical_padding=12))
        self._column_timer.start(0)

    def _fit_columns(self):
        metrics = self.table.fontMetrics()
        header = self.table.horizontalHeader()
        padding = metrics.horizontalAdvance("MM")
        status = metrics.horizontalAdvance("Already added") + padding
        tone = metrics.horizontalAdvance("TX CTCSS 123.0 Hz") + padding
        self.table.setColumnWidth(6, status)
        self.table.setColumnWidth(5, tone)
        primary = sum(header.sectionSize(column) for column in (0, 2, 3)) + status
        primary += metrics.horizontalAdvance("Frequency name and locality") + padding
        available = self.table.viewport().width()
        mode = metrics.horizontalAdvance("Mode") + padding
        self.table.setColumnHidden(4, primary + mode > available)
        self.table.setColumnHidden(5, primary + mode + tone > available)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_column_timer"):
            self._column_timer.start(0)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.PaletteChange) and hasattr(self, "details"):
            self.apply_theme()

    def _toggle_details(self, visible):
        self.details_toggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
        self.details.setVisible(visible)

    def _schedule_search(self, *_):
        # Keystrokes only schedule work and fence previous results. All catalog
        # I/O remains in the bounded worker after the operator pauses typing.
        self._generation += 1
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self._search_timer.start()

    def search(self, *_):
        self._search_timer.stop()
        self._query = self.search_edit.text().strip()
        self._offset = 0
        self._request()

    def _page(self, direction):
        if self._search_timer.isActive():
            self.search()
            return
        self._offset = max(0, self._offset + direction * _PAGE)
        self._request()

    def _request(self):
        self._generation += 1
        if self._future is not None:
            self._future.cancel()
        self.status_label.setText("Loading frequencies…")
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)
        if self.review_only:
            self._future = _WORKERS.submit(_load_review, self._store, self._profiles)
        else:
            self._future = _WORKERS.submit(_load_page, self._store, self._query, self.service_combo.currentData(), self._offset)
        self._pending_generation = self._generation
        self._timer.start()

    def _poll(self):
        future = self._future
        if future is None or not future.done():
            return
        generation = self._pending_generation
        self._future = None
        self._timer.stop()
        if generation != self._generation:
            return
        try:
            result = future.result()
        except Exception as exc:
            self.status_label.setText(f"Could not load frequencies: {exc}")
            return
        if self.review_only:
            # A new catalog version requires a fresh, explicit selection.
            for key, selected in tuple(self._selected.items()):
                current = result.get(key)
                if current != selected or not current.active or current.retired:
                    self._selected.pop(key, None)
            filtered = []
            for key, profile in self._assigned.items():
                current = result.get(key)
                saved = snapshot_for_profile(profile)
                item = current or saved
                if self.service_combo.currentData() and (not item or item.service != self.service_combo.currentData()):
                    continue
                if self._query and (not item or not _matches(item, self._query)):
                    continue
                filtered.append((key, current, saved, profile))
            self._rows = tuple(filtered[self._offset:self._offset + _PAGE])
            has_next = self._offset + _PAGE < len(filtered)
        else:
            self._rows = tuple((r.frequency_resource_key, r, None, self._assigned.get(r.frequency_resource_key)) for r in result)
            has_next = len(result) == _PAGE
        self._render(has_next)

    def _render(self, has_next):
        self._populating = True
        self.table.setRowCount(len(self._rows))
        for index, (key, current, saved, profile) in enumerate(self._rows):
            resource = current or saved
            available = bool(current and current.active and not current.retired)
            state = catalog_reference_state(profile, current) if self.review_only else None
            band_reference = bool(current and current.resource_kind == "band_range")
            selectable = available and not band_reference and (
                state in ("changed", "invalid") if self.review_only else not profile)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsSelectable | (Qt.ItemIsEnabled | Qt.ItemIsUserCheckable if selectable else Qt.NoItemFlags))
            check.setData(Qt.UserRole, key)
            check.setCheckState(Qt.Checked if key in self._selected and selectable else Qt.Unchecked)
            self.table.setItem(index, 0, check)
            if not self.review_only and profile:
                status = "Already added"
            elif band_reference:
                status = "Band reference only"
            elif self.review_only:
                status = {
                    "missing": "Missing from catalog; saved version retained",
                    "retired": "Retired or inactive; saved version retained",
                    "invalid": "Saved version unavailable; review to repair",
                    "changed": "Updated",
                    "current": "Current",
                }[state]
            else:
                status = "Already added" if profile else "Available"
            name = resource.label if resource else key
            if resource and resource.locality:
                name += f" · {resource.locality}"
            values = (name, _frequency(resource.receive_hz or resource.center_hz) if resource else "—",
                      _frequency(resource.transmit_hz) if resource else "—", resource.mode or "—" if resource else "—",
                      resource.tone or "—" if resource else "—", status)
            for column, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(index, column, item)
        self._populating = False
        self._column_timer.start(0)
        self.status_label.setText(f"Page {self._offset // _PAGE + 1} · {len(self._rows)} frequencies shown" if self._rows else "No matching frequencies. Change the search or service.")
        self.previous_button.setEnabled(self._offset > 0)
        self.next_button.setEnabled(has_next)
        if self._rows:
            self.table.selectRow(0)
            self._show_details()
        else:
            self.details.clear()
        self._update_actions()

    def _item_changed(self, item):
        if self._populating or item.column() != 0:
            return
        row = item.row()
        if row >= len(self._rows):
            return
        key, resource, _, _ = self._rows[row]
        if item.checkState() == Qt.Checked and resource and item.flags() & Qt.ItemIsUserCheckable:
            self._selected[key] = resource
        else:
            self._selected.pop(key, None)
        self._update_actions()

    def _show_details(self):
        index = self.table.currentRow()
        if index < 0 or index >= len(self._rows):
            self.details.clear()
            return
        key, current, saved, profile = self._rows[index]
        def describe(label, resource):
            if resource is None:
                return f"{label}: unavailable"
            return f"{label}: {frequency_programming_text(resource)}"
        lines = [describe("Current", current)]
        if self.review_only:
            lines.insert(0, describe("Saved", saved))
            if not current or not current.active or current.retired:
                lines.append("The saved assignment remains in the group; it cannot be updated from this catalog entry.")
        self.details.setPlainText("\n\n".join(lines))

    def _update_actions(self):
        count = len(self._selected)
        self.selection_label.setText(f"{count} selected")
        self.commit_button.setText(f"Accept {count} updates for {self.group_name}" if self.review_only else f"Add {count} frequencies to {self.group_name}")
        self.commit_button.setEnabled(count > 0)

    def _commit(self):
        if self._selected:
            self.selected_resources = tuple(self._selected.values())
            self.accept()

    def done(self, result):
        self._generation += 1
        self._search_timer.stop()
        self._timer.stop()
        if self._future is not None:
            self._future.cancel()
        if result != QDialog.DialogCode.Accepted:
            self.selected_resources = ()
        super().done(result)
