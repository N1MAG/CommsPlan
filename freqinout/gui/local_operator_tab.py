from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QFileDialog,
    QMessageBox,
    QDialog,
    QFormLayout,
    QTextEdit,
    QCheckBox,
    QBoxLayout,
    QScrollArea,
)

from freqinout.core.logger import log
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.local_ops_store import (
    delete_operators,
    get_all_operators,
    latest_report_summaries_for_callsigns,
    upsert_operator,
)
from freqinout.gui.theme import (
    resolve_theme,
    button_style,
    button_height_for_font,
    contrast_text_for_background,
    control_height_for_font,
    horizontal_layout_breakpoint,
    label_style,
)


LOCAL_CATEGORIES = ["VHF", "UHF", "GMRS", "MURS", "FRS", "Other"]


class LocalOperatorTab(QWidget):
    """
    Local operator roster for ad hoc VHF/UHF/GMRS/MURS/FRS net operations.
    """

    local_operator_updated = Signal()
    local_reports_requested = Signal(str)

    COL_SELECT = 0
    COL_CALLSIGN = 1
    COL_NAME = 2
    COL_LOCATION = 3
    COL_ACTIVITY = 4
    COL_SITREP = 5
    COL_REPORT = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = SettingsManager()
        self._rows: List[Dict[str, Any]] = []
        self._report_summaries: Dict[str, Dict[str, Any]] = {}
        self._build_ui()
        self._load_data()
        self.apply_theme()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        self.title_label = QLabel("Local Operators")
        header.addWidget(self.title_label)
        header.addStretch()
        layout.addLayout(header)

        filter_row = QHBoxLayout()
        self.filter_row = filter_row
        filter_row.addWidget(QLabel("Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Callsign, name, city/state, category, sitrep, report topic, keyword, notes")
        filter_row.addWidget(self.search_edit, stretch=1)
        filter_row.addWidget(QLabel("Category:"))
        self.category_filter = QComboBox()
        self.category_filter.addItem("All")
        filter_row.addWidget(self.category_filter)
        layout.addLayout(filter_row)

        actions = QHBoxLayout()
        self.actions_row = actions
        self.refresh_btn = QPushButton("Refresh")
        self.add_btn = QPushButton("Add")
        self.edit_btn = QPushButton("Edit Selected")
        self.view_reports_btn = QPushButton("View Reports")
        self.delete_btn = QPushButton("Delete Selected")
        self.import_btn = QPushButton("Import CSV")
        self.export_btn = QPushButton("Export CSV")
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.add_btn)
        actions.addWidget(self.edit_btn)
        actions.addWidget(self.view_reports_btn)
        actions.addWidget(self.delete_btn)
        actions.addWidget(self.import_btn)
        actions.addWidget(self.export_btn)
        actions.addStretch()
        layout.addLayout(actions)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            [
                "Selected",
                "Callsign",
                "Name",
                "Location",
                "Activity",
                "SitRep",
                "Latest Report",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_SELECT, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_CALLSIGN, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_LOCATION, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_ACTIVITY, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_SITREP, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_REPORT, QHeaderView.Stretch)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        layout.addWidget(self.table)

        self._apply_responsive_layout()

        self.refresh_btn.clicked.connect(self._load_data)
        self.add_btn.clicked.connect(self._add_operator)
        self.edit_btn.clicked.connect(self._edit_selected)
        self.view_reports_btn.clicked.connect(self._view_selected_reports)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.import_btn.clicked.connect(self._import_csv)
        self.export_btn.clicked.connect(self._export_csv)
        self.search_edit.textChanged.connect(self._apply_filters)
        self.category_filter.currentIndexChanged.connect(self._apply_filters)

    def apply_theme(self) -> None:
        theme = resolve_theme(self.settings)
        self.title_label.setStyleSheet(label_style("text", theme, weight=700))
        self.refresh_btn.setStyleSheet(button_style("muted", theme))
        self.add_btn.setStyleSheet(button_style("eligible_success", theme))
        role = "eligible_info" if self._selected_callsigns() else "muted"
        self.edit_btn.setStyleSheet(button_style(role, theme))
        self.view_reports_btn.setStyleSheet(button_style("eligible_info" if len(self._selected_callsigns()) == 1 else "muted", theme))
        self.delete_btn.setStyleSheet(button_style("eligible_danger" if self._selected_callsigns() else "muted", theme))
        self.import_btn.setStyleSheet(button_style("muted", theme))
        self.export_btn.setStyleSheet(button_style("muted", theme))
        for button in (
            self.refresh_btn,
            self.add_btn,
            self.edit_btn,
            self.view_reports_btn,
            self.delete_btn,
            self.import_btn,
            self.export_btn,
        ):
            button.setMinimumHeight(button_height_for_font(button))
        self.search_edit.setMinimumHeight(control_height_for_font(self.search_edit))
        self.category_filter.setMinimumHeight(control_height_for_font(self.category_filter))
        self.table.verticalHeader().setDefaultSectionSize(
            max(1, self.table.fontMetrics().lineSpacing() + 10)
        )
        for row in range(self.table.rowCount()):
            item = self.table.item(row, self.COL_SITREP)
            if item is not None:
                self._apply_sitrep_item_style(item, item.text(), theme)
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        """Keep the dense roster controls usable without changing cached data."""
        compact = self.width() > 0 and self.width() < max(
            horizontal_layout_breakpoint(self.filter_row, reserve_controls=1),
            horizontal_layout_breakpoint(self.actions_row, reserve_controls=1),
        )
        direction = QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight
        self.filter_row.setDirection(direction)
        self.actions_row.setDirection(direction)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.apply_theme()

    def on_settings_saved(self) -> None:
        try:
            self.settings.reload()
        except Exception:
            pass
        self.apply_theme()

    def _load_data(self) -> None:
        try:
            self._rows = get_all_operators()
            self._report_summaries = latest_report_summaries_for_callsigns(
                [str(row.get("callsign", "")) for row in self._rows]
            )
        except Exception as e:
            log.error("LocalOperatorTab: load failed: %s", e)
            self._rows = []
            self._report_summaries = {}
        self._refresh_category_filter()
        self._apply_filters()
        self.local_operator_updated.emit()

    def _refresh_category_filter(self) -> None:
        cur = self.category_filter.currentText()
        categories = sorted(
            {str(r.get("category", "")).strip() for r in self._rows if str(r.get("category", "")).strip()},
            key=str.upper,
        )
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("All")
        for cat in categories:
            self.category_filter.addItem(cat)
        idx = self.category_filter.findText(cur)
        self.category_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.category_filter.blockSignals(False)

    def _row_matches(self, row: Dict[str, Any], query: str, category: str) -> bool:
        if category and category.upper() != "ALL":
            if str(row.get("category", "")).strip().upper() != category.upper():
                return False
        if not query:
            return True
        hay = " ".join(
            [
                str(row.get("callsign", "")),
                str(row.get("first_name", "")),
                str(row.get("last_name", "")),
                str(row.get("name", "")),
                str(row.get("city", "")),
                str(row.get("state", "")),
                str(row.get("category", "")),
                str(row.get("first_seen_utc", "")),
                str(row.get("last_seen_utc", "")),
                str(row.get("checkin_count", "")),
                str(row.get("sitrep_status", "")),
                str(row.get("notes", "")),
                self._report_search_text(str(row.get("callsign", ""))),
            ]
        ).upper()
        return query.upper() in hay

    def _apply_filters(self) -> None:
        query = self.search_edit.text().strip()
        category = self.category_filter.currentText().strip()
        filtered = [r for r in self._rows if self._row_matches(r, query, category)]
        self._populate_table(filtered)
        self.apply_theme()

    def _populate_table(self, rows: List[Dict[str, Any]]) -> None:
        sorting_enabled = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        try:
            self.table.setRowCount(0)
            for row in rows:
                r = self.table.rowCount()
                self.table.insertRow(r)
                sel_chk = QCheckBox()
                sel_chk.stateChanged.connect(self.apply_theme)
                sel_wrap = QWidget()
                sel_layout = QHBoxLayout(sel_wrap)
                sel_layout.setContentsMargins(0, 0, 0, 0)
                sel_layout.setAlignment(Qt.AlignCenter)
                sel_layout.addWidget(sel_chk)
                self.table.setCellWidget(r, self.COL_SELECT, sel_wrap)

                vals = [
                    str(row.get("callsign", "")).upper(),
                    self._operator_display_name(row),
                    self._operator_location(row),
                    self._operator_activity(row),
                    str(row.get("sitrep_status", "GREEN")).upper(),
                    self._report_display_text(str(row.get("callsign", ""))),
                ]
                for idx, value in enumerate(vals, start=1):
                    item = QTableWidgetItem(value)
                    self.table.setItem(r, idx, item)
                    if idx == self.COL_SITREP:
                        self._apply_sitrep_item_style(item, value)
                    if idx == self.COL_REPORT:
                        item.setToolTip(self._report_tooltip(str(row.get("callsign", ""))))
        finally:
            self.table.setSortingEnabled(sorting_enabled)

    @staticmethod
    def _operator_display_name(row: Dict[str, Any]) -> str:
        name = str(row.get("name", "")).strip()
        if name:
            return name
        return " ".join(
            part
            for part in (
                str(row.get("first_name", "")).strip(),
                str(row.get("last_name", "")).strip(),
            )
            if part
        )

    @staticmethod
    def _operator_location(row: Dict[str, Any]) -> str:
        city = str(row.get("city", "")).strip()
        state = str(row.get("state", "")).strip().upper()
        if city and state:
            return f"{city}, {state}"
        return city or state

    @staticmethod
    def _operator_activity(row: Dict[str, Any]) -> str:
        category = str(row.get("category", "")).strip().upper()
        try:
            checkins = int(row.get("checkin_count", 0) or 0)
        except Exception:
            checkins = 0
        parts = []
        if category:
            parts.append(category)
        parts.append(f"{checkins} check-in{'s' if checkins != 1 else ''}")
        return " | ".join(parts)

    def _report_summary(self, callsign: str) -> Dict[str, Any]:
        return self._report_summaries.get(str(callsign or "").strip().upper(), {})

    def _report_display_text(self, callsign: str) -> str:
        summary = self._report_summary(callsign)
        return str(summary.get("display", "") or "")

    def _report_search_text(self, callsign: str) -> str:
        summary = self._report_summary(callsign)
        topics = " ".join(str(topic) for topic in summary.get("topics", []) if str(topic).strip())
        return " ".join(
            [
                str(summary.get("display", "")),
                str(summary.get("latest_display", "")),
                str(summary.get("highest_display", "")),
                topics,
            ]
        )

    def _report_tooltip(self, callsign: str) -> str:
        summary = self._report_summary(callsign)
        if not summary:
            return ""
        parts = [str(summary.get("display", ""))]
        count = int(summary.get("count", 0) or 0)
        if count > 1:
            parts.append(f"{count} recent report(s) loaded for this operator")
        topics = ", ".join(str(topic) for topic in summary.get("topics", []) if str(topic).strip())
        if topics:
            parts.append(f"Topics: {topics}")
        return "\n".join(part for part in parts if part)

    def _apply_sitrep_item_style(
        self,
        item: QTableWidgetItem,
        status: str,
        theme: Optional[Dict[str, str]] = None,
    ) -> None:
        theme = theme or resolve_theme(self.settings)
        key = (status or "").strip().upper()
        if key == "RED":
            background = theme["danger"]
        elif key == "YELLOW":
            background = theme["warning"]
        else:
            background = theme["success"]
        item.setBackground(QColor(background))
        item.setForeground(QColor(contrast_text_for_background(background, theme)))

    def _selected_callsigns(self) -> List[str]:
        out: List[str] = []
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, self.COL_SELECT)
            chk = w.findChild(QCheckBox) if isinstance(w, QWidget) else None
            if chk is None or not chk.isChecked():
                continue
            cs_item = self.table.item(r, self.COL_CALLSIGN)
            cs = cs_item.text().strip().upper() if cs_item else ""
            if cs:
                out.append(cs)
        return out

    def _dialog_profile(self, existing: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Local Operator" if existing else "Add Local Operator")
        if (existing or {}).get("_fcc_reference"):
            dlg.setWindowTitle("Review callsign reference — Add Local Operator")
        dialog_layout = QVBoxLayout(dlg)
        form_scroll = QScrollArea(dlg)
        form_scroll.setWidgetResizable(True)
        form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        form_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        form_body = QWidget(form_scroll)
        form = QFormLayout(form_body)
        form_scroll.setWidget(form_body)
        dialog_layout.addWidget(form_scroll)

        callsign_edit = QLineEdit(str((existing or {}).get("callsign", "")))
        if (existing or {}).get("_fcc_reference"):
            callsign_edit.setReadOnly(True)
        first_name_edit = QLineEdit(str((existing or {}).get("first_name", "")))
        last_name_edit = QLineEdit(str((existing or {}).get("last_name", "")))
        city_edit = QLineEdit(str((existing or {}).get("city", "")))
        state_edit = QLineEdit(str((existing or {}).get("state", "")))
        category_combo = QComboBox()
        category_combo.setEditable(True)
        category_combo.addItems(LOCAL_CATEGORIES)
        if existing and str((existing or {}).get("category", "")):
            category_combo.setCurrentText(str((existing or {}).get("category", "")))
        notes_edit = QTextEdit(str((existing or {}).get("notes", "")))
        notes_edit.setMinimumHeight(
            max(
                control_height_for_font(notes_edit, vertical_padding=18, floor=44) * 2,
                notes_edit.fontMetrics().lineSpacing() * 3 + 20,
            )
        )
        sitrep_combo = QComboBox()
        sitrep_combo.addItems(["GREEN", "YELLOW", "RED"])
        sitrep_combo.setCurrentText(str((existing or {}).get("sitrep_status", "GREEN")).strip().upper() or "GREEN")

        form.addRow("Callsign:", callsign_edit)
        form.addRow("First Name:", first_name_edit)
        form.addRow("Last Name:", last_name_edit)
        form.addRow("City:", city_edit)
        form.addRow("State:", state_edit)
        form.addRow("Category:", category_combo)
        form.addRow("SitRep:", sitrep_combo)
        form.addRow("Notes:", notes_edit)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        cancel_btn = QPushButton("Cancel")
        btn_row.addStretch()
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        dialog_layout.addLayout(btn_row)

        out: Dict[str, Any] = {}

        def _save() -> None:
            cs = callsign_edit.text().strip().upper()
            if not cs:
                QMessageBox.warning(self, "Validation", "Callsign is required.")
                return
            out.update(
                {
                    "callsign": cs,
                    "first_name": first_name_edit.text().strip(),
                    "last_name": last_name_edit.text().strip(),
                    "city": city_edit.text().strip(),
                    "state": state_edit.text().strip().upper(),
                    "category": category_combo.currentText().strip(),
                    "sitrep_status": sitrep_combo.currentText().strip().upper(),
                    "notes": notes_edit.toPlainText().strip(),
                }
            )
            dlg.accept()

        save_btn.clicked.connect(_save)
        cancel_btn.clicked.connect(dlg.reject)
        if dlg.exec() != QDialog.Accepted:
            return None
        return out if out else None

    def _find_row_by_callsign(self, callsign: str) -> Optional[Dict[str, Any]]:
        cs = (callsign or "").strip().upper()
        for row in self._rows:
            if str(row.get("callsign", "")).strip().upper() == cs:
                return row
        return None

    def _add_operator(self) -> None:
        row = self._dialog_profile(None)
        if not row:
            return
        upsert_operator(
            row["callsign"],
            first_name=row.get("first_name", ""),
            last_name=row.get("last_name", ""),
            city=row.get("city", ""),
            state=row.get("state", ""),
            category=row.get("category", ""),
            sitrep_status=row.get("sitrep_status", "GREEN"),
            notes=row.get("notes", ""),
            touch_seen=False,
            increment_checkins=False,
        )
        self._load_data()

    def review_fcc_license(self, record) -> None:
        from freqinout.core.local_ops_store import insert_reference_operator
        row = self._dialog_profile({
            "_fcc_reference": True, "callsign": record.callsign,
            "first_name": record.first_name if record.kind == "Individual" else record.name,
            "last_name": record.last_name if record.kind == "Individual" else "",
            "state": record.state, "category": "GMRS" if record.service == "GMRS" else "Other",
            "notes": f"{'ISED Canada' if getattr(record, 'jurisdiction', '') == 'CA' else 'FCC'} {record.service} reference; source {record.source_date or 'unknown date'}. Licensee/mailing region; not confirmed operating identity/location.",
        })
        if not row:
            return
        if not insert_reference_operator(row):
            QMessageBox.information(self, "Operator already exists", "This callsign is already managed. Review it in Local Callsigns; reference data has not replaced it.")
            return
        self._load_data()
        self.local_operator_updated.emit()

    def _edit_selected(self) -> None:
        selected = self._selected_callsigns()
        if not selected:
            QMessageBox.information(self, "Edit Local Operator", "Select one operator to edit.")
            return
        if len(selected) > 1:
            QMessageBox.warning(self, "Edit Local Operator", "Select only one operator to edit.")
            return
        existing = self._find_row_by_callsign(selected[0])
        if not existing:
            return
        updated = self._dialog_profile(existing)
        if not updated:
            return
        old_cs = str(existing.get("callsign", "")).strip().upper()
        new_cs = str(updated.get("callsign", "")).strip().upper()
        if old_cs and new_cs and old_cs != new_cs:
            delete_operators([old_cs])
        upsert_operator(
            new_cs,
            first_name=updated.get("first_name", ""),
            last_name=updated.get("last_name", ""),
            city=updated.get("city", ""),
            state=updated.get("state", ""),
            category=updated.get("category", ""),
            sitrep_status=updated.get("sitrep_status", existing.get("sitrep_status", "GREEN")),
            notes=updated.get("notes", ""),
            touch_seen=False,
            increment_checkins=False,
            first_seen_utc=existing.get("first_seen_utc", ""),
            last_seen_utc=existing.get("last_seen_utc", ""),
            checkin_count=existing.get("checkin_count", 0),
        )
        self._load_data()

    def _delete_selected(self) -> None:
        selected = self._selected_callsigns()
        if not selected:
            QMessageBox.information(self, "Delete Local Operators", "Select one or more operators to delete.")
            return
        resp = QMessageBox.question(
            self,
            "Delete Local Operators",
            f"Delete {len(selected)} selected local operator(s)?",
        )
        if resp != QMessageBox.Yes:
            return
        delete_operators(selected)
        self._load_data()

    def _view_selected_reports(self) -> None:
        selected = self._selected_callsigns()
        if not selected:
            QMessageBox.information(self, "View Local Reports", "Select one local operator to view reports.")
            return
        if len(selected) > 1:
            QMessageBox.information(self, "View Local Reports", "Select only one local operator to view reports.")
            return
        self.local_reports_requested.emit(selected[0])

    @staticmethod
    def _csv_pick(row: Dict[str, Any], keys: List[str]) -> str:
        low_map = {str(k).strip().lower(): v for k, v in row.items()}
        for key in keys:
            val = low_map.get(key.lower())
            if val is None:
                continue
            txt = str(val).strip()
            if txt:
                return txt
        return ""

    @staticmethod
    def _split_name(value: str) -> tuple[str, str]:
        txt = (value or "").strip()
        if not txt:
            return "", ""
        parts = [p for p in txt.split() if p]
        if len(parts) <= 1:
            return (parts[0] if parts else ""), ""
        return parts[0], " ".join(parts[1:])

    def _import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Local Operators", "", "CSV Files (*.csv)")
        if not path:
            return
        imported = 0
        try:
            with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cs = self._csv_pick(row, ["callsign", "call", "operator_callsign"]).upper()
                    if not cs:
                        continue
                    first_name = self._csv_pick(row, ["first_name", "firstname", "fname"])
                    last_name = self._csv_pick(row, ["last_name", "lastname", "lname"])
                    full_name = self._csv_pick(row, ["name", "operator_name"])
                    if not first_name and not last_name and full_name:
                        first_name, last_name = self._split_name(full_name)
                    city = self._csv_pick(row, ["city"])
                    state = self._csv_pick(row, ["state", "st"]).upper()
                    category = self._csv_pick(row, ["category", "service", "type"])
                    notes = self._csv_pick(row, ["notes", "note", "comment"])
                    first_seen = self._csv_pick(row, ["first_seen_utc", "first_seen"])
                    last_seen = self._csv_pick(row, ["last_seen_utc", "last_seen"])
                    status = self._csv_pick(row, ["sitrep_status", "status"])
                    count_txt = self._csv_pick(row, ["checkins", "checkin_count", "count"])
                    count_val = None
                    if count_txt:
                        try:
                            count_val = int(float(count_txt))
                        except Exception:
                            count_val = None
                    upsert_operator(
                        cs,
                        first_name=first_name,
                        last_name=last_name,
                        name=full_name,
                        city=city,
                        state=state,
                        category=category,
                        notes=notes if notes else None,
                        sitrep_status=status if status else None,
                        first_seen_utc=first_seen if first_seen else None,
                        last_seen_utc=last_seen if last_seen else None,
                        checkin_count=count_val,
                        touch_seen=False,
                        increment_checkins=False,
                    )
                    imported += 1
            self._load_data()
            QMessageBox.information(self, "Import Local Operators", f"Imported/updated {imported} row(s).")
        except Exception as e:
            QMessageBox.warning(self, "Import Local Operators", f"Import failed:\n{e}")

    def _export_csv(self) -> None:
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Export Local Operators",
            "local_operators.csv",
            "CSV Files (*.csv)",
        )
        if not out:
            return
        rows = self._rows
        try:
            with Path(out).open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "callsign",
                        "first_name",
                        "last_name",
                        "name",
                        "city",
                        "state",
                        "category",
                        "first_seen_utc",
                        "last_seen_utc",
                        "checkin_count",
                        "notes",
                        "sitrep_status",
                    ],
                )
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {
                            "callsign": row.get("callsign", ""),
                            "first_name": row.get("first_name", ""),
                            "last_name": row.get("last_name", ""),
                            "name": row.get("name", ""),
                            "city": row.get("city", ""),
                            "state": row.get("state", ""),
                            "category": row.get("category", ""),
                            "first_seen_utc": row.get("first_seen_utc", ""),
                            "last_seen_utc": row.get("last_seen_utc", ""),
                            "checkin_count": row.get("checkin_count", 0),
                            "notes": row.get("notes", ""),
                            "sitrep_status": row.get("sitrep_status", "GREEN"),
                        }
                    )
            QMessageBox.information(self, "Export Local Operators", f"Exported {len(rows)} row(s).")
        except Exception as e:
            QMessageBox.warning(self, "Export Local Operators", f"Export failed:\n{e}")
