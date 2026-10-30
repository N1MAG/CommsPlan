"""Review radio programming files before adding frequencies to the catalog."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
import io
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QHeaderView, QPlainTextEdit, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from freqinout.core.radio_programming_import import parse_radio_csv
from freqinout.gui.theme import (ThemedItemCheckDelegate, active_app_theme, button_height_for_font,
    button_style, control_height_for_font, multiline_height_for_font)

_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="radio-import")
MAX_FILES = 20
MAX_PREVIEW_ROWS = 2000


class RadioRecordTable(QTableWidget):
    """Space toggles the highlighted record from any column."""

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Select) and event.modifiers() == Qt.NoModifier:
            item = self.item(self.currentRow(), 0)
            if self.isEnabled() and item is not None and item.flags() & Qt.ItemIsEnabled and item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)
                event.accept()
                return
        super().keyPressEvent(event)


def _read_headers(path):
    """Read only bounded file data, on the preview worker, even for invalid mappings."""
    with open(path, "rb") as stream:
        data = stream.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("File exceeds the 10 MiB limit")
    content = data.decode("utf-8-sig")
    dialect = csv.Sniffer().sniff(content[:8192], delimiters=",;\t")
    return tuple(h.strip() for h in next(csv.reader(io.StringIO(content, newline=""), dialect, strict=True)))


def _frequency(value):
    if value is None:
        return "Not specified"
    return f"{value / 1000000:.6f}".rstrip("0").rstrip(".") + " MHz"


def _preview_files(paths, service, mapping, frequency_unit):
    previews, failures, count, headers = [], [], 0, []
    for path in paths:
        try:
            for header in _read_headers(path):
                if header and header not in headers:
                    headers.append(header)
        except (OSError, UnicodeError, csv.Error, StopIteration, ValueError):
            pass  # The parser reports authoritative file errors.
        try:
            if frequency_unit is not None:
                preview = parse_radio_csv(path, service=service, mapping=mapping, frequency_unit=frequency_unit)
            else:
                preview = parse_radio_csv(path, service=service)
            count += len(preview.rows)
            if count > MAX_PREVIEW_ROWS:
                raise ValueError(f"Preview exceeds {MAX_PREVIEW_ROWS} rows. Choose smaller files.")
            previews.append((path, preview))
        except (ValueError, OSError) as exc:
            failures.append((path, str(exc)))
    return previews, failures, headers


class RadioImportDialog(QDialog):
    """A bounded, asynchronous import review; no radio or schedule is changed."""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self._store = store
        self._paths = ()
        self._entries = []
        self._generation = 0
        self._future = None
        self._operation = None
        self._committing = False
        self._completed = False
        self._mapping = {}
        self._frequency_unit = None
        self.setWindowTitle("Import radio frequencies")
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        purpose = QLabel("Choose files, review records, and import frequencies for manual programming.")
        purpose.setToolTip("Identical records are skipped. Variants from different export formats are kept separately.")
        purpose.setWordWrap(True)
        purpose.setTextFormat(Qt.PlainText)
        layout.addWidget(purpose)
        controls = QHBoxLayout()
        self.choose_button = QPushButton("Choose CSV/TSV files…")
        self.choose_button.clicked.connect(self._choose_files)
        controls.addWidget(self.choose_button)
        controls.addWidget(QLabel("Service"))
        self.service_combo = QComboBox()
        self.service_combo.addItems(["OTHER", "AMATEUR", "GMRS", "FRS", "MURS"])
        self.service_combo.currentTextChanged.connect(self._service_changed)
        controls.addWidget(self.service_combo)
        self.mapping_toggle = QPushButton("Column mapping…")
        self.mapping_toggle.clicked.connect(self._open_mapping)
        controls.addWidget(self.mapping_toggle)
        controls.addStretch()
        layout.addLayout(controls)
        self.mapping_dialog = QDialog(self)
        self.mapping_dialog.setWindowTitle("Column mapping")
        mapping_dialog_layout = QVBoxLayout(self.mapping_dialog)
        self.mapping_panel = QWidget(self.mapping_dialog)
        mapping_layout = QGridLayout(self.mapping_panel)
        explanation = QLabel("Map columns for generic files; the mapping applies to every selected file.")
        explanation.setWordWrap(True)
        explanation.setTextFormat(Qt.PlainText)
        mapping_layout.addWidget(explanation, 0, 0, 1, 4)
        self.mapping_combos = {}
        fields = (("receive_hz", "Receive frequency"), ("transmit_hz", "Transmit frequency"),
                  ("label", "Channel name"), ("mode", "Mode"), ("comment", "Comment"))
        for index, (field, label) in enumerate(fields):
            combo = QComboBox()
            combo.addItem("Automatic", "")
            self.mapping_combos[field] = combo
            row, column = divmod(index, 2)
            mapping_layout.addWidget(QLabel(label), row + 1, column * 2)
            mapping_layout.addWidget(combo, row + 1, column * 2 + 1)
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["MHz", "Hz"])
        mapping_layout.addWidget(QLabel("Frequency units"), 3, 2)
        mapping_layout.addWidget(self.unit_combo, 3, 3)
        mapping_layout.setColumnStretch(1, 1)
        mapping_layout.setColumnStretch(3, 1)
        self.review_mapping_button = QPushButton("Review mapping")
        self.review_mapping_button.clicked.connect(self._apply_mapping)
        mapping_layout.addWidget(self.review_mapping_button, 4, 3, alignment=Qt.AlignRight)
        mapping_dialog_layout.addWidget(self.mapping_panel)
        self.status_label = QLabel("Choose one or more files to preview.")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_scroll = QScrollArea()
        self.status_scroll.setFrameShape(QFrame.NoFrame)
        self.status_scroll.setWidgetResizable(True)
        self.status_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.status_scroll.setWidget(self.status_label)
        self.status_scroll.setMaximumHeight(multiline_height_for_font(self.status_label, visible_lines=3, vertical_padding=8))
        layout.addWidget(self.status_scroll)
        selection_controls = QHBoxLayout()
        self.selection_label = QLabel("0 of 0 records selected")
        selection_controls.addWidget(self.selection_label)
        selection_controls.addStretch()
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(lambda: self._set_all_selected(True))
        selection_controls.addWidget(self.select_all_button)
        self.clear_selection_button = QPushButton("Clear selection")
        self.clear_selection_button.clicked.connect(lambda: self._set_all_selected(False))
        selection_controls.addWidget(self.clear_selection_button)
        layout.addLayout(selection_controls)
        self.table = RadioRecordTable(0, 5)
        self.table.setItemDelegateForColumn(0, ThemedItemCheckDelegate(self.table))
        self.table.setAccessibleName("Radio frequency records; uncheck records to exclude them")
        self.table.setHorizontalHeaderLabels(["Import", "File / row", "Channel", "Receive", "Review"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for column in (0, 3):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._show_details)
        self.table.itemChanged.connect(self._update_import_enabled)
        layout.addWidget(self.table, 3)
        self.details_toggle = QToolButton()
        self.details_toggle.setText("Selected record details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.RightArrow)
        self.details_toggle.toggled.connect(self._toggle_details)
        layout.addWidget(self.details_toggle)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select a row to review its values and validation messages.")
        self.details.hide()
        self.details.setMaximumHeight(multiline_height_for_font(self.details, visible_lines=7, vertical_padding=12))
        layout.addWidget(self.details)
        footer = QHBoxLayout()
        footer.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.import_button = QPushButton("Import 0 records")
        self.import_button.clicked.connect(self._commit)
        self.import_button.setEnabled(False)
        theme = active_app_theme()
        self.choose_button.setStyleSheet(button_style("secondary", theme))
        self.cancel_button.setStyleSheet(button_style("muted", theme))
        self.import_button.setStyleSheet(button_style("primary", theme))
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.import_button)
        layout.addLayout(footer)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._poll)
        self.apply_theme()
        self._update_import_enabled()

    def apply_theme(self):
        theme = active_app_theme()
        for button, role in ((self.choose_button, "secondary"), (self.mapping_toggle, "secondary"),
                             (self.select_all_button, "secondary"), (self.clear_selection_button, "secondary"),
                             (self.review_mapping_button, "primary"), (self.cancel_button, "muted"),
                             (self.import_button, "primary")):
            button.setStyleSheet(button_style(role, theme))
            button.setMinimumHeight(button_height_for_font(button))
        for combo in (self.service_combo, self.unit_combo, *self.mapping_combos.values()):
            combo.setMinimumHeight(control_height_for_font(combo))
        self.details_toggle.setMinimumHeight(button_height_for_font(self.details_toggle))
        self.table.verticalHeader().setDefaultSectionSize(control_height_for_font(self.table, vertical_padding=10, floor=1))
        self.table.horizontalHeader().setMinimumHeight(control_height_for_font(self.table.horizontalHeader(), vertical_padding=10, floor=1))
        self.status_scroll.setMaximumHeight(multiline_height_for_font(self.status_label, visible_lines=3, vertical_padding=8))
        self.details.setMaximumHeight(multiline_height_for_font(self.details, visible_lines=3 if self.height() < 650 else 7, vertical_padding=12))
        self.table.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "details"):
            self.details.setMaximumHeight(multiline_height_for_font(self.details, visible_lines=3 if self.height() < 650 else 7, vertical_padding=12))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.PaletteChange) and hasattr(self, "details"):
            self.apply_theme()

    def _choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose radio programming files", "", "Radio files (*.csv *.tsv);;All files (*)")
        if paths:
            self.preview_paths(paths)

    def preview_paths(self, paths):
        """Start a new preview, replacing any earlier pending preview."""
        if self._committing or self._completed:
            return
        paths = tuple(map(str, paths))
        self._generation += 1
        if self._future is not None:
            self._future.cancel()
        self._paths = paths
        self._entries = []
        self.table.setRowCount(0)
        self.details.clear()
        self._update_import_enabled()
        if not paths:
            self.status_label.setText("Choose one or more files to preview.")
            return
        if len(paths) > MAX_FILES:
            self.status_label.setText(f"Choose no more than {MAX_FILES} files at a time.")
            return
        self.status_label.setText("Reading files…")
        self._operation = (self._generation, "preview")
        self._future = _WORKERS.submit(_preview_files, paths, self.service_combo.currentText(), dict(self._mapping), self._frequency_unit)
        self._update_import_enabled()
        self._timer.start()

    def _open_mapping(self):
        self.mapping_dialog.show()
        self.mapping_dialog.raise_()
        self.mapping_dialog.activateWindow()

    def _toggle_details(self, visible):
        self.details_toggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
        self.details.setVisible(visible)

    def _set_all_selected(self, selected):
        self.table.blockSignals(True)
        for index in range(self.table.rowCount()):
            item = self.table.item(index, 0)
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(Qt.Checked if selected else Qt.Unchecked)
        self.table.blockSignals(False)
        self._update_import_enabled()

    def _apply_mapping(self):
        self._mapping = {field: combo.currentData() for field, combo in self.mapping_combos.items() if combo.currentData()}
        self._frequency_unit = self.unit_combo.currentText()
        self.mapping_dialog.hide()
        if self._paths:
            self.preview_paths(self._paths)

    def _service_changed(self, _service):
        if self._paths and not self._committing and not self._completed:
            self.preview_paths(self._paths)

    def _poll(self):
        future = self._future
        if future is None or not future.done():
            return
        generation, operation = self._operation
        self._future = None
        self._timer.stop()
        if generation != self._generation:
            return
        try:
            result = future.result()
        except Exception as exc:
            self._committing = False
            self.choose_button.setEnabled(True)
            self.service_combo.setEnabled(True)
            self.mapping_toggle.setEnabled(True)
            self.mapping_panel.setEnabled(True)
            self.cancel_button.setEnabled(True)
            self.table.setEnabled(True)
            self.status_label.setText(f"{operation.capitalize()} failed: {exc}")
            self._update_import_enabled()
            return
        if operation == "preview":
            self._display_preview(*result)
        else:
            self._committing = False
            self._completed = True
            added, skipped = result
            self.status_label.setText(f"Import complete: {added} added, {skipped} skipped (already present or duplicated).")
            self.cancel_button.setText("Close")
            self.cancel_button.setEnabled(True)

    def _display_preview(self, previews, failures, headers):
        for combo in self.mapping_combos.values():
            selected = combo.currentData()
            combo.clear()
            combo.addItem("Automatic", "")
            for header in headers:
                combo.addItem(header, header)
            index = combo.findData(selected)
            combo.setCurrentIndex(max(index, 0))
        summary = [f"{len(previews)} of {len(self._paths)} files read"]
        summary.extend(f"{Path(path).name}: {preview.format_name}, {len(preview.rows)} rows" for path, preview in previews)
        summary.extend(f"{Path(path).name}: {error}" for path, error in failures)
        self.status_label.setText("\n".join(summary))
        self._entries = [(path, row) for path, preview in previews for row in preview.rows]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._entries))
        self.table.setHorizontalHeaderItem(1, QTableWidgetItem("Row" if len(self._paths) == 1 else "File / row"))
        for index, (path, row) in enumerate(self._entries):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | (Qt.ItemIsUserCheckable if row.resource is not None and not row.errors else Qt.NoItemFlags))
            check.setCheckState(Qt.Checked if row.resource is not None and not row.errors else Qt.Unchecked)
            self.table.setItem(index, 0, check)
            resource = row.resource
            review = "; ".join([*(f"Error: {e}" for e in row.errors), *(f"Warning: {w}" for w in row.warnings)]) or "Ready"
            values = (str(row.row_number) if len(self._paths) == 1 else f"{Path(path).name} / {row.row_number}", resource.label if resource else "", _frequency(resource.receive_hz or resource.center_hz) if resource else "", review)
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(index, col, item)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        if len(self._paths) > 1:
            metrics = self.table.fontMetrics()
            self.table.setColumnWidth(1, min(self.table.columnWidth(1), metrics.horizontalAdvance("M" * 20)))
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        if self._entries:
            self.table.selectRow(0)
            self._show_details()
        self._update_import_enabled()

    def _show_details(self):
        index = self.table.currentRow()
        if index < 0 or index >= len(self._entries):
            self.details.clear()
            return
        path, row = self._entries[index]
        lines = [f"File: {path}", f"Row: {row.row_number}"]
        if row.resource:
            resource = row.resource
            lines.extend([f"Receive: {_frequency(resource.receive_hz or resource.center_hz)}", f"Transmit: {_frequency(resource.transmit_hz)}", f"Mode: {resource.mode or 'not specified'}", f"Bandwidth: {str(resource.bandwidth_hz) + ' Hz' if resource.bandwidth_hz is not None else 'not specified'}", f"Tone: {resource.tone or 'not specified'}"])
        lines.extend(f"Error: {error}" for error in row.errors)
        lines.extend(f"Warning: {warning}" for warning in row.warnings)
        lines.append("Source values:")
        lines.extend(f"{key}: {value}" for key, value in row.raw_values.items())
        self.details.setPlainText("\n".join(lines))

    def _update_import_enabled(self, *_):
        valid = [self.table.item(i, 0) for i in range(self.table.rowCount())
                 if self.table.item(i, 0) is not None and self.table.item(i, 0).flags() & Qt.ItemIsUserCheckable]
        selected = sum(item.checkState() == Qt.Checked for item in valid)
        self.selection_label.setText(f"{selected} of {len(valid)} records selected")
        self.import_button.setText(f"Import {selected} {'record' if selected == 1 else 'records'}")
        available = not self._committing and not self._completed and self._future is None
        self.import_button.setEnabled(available and selected > 0)
        self.select_all_button.setEnabled(available and bool(valid))
        self.clear_selection_button.setEnabled(available and bool(valid))

    def _commit(self):
        if self._committing or self._completed or self._future is not None:
            return
        resources = [row.resource for i, (_, row) in enumerate(self._entries) if row.resource is not None and not row.errors and self.table.item(i, 0).checkState() == Qt.Checked]
        if not resources:
            return
        self._committing = True
        self.table.setEnabled(False)
        self.choose_button.setEnabled(False)
        self.service_combo.setEnabled(False)
        self.mapping_toggle.setEnabled(False)
        self.mapping_panel.setEnabled(False)
        self.mapping_dialog.hide()
        self._update_import_enabled()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Importing selected frequencies…")
        self._generation += 1
        self._operation = (self._generation, "import")
        self._future = _WORKERS.submit(self._store.import_radio_frequencies, resources)
        self._timer.start()

    def reject(self):
        if self._committing:
            return
        self._generation += 1
        self._timer.stop()
        if self._future is not None:
            self._future.cancel()
        self.mapping_dialog.reject()
        if self._completed:
            super().accept()
        else:
            super().reject()

    def closeEvent(self, event):
        if self._committing:
            event.ignore()
            return
        self._generation += 1
        self._timer.stop()
        if self._future is not None:
            self._future.cancel()
        self.mapping_dialog.reject()
        if self._completed:
            event.accept()
            super().accept()
        else:
            super().closeEvent(event)
