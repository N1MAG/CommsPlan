from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QSizePolicy,
)

from freqinout.core.fldigi_macro_parser import rewrite_macro_profile_file_reference
from freqinout.core.fldigi_macro_profile import (
    FldigiMacroProfileStore,
    normalize_macro_mapping_source_path,
    standard_macro_mapping_source_filename,
)
from freqinout.gui.theme import button_style, horizontal_layout_breakpoint, resolve_theme


@dataclass
class _RowData:
    macro_id: str = ""
    macro_label: str = ""
    source_file: str = ""
    confidence: str = ""
    scope: str = ""
    function: str = ""
    custom_name: str = ""
    enabled: bool = False
    read_only: bool = False
    source_warning: str = ""
    macro_source_file: str = ""


class FldigiMacroMappingDialog(QDialog):
    COLUMN_ENABLED = 0
    COLUMN_SCOPE = 1
    COLUMN_FUNCTION = 2
    COLUMN_CUSTOM_NAME = 3
    COLUMN_MACRO_ID = 4
    COLUMN_MACRO_LABEL = 5
    COLUMN_SOURCE_FILE = 6
    COLUMN_CONFIDENCE = 7
    COLUMN_READ_ONLY = 8

    def __init__(self, settings, profile_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FLDigi Macro Discovery & Mapping")
        self.setSizeGripEnabled(True)

        self.settings = settings
        self.store = FldigiMacroProfileStore(settings)
        self.profile_path = self.store.normalize_path(profile_path)
        self._row_sources: List[Dict[str, object]] = []
        self._row_widgets: List[Dict[str, object]] = []
        self._save_failed = False

        self._build_ui()
        self._reload_profile(persist_scan=True)

    # ---------------- UI ---------------- #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        title = QLabel("<h3>Macro Discovery & Mapping</h3>")
        root.addWidget(title)

        self.profile_label = QLabel("")
        self.profile_label.setWordWrap(True)
        root.addWidget(self.profile_label)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        self.mode_label = QLabel("")
        self.mode_label.setWordWrap(True)
        root.addWidget(self.mode_label)

        self.note_label = QLabel(
            "Default view shows high-confidence macros with clearly identified source files. Use the confidence filter to show review, low-confidence, or saved-only rows."
        )
        self.note_label.setWordWrap(True)
        root.addWidget(self.note_label)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Confidence filter:"))
        self.confidence_filter_combo = QComboBox()
        self.confidence_filter_combo.addItem("High-confidence only (default)", "high")
        self.confidence_filter_combo.addItem("Show all rows", "all")
        filter_row.addWidget(self.confidence_filter_combo)
        filter_row.addStretch()
        root.addLayout(filter_row)

        self.confidence_filter_status = QLabel("")
        self.confidence_filter_status.setWordWrap(True)
        root.addWidget(self.confidence_filter_status)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "Enabled",
                "Scope",
                "Function",
                "Custom Name",
                "Macro ID",
                "Macro Label",
                "Source File",
                "Confidence",
                "Read Only",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COLUMN_SOURCE_FILE, QHeaderView.Stretch)
        for column in (
            self.COLUMN_ENABLED,
            self.COLUMN_SCOPE,
            self.COLUMN_FUNCTION,
            self.COLUMN_CUSTOM_NAME,
            self.COLUMN_MACRO_ID,
            self.COLUMN_MACRO_LABEL,
            self.COLUMN_CONFIDENCE,
            self.COLUMN_READ_ONLY,
        ):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        root.addWidget(self.table, stretch=1)

        controls = QHBoxLayout()
        self._controls_layout = controls
        self.rescan_btn = QPushButton("Rescan")
        self.add_manual_btn = QPushButton("Add Manual Row")
        self.browse_btn = QPushButton("Browse Selected Source...")
        self.update_macro_path_btn = QPushButton("Update Macro Path")
        self.remove_btn = QPushButton("Remove Selected Row")
        controls.addWidget(self.rescan_btn)
        controls.addWidget(self.add_manual_btn)
        controls.addWidget(self.browse_btn)
        controls.addWidget(self.update_macro_path_btn)
        controls.addWidget(self.remove_btn)
        controls.addStretch()
        root.addLayout(controls)

        bottom = QHBoxLayout()
        self._bottom_layout = bottom
        bottom.addStretch()
        self.save_btn = QPushButton("Save Mappings")
        self.close_btn = QPushButton("Close")
        bottom.addWidget(self.save_btn)
        bottom.addWidget(self.close_btn)
        root.addLayout(bottom)

        theme = resolve_theme(self.settings)
        self.rescan_btn.setStyleSheet(button_style("info", theme))
        self.add_manual_btn.setStyleSheet(button_style("eligible_info", theme))
        self.browse_btn.setStyleSheet(button_style("eligible_success", theme))
        self.update_macro_path_btn.setStyleSheet(button_style("warning", theme))
        self.update_macro_path_btn.setToolTip("Back up and update the selected macro's <FILE:...> path when it should use CommsPlan's configured check-in folder.")
        self.remove_btn.setStyleSheet(button_style("danger", theme))
        self.save_btn.setStyleSheet(button_style("success", theme))
        self.close_btn.setStyleSheet(button_style("muted", theme))

        self.rescan_btn.clicked.connect(self._on_rescan)
        self.add_manual_btn.clicked.connect(self._add_manual_row)
        self.browse_btn.clicked.connect(self._browse_selected_source)
        self.update_macro_path_btn.clicked.connect(self._update_selected_macro_path)
        self.remove_btn.clicked.connect(self._remove_selected_row)
        self.confidence_filter_combo.currentIndexChanged.connect(lambda _idx: self._apply_confidence_filter())
        self.save_btn.clicked.connect(self._save_mappings)
        self.close_btn.clicked.connect(self.reject)

        self._refresh_responsive_geometry()

    def _refresh_responsive_geometry(self) -> None:
        """Stack dialog actions when the available width is compact."""
        compact_threshold = horizontal_layout_breakpoint(
            self._controls_layout,
            reserve_controls=2,
        )
        compact = int(self.width() or 0) < compact_threshold
        for layout in (
            getattr(self, "_controls_layout", None),
            getattr(self, "_bottom_layout", None),
        ):
            if isinstance(layout, QBoxLayout):
                layout.setDirection(
                    QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight
                )
        self.table.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_responsive_geometry()

    # ---------------- DATA LOAD ---------------- #

    def _reload_profile(self, *, persist_scan: bool) -> None:
        if self.profile_path and persist_scan and Path(self.profile_path).exists():
            record = self.store.save_scan(self.profile_path)
        else:
            record = self.store.scan_profile(self.profile_path) if self.profile_path else {
                "profile_path": "",
                "profile_name": "",
                "detected_macros": [],
                "mappings": [],
            }
        self._populate_from_record(record)

    def _populate_from_record(self, record: Dict[str, object]) -> None:
        self._row_sources = []
        self._row_widgets = []
        self.table.setRowCount(0)

        profile_path = str(record.get("profile_path") or self.profile_path or "")
        fallback_name = Path(profile_path).stem if profile_path else ""
        profile_name = str(record.get("profile_name") or fallback_name)
        self.profile_label.setText(f"Profile: {profile_name or '(unnamed)'}\nPath: {profile_path or '(none)'}")
        self.summary_label.setText(self.store.summary_text(record))
        mode = self.store.profile_mode(profile_path)
        self.mode_label.setText(
            "Mode: mapped" if mode == "mapped" else "Mode: legacy (mappings stay inactive until complete and enabled)"
        )

        detected_macros = record.get("detected_macros", [])
        existing_mappings = list(record.get("mappings") or [])
        matched: set[int] = set()
        rows: List[_RowData] = []

        def find_mapping(macro_id: str, source_file: str) -> Optional[Dict[str, object]]:
            normalized_id = self._norm(macro_id)
            normalized_source = self._source_norm(source_file)
            macro_match: Optional[tuple[int, Dict[str, object]]] = None
            source_match: Optional[tuple[int, Dict[str, object]]] = None
            for idx, mapping in enumerate(existing_mappings):
                if idx in matched or not isinstance(mapping, dict):
                    continue
                mapping_id = self._norm(mapping.get("macro_id", ""))
                mapping_source = self._source_norm(mapping.get("source_file", ""))
                if normalized_id and mapping_id == normalized_id:
                    if normalized_source and mapping_source == normalized_source:
                        matched.add(idx)
                        return mapping
                    if macro_match is None:
                        macro_match = (idx, mapping)
                if normalized_source and mapping_source == normalized_source and source_match is None:
                    source_match = (idx, mapping)
            if macro_match is not None:
                idx, mapping = macro_match
                matched.add(idx)
                return mapping
            if source_match is not None:
                idx, mapping = source_match
                matched.add(idx)
                return mapping
            return None

        if isinstance(detected_macros, list):
            for macro in detected_macros:
                if not isinstance(macro, dict):
                    continue
                macro_id = str(macro.get("macro_id") or "")
                confidence = str(macro.get("confidence") or "")
                detected_files = macro.get("detected_files") or []
                review_files = macro.get("review_files") or []
                if isinstance(detected_files, list) and detected_files:
                    for source_file in detected_files:
                        source_text = str(source_file or "").strip()
                        mapping = find_mapping(macro_id, source_text)
                        rows.append(self._row_data_from_mapping(macro, source_text, confidence, mapping))
                elif isinstance(review_files, list) and review_files:
                    for source_file in review_files:
                        source_text = str(source_file or "").strip()
                        mapping = find_mapping(macro_id, source_text)
                        rows.append(self._row_data_from_mapping(macro, source_text, "review", mapping))
                else:
                    mapping = find_mapping(macro_id, "")
                    rows.append(self._row_data_from_mapping(macro, "", confidence, mapping))

        for idx, mapping in enumerate(existing_mappings):
            if idx in matched or not isinstance(mapping, dict):
                continue
            rows.append(
                _RowData(
                    macro_id=str(mapping.get("macro_id") or ""),
                    macro_label=str(mapping.get("macro_label") or ""),
                    source_file=str(mapping.get("source_file") or ""),
                    confidence="saved",
                    scope=str(mapping.get("scope") or ""),
                    function=str(mapping.get("function") or ""),
                    custom_name=str(mapping.get("custom_name") or ""),
                    enabled=bool(mapping.get("enabled", False)),
                    read_only=bool(mapping.get("read_only", False)),
                )
            )

        for row_data in rows:
            origin = "saved" if row_data.confidence == "saved" else "discovered"
            self._append_row(row_data, origin=origin, original=self._row_snapshot(row_data))

        if self.table.rowCount() == 0:
            blank = _RowData(confidence="no macros discovered")
            self._append_row(blank, origin="manual", original=self._row_snapshot(blank))
        self._apply_confidence_filter()

    @staticmethod
    def _norm(value: object) -> str:
        return str(value or "").strip().casefold()

    def _source_norm(self, value: object) -> str:
        return normalize_macro_mapping_source_path(value, self.settings).casefold()

    def _row_data_from_mapping(
        self,
        macro: Dict[str, object],
        source_file: str,
        confidence: str,
        mapping: Optional[Dict[str, object]],
    ) -> _RowData:
        mapping = mapping or {}
        saved_source = str(mapping.get("source_file") or "").strip()
        discovered_source = normalize_macro_mapping_source_path(source_file, self.settings)
        display_source = str(saved_source or discovered_source or "")
        return _RowData(
            macro_id=str(macro.get("macro_id") or mapping.get("macro_id") or ""),
            macro_label=str(macro.get("macro_label") or mapping.get("macro_label") or ""),
            source_file=display_source,
            confidence=str(confidence or mapping.get("confidence") or "low"),
            scope=str(mapping.get("scope") or ""),
            function=str(mapping.get("function") or ""),
            custom_name=str(mapping.get("custom_name") or ""),
            enabled=bool(mapping.get("enabled", False)),
            read_only=bool(mapping.get("read_only", False)),
            source_warning=self._source_path_warning(source_file, display_source),
            macro_source_file=str(source_file or "").strip(),
        )

    def _source_path_warning(self, macro_source: object, configured_source: object) -> str:
        macro_text = str(macro_source or "").strip()
        configured_text = str(configured_source or "").strip()
        if not macro_text or not configured_text:
            return ""
        if not standard_macro_mapping_source_filename(macro_text):
            return ""
        if self._path_compare_key(macro_text) == self._path_compare_key(configured_text):
            return ""
        return (
            f"Macro text points to {macro_text}. CommsPlan is using the configured file "
            f"{configured_text}. Update the FLDigi macro if it should read CommsPlan's live file."
        )

    @staticmethod
    def _path_compare_key(path: object) -> str:
        text = str(path or "").strip().replace("\\", "/").rstrip("/")
        return text.casefold()

    def _row_snapshot(self, row: _RowData) -> Dict[str, object]:
        return {
            "confidence": row.confidence,
            "scope": row.scope,
            "function": row.function,
            "custom_name": row.custom_name,
            "macro_id": row.macro_id,
            "macro_label": row.macro_label,
            "source_file": row.source_file,
            "read_only": row.read_only,
            "enabled": row.enabled,
        }

    def _append_row(self, row: _RowData, *, origin: str = "discovered", original: Optional[Dict[str, object]] = None) -> None:
        index = self.table.rowCount()
        self.table.insertRow(index)
        self._row_sources.append(
            {
                "origin": origin,
                "original": dict(original or {}),
                "macro_source_file": row.macro_source_file,
                "source_warning": row.source_warning,
            }
        )

        enabled_item = QTableWidgetItem()
        enabled_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        enabled_item.setCheckState(Qt.Checked if row.enabled else Qt.Unchecked)
        self.table.setItem(index, self.COLUMN_ENABLED, enabled_item)

        self.table.setCellWidget(index, self.COLUMN_SCOPE, self._build_combo(["", "NCS", "ANCS", "Joiner", "SHARED"], row.scope))
        self.table.setCellWidget(index, self.COLUMN_FUNCTION, self._build_combo(["", "TFC", "QRU", "LATE", "ALL", "ACK_PENDING", "NEXT_TFC", "CUSTOM"], row.function))

        custom_edit = QLineEdit(row.custom_name)
        custom_edit.setPlaceholderText("Optional for CUSTOM")
        self.table.setCellWidget(index, self.COLUMN_CUSTOM_NAME, custom_edit)

        self.table.setItem(index, self.COLUMN_MACRO_ID, self._read_only_item(row.macro_id))
        self.table.setItem(index, self.COLUMN_MACRO_LABEL, self._read_only_item(row.macro_label))

        source_edit = QLineEdit(row.source_file)
        source_edit.setPlaceholderText("Enter or browse file path")
        if row.source_warning:
            source_edit.setToolTip(row.source_warning)
            theme = resolve_theme(self.settings)
            source_edit.setStyleSheet(
                "QLineEdit {"
                f" background: {theme['surface_alt']}; color: {theme['text']};"
                f" border: 1px solid {theme['warning']};"
                " }"
            )
        self.table.setCellWidget(index, self.COLUMN_SOURCE_FILE, source_edit)

        self.table.setItem(index, self.COLUMN_CONFIDENCE, self._read_only_item(row.confidence))

        read_only_item = QTableWidgetItem()
        read_only_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        read_only_item.setCheckState(Qt.Checked if row.read_only else Qt.Unchecked)
        self.table.setItem(index, self.COLUMN_READ_ONLY, read_only_item)

    def _read_only_item(self, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        return item

    def _build_combo(self, items: List[str], current: str) -> QComboBox:
        combo = QComboBox()
        combo.addItems(items)
        combo.setEditable(True)
        idx = combo.findText(current)
        if idx < 0 and current.strip().upper() == "JOINER":
            idx = combo.findText("Joiner")
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(current)
        return combo

    def _confidence_filter_value(self) -> str:
        if hasattr(self, "confidence_filter_combo"):
            return str(self.confidence_filter_combo.currentData() or "high").strip().lower() or "high"
        return "high"

    def _row_confidence_value(self, row: int) -> str:
        confidence_item = self.table.item(row, self.COLUMN_CONFIDENCE)
        return confidence_item.text().strip().lower() if confidence_item else ""

    def _confidence_row_matches_filter(self, row_confidence: str, filter_value: str) -> bool:
        confidence = str(row_confidence or "").strip().lower()
        selected = str(filter_value or "").strip().lower() or "high"
        if selected == "all":
            return True
        return confidence == selected

    def _apply_confidence_filter(self) -> None:
        filter_value = self._confidence_filter_value()
        total = self.table.rowCount()
        shown = 0
        for row in range(total):
            visible = self._confidence_row_matches_filter(self._row_confidence_value(row), filter_value)
            self.table.setRowHidden(row, not visible)
            if visible:
                shown += 1
        if hasattr(self, "confidence_filter_status"):
            label = "High-confidence rows" if filter_value == "high" else "All rows"
            self.confidence_filter_status.setText(f"Showing {shown} of {total} rows. Filter: {label}.")

    # ---------------- ACTIONS ---------------- #

    def _on_rescan(self) -> None:
        if not self.profile_path:
            QMessageBox.information(self, "Macro Discovery & Mapping", "No macro profile is selected.")
            return
        self._reload_profile(persist_scan=True)

    def _add_manual_row(self) -> None:
        row = _RowData(confidence="manual")
        self._append_row(row, origin="manual", original=self._row_snapshot(row))
        self._apply_confidence_filter()

    def _selected_row(self) -> int:
        return self.table.currentRow()

    def _browse_selected_source(self) -> None:
        row = self._selected_row()
        if row < 0:
            QMessageBox.information(self, "Macro Discovery & Mapping", "Select a row first.")
            return
        source_widget = self.table.cellWidget(row, self.COLUMN_SOURCE_FILE)
        if not isinstance(source_widget, QLineEdit):
            return
        start_dir = Path(source_widget.text().strip()).parent if source_widget.text().strip() else Path(self.profile_path).parent
        fn, _ = QFileDialog.getOpenFileName(
            self,
            "Select mapped source file",
            str(start_dir) if str(start_dir) else "",
            "All Files (*)",
        )
        if fn:
            source_widget.setText(fn)

    def _update_selected_macro_path(self) -> None:
        row = self._selected_row()
        if row < 0:
            QMessageBox.information(self, "Macro Discovery & Mapping", "Select a mapped macro row first.")
            return
        if not self.profile_path or not Path(self.profile_path).exists():
            QMessageBox.warning(self, "Macro Discovery & Mapping", "No macro profile file is selected.")
            return

        macro_id_item = self.table.item(row, self.COLUMN_MACRO_ID)
        macro_label_item = self.table.item(row, self.COLUMN_MACRO_LABEL)
        source_widget = self.table.cellWidget(row, self.COLUMN_SOURCE_FILE)
        function_widget = self.table.cellWidget(row, self.COLUMN_FUNCTION)
        macro_id = macro_id_item.text().strip() if macro_id_item else ""
        macro_label = macro_label_item.text().strip() if macro_label_item else ""
        configured_source = self._widget_text(source_widget)
        function = self._widget_text(function_widget).upper()
        row_source = self._row_sources[row] if row < len(self._row_sources) else {}
        macro_source = str(row_source.get("macro_source_file") or "").strip() if isinstance(row_source, dict) else ""

        if not macro_id:
            QMessageBox.information(self, "Macro Discovery & Mapping", "This row is not tied to a specific FLDigi macro slot.")
            return
        if function == "CUSTOM" or not standard_macro_mapping_source_filename(configured_source):
            QMessageBox.information(
                self,
                "Macro Discovery & Mapping",
                "Only CommsPlan-managed check-in files are repaired here. Custom macro files are left exactly where the operator placed them.",
            )
            return
        if not macro_source:
            QMessageBox.information(self, "Macro Discovery & Mapping", "No scanned macro file path is available for this row. Rescan the macro profile first.")
            return
        if self._path_compare_key(macro_source) == self._path_compare_key(configured_source):
            QMessageBox.information(self, "Macro Discovery & Mapping", "This macro already points to the configured CommsPlan file path.")
            return

        prompt = (
            f"CommsPlan will back up the macro file, then update {macro_id}"
            f"{f' ({macro_label})' if macro_label else ''}.\n\n"
            f"Current macro path:\n{macro_source}\n\n"
            f"Configured CommsPlan path:\n{configured_source}\n\n"
            "After this, refresh or reload macros in FLDigi if FLDigi already has this profile open."
        )
        if QMessageBox.question(self, "Update FLDigi Macro Path", prompt) != QMessageBox.Yes:
            return

        result = rewrite_macro_profile_file_reference(
            self.profile_path,
            macro_id=macro_id,
            old_path=macro_source,
            new_path=configured_source,
        )
        if not result.get("ok"):
            QMessageBox.warning(
                self,
                "Macro Discovery & Mapping",
                "CommsPlan could not find the selected <FILE:...> reference in that macro slot. Rescan the profile and try again.",
            )
            return

        backup_path = str(result.get("backup_path") or "")
        self._reload_profile(persist_scan=True)
        QMessageBox.information(
            self,
            "Macro Discovery & Mapping",
            f"Macro path updated. Backup saved at:\n{backup_path}",
        )

    def _remove_selected_row(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        self.table.removeRow(row)
        if 0 <= row < len(self._row_sources):
            self._row_sources.pop(row)
        self._apply_confidence_filter()

    def _gather_rows(self) -> List[Dict[str, object]]:
        mappings: List[Dict[str, object]] = []
        for row in range(self.table.rowCount()):
            enabled_item = self.table.item(row, self.COLUMN_ENABLED)
            macro_id_item = self.table.item(row, self.COLUMN_MACRO_ID)
            macro_label_item = self.table.item(row, self.COLUMN_MACRO_LABEL)
            confidence_item = self.table.item(row, self.COLUMN_CONFIDENCE)
            read_only_item = self.table.item(row, self.COLUMN_READ_ONLY)
            scope_widget = self.table.cellWidget(row, self.COLUMN_SCOPE)
            function_widget = self.table.cellWidget(row, self.COLUMN_FUNCTION)
            custom_widget = self.table.cellWidget(row, self.COLUMN_CUSTOM_NAME)
            source_widget = self.table.cellWidget(row, self.COLUMN_SOURCE_FILE)

            scope = self._widget_text(scope_widget)
            if scope.strip().lower() == "joiner":
                scope = "JOINER"
            function = self._widget_text(function_widget).upper()
            custom_name = self._widget_text(custom_widget)
            source_file = self._widget_text(source_widget)
            macro_id = macro_id_item.text().strip() if macro_id_item else ""
            macro_label = macro_label_item.text().strip() if macro_label_item else ""
            confidence = confidence_item.text().strip() if confidence_item else ""
            enabled = bool(enabled_item and enabled_item.checkState() == Qt.Checked)
            read_only = bool(read_only_item and read_only_item.checkState() == Qt.Checked)

            if not any([scope, function, custom_name, source_file, macro_id, macro_label, confidence, enabled, read_only]):
                continue

            if function != "CUSTOM":
                custom_name = ""
            elif not custom_name:
                custom_name = self.store.next_custom_name(mappings)

            mapping = {
                "scope": scope,
                "function": function,
                "custom_name": custom_name,
                "macro_id": macro_id,
                "macro_label": macro_label,
                "source_file": source_file,
                "read_only": read_only,
                "enabled": enabled,
            }
            if confidence:
                mapping["confidence"] = confidence
            row_source = self._row_sources[row] if row < len(self._row_sources) else {"origin": "discovered", "original": {}}
            if self.store.mapping_should_persist(
                mapping,
                original=row_source.get("original") if isinstance(row_source, dict) else {},
                origin=str(row_source.get("origin", "discovered")) if isinstance(row_source, dict) else "discovered",
            ):
                mappings.append(mapping)
        return mappings

    def _widget_text(self, widget) -> str:
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        if isinstance(widget, QComboBox):
            return widget.currentText().strip()
        return ""

    def _save_mappings(self) -> None:
        if not self.profile_path:
            QMessageBox.warning(self, "Macro Discovery & Mapping", "No profile path is selected.")
            return
        mappings = self._gather_rows()
        record = self.store.upsert_mappings(self.profile_path, mappings)
        self.summary_label.setText(self.store.summary_text(record))
        self.mode_label.setText(
            "Mode: mapped" if self.store.profile_mode(self.profile_path) == "mapped" else "Mode: legacy (mappings stay inactive until complete and enabled)"
        )
        QMessageBox.information(self, "Macro Discovery & Mapping", "Mappings saved for this profile.")

    def accept(self) -> None:
        self._save_mappings()
        if self._save_failed:
            return
        super().accept()
