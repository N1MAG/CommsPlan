from __future__ import annotations

import datetime
import html
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QFileDialog,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QLineEdit,
    QFormLayout,
    QHeaderView,
    QScrollArea,
    QTextBrowser,
    QSizePolicy,
)

from freqinout.core.checkins_db import upsert_operator_metadata
from freqinout.core.logger import log
from freqinout.core.settings_manager import SettingsManager
from freqinout.utils.timezones import get_timezone
from freqinout.gui.help_registry import resolve_help_host
from freqinout.gui.theme import active_app_theme, resolve_theme, button_style, label_style
from freqinout.gui.peer_evidence_lane import PeerEvidenceLane


# Simple FEMA region mapping for filtering
FEMA_REGIONS = {
    "R01": ["CT", "ME", "MA", "NH", "RI", "VT"],
    "R02": ["NJ", "NY", "PR", "VI"],
    "R03": ["DC", "DE", "MD", "PA", "VA", "WV"],
    "R04": ["AL", "FL", "GA", "KY", "MS", "NC", "SC", "TN"],
    "R05": ["IL", "IN", "MI", "MN", "OH", "WI"],
    "R06": ["AR", "LA", "NM", "OK", "TX"],
    "R07": ["IA", "KS", "MO", "NE"],
    "R08": ["CO", "MT", "ND", "SD", "UT", "WY"],
    "R09": ["AZ", "CA", "HI", "NV", "GU", "AS", "MP"],
    "R10": ["AK", "ID", "OR", "WA"],
}
STATE_TO_REGION = {st: region for region, states in FEMA_REGIONS.items() for st in states}

DAY_CHOICES: Sequence[str] = (
    "ALL",
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)

TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
CALLSIGN_RE = re.compile(r"^[A-Z0-9/\\-]{3,20}$")


def _nets_db_path() -> Path:
    from freqinout.core.config_paths import get_config_dir

    return get_config_dir() / "config" / "freqinout_nets.db"


def _normalize_callsign(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return re.sub(r"/(P|M|MM|QRP|SOTA|ROVER|[A-Z0-9]{1,4})$", "", raw)


def _normalize_day_label(value: object) -> str:
    txt = str(value or "").strip()
    if not txt:
        return "ALL"
    up = txt.upper()
    if up in {"ALL", "DAILY"}:
        return "ALL"
    for day in DAY_CHOICES[1:]:
        low = day.lower()
        if txt.lower().startswith(low[:3]) or low.startswith(txt.lower()[:3]):
            return day
    raise ValueError("Select a valid UTC day.")


def _normalize_groups_csv(value: object) -> Tuple[List[str], str]:
    groups: List[str] = []
    seen = set()
    for raw in str(value or "").replace(";", ",").split(","):
        group = raw.strip().upper()
        if not group or group in seen:
            continue
        seen.add(group)
        groups.append(group)
    return groups, ", ".join(groups)


def _normalize_schedule_row_input(values: Dict[str, object]) -> Dict[str, str]:
    callsign = _normalize_callsign(values.get("callsign"))
    if not callsign or not CALLSIGN_RE.match(callsign):
        raise ValueError("Enter a valid callsign.")

    day_utc = _normalize_day_label(values.get("day_utc"))
    start_utc = str(values.get("start_utc") or "").strip()
    end_utc = str(values.get("end_utc") or "").strip()
    if not TIME_RE.match(start_utc):
        raise ValueError("Start UTC must use HH:MM.")
    if not TIME_RE.match(end_utc):
        raise ValueError("End UTC must use HH:MM.")

    band = str(values.get("band") or "").strip().upper()
    mode = str(values.get("mode") or "").strip().upper()
    frequency = str(values.get("frequency") or "").strip()
    if not band:
        raise ValueError("Band is required.")
    if not mode:
        raise ValueError("Mode is required.")
    if not frequency:
        raise ValueError("Frequency is required.")
    try:
        float(frequency)
    except Exception as exc:
        raise ValueError("Frequency must be numeric.") from exc

    groups_list, groups_display = _normalize_groups_csv(values.get("groups"))
    return {
        "callsign": callsign,
        "name": str(values.get("name") or "").strip(),
        "state": str(values.get("state") or "").strip().upper(),
        "groups": groups_display,
        "groups_json": json.dumps(groups_list) if groups_list else "",
        "day_utc": day_utc,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "band": band,
        "mode": mode,
        "frequency": frequency,
        "notes": str(values.get("notes") or "").strip(),
    }


def _schedule_row_key(row: Dict[str, object]) -> Tuple[str, str, str, str, str, str, str]:
    return (
        _normalize_callsign(row.get("callsign") or row.get("owner_callsign")),
        _normalize_day_label(row.get("day_utc")),
        str(row.get("start_utc") or "").strip(),
        str(row.get("end_utc") or "").strip(),
        str(row.get("band") or "").strip().upper(),
        str(row.get("mode") or "").strip().upper(),
        str(row.get("frequency") or "").strip(),
    )


def _delete_callsign_variants(cur: sqlite3.Cursor, table: str, callsign: str) -> int:
    base = _normalize_callsign(callsign)
    if not base:
        return 0
    deleted = 0
    try:
        cur.execute(
            f"""
            SELECT DISTINCT owner_callsign
            FROM {table}
            WHERE owner_callsign IS NOT NULL AND TRIM(owner_callsign) <> ''
            """
        )
        raw_values = [str(v or "").strip() for (v,) in cur.fetchall()]
    except Exception:
        raw_values = []
    for raw in raw_values:
        if _normalize_callsign(raw) != base:
            continue
        cur.execute(f"DELETE FROM {table} WHERE owner_callsign=?", (raw,))
        deleted += int(cur.rowcount or 0)
    return deleted


def _delete_explicit_row_variants(cur: sqlite3.Cursor, row_key: Tuple[str, str, str, str, str, str, str]) -> int:
    callsign, day_utc, start_utc, end_utc, band, mode, frequency = row_key
    base = _normalize_callsign(callsign)
    if not base:
        return 0
    deleted = 0
    try:
        cur.execute(
            """
            SELECT DISTINCT owner_callsign
            FROM peer_hf_schedule
            WHERE owner_callsign IS NOT NULL AND TRIM(owner_callsign) <> ''
            """
        )
        raw_values = [str(v or "").strip() for (v,) in cur.fetchall()]
    except Exception:
        raw_values = []
    for raw in raw_values:
        if _normalize_callsign(raw) != base:
            continue
        cur.execute(
            """
            DELETE FROM peer_hf_schedule
            WHERE owner_callsign=?
              AND UPPER(TRIM(day_utc))=UPPER(TRIM(?))
              AND TRIM(start_utc)=TRIM(?)
              AND TRIM(end_utc)=TRIM(?)
              AND UPPER(TRIM(band))=UPPER(TRIM(?))
              AND UPPER(TRIM(mode))=UPPER(TRIM(?))
              AND TRIM(frequency)=TRIM(?)
            """,
            (raw, day_utc, start_utc, end_utc, band, mode, frequency),
        )
        deleted += int(cur.rowcount or 0)
    return deleted


def save_explicit_peer_schedule_rows(
    db_path: Path,
    owner_callsign: str,
    rows: Sequence[Dict[str, str]],
    *,
    source_type: str,
    replace_callsign: bool = False,
    remove_row_keys: Optional[Sequence[Tuple[str, str, str, str, str, str, str]]] = None,
    operator_profile: Optional[Dict[str, str]] = None,
) -> int:
    owner = _normalize_callsign(owner_callsign)
    if not owner:
        raise ValueError("A valid callsign is required.")
    if not rows:
        raise ValueError("At least one schedule row is required.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        inserted = 0
        source_label = str(source_type or "MANUAL").strip().upper() or "MANUAL"
        normalized_rows = [dict(row) for row in rows]
        row_keys = {_schedule_row_key({"callsign": owner, **row}) for row in normalized_rows}
        if len(row_keys) != len(normalized_rows):
            raise ValueError("Duplicate schedule rows are not allowed.")

        if replace_callsign:
            _delete_callsign_variants(cur, "peer_hf_schedule", owner)
            _delete_callsign_variants(cur, "peer_hf_schedule_inferred", owner)
        if remove_row_keys:
            for row_key in remove_row_keys:
                _delete_explicit_row_variants(cur, row_key)

        for row in normalized_rows:
            meta_payload: Dict[str, Any] = {}
            notes = str(row.get("notes") or "").strip()
            if notes:
                meta_payload["notes"] = notes
            meta_json = json.dumps(meta_payload) if meta_payload else None
            now_str = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()
            cur.execute(
                """
                INSERT INTO peer_hf_schedule
                    (owner_callsign, day_utc, start_utc, end_utc, band, mode, frequency,
                     meta_json, imported_at, source_type, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner,
                    row["day_utc"],
                    row["start_utc"],
                    row["end_utc"],
                    row["band"],
                    row["mode"],
                    row["frequency"],
                    meta_json,
                    now_str,
                    source_label,
                    now_str,
                ),
            )
            inserted += 1

        if operator_profile:
            groups_json = operator_profile.get("groups_json") or ""
            groups_list = []
            if groups_json:
                try:
                    groups_list = json.loads(groups_json)
                except Exception:
                    groups_list = []
            upsert_operator_metadata(
                [
                    {
                        "callsign": owner,
                        "name": operator_profile.get("name") or "",
                        "state": operator_profile.get("state") or "",
                        "group1": groups_list[0] if len(groups_list) > 0 else "",
                        "group2": groups_list[1] if len(groups_list) > 1 else "",
                        "group3": groups_list[2] if len(groups_list) > 2 else "",
                        "groups_json": groups_json or None,
                        "trusted": 0,
                        "last_seen_utc": datetime.datetime.now(datetime.timezone.utc).replace(
                            tzinfo=None
                        ).isoformat(),
                    }
                ],
                conn=conn,
            )

        conn.commit()
        return inserted
    finally:
        conn.close()


class ManualPeerScheduleDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, *, initial: Optional[Dict[str, str]] = None):
        super().__init__(parent)
        self.setWindowTitle("Peer Schedule")
        self.setModal(True)
        self._build_ui(initial or {})

    def _build_ui(self, initial: Dict[str, str]) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        self.callsign_edit = QLineEdit(initial.get("callsign", ""))
        self.callsign_edit.setPlaceholderText("Callsign")
        form.addRow("Callsign", self.callsign_edit)

        self.name_edit = QLineEdit(initial.get("name", ""))
        form.addRow("Operator Name", self.name_edit)

        self.state_edit = QLineEdit(initial.get("state", ""))
        self.state_edit.setMaxLength(8)
        self.state_edit.setPlaceholderText("CO")
        form.addRow("State", self.state_edit)

        self.groups_edit = QLineEdit(initial.get("groups", ""))
        self.groups_edit.setPlaceholderText("MAGNET, AMRRON")
        form.addRow("Groups", self.groups_edit)

        self.day_combo = QComboBox()
        for day in DAY_CHOICES:
            self.day_combo.addItem(day)
        day_value = initial.get("day_utc", "ALL") or "ALL"
        idx = self.day_combo.findText(_normalize_day_label(day_value))
        self.day_combo.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("Day (UTC)", self.day_combo)

        self.start_edit = QLineEdit(initial.get("start_utc", ""))
        self.start_edit.setPlaceholderText("18:00")
        form.addRow("Start UTC", self.start_edit)

        self.end_edit = QLineEdit(initial.get("end_utc", ""))
        self.end_edit.setPlaceholderText("18:30")
        form.addRow("End UTC", self.end_edit)

        self.band_edit = QLineEdit(initial.get("band", ""))
        self.band_edit.setPlaceholderText("40M")
        form.addRow("Band", self.band_edit)

        self.mode_edit = QLineEdit(initial.get("mode", ""))
        self.mode_edit.setPlaceholderText("JS8")
        form.addRow("Mode", self.mode_edit)

        self.frequency_edit = QLineEdit(initial.get("frequency", ""))
        self.frequency_edit.setPlaceholderText("7.078")
        form.addRow("Frequency", self.frequency_edit)

        self.notes_edit = QLineEdit(initial.get("notes", ""))
        self.notes_edit.setPlaceholderText("Optional notes")
        form.addRow("Notes", self.notes_edit)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(label_style("danger", active_app_theme(), weight=600))
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def data(self) -> Dict[str, str]:
        return _normalize_schedule_row_input(
            {
                "callsign": self.callsign_edit.text(),
                "name": self.name_edit.text(),
                "state": self.state_edit.text(),
                "groups": self.groups_edit.text(),
                "day_utc": self.day_combo.currentText(),
                "start_utc": self.start_edit.text(),
                "end_utc": self.end_edit.text(),
                "band": self.band_edit.text(),
                "mode": self.mode_edit.text(),
                "frequency": self.frequency_edit.text(),
                "notes": self.notes_edit.text(),
            }
        )

    def _accept_if_valid(self) -> None:
        try:
            self.data()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.error_label.setText("")
        self.accept()


class PeerSchedTab(QWidget):
    """
    View and manage explicit and inferred peer HF schedules (non-net).
    """

    COLS: Sequence[str] = (
        "CALLSIGN",
        "NAME",
        "STATE",
        "GROUPS",
        "DAY (UTC)",
        "START UTC",
        "END UTC",
        "BAND",
        "MODE",
        "FREQ",
        "OVERLAP",
    )
    DAY_CANON = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = SettingsManager()
        self._rows: List[Dict] = []
        self._visible_rows: List[Dict] = []
        self._operator_meta: Dict[str, Dict[str, str]] = {}
        self._my_schedule: List[Dict] = []
        self._my_schedule_by_mode: Dict[str, List[Dict]] = {}
        default_mode = (self.settings.get("display_time_mode", "LOCAL") or "LOCAL").upper()
        self._show_local_times = default_mode != "UTC"
        self._responsive_layout_mode = "wide"
        self._responsive_compact_width = 1200
        self._peer_evidence_active = True
        self._peer_evidence_generation = 0
        self._peer_evidence_timezone = None
        self._peer_evidence_lane = PeerEvidenceLane(self)
        self._peer_evidence_lane.ready.connect(self._on_peer_evidence_ready)
        self._build_ui()
        self._load_operator_meta()
        self._load_data()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        self.peer_schedule_scroll_area = QScrollArea()
        self.peer_schedule_scroll_area.setObjectName("peerScheduleScrollArea")
        self.peer_schedule_scroll_area.setWidgetResizable(True)
        self.peer_schedule_scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.peer_schedule_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer_layout.addWidget(self.peer_schedule_scroll_area)

        content = QWidget()
        content.setObjectName("peerScheduleScrollContent")
        self.peer_schedule_scroll_area.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.addWidget(QLabel("<h3>Peer HF Schedules</h3>"))
        header.addStretch()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Reload peer schedule data from the database.")
        self.tz_toggle_btn = QPushButton("Times: UTC")
        self.tz_toggle_btn.setToolTip("Switch the table between UTC and local time display.")
        self.help_btn = QPushButton("Help")
        self.help_btn.setToolTip("Open HF Peers help.")
        header.addWidget(self.help_btn)
        layout.addLayout(header)

        self.schedule_source_label = QLabel("Schedule Source:")
        self.add_btn = QPushButton("Add Manual Row...")
        self.add_btn.setToolTip("Type one explicit peer schedule row by hand.")
        self.import_btn = QPushButton("Import Schedule File...")
        self.import_btn.setToolTip("Import a peer schedule JSON file.")
        self.selected_row_label = QLabel("Selected Row:")
        self.edit_btn = QPushButton("View/Edit Selected Row")
        self.edit_btn.setToolTip("View or edit the selected explicit row.")
        self.delete_row_btn = QPushButton("Delete Selected Row")
        self.delete_row_btn.setToolTip("Delete the selected explicit row.")
        self._peer_action_layout = QGridLayout()
        self._peer_action_layout.setContentsMargins(0, 0, 0, 0)
        self._peer_action_layout.setSpacing(8)
        layout.addLayout(self._peer_action_layout)

        self.cleanup_label = QLabel("Cleanup:")
        self.cleanup_callsign_label = QLabel("Remove schedule for callsign:")
        self.delete_callsign_combo = QComboBox()
        self.delete_callsign_combo.addItem("Select callsign", None)
        self.delete_callsign_combo.setMinimumWidth(150)
        self.delete_btn = QPushButton("Delete Callsign Schedule...")
        self.delete_btn.setToolTip("Delete all explicit schedule rows for the selected callsign.")
        self._peer_cleanup_layout = QGridLayout()
        self._peer_cleanup_layout.setContentsMargins(0, 0, 0, 0)
        self._peer_cleanup_layout.setSpacing(8)
        layout.addLayout(self._peer_cleanup_layout)

        # Filters
        self.callsign_filter_label = QLabel("Callsign:")
        self.callsign_filter = QComboBox()
        self.callsign_filter.addItem("All")
        self.callsign_filter.setMinimumWidth(150)

        self.region_filter_label = QLabel("Region:")
        self.region_filter = QComboBox()
        self.region_filter.addItem("All")
        for r in sorted(FEMA_REGIONS.keys()):
            self.region_filter.addItem(r)
        self.region_filter.setMinimumWidth(120)

        self.group_filter_label = QLabel("Group:")
        self.group_filter = QComboBox()
        self.group_filter.addItem("All")
        self.group_filter.setMinimumWidth(150)

        self.search_filter_label = QLabel("Search:")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by callsign/name/groups/band/mode/freq")
        self.clear_filters_btn = QPushButton("Clear Filters")
        self._peer_filter_layout = QGridLayout()
        self._peer_filter_layout.setContentsMargins(0, 0, 0, 0)
        self._peer_filter_layout.setSpacing(8)
        layout.addLayout(self._peer_filter_layout)

        self.inspect_evidence_label = QLabel("Evidence callsign:")
        self.inspect_evidence_edit = QLineEdit()
        self.inspect_evidence_edit.setPlaceholderText("Callsign")
        self.inspect_evidence_edit.setAccessibleName("Evidence callsign")
        self.inspect_evidence_edit.setClearButtonEnabled(True)
        self.inspect_evidence_btn = QPushButton("Inspect Evidence")
        self.inspect_evidence_btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.inspect_evidence_btn.setToolTip("Read bounded retained evidence for the entered callsign.")
        self.inspect_evidence_disclosure_btn = QPushButton("Evidence details")
        self.inspect_evidence_disclosure_btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.inspect_evidence_disclosure_btn.setCheckable(True)
        self.inspect_evidence_disclosure_btn.setChecked(False)
        self.inspect_evidence_disclosure_btn.setToolTip("Show or hide retained evidence details.")
        self.inspect_evidence_status = QLabel("Enter a callsign, or select a schedule row to fill it.")
        self.inspect_evidence_status.setWordWrap(True)
        self._peer_evidence_layout = QGridLayout()
        self._peer_evidence_layout.setContentsMargins(0, 0, 0, 0)
        self._peer_evidence_layout.setSpacing(8)
        layout.addLayout(self._peer_evidence_layout)
        layout.addWidget(self.inspect_evidence_status)

        # Table
        self.table = QTableWidget(0, len(self.COLS))
        self._overlap_col = self.COLS.index("OVERLAP")
        self._set_time_headers()
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        layout.addWidget(self.table)

        self.inspect_evidence_browser = QTextBrowser()
        self.inspect_evidence_browser.setObjectName("peerEvidenceInspector")
        self.inspect_evidence_browser.setReadOnly(True)
        self.inspect_evidence_browser.setOpenExternalLinks(False)
        self.inspect_evidence_browser.setMinimumHeight(0)
        self.inspect_evidence_browser.setMaximumHeight(16777215)
        self.inspect_evidence_browser.setVisible(False)
        layout.addWidget(self.inspect_evidence_browser)
        self._arrange_peer_action_rows(compact=False)

        # Signals
        self.add_btn.clicked.connect(self._add_schedule)
        self.edit_btn.clicked.connect(self._edit_selected_row)
        self.delete_row_btn.clicked.connect(self._delete_selected_row)
        self.import_btn.clicked.connect(self._import_schedule)
        self.refresh_btn.clicked.connect(self._load_data)
        self.callsign_filter.currentIndexChanged.connect(self._apply_filters)
        self.region_filter.currentIndexChanged.connect(self._apply_filters)
        self.group_filter.currentIndexChanged.connect(self._apply_filters)
        self.search_edit.textChanged.connect(self._apply_filters)
        self.clear_filters_btn.clicked.connect(self._clear_filters)
        self.delete_callsign_combo.currentIndexChanged.connect(self._update_delete_button_state)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.tz_toggle_btn.clicked.connect(self._toggle_timezone_view)
        self.help_btn.clicked.connect(lambda: self._open_context_help("tab.hf-peers"))
        self.table.cellClicked.connect(self._on_table_cell_clicked)
        self.table.itemSelectionChanged.connect(self._update_row_action_state)
        self.inspect_evidence_edit.textChanged.connect(self._on_peer_evidence_entry_changed)
        self.inspect_evidence_btn.clicked.connect(self._inspect_peer_evidence)
        self.inspect_evidence_disclosure_btn.toggled.connect(self._toggle_peer_evidence_details)
        self.tz_toggle_btn.setText("Times: Local" if self._show_local_times else "Times: UTC")
        self._apply_theme()
        self._update_delete_button_state()
        self._update_row_action_state()
        self._update_peer_responsive_layout()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_peer_responsive_layout()

    def _peer_responsive_mode_for_width(self, width: int) -> str:
        try:
            return "compact" if int(width) < int(self._responsive_compact_width) else "wide"
        except Exception:
            return "wide"

    def _update_peer_responsive_layout(self) -> None:
        if not hasattr(self, "_peer_filter_layout"):
            return
        mode = self._peer_responsive_mode_for_width(int(self.width() or 0))
        if mode == self._responsive_layout_mode and self._peer_filter_layout.count() > 0:
            return
        self._responsive_layout_mode = mode
        self._arrange_peer_action_rows(compact=(mode == "compact"))
        self._apply_peer_table_sizing(compact=(mode == "compact"))
        self._update_peer_evidence_geometry()

    def _update_peer_evidence_geometry(self) -> None:
        if not hasattr(self, "inspect_evidence_browser"):
            return
        line_height = max(1, self.fontMetrics().height())
        browser_line_height = max(1, self.inspect_evidence_browser.fontMetrics().height())
        minimum = max(line_height * 5, browser_line_height * 6) + line_height * 2
        maximum = max(minimum * 3, browser_line_height * 18)
        self.inspect_evidence_browser.setMinimumHeight(minimum)
        self.inspect_evidence_browser.setMaximumHeight(maximum)

    @staticmethod
    def _clear_grid_layout(layout: QGridLayout) -> None:
        while layout.count():
            layout.takeAt(0)

    @staticmethod
    def _place_grid_widgets(layout: QGridLayout, placements: list[tuple]) -> None:
        for col in range(12):
            layout.setColumnStretch(col, 0)
        for item in placements:
            widget, row, col, *span = item
            row_span, col_span = span if span else (1, 1)
            layout.addWidget(widget, row, col, row_span, col_span)

    def _arrange_peer_action_rows(self, *, compact: bool) -> None:
        for grid in (
            self._peer_action_layout,
            self._peer_cleanup_layout,
            self._peer_filter_layout,
            self._peer_evidence_layout,
        ):
            self._clear_grid_layout(grid)

        if compact:
            for combo in (
                self.delete_callsign_combo,
                self.callsign_filter,
                self.region_filter,
                self.group_filter,
            ):
                combo.setMinimumWidth(0)
            action_placements = [
                (self.schedule_source_label, 0, 0),
                (self.add_btn, 0, 1),
                (self.import_btn, 0, 2),
                (self.refresh_btn, 0, 3),
                (self.selected_row_label, 1, 0),
                (self.edit_btn, 1, 1),
                (self.delete_row_btn, 1, 2),
            ]
            cleanup_placements = [
                (self.cleanup_label, 0, 0),
                (self.cleanup_callsign_label, 0, 1),
                (self.delete_callsign_combo, 0, 2),
                (self.delete_btn, 0, 3),
            ]
            filter_placements = [
                (self.callsign_filter_label, 0, 0),
                (self.callsign_filter, 0, 1),
                (self.region_filter_label, 0, 2),
                (self.region_filter, 0, 3),
                (self.group_filter_label, 0, 4),
                (self.group_filter, 0, 5),
                (self.search_filter_label, 1, 0),
                (self.search_edit, 1, 1, 1, 4),
                (self.clear_filters_btn, 1, 5),
                (self.tz_toggle_btn, 1, 6),
            ]
            evidence_placements = [
                (self.inspect_evidence_label, 0, 0),
                (self.inspect_evidence_edit, 0, 1, 1, 3),
                (self.inspect_evidence_btn, 1, 0),
                (self.inspect_evidence_disclosure_btn, 1, 1),
            ]
        else:
            self.delete_callsign_combo.setMinimumWidth(150)
            self.callsign_filter.setMinimumWidth(150)
            self.region_filter.setMinimumWidth(120)
            self.group_filter.setMinimumWidth(150)
            action_placements = [
                (self.schedule_source_label, 0, 0),
                (self.add_btn, 0, 1),
                (self.import_btn, 0, 2),
                (self.refresh_btn, 0, 3),
                (self.selected_row_label, 0, 4),
                (self.edit_btn, 0, 5),
                (self.delete_row_btn, 0, 6),
            ]
            cleanup_placements = [
                (self.cleanup_label, 0, 0),
                (self.cleanup_callsign_label, 0, 1),
                (self.delete_callsign_combo, 0, 2),
                (self.delete_btn, 0, 3),
            ]
            filter_placements = [
                (self.callsign_filter_label, 0, 0),
                (self.callsign_filter, 0, 1),
                (self.region_filter_label, 0, 2),
                (self.region_filter, 0, 3),
                (self.group_filter_label, 0, 4),
                (self.group_filter, 0, 5),
                (self.search_filter_label, 0, 6),
                (self.search_edit, 0, 7),
                (self.clear_filters_btn, 0, 8),
                (self.tz_toggle_btn, 0, 9),
            ]
            evidence_placements = [
                (self.inspect_evidence_label, 0, 0),
                (self.inspect_evidence_edit, 0, 1),
                (self.inspect_evidence_btn, 0, 2),
                (self.inspect_evidence_disclosure_btn, 0, 3),
            ]

        self._place_grid_widgets(self._peer_action_layout, action_placements)
        self._place_grid_widgets(self._peer_cleanup_layout, cleanup_placements)
        self._place_grid_widgets(self._peer_filter_layout, filter_placements)
        self._place_grid_widgets(self._peer_evidence_layout, evidence_placements)
        self._peer_action_layout.setColumnStretch(7 if not compact else 4, 1)
        self._peer_cleanup_layout.setColumnStretch(4, 1)
        self._peer_filter_layout.setColumnStretch(7 if not compact else 4, 1)
        self._peer_evidence_layout.setColumnStretch(1, 1)

    def _apply_peer_table_sizing(self, *, compact: bool) -> None:
        if not hasattr(self, "table"):
            return
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        # Keep the identity column readable when the compact table owns its
        # horizontal overflow; the table may scroll, but the page must not.
        stretch_columns = {1, 3, self._overlap_col} if compact else {1, 3, self._overlap_col}
        for col in range(self.table.columnCount()):
            if col in stretch_columns:
                header.setSectionResizeMode(col, QHeaderView.Stretch)
            else:
                header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        if compact:
            metrics = self.table.fontMetrics()
            readable_width = metrics.horizontalAdvance("CALLSIGN") + metrics.horizontalAdvance("M") * 3
            self.table.setColumnWidth(0, max(self.table.columnWidth(0), readable_width))

    # ---------- data ----------

    def _db_path(self) -> Path:
        return _nets_db_path()

    @staticmethod
    def _normalize_callsign(value: object) -> str:
        return _normalize_callsign(value)

    def _load_operator_meta(self) -> None:
        """
        Load operator info (name/state/groups) for display and region mapping.
        """
        self._operator_meta = {}
        try:
            db_path = self._db_path()
            if not db_path.exists():
                return
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT callsign, name, state, group1, group2, group3, groups_json FROM operator_checkins"
            )
            for cs, name, state, g1, g2, g3, gj in cur.fetchall():
                cs_norm = self._normalize_callsign(cs)
                if not cs_norm:
                    continue
                groups: List[str] = []
                for g in (g1, g2, g3):
                    if g:
                        groups.append(str(g).strip().upper())
                try:
                    if gj:
                        for g in json.loads(gj):
                            gtxt = str(g).strip().upper()
                            if gtxt:
                                groups.append(gtxt)
                except Exception:
                    pass
                deduped: List[str] = []
                seen = set()
                for g in groups:
                    if g and g not in seen:
                        seen.add(g)
                        deduped.append(g)
                self._operator_meta[cs_norm] = {
                    "name": (name or "").strip(),
                    "state": (state or "").strip().upper(),
                    "groups": ", ".join(deduped),
                }
            conn.close()
        except Exception as e:
            log.debug("PeerSched: failed to load operator meta: %s", e)

    def _load_data(self) -> None:
        """
        Load peer schedules from DB and populate filters/table.
        """
        self._fence_peer_evidence()
        self._load_operator_meta()
        self._rows = []
        try:
            db_path = self._db_path()
            if not db_path.exists():
                return
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            has_effective_view = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name='peer_hf_schedule_effective'"
            ).fetchone()
            if has_effective_view:
                cur.execute(
                    """
                    SELECT
                        owner_callsign,
                        day_utc,
                        start_utc,
                        end_utc,
                        band,
                        mode,
                        frequency,
                        source_type,
                        confidence
                    FROM peer_hf_schedule_effective
                    """
                )
                rows = cur.fetchall()
            else:
                cur.execute(
                    """
                    SELECT owner_callsign, day_utc, start_utc, end_utc, band, mode, frequency, 'IMPORTED', NULL
                    FROM peer_hf_schedule
                """
                )
                rows = cur.fetchall()
            # Collapse suffix variants to base callsign and dedupe by schedule identity.
            deduped_rows: Dict[tuple, Dict] = {}
            for cs, day, start, end, band, mode, freq, source_type, confidence in rows:
                cs_norm = self._normalize_callsign(cs)
                if not cs_norm:
                    continue
                day_val = (day or "ALL")
                start_val = (start or "")
                end_val = (end or "")
                band_val = (band or "")
                mode_val = (mode or "")
                freq_txt = str(freq or "")
                src = (source_type or "IMPORTED")
                key = (
                    cs_norm,
                    str(day_val).strip().upper(),
                    str(start_val).strip(),
                    str(end_val).strip(),
                    str(band_val).strip().upper(),
                    str(mode_val).strip().upper(),
                    str(freq_txt).strip(),
                )
                row_obj = {
                    "callsign": cs_norm,
                    "day_utc": day_val,
                    "start_utc": start_val,
                    "end_utc": end_val,
                    "band": band_val,
                    "mode": mode_val,
                    "frequency": freq_txt,
                    "freq_num": self._parse_freq(freq),
                    "source_type": src,
                    "confidence": confidence,
                }
                src_upper = str(src).strip().upper()
                if src_upper == "MANUAL":
                    src_priority = 2
                elif src_upper == "IMPORTED":
                    src_priority = 1
                else:
                    src_priority = 0
                prev = deduped_rows.get(key)
                if prev is None:
                    row_obj["_src_priority"] = src_priority
                    deduped_rows[key] = row_obj
                    continue
                prev_priority = int(prev.get("_src_priority", 0) or 0)
                if src_priority > prev_priority:
                    row_obj["_src_priority"] = src_priority
                    deduped_rows[key] = row_obj
                    continue
                if src_priority == prev_priority:
                    try:
                        conf_prev = float(prev.get("confidence") or 0.0)
                    except Exception:
                        conf_prev = 0.0
                    try:
                        conf_new = float(confidence or 0.0)
                    except Exception:
                        conf_new = 0.0
                    if conf_new > conf_prev:
                        row_obj["_src_priority"] = src_priority
                        deduped_rows[key] = row_obj
            self._rows = sorted(
                [
                    {k: v for k, v in row.items() if k != "_src_priority"}
                    for row in deduped_rows.values()
                ],
                key=lambda r: (
                    str(r.get("callsign", "")),
                    str(r.get("day_utc", "")),
                    str(r.get("start_utc", "")),
                    str(r.get("frequency", "")),
                ),
            )
            conn.close()
        except Exception as e:
            log.error("PeerSched: failed to load peer schedules: %s", e)
        self._load_my_schedule()
        self._populate_filters()
        self._populate_delete_callsigns()
        self._apply_filters()

    def _populate_filters(self) -> None:
        calls = sorted({row["callsign"] for row in self._rows if row.get("callsign")})
        groups = sorted(
            {
                g.strip()
                for meta in self._operator_meta.values()
                for g in meta.get("groups", "").split(",")
                if g.strip()
            }
        )

        # Callsign filter
        current_call = self.callsign_filter.currentText()
        self.callsign_filter.blockSignals(True)
        self.callsign_filter.clear()
        self.callsign_filter.addItem("All")
        for c in calls:
            self.callsign_filter.addItem(c)
        idx = self.callsign_filter.findText(current_call)
        if idx >= 0:
            self.callsign_filter.setCurrentIndex(idx)
        self.callsign_filter.blockSignals(False)

        # Group filter
        current_group = self.group_filter.currentText()
        self.group_filter.blockSignals(True)
        self.group_filter.clear()
        self.group_filter.addItem("All")
        for g in groups:
            self.group_filter.addItem(g)
        idx = self.group_filter.findText(current_group)
        if idx >= 0:
            self.group_filter.setCurrentIndex(idx)
        self.group_filter.blockSignals(False)

    def _populate_delete_callsigns(self) -> None:
        calls = sorted(
            {
                row["callsign"]
                for row in self._rows
                if row.get("callsign")
                and str(row.get("source_type") or "").strip().upper() != "INFERRED"
            }
        )
        current = self.delete_callsign_combo.currentData()
        self.delete_callsign_combo.blockSignals(True)
        self.delete_callsign_combo.clear()
        self.delete_callsign_combo.addItem("Select callsign", None)
        self.delete_callsign_combo.addItem("Clear All Schedules", "__CLEAR_ALL__")
        for c in calls:
            self.delete_callsign_combo.addItem(c, c)
        idx = self.delete_callsign_combo.findData(current)
        if idx >= 0:
            self.delete_callsign_combo.setCurrentIndex(idx)
        self.delete_callsign_combo.blockSignals(False)
        self._update_delete_button_state()

    def _apply_filters(self) -> None:
        cs_filter = self.callsign_filter.currentText()
        region_filter = self.region_filter.currentText()
        group_filter = self.group_filter.currentText()
        search = self.search_edit.text().strip().lower()

        filtered: List[Dict] = []
        for row in self._rows:
            cs = row.get("callsign", "")
            if cs_filter != "All" and cs != cs_filter:
                continue
            meta = self._operator_meta.get(cs, {})
            state = meta.get("state", "")
            region = STATE_TO_REGION.get(state, "")
            if region_filter != "All" and region != region_filter:
                continue
            groups = [g.strip() for g in meta.get("groups", "").split(",") if g.strip()]
            if group_filter != "All" and group_filter not in groups:
                continue
            if search:
                blob = " ".join(
                    [
                        cs,
                        meta.get("name", ""),
                        meta.get("groups", ""),
                        row.get("band", ""),
                        row.get("mode", ""),
                        row.get("frequency", ""),
                    ]
                ).lower()
                if search not in blob:
                    continue
            filtered.append(row)

        self._visible_rows = filtered
        was_sorting = self.table.isSortingEnabled()
        sort_col = self.table.horizontalHeader().sortIndicatorSection()
        sort_order = self.table.horizontalHeader().sortIndicatorOrder()
        if was_sorting:
            self.table.setSortingEnabled(False)
        self.table.setRowCount(len(filtered))
        for r, row in enumerate(filtered):
            cs = row.get("callsign", "")
            meta = self._operator_meta.get(cs, {})
            overlap_ranges = self._compute_overlaps(row)
            overlap_display = self._format_overlap_summary(overlap_ranges)
            mode_val = row.get("mode", "")
            source_upper = str(row.get("source_type") or "").strip().upper()
            if source_upper == "INFERRED":
                mode_val = f"{mode_val} [I]"
            elif source_upper == "MANUAL":
                mode_val = f"{mode_val} [M]"
            vals = [
                cs,
                meta.get("name", ""),
                meta.get("state", ""),
                meta.get("groups", ""),
                row.get("day_utc", ""),
                row.get("start_utc", ""),
                row.get("end_utc", ""),
                row.get("band", ""),
                mode_val,
                row.get("frequency", ""),
                overlap_display,
            ]
            if self._show_local_times:
                day_loc, start_loc = self._convert_day_time(row.get("day_utc", ""), row.get("start_utc", ""))
                _, end_loc = self._convert_day_time(row.get("day_utc", ""), row.get("end_utc", ""))
                vals[4] = day_loc
                vals[5] = start_loc
                vals[6] = end_loc
            for c, val in enumerate(vals):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if c == 0:
                    item.setData(Qt.UserRole, dict(row))
                if c == self._overlap_col and overlap_ranges:
                    item.setToolTip("\n".join(self._format_overlap_ranges(overlap_ranges)))
                    item.setData(Qt.UserRole, overlap_ranges)
                self.table.setItem(r, c, item)
        if was_sorting:
            self.table.setSortingEnabled(True)
            if 0 <= sort_col < self.table.columnCount():
                self.table.sortItems(sort_col, sort_order)
        self._apply_peer_table_sizing(compact=self._responsive_layout_mode == "compact")
        self._update_row_action_state()

    # ---------- helpers ----------

    def _selected_delete_action(self) -> Optional[str]:
        selected = self.delete_callsign_combo.currentData()
        if not selected:
            return None
        if selected == "__CLEAR_ALL__":
            return selected
        return self._normalize_callsign(selected)

    def _delete_callsign_variants(self, cur: sqlite3.Cursor, table: str, callsign: str) -> int:
        return _delete_callsign_variants(cur, table, callsign)

    def _settings_db_path(self) -> Path:
        cfg_path = getattr(self.settings, "_config_path", None)
        if cfg_path:
            try:
                return Path(cfg_path)
            except Exception:
                pass
        from freqinout.core.config_paths import get_config_dir

        return get_config_dir() / "config" / "freqinout.db"

    def _current_timezone(self) -> datetime.tzinfo:
        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        return get_timezone(tz_name)

    def _day_offset(self, day_name: str) -> int:
        if not day_name:
            return 0
        day_name = day_name.strip().lower()
        for idx, name in enumerate(self.DAY_CANON):
            if name.lower().startswith(day_name[:3]):
                return idx
        return 0

    def _anchor_utc_sunday(self) -> datetime.datetime:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        delta = (now_utc.weekday() + 1) % 7  # Sunday=0, Monday=1, ...
        sunday = now_utc - datetime.timedelta(days=delta)
        return sunday.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=datetime.timezone.utc)

    def _convert_day_time(self, day: str, hhmm: str) -> tuple[str, str]:
        day = (day or "ALL").strip()
        if not hhmm:
            return day, hhmm
        try:
            hour, minute = hhmm.split(":")
            hour = int(hour)
            minute = int(minute)
        except Exception:
            return day, hhmm
        day_upper = day.upper()
        day_idx = 0 if day_upper == "ALL" else self._day_offset(day)
        anchor = self._anchor_utc_sunday()
        dt_utc = anchor + datetime.timedelta(days=day_idx, hours=hour, minutes=minute)
        dt_loc = dt_utc.astimezone(self._current_timezone())
        day_label = "ALL" if day_upper == "ALL" else dt_loc.strftime("%A")
        return day_label, dt_loc.strftime("%H:%M")

    def _parse_time_minutes(self, hhmm: str) -> Optional[int]:
        if not hhmm:
            return None
        try:
            hour, minute = hhmm.split(":")
            return int(hour) * 60 + int(minute)
        except Exception:
            return None

    def _parse_freq(self, freq_val) -> Optional[float]:
        try:
            return float(str(freq_val).strip())
        except Exception:
            return None

    def _day_to_index(self, day_name: str) -> Optional[int]:
        if not day_name:
            return None
        txt = day_name.strip().lower()
        for idx, name in enumerate(self.DAY_CANON):
            low = name.lower()
            if txt.startswith(low[:3]) or low.startswith(txt[:3]):
                return idx
        return None

    def _normalize_day(self, day: str) -> str:
        val = (day or "ALL").strip()
        up = val.upper()
        if up in {"", "ALL", "DAILY"}:
            return "ALL"
        idx = self._day_to_index(val)
        if idx is None:
            return "ALL"
        return self.DAY_CANON[idx]

    def _expand_week_segments(self, day: str, start_min: int, end_min: int) -> List[tuple[int, int, int]]:
        """
        Expand a schedule row into weekly minute windows:
          (weekday_index_sun0, start_minute, end_minute)
        Supports overnight ranges by splitting across day boundary.
        """
        if start_min < 0 or start_min > 1439 or end_min < 0 or end_min > 1439:
            return []
        days: List[int]
        day_norm = self._normalize_day(day)
        if day_norm == "ALL":
            days = list(range(7))
        else:
            idx = self._day_to_index(day_norm)
            if idx is None:
                return []
            days = [idx]
        segments: List[tuple[int, int, int]] = []
        for d in days:
            if end_min > start_min:
                segments.append((d, start_min, end_min))
                continue
            if end_min == start_min:
                continue
            # Overnight: split into [start,24:00) on day d and [00:00,end) next day.
            segments.append((d, start_min, 24 * 60))
            segments.append(((d + 1) % 7, 0, end_min))
        return segments

    def _load_my_schedule(self) -> None:
        self._my_schedule = []
        self._my_schedule_by_mode = {}

        def add_entry(day: str, start: str, end: str, mode: str, freq) -> None:
            start_min = self._parse_time_minutes(start)
            end_min = self._parse_time_minutes(end)
            freq_num = self._parse_freq(freq)
            if start_min is None or end_min is None or freq_num is None:
                return
            mode_key = (mode or "").strip().upper()
            if not mode_key:
                return
            segments = self._expand_week_segments(day, start_min, end_min)
            if not segments:
                return
            entry = {
                "day_utc": self._normalize_day(day),
                "start_min": start_min,
                "end_min": end_min,
                "segments": segments,
                "mode": mode_key,
                "freq": freq_num,
            }
            self._my_schedule.append(entry)
            self._my_schedule_by_mode.setdefault(mode_key, []).append(entry)

        db_path = self._settings_db_path()
        if db_path.exists():
            try:
                conn = sqlite3.connect(db_path)
                has_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_schedule_tab'"
                ).fetchone()
                if has_table:
                    for day, start, end, mode, freq in conn.execute(
                        "SELECT day_utc, start_utc, end_utc, mode, frequency FROM daily_schedule_tab"
                    ).fetchall():
                        add_entry(day, start, end, mode, freq)
                conn.close()
            except Exception as e:
                log.debug("PeerSched: failed to load daily schedule: %s", e)

        nets_path = self._db_path()
        if nets_path.exists():
            try:
                conn = sqlite3.connect(nets_path)
                has_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='net_schedule_tab'"
                ).fetchone()
                if has_table:
                    for day, start, end, mode, freq in conn.execute(
                        "SELECT day_utc, start_utc, end_utc, mode, frequency FROM net_schedule_tab"
                    ).fetchall():
                        add_entry(day, start, end, mode, freq)
                conn.close()
            except Exception as e:
                log.debug("PeerSched: failed to load net schedule: %s", e)

    def _compute_overlaps(self, row: Dict) -> List[tuple[int, int, int]]:
        mode = (row.get("mode") or "").strip().upper()
        freq = row.get("freq_num")
        if not mode or freq is None:
            return []
        peer_start = self._parse_time_minutes(row.get("start_utc", ""))
        peer_end = self._parse_time_minutes(row.get("end_utc", ""))
        if peer_start is None or peer_end is None:
            return []
        peer_segments = self._expand_week_segments(row.get("day_utc", "ALL"), peer_start, peer_end)
        if not peer_segments:
            return []

        matches = self._my_schedule_by_mode.get(mode, [])
        overlaps: List[tuple[int, int, int]] = []
        by_day: Dict[int, List[tuple[int, int]]] = {}
        for day_idx, seg_start, seg_end in peer_segments:
            by_day.setdefault(day_idx, []).append((seg_start, seg_end))
        for entry in matches:
            if abs(entry["freq"] - freq) > 0.0001:
                continue
            entry_segments = entry.get("segments") or []
            for day_idx, my_start, my_end in entry_segments:
                peers = by_day.get(int(day_idx), [])
                if not peers:
                    continue
                for peer_seg_start, peer_seg_end in peers:
                    start = max(int(peer_seg_start), int(my_start))
                    end = min(int(peer_seg_end), int(my_end))
                    if end > start:
                        overlaps.append((int(day_idx), start, end))
        if not overlaps:
            return overlaps
        # Merge touching/overlapping ranges per day for cleaner display.
        overlaps.sort(key=lambda x: (x[0], x[1], x[2]))
        merged: List[tuple[int, int, int]] = []
        for day_idx, start, end in overlaps:
            if not merged or merged[-1][0] != day_idx or start > merged[-1][2]:
                merged.append((day_idx, start, end))
                continue
            prev_day, prev_start, prev_end = merged[-1]
            merged[-1] = (prev_day, prev_start, max(prev_end, end))
        return merged

    def _format_overlap_ranges(self, ranges: List[tuple[int, int, int]]) -> List[str]:
        if not ranges:
            return []
        base_utc = self._anchor_utc_sunday()
        tz = self._current_timezone()
        use_local = self._show_local_times
        formatted = []
        for day_idx, start_min, end_min in ranges:
            start_dt = base_utc + datetime.timedelta(days=int(day_idx), minutes=int(start_min))
            end_dt = base_utc + datetime.timedelta(days=int(day_idx), minutes=int(end_min))
            if use_local:
                start_dt = start_dt.astimezone(tz)
                end_dt = end_dt.astimezone(tz)
            day_label = start_dt.strftime("%a")
            formatted.append(f"{day_label} {start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}")
        return formatted

    def _overlap_range_datetimes(
        self,
        ranges: List[tuple[int, int, int]],
    ) -> List[tuple[datetime.datetime, datetime.datetime]]:
        if not ranges:
            return []
        base_utc = self._anchor_utc_sunday()
        use_local = self._show_local_times
        tz = self._current_timezone() if use_local else datetime.timezone.utc
        out: List[tuple[datetime.datetime, datetime.datetime]] = []
        for day_idx, start_min, end_min in ranges:
            start_utc = base_utc + datetime.timedelta(days=int(day_idx), minutes=int(start_min))
            end_utc = base_utc + datetime.timedelta(days=int(day_idx), minutes=int(end_min))
            if use_local:
                start_dt = start_utc.astimezone(tz)
                end_dt = end_utc.astimezone(tz)
            else:
                start_dt = start_utc
                end_dt = end_utc
            out.append((start_dt, end_dt))
        out.sort(key=lambda it: it[0])
        return out

    def _format_overlap_summary(self, ranges: List[tuple[int, int, int]]) -> str:
        if not ranges:
            return ""
        slots = self._overlap_range_datetimes(ranges)
        if not slots:
            return ""
        use_local = self._show_local_times
        tz = self._current_timezone() if use_local else datetime.timezone.utc
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_dt = now_utc.astimezone(tz) if use_local else now_utc

        # 1) Action now: currently overlapping.
        active = [slot for slot in slots if slot[0] <= now_dt < slot[1]]
        if active:
            start_dt, end_dt = min(active, key=lambda it: it[1])
            return f"NOW {start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"

        # 2) Action later today.
        later_today = [slot for slot in slots if slot[0].date() == now_dt.date() and slot[0] > now_dt]
        if later_today:
            start_dt, end_dt = min(later_today, key=lambda it: it[0])
            return f"Today {start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"

        # 3) Next weekly overlap (roll past slots by +7 days).
        rolled: List[tuple[datetime.datetime, datetime.datetime]] = []
        for start_dt, end_dt in slots:
            while end_dt <= now_dt:
                start_dt += datetime.timedelta(days=7)
                end_dt += datetime.timedelta(days=7)
            rolled.append((start_dt, end_dt))
        if not rolled:
            return ""
        next_start, next_end = min(rolled, key=lambda it: it[0])
        if next_start.date() == now_dt.date():
            return f"Today {next_start.strftime('%H:%M')}-{next_end.strftime('%H:%M')}"
        return f"{next_start.strftime('%a')} {next_start.strftime('%H:%M')}-{next_end.strftime('%H:%M')}"

    def _set_time_headers(self) -> None:
        cols = list(self.COLS)
        if self._show_local_times:
            cols[4] = "DAY (Local)"
            cols[5] = "START (Local)"
            cols[6] = "END (Local)"
            cols[self._overlap_col] = "OVERLAP (Local)"
        else:
            cols[4] = "DAY (UTC)"
            cols[5] = "START UTC"
            cols[6] = "END UTC"
            cols[self._overlap_col] = "OVERLAP (UTC)"
        self.table.setHorizontalHeaderLabels(cols)

    def _toggle_timezone_view(self) -> None:
        self._show_local_times = not self._show_local_times
        self.tz_toggle_btn.setText("Times: Local" if self._show_local_times else "Times: UTC")
        self._set_time_headers()
        self._update_timezone_button_style()
        self._apply_filters()

    def _apply_theme(self) -> None:
        theme = resolve_theme(self.settings)
        self.add_btn.setStyleSheet(button_style("primary", theme))
        self.import_btn.setStyleSheet(button_style("secondary", theme))
        self.refresh_btn.setStyleSheet(button_style("muted", theme))
        self.help_btn.setStyleSheet(button_style("secondary", theme))
        self.delete_btn.setStyleSheet(button_style("muted", theme))
        self.clear_filters_btn.setStyleSheet(button_style("muted", theme))
        self.inspect_evidence_btn.setStyleSheet(button_style("primary", theme))
        self.inspect_evidence_disclosure_btn.setStyleSheet(button_style("secondary", theme))
        self._update_timezone_button_style()
        self._update_peer_evidence_geometry()
        self._update_row_action_state()

    def apply_theme(self) -> None:
        self._apply_theme()

    def _clear_filters(self) -> None:
        self.callsign_filter.setCurrentIndex(0)
        self.region_filter.setCurrentIndex(0)
        self.group_filter.setCurrentIndex(0)
        self.search_edit.clear()

    def _update_delete_button_state(self) -> None:
        theme = resolve_theme(self.settings)
        selection = self._selected_delete_action()
        has_rows = any(
            str(r.get("source_type") or "").strip().upper() != "INFERRED"
            for r in self._rows
        )
        if selection == "__CLEAR_ALL__":
            enabled = has_rows
            label = "Clear All Schedules..."
        elif selection:
            enabled = True
            label = "Delete Callsign Schedule..."
        else:
            enabled = False
            label = "Delete Callsign Schedule..."
        self.delete_btn.setEnabled(enabled)
        self.delete_btn.setText(label)
        role = "danger" if enabled else "muted"
        self.delete_btn.setStyleSheet(button_style(role, theme))

    def _selected_row_payload(self) -> Optional[Dict[str, Any]]:
        row_idx = self.table.currentRow()
        if row_idx < 0:
            return None
        item = self.table.item(row_idx, 0)
        payload = item.data(Qt.UserRole) if item is not None else None
        return dict(payload) if isinstance(payload, dict) else None

    def _fence_peer_evidence(self) -> None:
        self._peer_evidence_generation += 1
        self._peer_evidence_lane.invalidate()

    def _on_peer_evidence_entry_changed(self, _text: str) -> None:
        # Entry edits are navigation within the inspector, never an implicit
        # read.  They only fence a result that may belong to the old call.
        self._fence_peer_evidence()
        self.inspect_evidence_status.setText("Callsign changed; press Inspect Evidence to refresh retained evidence.")

    def _evidence_local_call(self) -> str:
        for key in ("operator_callsign", "callsign", "station_callsign"):
            try:
                value = str(self.settings.get(key, "") or "").strip().upper()
            except Exception:
                value = ""
            if value:
                return value
        return ""

    @staticmethod
    def _evidence_call(value: object) -> str:
        return str(value or "").strip().upper()

    @staticmethod
    def _evidence_html(value: object) -> str:
        return html.escape(str(value if value is not None else ""), quote=True)

    def _format_evidence_timestamp(self, value: object) -> str:
        try:
            timestamp = float(value)
            utc = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
            if self._peer_evidence_timezone is None:
                self._peer_evidence_timezone = self._current_timezone()
            local = utc.astimezone(self._peer_evidence_timezone)
            return f"UTC {utc.strftime('%Y-%m-%d %H:%M:%S')} · local {local.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        except (TypeError, ValueError, OverflowError, OSError):
            return "time unknown"

    def _toggle_peer_evidence_details(self, visible: bool) -> None:
        self.inspect_evidence_browser.setVisible(bool(visible))
        self.inspect_evidence_disclosure_btn.setText(
            "Hide evidence details" if visible else "Evidence details"
        )

    def _ensure_peer_evidence_visible(self, generation: int) -> None:
        if (
            not self._peer_evidence_active
            or generation != self._peer_evidence_generation
            or self._responsive_layout_mode != "compact"
            or not self.inspect_evidence_browser.isVisible()
        ):
            return
        self.peer_schedule_scroll_area.ensureWidgetVisible(
            self.inspect_evidence_browser,
            0,
            self.fontMetrics().height(),
        )
        scroll_widget = self.peer_schedule_scroll_area.widget()
        if scroll_widget is not None:
            top = self.inspect_evidence_browser.mapTo(scroll_widget, QPoint(0, 0)).y()
            bar = self.peer_schedule_scroll_area.verticalScrollBar()
            bar.setValue(max(0, min(top - self.fontMetrics().height(), bar.maximum())))

    def _inspect_peer_evidence(self) -> None:
        if not self._peer_evidence_active:
            self.inspect_evidence_status.setText("Evidence inspector is inactive; activate this tab before inspecting.")
            return
        callsign = self._evidence_call(self.inspect_evidence_edit.text())
        if not callsign or not CALLSIGN_RE.match(callsign):
            self._fence_peer_evidence()
            self.inspect_evidence_status.setText("Enter a valid callsign to inspect retained evidence.")
            self.inspect_evidence_disclosure_btn.setChecked(True)
            self.inspect_evidence_browser.setHtml("<p>Evidence is waiting for a valid callsign.</p>")
            return
        # Capture only immutable request values.  The worker never receives a
        # QWidget, SettingsManager, database connection, or bound reader.
        db_path = Path(self._db_path())
        local_call = self._evidence_local_call()
        self._peer_evidence_timezone = self._current_timezone()
        self.inspect_evidence_disclosure_btn.setChecked(True)
        self.inspect_evidence_status.setText(f"Reading bounded retained evidence for {callsign}…")
        self.inspect_evidence_browser.setHtml("<p>Loading retained evidence…</p>")
        self._peer_evidence_generation = self._peer_evidence_lane.request(
            db_path, callsign, local_call=local_call
        )
        generation = self._peer_evidence_generation
        QTimer.singleShot(0, lambda: self._ensure_peer_evidence_visible(generation))

    def _on_peer_evidence_ready(self, generation: int, snapshot: object) -> None:
        if not self._peer_evidence_active or int(generation) != int(self._peer_evidence_generation):
            return
        if snapshot is None:
            self.inspect_evidence_status.setText("Evidence could not be read.")
            self.inspect_evidence_browser.setHtml("<p>Retained evidence is unavailable.</p>")
            generation = self._peer_evidence_generation
            QTimer.singleShot(0, lambda: self._ensure_peer_evidence_visible(generation))
            return
        callsign = self._evidence_call(getattr(snapshot, "callsign", ""))
        coverage = getattr(snapshot, "coverage", None)
        errors = tuple(getattr(coverage, "errors", ()) or ())
        self.inspect_evidence_status.setText(
            f"Evidence loaded for {callsign or self.inspect_evidence_edit.text().strip().upper()}."
            + (" Coverage is incomplete." if errors else "")
        )
        self.inspect_evidence_browser.setHtml(self._render_peer_evidence(snapshot))
        generation = self._peer_evidence_generation
        QTimer.singleShot(0, lambda: self._ensure_peer_evidence_visible(generation))

    def _render_peer_evidence(self, snapshot: object) -> str:
        esc = self._evidence_html
        callsign = esc(getattr(snapshot, "callsign", "") or "Unknown callsign")
        coverage = getattr(snapshot, "coverage", None)
        observations = tuple(getattr(snapshot, "observations", ()) or ())[:100]
        schedules = tuple(getattr(snapshot, "schedules", ()) or ())
        frequencies = tuple(getattr(snapshot, "frequency_summaries", ()) or ())
        times = tuple(getattr(snapshot, "time_summaries", ()) or ())
        candidates = tuple(getattr(snapshot, "relay_candidates", ()) or ())[:10]
        errors = tuple(getattr(coverage, "errors", ()) or ())
        parts = [f"<h3>Evidence for {callsign}</h3>"]
        detail = getattr(coverage, "detail", "") if coverage is not None else ""
        if detail:
            parts.append(f"<p>{esc(detail)}</p>")
        if errors:
            parts.append("<p><b>Coverage unknown:</b> " + ", ".join(esc(item) for item in errors) + "</p>")

        parts.append("<h4>Declared and inferred recurrence</h4>")
        parts.append("<p>Recurrence confidence describes repeated retained observations, not RF delivery probability.</p>")
        if schedules:
            rows = []
            for schedule in schedules[:20]:
                source = str(getattr(schedule, "source_type", "") or "").upper()
                kind = "Inferred" if source.startswith("INFERRED") else "Declared"
                confidence = getattr(schedule, "confidence", None)
                try:
                    confidence_text = "unknown" if confidence is None else f"{float(confidence):.2f} recurrence confidence"
                except (TypeError, ValueError):
                    confidence_text = "unknown recurrence confidence"
                rows.append(
                    "<li>"
                    + " · ".join(
                        esc(value)
                        for value in (
                            kind,
                            f"{getattr(schedule, 'day_utc', '') or 'ALL'} UTC",
                            f"{getattr(schedule, 'start_utc', '')}-{getattr(schedule, 'end_utc', '')} UTC",
                            getattr(schedule, "band", "") or "band unknown",
                            getattr(schedule, "mode", "") or "mode unknown",
                            getattr(schedule, "frequency", "") or "frequency unknown",
                            confidence_text,
                        )
                    )
                    + "</li>"
                )
            parts.append("<ul>" + "".join(rows) + "</ul>")
        else:
            parts.append("<p>No retained declared or inferred schedule rows.</p>")

        if frequencies:
            parts.append("<h4>Observed frequencies</h4><ul>")
            for summary in frequencies[:20]:
                freq = getattr(summary, "frequency_hz", None)
                try:
                    freq_text = "unknown" if freq is None else f"{float(freq) / 1_000_000:.6f} MHz"
                except (TypeError, ValueError):
                    freq_text = "unknown"
                parts.append(
                    "<li>"
                    + esc(
                        f"{getattr(summary, 'band', '') or 'band unknown'} · {freq_text} · "
                        f"{getattr(summary, 'count', 0)} observation(s)"
                    )
                    + "</li>"
                )
            parts.append("</ul>")
        if times:
            parts.append("<h4>Observed UTC times</h4><ul>")
            weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
            for summary in times[:20]:
                day_value = getattr(summary, "day_utc", "?")
                try:
                    day_value = weekdays[int(day_value)]
                except (TypeError, ValueError, IndexError):
                    day_value = str(day_value)
                parts.append(
                    "<li>"
                    + esc(
                        f"weekday {day_value}, hour {getattr(summary, 'hour_utc', '?')} UTC · "
                        f"{getattr(summary, 'count', 0)} observation(s)"
                    )
                    + "</li>"
                )
            parts.append("</ul>")

        parts.append(f"<h4>Observations ({len(observations)} shown, bounded at 100)</h4>")
        if observations:
            parts.append("<ul>")
            for observation in observations:
                origin = getattr(observation, "origin", "") or "unknown origin"
                destination = getattr(observation, "destination", "") or "unknown destination"
                snr = getattr(observation, "snr", None)
                try:
                    if snr is None:
                        snr_text = "SNR unknown"
                    elif (
                        str(getattr(observation, "classification", "") or "").lower() == "direct"
                        and bool(getattr(observation, "direction_known", False))
                        and bool(getattr(observation, "source_radio_id", ""))
                    ):
                        snr_text = f"measured SNR {float(snr):.1f} dB"
                    else:
                        snr_text = f"retained SNR {float(snr):.1f} dB (not a measured local edge)"
                except (TypeError, ValueError):
                    snr_text = "SNR unknown"
                endpoint = getattr(observation, "source_radio_id", "") or "endpoint unknown"
                source_app = getattr(observation, "source_app", "") or "app unknown"
                details = [
                    f"{origin} → {destination}",
                    getattr(observation, "classification", "unknown") or "unknown class",
                    snr_text,
                    f"source {endpoint}/{source_app}",
                    self._format_evidence_timestamp(getattr(observation, "observed_ts", None)),
                    f"raw {getattr(observation, 'source_reference', '') or 'reference unknown'}",
                ]
                reporter = getattr(observation, "reporter", "")
                subject = getattr(observation, "subject", "")
                claimed_reporter = getattr(observation, "claimed_reporter", "")
                transport_sender = getattr(observation, "transport_sender", "")
                if reporter or subject:
                    details.append(f"reporter {reporter or 'unknown'}; subject {subject or 'unknown'}")
                if claimed_reporter:
                    details.append(f"claimed reporter {claimed_reporter}")
                if transport_sender:
                    details.append(f"transport sender {transport_sender}")
                parts.append("<li>" + esc(" · ".join(str(item) for item in details)) + "</li>")
            parts.append("</ul>")
        else:
            parts.append("<p>No retained observations for this callsign.</p>")

        parts.append(f"<h4>Receiving candidates ({len(candidates)} shown, bounded at 10)</h4>")
        if candidates:
            parts.append("<ul>")
            for candidate in candidates:
                path = " → ".join(str(value) for value in (getattr(candidate, "path", ()) or ()))
                candidate_freq = getattr(candidate, "frequency_hz", None)
                try:
                    frequency_text = "unknown frequency" if candidate_freq is None else f"{float(candidate_freq) / 1_000_000:.6f} MHz"
                except (TypeError, ValueError):
                    frequency_text = "unknown frequency"
                latest = getattr(candidate, "latest_observed_ts", None)
                freshness = self._format_evidence_timestamp(latest)
                parts.append(
                    "<li>"
                    + esc(
                        f"{path or 'path unavailable'} · receiving candidate · unverified; not a send route · "
                        f"{getattr(candidate, 'band', '') or 'band unknown'} · "
                        f"frequency {frequency_text} · {getattr(candidate, 'edges', 0)} hop(s) · "
                        f"endpoint {getattr(candidate, 'source_radio_id', '') or 'unknown'} · "
                        f"app {getattr(candidate, 'source_app', '') or 'unknown'} · "
                        f"freshness {freshness} · reverse evidence unknown · remote permission unknown"
                    )
                    + "</li>"
                )
            parts.append("</ul>")
        else:
            parts.append("<p>No fresh receiving path candidate is available.</p>")
        return "".join(parts)

    def _update_row_action_state(self) -> None:
        theme = resolve_theme(self.settings)
        selected = self._selected_row_payload()
        if selected:
            callsign = self._evidence_call(selected.get("callsign"))
            if callsign and self.inspect_evidence_edit.text().strip().upper() != callsign:
                self.inspect_evidence_edit.setText(callsign)
        source_type = str((selected or {}).get("source_type") or "").strip().upper()
        explicit_selected = bool(selected) and source_type != "INFERRED"
        self.edit_btn.setEnabled(explicit_selected)
        self.delete_row_btn.setEnabled(explicit_selected)
        self.edit_btn.setStyleSheet(button_style("primary" if explicit_selected else "muted", theme))
        self.delete_row_btn.setStyleSheet(button_style("danger" if explicit_selected else "muted", theme))

    def set_tab_active(self, active: bool) -> None:
        self._peer_evidence_active = bool(active)
        if not self._peer_evidence_active:
            self._fence_peer_evidence()

    def shutdown(self) -> None:
        self._peer_evidence_active = False
        self._peer_evidence_lane.shutdown()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt virtual
        self.shutdown()
        event.accept()

    def _update_timezone_button_style(self) -> None:
        theme = resolve_theme(self.settings)
        role = "info" if not self._show_local_times else "muted"
        self.tz_toggle_btn.setStyleSheet(button_style(role, theme))

    def _open_context_help(self, context_key: str) -> None:
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_context_help"):
            try:
                host.open_context_help(context_key)
            except Exception:
                pass

    def _notify_peer_schedule_changed(self) -> None:
        self._load_data()
        window = self.window()
        if window is not None and hasattr(window, "on_peer_schedule_data_changed"):
            try:
                window.on_peer_schedule_data_changed()
            except Exception as e:
                log.debug("PeerSched: downstream refresh failed: %s", e)

    def _on_table_cell_clicked(self, row: int, col: int) -> None:
        if col != self._overlap_col:
            return
        item = self.table.item(row, col)
        if not item:
            return
        ranges = item.data(Qt.UserRole)
        if not ranges or len(ranges) <= 1:
            return
        lines = self._format_overlap_ranges(ranges)
        if not lines:
            return
        msg = "\n".join(lines)
        QMessageBox.information(self, "Overlap Details", msg)

    def _prompt_existing_callsign_policy(self, callsign: str, *, for_edit: bool = False) -> str:
        prompt = QMessageBox(self)
        prompt.setIcon(QMessageBox.Question)
        prompt.setWindowTitle("Existing Explicit Schedule")
        verb = "updated" if for_edit else "added"
        prompt.setText(f"{callsign} already has explicit peer schedule rows.")
        prompt.setInformativeText(
            f"Choose whether this manual schedule should replace that callsign's current explicit rows or append to them."
        )
        replace_btn = prompt.addButton("Replace Explicit Rows", QMessageBox.AcceptRole)
        append_btn = prompt.addButton("Append Row", QMessageBox.ActionRole)
        prompt.addButton(QMessageBox.Cancel)
        prompt.setDefaultButton(replace_btn)
        prompt.exec()
        clicked = prompt.clickedButton()
        if clicked == replace_btn:
            return "replace"
        if clicked == append_btn:
            return "append"
        return "cancel"

    def _add_schedule(self) -> None:
        dialog = ManualPeerScheduleDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            data = dialog.data()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Schedule", str(exc))
            return

        callsign = data["callsign"]
        existing_explicit = [
            row
            for row in self._rows
            if row.get("callsign") == callsign and str(row.get("source_type") or "").strip().upper() != "INFERRED"
        ]
        replace_callsign = False
        if existing_explicit:
            policy = self._prompt_existing_callsign_policy(callsign)
            if policy == "cancel":
                return
            replace_callsign = policy == "replace"
        try:
            save_explicit_peer_schedule_rows(
                self._db_path(),
                callsign,
                [data],
                source_type="MANUAL",
                replace_callsign=replace_callsign,
                operator_profile=data,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Save Schedule", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", f"Could not save schedule:\n{exc}")
            log.error("PeerSched: add manual schedule failed: %s", exc)
            return
        self._notify_peer_schedule_changed()

    def _edit_selected_row(self) -> None:
        selected = self._selected_row_payload()
        if not selected or str(selected.get("source_type") or "").strip().upper() == "INFERRED":
            return
        meta = self._operator_meta.get(selected.get("callsign", ""), {})
        initial = {
            "callsign": selected.get("callsign", ""),
            "name": meta.get("name", ""),
            "state": meta.get("state", ""),
            "groups": meta.get("groups", ""),
            "day_utc": selected.get("day_utc", ""),
            "start_utc": selected.get("start_utc", ""),
            "end_utc": selected.get("end_utc", ""),
            "band": selected.get("band", ""),
            "mode": selected.get("mode", ""),
            "frequency": selected.get("frequency", ""),
        }
        dialog = ManualPeerScheduleDialog(self, initial=initial)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            data = dialog.data()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Schedule", str(exc))
            return

        old_key = _schedule_row_key(selected)
        old_callsign = selected.get("callsign", "")
        new_callsign = data["callsign"]
        remove_row_keys = [old_key]
        replace_callsign = False
        if new_callsign != old_callsign:
            existing_target_rows = [
                row
                for row in self._rows
                if row.get("callsign") == new_callsign
                and str(row.get("source_type") or "").strip().upper() != "INFERRED"
            ]
            if existing_target_rows:
                policy = self._prompt_existing_callsign_policy(new_callsign, for_edit=True)
                if policy == "cancel":
                    return
                replace_callsign = policy == "replace"
        try:
            save_explicit_peer_schedule_rows(
                self._db_path(),
                new_callsign,
                [data],
                source_type="MANUAL",
                replace_callsign=replace_callsign,
                remove_row_keys=remove_row_keys,
                operator_profile=data,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Save Schedule", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", f"Could not save schedule:\n{exc}")
            log.error("PeerSched: edit schedule failed: %s", exc)
            return
        self._notify_peer_schedule_changed()

    def _delete_selected_row(self) -> None:
        selected = self._selected_row_payload()
        if not selected or str(selected.get("source_type") or "").strip().upper() == "INFERRED":
            return
        callsign = selected.get("callsign", "")
        confirm = QMessageBox.question(
            self,
            "Delete Row",
            f"Delete the selected explicit schedule row for {callsign}?",
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            conn = sqlite3.connect(self._db_path())
            cur = conn.cursor()
            deleted = _delete_explicit_row_variants(cur, _schedule_row_key(selected))
            conn.commit()
            conn.close()
        except Exception as exc:
            QMessageBox.critical(self, "Delete Failed", f"DB delete failed:\n{exc}")
            log.error("PeerSched: delete row failed: %s", exc)
            return
        if deleted:
            self._notify_peer_schedule_changed()

    # ---------- import / delete ----------

    def _import_schedule(self) -> None:
        """
        Import a peer HF schedule JSON and store it in peer_hf_schedule.
        """
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Peer HF Schedule",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            raw = Path(path).read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as e:
            QMessageBox.critical(self, "Import Failed", f"Could not read JSON:\n{e}")
            return

        owner = self._normalize_callsign(data.get("callsign"))
        rows = data.get("rows", [])
        if not owner or not isinstance(rows, list):
            QMessageBox.warning(self, "Invalid File", "Expected keys: 'callsign' and 'rows'.")
            return

        valid_rows: List[Dict] = []
        for row in rows:
            try:
                day = (row.get("day_utc", "ALL") or "ALL").strip()
                start = (row.get("start_utc") or "").strip()
                end = (row.get("end_utc") or "").strip()
                band = (row.get("band") or "").strip()
                mode = (row.get("mode") or "").strip()
                freq = str(row.get("frequency") or "").strip()
                if not start or not end or not band:
                    continue
                valid_rows.append(
                    {
                        "day_utc": day,
                        "start_utc": start,
                        "end_utc": end,
                        "band": band,
                        "mode": mode,
                        "frequency": freq,
                    }
                )
            except Exception:
                continue

        if not valid_rows:
            QMessageBox.warning(self, "Import", "No valid rows found to import.")
            return

        try:
            save_explicit_peer_schedule_rows(
                self._db_path(),
                owner,
                [
                    {
                        **row,
                        "notes": "",
                    }
                    for row in valid_rows
                ],
                source_type="IMPORTED",
                replace_callsign=True,
                operator_profile={
                    "callsign": owner,
                    "name": str(data.get("name") or "").strip(),
                    "state": str(data.get("state") or "").strip().upper(),
                    "groups": ", ".join(
                        [
                            str(g).strip().upper()
                            for g in (
                                data.get("group1"),
                                data.get("group2"),
                                data.get("group3"),
                            )
                            if str(g or "").strip()
                        ]
                    ),
                    "groups_json": json.dumps(data.get("groups") or [])
                    if isinstance(data.get("groups"), list)
                    else "",
                },
            )
            QMessageBox.information(self, "Import", f"Imported {len(valid_rows)} rows for {owner}.")
            self._notify_peer_schedule_changed()
        except Exception as e:
            QMessageBox.critical(self, "Import Failed", f"DB write failed:\n{e}")
            log.error("PeerSched: import failed: %s", e)

    def _delete_selected(self) -> None:
        action = self._selected_delete_action()
        if not action:
            QMessageBox.information(self, "Delete", "Select a callsign to delete.")
            return
        if action == "__CLEAR_ALL__":
            self._clear_all()
            return
        cs = action
        confirm = QMessageBox.question(
            self,
            "Delete Schedule",
            f"Delete all explicit schedule rows for {cs}?",
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            conn = sqlite3.connect(self._db_path())
            cur = conn.cursor()
            deleted = self._delete_callsign_variants(cur, "peer_hf_schedule", cs)
            conn.commit()
            conn.close()
            self._notify_peer_schedule_changed()
            QMessageBox.information(self, "Delete", f"Deleted {deleted} explicit row(s) for {cs}.")
        except Exception as e:
            QMessageBox.critical(self, "Delete Failed", f"DB delete failed:\n{e}")
            log.error("PeerSched: delete failed for %s: %s", cs, e)

    def _clear_all(self) -> None:
        has_imported = any(
            str(r.get("source_type") or "").strip().upper() != "INFERRED"
            for r in self._rows
        )
        if not has_imported:
            return
        confirm = QMessageBox.question(
            self,
            "Clear All",
            "Delete all explicit peer schedules?",
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            conn = sqlite3.connect(self._db_path())
            cur = conn.cursor()
            cur.execute("DELETE FROM peer_hf_schedule")
            conn.commit()
            conn.close()
            self._notify_peer_schedule_changed()
            QMessageBox.information(self, "Clear All", "All peer schedules removed.")
        except Exception as e:
            QMessageBox.critical(self, "Clear Failed", f"DB delete failed:\n{e}")
            log.error("PeerSched: clear all failed: %s", e)
