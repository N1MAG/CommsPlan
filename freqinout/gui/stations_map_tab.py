from __future__ import annotations

import datetime
import html
import json
import sqlite3
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, List, Dict, Mapping, Optional, Set, Tuple
import math
import time
import logging
import sys
import queue
import weakref
from collections import deque

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, QCoreApplication, QMetaObject, QSize, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QFrame,
    QGridLayout,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QMenu,
    QStyle,
    QSizePolicy,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTabWidget,
)
from freqinout.core.config_paths import get_config_dir
from freqinout.core.sqlite_utils import connect_sqlite_readonly
from freqinout.core.map_read_budget import note_map_read_cap
from freqinout.core.view_contracts import compose_intent_from_mapping, map_context_from_mapping
from freqinout.gui.native_map_renderer import NativeMapRenderer
from freqinout.gui.native_map_projection import build_native_overlay_projection

JS8NET_PATH = Path(__file__).resolve().parents[2] / "third_party" / "js8net" / "js8net-main"
if JS8NET_PATH.exists():
    sys.path.insert(0, str(JS8NET_PATH))
try:
    import js8net  # type: ignore
except Exception:
    js8net = None
from freqinout.core.logger import log
from freqinout.core.js8_spotter_forms import (
    PURPOSE_HAZARD,
    PURPOSE_INFRASTRUCTURE,
    PURPOSE_WEATHER,
    form_codes_enabled_for,
    forms_enabled_for,
)
from freqinout.core.perf_metrics import emit_span, span as perf_span
from freqinout.core.operator_activity import (
    load_js8_direct_contact_summary,
    load_operator_activity_summary,
    parse_utc_timestamp,
)
from freqinout.core.observation_queries import ObservationQuery, map_observation_rows, matching_observation_callsigns
from freqinout.core.regional_intelligence import (
    RegionalAreaRollup,
    build_regional_intelligence_from_db,
)
from freqinout.core.commstat_sitrep import resolve_commstat_reported_for_state
from freqinout.core.rf_pins import delete_rf_pins, list_rf_pins, save_rf_pin
from freqinout.core.message_intelligence import TOPIC_TAXONOMY, normalize_topic_terms
from freqinout.core.message_ingest import MessageIngestor
from freqinout.core.js8_log_link_indexer import JS8LogLinkIndexer
from freqinout.core.js8_runtime_ingest import ingest_js8_links_for_runtime_sources
from freqinout.core.js8_source_context import resolve_js8_source_context
from freqinout.core.plan_context_service import PlanContextService
from freqinout.core.propagation_service import PropagationService
from freqinout.core.sitrep_metadata import source_family_key, source_family_label, source_short_label, transport_label
from freqinout.core.message_inbox_filters import looks_like_callsign_text
from freqinout.core.message_search_values import searchable_text_values
from freqinout.core.varac_runtime_ingest import ingest_varac_for_runtime_sources
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.support_reporting import build_support_summary, bullet_lines
from freqinout.radio_interface.js8_rx_hub import JS8RxHub
from freqinout.gui.qsy_helper import current_scheduler_freq
from freqinout.gui.help_registry import resolve_help_host
from freqinout.gui.plan_context_label import PlanContextLabel
from freqinout.gui.map_layer_controls import MapLayerControls
from freqinout.gui.map_layer_state import toggle_map_layer
from freqinout.gui.theme import (
    contrast_text_for_background,
    resolve_theme,
    BAND_COLORS_DARK,
    BAND_COLORS_LIGHT,
    button_style,
    style_splitter_handles,
)
from freqinout.utils.timezones import get_timezone


USA_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]
LOWER48_STATES = [s for s in USA_STATES if s not in {"AK", "HI"}]

CANADA_PROVINCES = ["AB", "BC", "MB", "NB", "NL", "NT", "NS", "NU", "ON", "PE", "QC", "SK", "YT"]

USA_FRAME = ((7.0, -172.0), (83.0, -50.0))  # lat_min, lon_min, lat_max, lon_max

# FEMA regions mapping (states -> region id)
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
STATE_TO_FEMA_REGION = {state: region for region, states in FEMA_REGIONS.items() for state in states}

PROP_BANDS = ["80M", "40M", "30M", "20M", "15M", "10M"]
PROP_BAND_COLORS = {
    "80M": "#4B2E83",
    "40M": "#1E88E5",
    "30M": "#00897B",
    "20M": "#43A047",
    "15M": "#FB8C00",
    "10M": "#E53935",
}
PROP_DEFAULT_PROFILES = {
    "80M": {"ideal_km": 600, "spread_km": 1200, "day": 0.35, "night": 1.0},
    "40M": {"ideal_km": 1200, "spread_km": 1800, "day": 0.65, "night": 0.95},
    "30M": {"ideal_km": 1800, "spread_km": 2200, "day": 0.8, "night": 0.85},
    "20M": {"ideal_km": 2800, "spread_km": 2600, "day": 1.0, "night": 0.6},
    "15M": {"ideal_km": 3600, "spread_km": 3000, "day": 0.9, "night": 0.35},
    "10M": {"ideal_km": 4200, "spread_km": 3600, "day": 0.8, "night": 0.2},
}

RF_PIN_TOPICS = TOPIC_TAXONOMY
ALERT_MAP_TOPICS = frozenset({"Fire", "Weather", "Shelter", "Medical", "Security", "General Intel"})
INFRASTRUCTURE_MAP_TOPICS = frozenset(
    {"Infrastructure", "Power", "Water", "Comms", "Fuel", "Food", "Travel/Roads", "Logistics"}
)


class _RfPinDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, *, pin: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._source_ref = str(getattr(pin, "source_ref", "") or "").strip()
        self.setWindowTitle("Edit Planning Pin" if self._source_ref else "Add Planning Pin")
        self.title_edit = QLineEdit(self)
        self.title_edit.setPlaceholderText("Short label shown in map details")
        self.group_edit = QLineEdit(self)
        self.group_edit.setPlaceholderText("Operating group or target, optional")
        self.grid_edit = QLineEdit(self)
        self.grid_edit.setPlaceholderText("Grid square, e.g. DM79QJ")
        self.state_edit = QLineEdit(self)
        self.state_edit.setPlaceholderText("State/province, optional")
        self.topic_combo = QComboBox(self)
        self.topic_combo.addItems(list(RF_PIN_TOPICS))
        self.summary_edit = QLineEdit(self)
        self.summary_edit.setPlaceholderText("Brief note, optional")

        form = QFormLayout(self)
        form.setContentsMargins(18, 18, 18, 18)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.addRow("Pin Name", self.title_edit)
        form.addRow("Group", self.group_edit)
        form.addRow("Grid", self.grid_edit)
        form.addRow("State", self.state_edit)
        form.addRow("Topic", self.topic_combo)
        form.addRow("Summary", self.summary_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if pin is not None:
            self._load_pin(pin)

    def _load_pin(self, pin: Any) -> None:
        self.title_edit.setText(str(getattr(pin, "subject", "") or "").strip())
        self.group_edit.setText(str(getattr(pin, "to_target", "") or "").strip().lstrip("@"))
        self.grid_edit.setText(str(getattr(pin, "grid", "") or "").strip().upper())
        self.state_edit.setText(str(getattr(pin, "state", "") or "").strip().upper())
        topics = tuple(getattr(pin, "observed_topics", ()) or ())
        if topics:
            idx = self.topic_combo.findText(str(topics[0]).strip())
            if idx >= 0:
                self.topic_combo.setCurrentIndex(idx)
        self.summary_edit.setText(str(getattr(pin, "summary", "") or "").strip())

    def pin_payload(self) -> Dict[str, Any]:
        label = self.title_edit.text().strip()
        group = self.group_edit.text().strip().lstrip("@")
        grid = self.grid_edit.text().strip().upper()
        state = self.state_edit.text().strip().upper()
        topic = self.topic_combo.currentText().strip()
        summary = self.summary_edit.text().strip()
        created_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        ref_seed = label or grid or state or "pin"
        payload: Dict[str, Any] = {
            "raw_reference": self._source_ref or f"rf_pin:{int(time.time() * 1000)}:{ref_seed}",
            "label": label or summary or "Planning Pin",
            "group": group,
            "groups": [group] if group else [],
            "to_target": group,
            "grid": grid,
            "state": state,
            "topics": [topic] if topic else [],
            "summary": summary or label or "Planning Pin",
            "source_app": "FIO",
            "source_family": "rf_pin",
            "source_kind": "pin",
            "created_utc": created_utc,
            "event_utc": created_utc,
            "status": "PIN",
            "pin_kind": "operator",
        }
        return payload


class _RfPinManagerDialog(QDialog):
    COL_LABEL = 0
    COL_GROUP = 1
    COL_TOPIC = 2
    COL_AREA = 3
    COL_SUMMARY = 4

    def __init__(self, db_path: Path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manage Planning Pins")
        self._db_path = Path(db_path)
        self._deleted = False
        self._changed = False
        self._pins_by_ref: Dict[str, Any] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Pin", "Group", "Topic", "Area", "Summary"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_LABEL, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_GROUP, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_TOPIC, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_AREA, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_SUMMARY, QHeaderView.Stretch)
        layout.addWidget(self.table)

        actions = QHBoxLayout()
        self.edit_button = QPushButton("Edit Selected", self)
        self.delete_button = QPushButton("Delete Selected", self)
        self.close_button = QPushButton("Close", self)
        self.edit_button.clicked.connect(self._edit_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.close_button.clicked.connect(self.accept)
        actions.addStretch(1)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.delete_button)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self._load_rows()

    @property
    def changed(self) -> bool:
        return self._changed or self._deleted

    def _load_rows(self) -> None:
        try:
            pins = list_rf_pins(self._db_path, limit=500)
        except Exception as exc:
            log.warning("StationsMap: failed to list RF pins: %s", exc, exc_info=True)
            pins = ()
        self.table.setRowCount(0)
        self._pins_by_ref = {}
        for pin in pins:
            source_ref = str(pin.source_ref or "").strip()
            if source_ref:
                self._pins_by_ref[source_ref] = pin
            row = self.table.rowCount()
            self.table.insertRow(row)
            topic = ", ".join(str(t).strip() for t in pin.observed_topics if str(t).strip())
            area = " / ".join(part for part in (pin.state, pin.grid) if str(part).strip())
            values = [
                pin.subject or "Planning Pin",
                pin.to_target,
                topic,
                area,
                pin.summary,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                if col == self.COL_LABEL:
                    item.setData(Qt.UserRole, source_ref)
                self.table.setItem(row, col, item)
        self.edit_button.setEnabled(self.table.rowCount() > 0)
        self.delete_button.setEnabled(self.table.rowCount() > 0)

    def _selected_source_refs(self) -> List[str]:
        refs: List[str] = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), self.COL_LABEL)
            source_ref = str(item.data(Qt.UserRole) if item is not None else "").strip()
            if source_ref:
                refs.append(source_ref)
        return refs

    def _edit_selected(self) -> None:
        refs = self._selected_source_refs()
        if len(refs) != 1:
            QMessageBox.information(self, "Manage Planning Pins", "Select one planning pin to edit.")
            return
        pin = self._pins_by_ref.get(refs[0])
        if pin is None:
            QMessageBox.warning(self, "Manage Planning Pins", "CommsPlan could not find the selected planning pin.")
            return
        dialog = _RfPinDialog(self, pin=pin)
        if dialog.exec() != QDialog.Accepted:
            return
        payload = dialog.pin_payload()
        if not payload.get("grid"):
            QMessageBox.warning(self, "Edit Planning Pin", "Add a grid square so CommsPlan can place the planning pin on the map.")
            return
        try:
            save_rf_pin(self._db_path, payload)
        except Exception as exc:
            log.warning("StationsMap: failed to update RF pin: %s", exc, exc_info=True)
            QMessageBox.warning(self, "Edit Planning Pin", f"CommsPlan could not update this planning pin.\n{exc}")
            return
        self._changed = True
        self._load_rows()

    def _delete_selected(self) -> None:
        refs = self._selected_source_refs()
        if not refs:
            QMessageBox.information(self, "Manage Planning Pins", "Select one or more planning pins to delete.")
            return
        answer = QMessageBox.question(
            self,
            "Delete Planning Pins",
            f"Delete {len(refs)} selected planning pin(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            delete_rf_pins(self._db_path, refs)
        except Exception as exc:
            log.warning("StationsMap: failed to delete RF pins: %s", exc, exc_info=True)
            QMessageBox.warning(self, "Delete Planning Pins", f"CommsPlan could not delete the selected planning pin(s).\n{exc}")
            return
        self._deleted = True
        self._load_rows()

US_STATE_NAMES = {
    "AL": "ALABAMA",
    "AK": "ALASKA",
    "AZ": "ARIZONA",
    "AR": "ARKANSAS",
    "CA": "CALIFORNIA",
    "CO": "COLORADO",
    "CT": "CONNECTICUT",
    "DE": "DELAWARE",
    "FL": "FLORIDA",
    "GA": "GEORGIA",
    "HI": "HAWAII",
    "ID": "IDAHO",
    "IL": "ILLINOIS",
    "IN": "INDIANA",
    "IA": "IOWA",
    "KS": "KANSAS",
    "KY": "KENTUCKY",
    "LA": "LOUISIANA",
    "ME": "MAINE",
    "MD": "MARYLAND",
    "MA": "MASSACHUSETTS",
    "MI": "MICHIGAN",
    "MN": "MINNESOTA",
    "MS": "MISSISSIPPI",
    "MO": "MISSOURI",
    "MT": "MONTANA",
    "NE": "NEBRASKA",
    "NV": "NEVADA",
    "NH": "NEW HAMPSHIRE",
    "NJ": "NEW JERSEY",
    "NM": "NEW MEXICO",
    "NY": "NEW YORK",
    "NC": "NORTH CAROLINA",
    "ND": "NORTH DAKOTA",
    "OH": "OHIO",
    "OK": "OKLAHOMA",
    "OR": "OREGON",
    "PA": "PENNSYLVANIA",
    "RI": "RHODE ISLAND",
    "SC": "SOUTH CAROLINA",
    "SD": "SOUTH DAKOTA",
    "TN": "TENNESSEE",
    "TX": "TEXAS",
    "UT": "UTAH",
    "VT": "VERMONT",
    "VA": "VIRGINIA",
    "WA": "WASHINGTON",
    "WV": "WEST VIRGINIA",
    "WI": "WISCONSIN",
    "WY": "WYOMING",
}

# Canada province names (uppercase for matching)
CANADA_PROVINCE_NAMES = {
    "AB": "ALBERTA",
    "BC": "BRITISH COLUMBIA",
    "MB": "MANITOBA",
    "NB": "NEW BRUNSWICK",
    "NL": "NEWFOUNDLAND AND LABRADOR",
    "NT": "NORTHWEST TERRITORIES",
    "NS": "NOVA SCOTIA",
    "NU": "NUNAVUT",
    "ON": "ONTARIO",
    "PE": "PRINCE EDWARD ISLAND",
    "QC": "QUEBEC",
    "SK": "SASKATCHEWAN",
    "YT": "YUKON",
}

# Reverse map for fast lookup of full state names -> abbreviation
US_STATE_ABBR_FROM_NAME = {name: abbr for abbr, name in US_STATE_NAMES.items()}
CANADA_PROV_ABBR_FROM_NAME = {name: abbr for abbr, name in CANADA_PROVINCE_NAMES.items()}

STATE_CENTERS = {
    # USA
    "AL": (32.806, -86.792),
    "AK": (61.370, -152.404),
    "AZ": (33.729, -111.431),
    "AR": (34.969, -92.373),
    "CA": (36.116, -119.681),
    "CO": (39.059, -105.311),
    "CT": (41.600, -72.755),
    "DE": (38.910, -75.527),
    "FL": (27.766, -81.686),
    "GA": (33.040, -83.643),
    "HI": (21.094, -157.498),
    "ID": (44.240, -114.478),
    "IL": (40.349, -88.987),
    "IN": (39.849, -86.258),
    "IA": (42.011, -93.210),
    "KS": (38.526, -96.726),
    "KY": (37.668, -84.670),
    "LA": (31.169, -91.867),
    "ME": (44.693, -69.381),
    "MD": (39.063, -76.802),
    "MA": (42.230, -71.531),
    "MI": (44.182, -84.506),
    "MN": (46.729, -94.685),
    "MS": (32.741, -89.678),
    "MO": (38.457, -92.288),
    "MT": (46.921, -110.454),
    "NE": (41.125, -98.268),
    "NV": (38.313, -117.055),
    "NH": (43.193, -71.572),
    "NJ": (40.058, -74.406),
    "NM": (34.307, -106.018),
    "NY": (42.165, -74.948),
    "NC": (35.630, -79.807),
    "ND": (47.551, -101.002),
    "OH": (40.388, -82.764),
    "OK": (35.565, -96.928),
    "OR": (43.804, -120.554),
    "PA": (41.203, -77.194),
    "RI": (41.580, -71.477),
    "SC": (33.837, -81.163),
    "SD": (44.299, -99.438),
    "TN": (35.747, -86.692),
    "TX": (31.054, -97.564),
    "UT": (40.150, -111.862),
    "VT": (44.045, -72.709),
    "VA": (37.769, -78.169),
    "WA": (47.400, -121.490),
    "WV": (38.491, -80.954),
    "WI": (44.268, -89.616),
    "WY": (42.756, -107.302),
    # Canada (rough centroids)
    "AB": (53.933, -116.576),
    "BC": (54.000, -125.000),
    "MB": (53.760, -98.813),
    "NB": (46.565, -66.461),
    "NL": (53.135, -57.660),
    "NT": (64.824, -124.845),
    "NS": (44.682, -63.744),
    "NU": (70.299, -83.107),
    "ON": (50.000, -85.000),
    "PE": (46.250, -63.000),
    "QC": (52.939, -73.549),
    "SK": (52.939, -106.450),
    "YT": (64.282, -135.000),
}

# Simple city list (major/medium) for labels; keep compact
CITIES = [
    # USA
    ("New York", 40.7128, -74.0060, 8804190),
    ("Los Angeles", 34.0522, -118.2437, 3898747),
    ("Chicago", 41.8781, -87.6298, 2746388),
    ("Houston", 29.7604, -95.3698, 2304580),
    ("Phoenix", 33.4484, -112.0740, 1608139),
    ("San Antonio", 29.4241, -98.4936, 1434625),
    ("San Diego", 32.7157, -117.1611, 1386932),
    ("Dallas", 32.7767, -96.7970, 1288457),
    ("San Jose", 37.3382, -121.8863, 1035317),
    ("Austin", 30.2672, -97.7431, 964177),
    ("Jacksonville", 30.3322, -81.6557, 949611),
    ("Fort Worth", 32.7555, -97.3308, 918915),
    ("Columbus", 39.9612, -82.9988, 906528),
    ("Charlotte", 35.2271, -80.8431, 879709),
    ("Indianapolis", 39.7684, -86.1581, 882039),
    ("San Francisco", 37.7749, -122.4194, 873965),
    ("Seattle", 47.6062, -122.3321, 737015),
    ("Denver", 39.7392, -104.9903, 716000),
    ("Washington", 38.9072, -77.0369, 689545),
    ("Boston", 42.3601, -71.0589, 675647),
    ("El Paso", 31.7619, -106.4850, 678815),
    ("Nashville", 36.1627, -86.7816, 689447),
    ("Detroit", 42.3314, -83.0458, 639111),
    ("Oklahoma City", 35.4676, -97.5164, 681054),
    ("Portland", 45.5051, -122.6750, 652503),
    ("Las Vegas", 36.1699, -115.1398, 641903),
    ("Memphis", 35.1495, -90.0490, 633104),
    ("Louisville", 38.2527, -85.7585, 617638),
    ("Baltimore", 39.2904, -76.6122, 576498),
    ("Milwaukee", 43.0389, -87.9065, 577222),
    ("Albuquerque", 35.0844, -106.6504, 564559),
    ("Tucson", 32.2226, -110.9747, 542629),
    ("Seattle", 47.6062, -122.3321, 737015),
    ("Fresno", 36.7378, -119.7871, 542107),
    ("Sacramento", 38.5816, -121.4944, 524943),
    ("Kansas City", 39.0997, -94.5786, 508090),
    ("Mesa", 33.4152, -111.8315, 504258),
    ("Atlanta", 33.7490, -84.3880, 498715),
    ("Colorado Springs", 38.8339, -104.8214, 478961),
    ("Omaha", 41.2565, -95.9345, 486051),
    ("Raleigh", 35.7796, -78.6382, 469124),
    ("Miami", 25.7617, -80.1918, 439890),
    ("Minneapolis", 44.9778, -93.2650, 429954),
    ("Tulsa", 36.1539, -95.9928, 413066),
    ("New Orleans", 29.9511, -90.0715, 383997),
    ("Wichita", 37.6872, -97.3301, 397532),
    ("Cleveland", 41.4993, -81.6944, 372624),
    ("Tampa", 27.9506, -82.4572, 384959),
    ("Bakersfield", 35.3733, -119.0187, 407615),
    ("Aurora", 39.7294, -104.8319, 386261),
    ("Honolulu", 21.3069, -157.8583, 345510),
    ("St Louis", 38.6270, -90.1994, 302838),
    ("Pittsburgh", 40.4406, -79.9959, 302971),
    ("Cincinnati", 39.1031, -84.5120, 309317),
    ("Anchorage", 61.2181, -149.9003, 288121),
    ("Boise", 43.6150, -116.2023, 235684),
    # Canada
    ("Toronto", 43.6510, -79.3470, 2731571),
    ("Vancouver", 49.2827, -123.1207, 662248),
    ("Calgary", 51.0447, -114.0719, 1239000),
    ("Montreal", 45.5019, -73.5674, 1780000),
    ("Ottawa", 45.4215, -75.6972, 934243),
    ("Winnipeg", 49.8951, -97.1384, 705244),
    ("Halifax", 44.6488, -63.5752, 439819),
    ("Edmonton", 53.5461, -113.4938, 1063000),
    ("Quebec City", 46.8139, -71.2080, 542298),
    ("Hamilton", 43.2557, -79.8711, 579200),
    ("London", 42.9849, -81.2453, 422324),
    # Mexico
    ("Mexico City", 19.4326, -99.1332, 9209944),
    ("Guadalajara", 20.6597, -103.3496, 1495000),
    ("Monterrey", 25.6866, -100.3161, 1135000),
    ("Tijuana", 32.5149, -117.0382, 1880000),
    ("Cancun", 21.1619, -86.8515, 888797),
    ("Leon", 21.1220, -101.6841, 1570000),
    ("Merida", 20.9674, -89.5926, 892363),
]


@dataclass
class StationPoint:
    callsign: str
    grid: str
    heard_by: Optional[str] = None
    name: str = ""
    state: str = ""
    group: str = ""
    groups: List[str] = field(default_factory=list)
    trusted: bool = True
    lat: float = 0.0
    lon: float = 0.0


@dataclass(frozen=True)
class _MapProjectionSnapshotResult:
    generation: int
    payload: str
    signature: str
    pending_payload: Dict[str, object]
    error: str = ""


class _MapProjectionSnapshotEmitter(QObject):
    finished = Signal(object)


WEATHER_REPORT_MAX_AGE_SEC = 12 * 60 * 60
ALERT_REPORT_MAX_AGE_SEC = 24 * 60 * 60
INFRASTRUCTURE_REPORT_MAX_AGE_SEC = 24 * 60 * 60
WEATHER_CLUSTER_DEGREES = 0.75
MAP_DEFAULT_RECENCY_LABEL = "24h"
MAP_DEFAULT_RECENCY_SECONDS = 24 * 60 * 60
MAP_PATH_MAX_RECENCY_LABEL = "24h"
MAP_PATH_MAX_RECENCY_SECONDS = 24 * 60 * 60
SITREP_ACTIVE_STATUS_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAP_NETWORK_PATH_DISPLAY_LIMIT = 250

WEATHER_SEVERITY_RANK = {
    "unknown": 0,
    "routine": 1,
    "green": 1,
    "caution": 2,
    "yellow": 2,
    "severe": 3,
    "red": 3,
}


def maidenhead_to_latlon(grid: str) -> Optional[tuple[float, float]]:
    """
    Convert Maidenhead grid locator to lat/lon (center of square/locator).
    Supports 4- or 6-character locators.
    """
    grid = (grid or "").strip().upper()
    if len(grid) < 4:
        return None
    try:
        lon = (ord(grid[0]) - ord("A")) * 20.0 + int(grid[2]) * 2.0 + 1.0 / 24.0
        lat = (ord(grid[1]) - ord("A")) * 10.0 + int(grid[3]) * 1.0 + 1.0 / 48.0
        if len(grid) >= 6:
            lon += (ord(grid[4]) - ord("A")) / 12.0
            lat += (ord(grid[5]) - ord("A")) / 24.0
        lon -= 180.0
        lat -= 90.0
        return lat, lon
    except Exception:
        return None


def maidenhead_grid4_bounds(grid: str) -> Optional[tuple[float, float, float, float]]:
    grid = (grid or "").strip().upper()
    if len(grid) < 4:
        return None
    if not re.match(r"^[A-R]{2}[0-9]{2}$", grid[:4]):
        return None
    lon_min = (ord(grid[0]) - ord("A")) * 20.0 + int(grid[2]) * 2.0 - 180.0
    lat_min = (ord(grid[1]) - ord("A")) * 10.0 + int(grid[3]) * 1.0 - 90.0
    lon_max = lon_min + 2.0
    lat_max = lat_min + 1.0
    return (lat_min, lon_min, lat_max, lon_max)


class StationsMapTab(QWidget):
    """
    Displays JS8Call-heard stations on an OSM-based map with a Maidenhead overlay.
    Stations and operational overlays render on CommsPlan's provider-free offline map.
    """

    def __init__(
        self,
        parent=None,
        *,
        plan_context_service: Optional[PlanContextService] = None,
        application_host: object | None = None,
    ):
        super().__init__(parent)
        self.plan_context_service = plan_context_service
        self._application_host_ref = weakref.ref(application_host) if application_host is not None else None
        self.show_callsigns = False
        self.stations: List[StationPoint] = []
        self.show_callsigns = False
        self.show_cities = False
        self.show_states = True
        self.show_station_markers = True
        self.show_link_paths = False
        self.show_grids = False
        self.show_grid_labels = False  # driven by the "Show grids" toggle
        self.show_weather_reports = True
        self.show_alert_reports = True
        self.show_infrastructure_reports = True
        self.show_regions = False
        self.show_city_labels = False
        self.city_pop_min = 100000
        self.link_mode = "off"
        self.link_value = ""
        self._paths_focus_station = ""
        self._saved_map_path_scope = None
        self._map_last_link_source_rows = 0
        self._map_last_link_missing_position_rows = 0
        self._map_last_link_all_time_count = 0
        self.relay_target = ""
        self._now_reachable_enabled: bool = False
        self._now_reachable_callsigns: Set[str] = set()
        self._now_reachable_meta: Dict[str, Dict] = {}
        self._refresh_links_button: Optional[QPushButton] = None
        self._now_reachable_button: Optional[QPushButton] = None
        self._now_reachable_label: Optional[QLabel] = None
        self._sitrep_status_only_enabled: bool = False
        self._observation_focus_enabled: bool = False
        self._observation_focus_mode: str = ""
        self._map_view_status_label: Optional[QLabel] = None
        self._map_all_stations_button: Optional[QPushButton] = None
        self._map_hf_reports_button: Optional[QPushButton] = None
        self._map_local_reports_button: Optional[QPushButton] = None
        self._map_reports_button: Optional[QPushButton] = None
        self._map_regional_intel_button: Optional[QPushButton] = None
        self._map_paths_button: Optional[QPushButton] = None
        self._map_propagation_button: Optional[QPushButton] = None
        self._map_rf_pins_button: Optional[QPushButton] = None
        self._map_mode_combo: Optional[QComboBox] = None
        self._map_traffic_subtype_combo: Optional[QComboBox] = None
        self._map_traffic_subtype_field: Optional[QWidget] = None
        self._map_intel_sensitivity_field: Optional[QWidget] = None
        self._map_path_scope_field: Optional[QWidget] = None
        self._map_intelligence_layers_section: Optional[QWidget] = None
        self._map_clear_filters_button: Optional[QPushButton] = None
        self._map_clear_layers_button: Optional[QPushButton] = None
        self._map_search_edit: Optional[QLineEdit] = None
        self._map_add_rf_pin_button: Optional[QPushButton] = None
        self._map_manage_rf_pins_button: Optional[QPushButton] = None
        self._map_topic_filter_combo: Optional[QComboBox] = None
        self._map_intel_sensitivity_combo: Optional[QComboBox] = None
        self._map_scope_filter_combo: Optional[QComboBox] = None
        self._map_state_filter_combo: Optional[QComboBox] = None
        self._map_source_filter_combo: Optional[QComboBox] = None
        self._map_status_filter_combo: Optional[QComboBox] = None
        self._map_trust_filter_combo: Optional[QComboBox] = None
        self.selected_band = "All"
        self._map_schedule_freqs = []
        self.recency_seconds: Optional[int] = None
        self.operator_rows: List[Dict] = []
        self.operator_index: Dict[str, Dict] = {}
        self._operator_groups: List[str] = []
        self._sitrep_report_groups: List[str] = []
        self._operator_regions: List[str] = []
        self._last_map_view: Optional[Dict[str, float]] = None
        self._last_map_auto_fit_sig: Optional[tuple] = None
        self._mesh_nodes_auto_fit_pending: bool = False

        # Share the application owner's settings snapshot when this workspace
        # is hosted by MainWindow.  A second SettingsManager caches its own
        # copy and can otherwise re-apply a stale palette immediately after a
        # live Light/Dark switch.  Standalone tests/tools retain the local
        # fallback.
        shared_settings = getattr(application_host, "settings", None)
        if shared_settings is not None:
            self.settings = shared_settings
        else:
            try:
                self.settings = SettingsManager()
            except Exception:
                self.settings = None

        self._last_js8_load_ts: float = 0.0
        self._last_exit_ts: float = 0.0
        try:
            if self.settings:
                self._last_js8_load_ts = float(self.settings.get("js8_links_last_load_utc", 0) or 0)
                self._last_exit_ts = float(self.settings.get("last_exit_utc", 0) or 0)
        except Exception:
            self._last_js8_load_ts = 0.0
            self._last_exit_ts = 0.0
        self._js8_timer: Optional[QTimer] = None
        self._js8_rx_timer: Optional[QTimer] = None
        self._js8_rx_hub: Optional[JS8RxHub] = None
        self._js8_rx_registered = False
        self._js8_indexer: Optional[JS8LogLinkIndexer] = None
        self._is_shutting_down = False
        self._js8_polling = False
        self._js8_live_source_context_cache: Dict[str, object] = {
            "endpoint": "",
            "expires": 0.0,
            "context": {},
        }
        self._background_ingest_controller = None
        self._js8_net_started = False
        self._map_initialized = False
        self._pending_map_payload: Optional[Dict[str, object]] = None
        self._last_map_payload_sig: Optional[str] = None
        self._map_read_lane = None
        self._map_read_inflight = False
        self._map_inspector_lane = None
        self._map_inspector_generation = 0
        self._map_read_cache_revision = 0
        self._map_payload_generation: int = 0
        self._map_payload_executor: ThreadPoolExecutor | None = None
        self._map_payload_emitter = _MapProjectionSnapshotEmitter(self)
        self._map_payload_emitter.finished.connect(self._on_map_payload_snapshot_ready)
        self._map_query_cache: Dict[tuple[str, str], tuple[float, object]] = {}
        self._last_map_render_input_sig: Optional[tuple] = None
        self._last_map_config: Optional[tuple] = None
        self._last_map_render_ts: float = 0.0
        self._stations_revision: int = 0
        self._render_pending: bool = False
        self._pending_refresh_level: int = 0
        self._pending_refresh_reason: str = ""
        self._pending_refresh_preserve_view: object = True
        self._map_visible: bool = False
        self._map_dirty: bool = False
        self._ingest_started: bool = False
        self._deferred_initial_ingest_pending: bool = False
        self._map_load_ok: bool = False
        self._map_page_loading: bool = False
        self._map_js_ready_retry_count: int = 0
        self._render_requested_during_load: bool = False
        self._render_requested_during_load_level: int = 0
        self._map_runtime_state: str = "cold"
        self._map_runtime_detail: str = "Map has not been opened yet."
        self._map_last_error: str = ""
        self._map_last_event_ts: float = 0.0
        self._map_marker_count: int = 0
        self._map_link_count: int = 0
        self._map_link_status_detail: str = "Links hidden."
        self._map_link_display_limited_count: int = 0
        self._map_link_loaded_count: int = 0
        self._app_active: bool = True
        self.prop_overlay_enabled: bool = False
        self.prop_adaptive_enabled: bool = True
        self.prop_mode: str = "blended"
        self.prop_window_hours: int = 6
        self._prop_region_scores: Dict[str, Dict] = {}
        self._prop_best_band_info: Dict[str, str] = {}
        self._last_prop_region_filter: str = ""
        self.prop_overlay_chk: Optional[QCheckBox] = None
        self.prop_adaptive_chk: Optional[QCheckBox] = None
        self.prop_badge: Optional[QLabel] = None
        self.prop_mode_combo: Optional[QComboBox] = None
        self.prop_window_combo: Optional[QComboBox] = None
        self.prop_target_type_combo: Optional[QComboBox] = None
        self.prop_target_value_combo: Optional[QComboBox] = None
        self._prop_target_syncing: bool = False
        self._map_stack: Optional[QStackedWidget] = None
        self._native_map_renderer: Optional[NativeMapRenderer] = None
        self._map_loading_label: Optional[QLabel] = None
        self._map_canvas_splitter: Optional[QSplitter] = None
        self._map_selected_panel: Optional[QFrame] = None
        self._map_selected_title: Optional[QLabel] = None
        self._map_selected_subtitle: Optional[QLabel] = None
        self._map_selected_tabs: Optional[QTabWidget] = None
        self._map_selected_body: Optional[QTextBrowser] = None
        self._map_selected_status_body: Optional[QTextBrowser] = None
        self._map_selected_paths_body: Optional[QTextBrowser] = None
        self._map_selected_messages_body: Optional[QTextBrowser] = None
        self._map_selected_center_btn: Optional[QPushButton] = None
        self._map_selected_paths_btn: Optional[QPushButton] = None
        self._map_selected_group_btn: Optional[QPushButton] = None
        self._map_selected_topic_btn: Optional[QPushButton] = None
        self._map_selected_messages_btn: Optional[QPushButton] = None
        self._map_selected_spotter_btn: Optional[QPushButton] = None
        self._map_selected_sop_btn: Optional[QPushButton] = None
        self._map_selected_payload: Dict[str, object] = {}
        self._regional_summary_collapsed: bool = True
        self._show_fio_button: Optional[QPushButton] = None
        self._controls_button: Optional[QPushButton] = None
        self._controls_drawer_open: bool = False
        self._controls_drawer_threshold: int = 1280
        self._drawer_mode: bool = True
        self._applied_controls_drawer_open: Optional[bool] = None
        self._map_filter_columns: Optional[int] = None
        self._map_canvas_layout_signature: Optional[tuple[bool, int, int]] = None
        self._main_splitter: Optional[QSplitter] = None
        self._controls_panel: Optional[QWidget] = None
        self._controls_handle_button: Optional[QToolButton] = None
        self._map_support_layout: Optional[QHBoxLayout] = None
        self._controls_top_spacer: Optional[QWidget] = None
        self._map_filter_bar: Optional[QWidget] = None
        base = Path(__file__).resolve().parents[2]
        self._prop_service = PropagationService(
            default_profiles=PROP_DEFAULT_PROFILES,
            profiles_path=base / "config" / "propagation" / "prop_profiles.json",
            climatology_db_path=base / "config" / "propagation" / "prop_climatology.db",
            outcome_db_path=get_config_dir() / "config" / "freqinout_nets.db",
            db_index_mode="floor5",
        )
        self._presence_weights_cache: Optional[Dict[str, Dict]] = None
        self._presence_weights_ts: float = 0.0
        self._query_cache: Dict[tuple, tuple[float, Any]] = {}
        self._query_cache_ttl_sec: float = 3.0
        self._prop_prewarm_done: bool = False
        self._initial_data_loaded: bool = False
        self._map_refresh_timer = QTimer(self)
        self._map_refresh_timer.setSingleShot(True)
        self._map_refresh_timer.setInterval(160)
        self._map_refresh_timer.timeout.connect(self._flush_requested_map_refresh)
        self._map_search_timer = QTimer(self)
        self._map_search_timer.setSingleShot(True)
        self._map_search_timer.setInterval(260)
        self._map_search_timer.timeout.connect(self._on_map_search_timeout)
        self._map_geometry_timer = QTimer(self)
        self._map_geometry_timer.setSingleShot(True)
        self._map_geometry_timer.setInterval(0)
        self._map_geometry_timer.timeout.connect(self._flush_map_geometry_reflow)

        self._map_geographic_country = ""
        self._map_country_catalog = ()
        from freqinout.gui.map_camera_preferences import validated_view
        saved_start = validated_view(self.settings.get("map_starting_view", None)) if self.settings else None
        self._map_user_camera_owned = bool(saved_start)
        if saved_start:
            self._last_map_view = saved_start
        self._build_ui()
        self._refresh_group_filter_options()
        self._refresh_region_filter_options()
        self._load_display_preferences()
        self._refresh_band_options()
        self.apply_theme()

    def _ensure_initial_data_loaded(self) -> None:
        if self._initial_data_loaded:
            return
        # The first visible refresh loads the roster on the map read worker.
        return

    def _bool_setting(self, key: str, default: bool = False) -> bool:
        if not self.settings:
            return default
        try:
            val = self.settings.get(key, default)
        except Exception:
            return default
        if isinstance(val, bool):
            return val
        if val is None:
            return default
        sval = str(val).strip().lower()
        return sval in ("1", "true", "yes", "on")

    def _load_display_preferences(self):
        if not self.settings:
            return
        def apply_chk(chk: QCheckBox, attr: str, key: str, default: bool = False):
            val = self._bool_setting(key, default)
            setattr(self, attr, val)
            chk.blockSignals(True)
            chk.setChecked(val)
            chk.blockSignals(False)

        apply_chk(self.show_calls_chk, "show_callsigns", "map_show_callsigns", False)
        apply_chk(self.show_regions_chk, "show_regions", "map_show_regions", False)
        apply_chk(self.show_states_chk, "show_states", "map_show_states", True)
        apply_chk(self.show_cities_chk, "show_cities", "map_show_cities", False)
        apply_chk(self.show_grid_labels_chk, "show_grids", "map_show_grids", False)
        apply_chk(self.map_stations_chk, "show_station_markers", "map_show_station_markers", True)
        apply_chk(self.map_links_chk, "show_link_paths", "map_show_link_paths", False)
        apply_chk(self.map_weather_chk, "show_weather_reports", "map_show_weather_reports", True)
        apply_chk(self.map_alerts_chk, "show_alert_reports", "map_show_alert_reports", True)
        apply_chk(self.map_infrastructure_chk, "show_infrastructure_reports", "map_show_infrastructure_reports", True)
        if not any(
            bool(value)
            for value in (
                self.show_station_markers,
                self.show_link_paths,
                self.show_weather_reports,
                self.show_alert_reports,
                self.show_infrastructure_reports,
            )
        ):
            self.show_station_markers = True
            self.map_stations_chk.blockSignals(True)
            self.map_stations_chk.setChecked(True)
            self.map_stations_chk.blockSignals(False)
        if self.show_link_paths:
            self.show_station_markers = True
            self.map_stations_chk.blockSignals(True)
            self.map_stations_chk.setChecked(True)
            self.map_stations_chk.blockSignals(False)
        # Map propagation overlay defaults OFF on every app launch.
        self.prop_overlay_enabled = False
        if self.prop_overlay_chk is not None:
            self.prop_overlay_chk.blockSignals(True)
            self.prop_overlay_chk.setChecked(False)
            self.prop_overlay_chk.blockSignals(False)
        try:
            self.prop_adaptive_enabled = self._bool_setting("map_prop_adaptive", True)
        except Exception:
            pass
        try:
            mode = (self.settings.get("map_prop_mode", "blended") or "blended").strip().lower()
        except Exception:
            mode = "blended"
        if mode == "adaptive":
            mode = "actual"
        if mode not in ("model", "actual", "blended"):
            mode = "blended"
        self.prop_mode = mode
        if self.prop_mode_combo is not None:
            idx_mode = self.prop_mode_combo.findData(mode)
            if idx_mode >= 0:
                self.prop_mode_combo.blockSignals(True)
                self.prop_mode_combo.setCurrentIndex(idx_mode)
                self.prop_mode_combo.blockSignals(False)
        try:
            self.prop_window_hours = int(self.settings.get("map_prop_window_hours", 6) or 6)
        except Exception:
            self.prop_window_hours = 6
        if self.prop_window_combo is not None:
            idx_window = self.prop_window_combo.findData(int(self.prop_window_hours or 6))
            if idx_window >= 0:
                self.prop_window_combo.blockSignals(True)
                self.prop_window_combo.setCurrentIndex(idx_window)
                self.prop_window_combo.blockSignals(False)
        # show_grid_labels mirrors show_grids
        self.show_grid_labels = self.show_grids

        try:
            idx = int(self.settings.get("map_city_pop_idx", 4) or 4)
        except Exception:
            idx = 4
        idx = max(0, min(idx, self.city_pop_combo.count() - 1))
        self.city_pop_combo.blockSignals(True)
        self.city_pop_combo.setCurrentIndex(idx)
        self.city_pop_combo.blockSignals(False)
        try:
            self.city_pop_min = int(self.city_pop_combo.itemData(idx))
        except Exception:
            pass
        self._sync_city_pop_enabled()
        self.show_city_labels = self.show_cities
        self._refresh_prop_target_controls()

    def _save_display_preferences(self):
        if not self.settings:
            return
        try:
            self.settings.set("map_show_callsigns", int(self.show_calls_chk.isChecked()))
            self.settings.set("map_show_regions", int(self.show_regions_chk.isChecked()))
            self.settings.set("map_show_states", int(self.show_states_chk.isChecked()))
            self.settings.set("map_show_cities", int(self.show_cities_chk.isChecked()))
            self.settings.set("map_show_grids", int(self.show_grid_labels_chk.isChecked()))
            self.settings.set("map_show_station_markers", int(self.map_stations_chk.isChecked()))
            self.settings.set("map_show_link_paths", int(self.map_links_chk.isChecked()))
            self.settings.set("map_show_weather_reports", int(self.map_weather_chk.isChecked()))
            self.settings.set("map_show_alert_reports", int(self.map_alerts_chk.isChecked()))
            self.settings.set("map_show_infrastructure_reports", int(self.map_infrastructure_chk.isChecked()))
            self.settings.set("map_city_pop_idx", self.city_pop_combo.currentIndex())
            if self.prop_overlay_chk is not None:
                self.settings.set("map_prop_overlay", int(self.prop_overlay_chk.isChecked()))
            else:
                self.settings.set("map_prop_overlay", int(bool(self.prop_overlay_enabled)))
            self.settings.set("map_prop_adaptive", int(bool(self.prop_adaptive_enabled)))
            self.settings.set("map_prop_mode", (self.prop_mode or "blended").lower())
            self.settings.set("map_prop_window_hours", int(self.prop_window_hours or 6))
        except Exception:
            pass

    def _start_js8_ingest_timer(self):
        try:
            app = QCoreApplication.instance()
            if app:
                app.aboutToQuit.connect(self._record_exit_time)
        except Exception:
            pass
        self._js8_timer = QTimer(self)
        self._js8_timer.setInterval(5 * 60 * 1000)  # 5 minutes
        self._js8_timer.timeout.connect(lambda: self._auto_ingest_and_refresh(initial=False))
        if self._map_visible and self._app_active and not self._is_shutting_down:
            self._js8_timer.start()
        # Start display refresh timer (separate from ingest) using selected interval
        # JS8 RX live ingestion timer
        self._start_js8_rx_listener()

    def _prewarm_map_perf_caches(self) -> None:
        # Lazy worker construction owns propagation/profile input reads.
        if self._map_visible and self._app_active:
            self._request_map_refresh(level="light", reason="prewarm")

    def _start_js8_rx_listener(self):
        """
        Attach to JS8 RX hub and ingest live traffic in real time.
        """
        if not self.settings:
            return
        host = (self.settings.get("js8_host", "") or "").strip() or "127.0.0.1"
        try:
            port = int(self.settings.get("js8_port", 2442) or 2442)
        except Exception:
            port = 2442
        if self._js8_rx_hub is None or self._js8_rx_hub.endpoint() != (host, port):
            if self._js8_rx_hub is not None and self._js8_rx_registered:
                try:
                    self._js8_rx_hub.unregister_listener(self._on_js8_rx_messages)
                except Exception:
                    pass
                self._js8_rx_registered = False
            self._js8_rx_hub = JS8RxHub.instance(host, port)
        if not self._js8_rx_registered:
            self._js8_rx_hub.register_listener(self._on_js8_rx_messages)
            self._js8_rx_registered = True
        self._js8_rx_hub.start(host, port)

    def _get_js8_indexer(self) -> Optional[JS8LogLinkIndexer]:
        if self._js8_indexer is None:
            try:
                db_path = get_config_dir() / "config" / "freqinout_nets.db"
                self._js8_indexer = JS8LogLinkIndexer(self.settings, db_path)
            except Exception as e:
                log.debug("StationsMap: indexer init failed: %s", e)
                self._js8_indexer = None
        return self._js8_indexer

    def _js8_live_source_context(self) -> Dict[str, str]:
        if not self.settings:
            return {}
        host = (self.settings.get("js8_host", "") or "").strip() or "127.0.0.1"
        try:
            port = int(self.settings.get("js8_port", 2442) or 2442)
        except Exception:
            port = 2442
        endpoint = f"{host}:{port}".strip().lower()
        cache = getattr(self, "_js8_live_source_context_cache", {}) or {}
        if (
            str(cache.get("endpoint", "") or "") == endpoint
            and float(cache.get("expires", 0.0) or 0.0) > time.time()
            and isinstance(cache.get("context"), dict)
        ):
            return dict(cache.get("context") or {})
        context = resolve_js8_source_context(self.settings, host=host, port=port)
        if context:
            self._js8_live_source_context_cache = {
                "endpoint": endpoint,
                "expires": time.time() + 30.0,
                "context": context,
            }
            return context
        self._js8_live_source_context_cache = {
            "endpoint": endpoint,
            "expires": time.time() + 30.0,
            "context": {},
        }
        return {}

    def _schedule_render(self) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        if not getattr(self, "_app_active", True) or not getattr(self, "_map_visible", False):
            self._map_dirty = True
            self._pending_refresh_level = max(getattr(self, "_pending_refresh_level", 0), 2)
            self._pending_refresh_reason = getattr(self, "_pending_refresh_reason", "") or "schedule"
            self._pending_refresh_preserve_view = True
            return
        self._request_map_refresh(level="medium", reason="schedule", preserve_view=True)

    def _flush_scheduled_render(self) -> None:
        self._flush_requested_map_refresh()

    @staticmethod
    def _refresh_level_rank(level: str) -> int:
        normalized = str(level or "").strip().lower()
        return {"light": 1, "medium": 2, "full": 3}.get(normalized, 2)

    @staticmethod
    def _refresh_level_name(rank: int) -> str:
        mapping = {1: "light", 2: "medium", 3: "full"}
        return mapping.get(int(rank or 0), "medium")

    def _emit_map_event(self, event: str, **meta: object) -> None:
        payload = {
            "event": str(event or "").strip(),
            "state": getattr(self, "_map_runtime_state", "cold"),
            "visible": bool(getattr(self, "_map_visible", False)),
            "initialized": bool(getattr(self, "_map_initialized", False)),
            "loading": bool(getattr(self, "_map_page_loading", False)),
            "markers": int(getattr(self, "_map_marker_count", 0) or 0),
            "links": int(getattr(self, "_map_link_count", 0) or 0),
        }
        payload.update(meta)
        try:
            log.info("MAP|%s", json.dumps(payload, sort_keys=True, default=str))
        except Exception:
            log.info("MAP|%s", payload)
        self._map_last_event_ts = time.time()

    def _cached_map_value(
        self,
        cache_name: str,
        cache_key: object,
        loader,
        *,
        ttl_sec: float,
        force: bool = False,
    ):
        if force or ttl_sec <= 0:
            value = loader()
            try:
                key_sig = json.dumps(cache_key, sort_keys=True, default=str)
                self._map_query_cache[(str(cache_name or ""), key_sig)] = (time.time(), value)
            except Exception:
                pass
            return value
        try:
            key_sig = json.dumps(cache_key, sort_keys=True, default=str)
        except Exception:
            key_sig = str(cache_key)
        cache_key_tuple = (str(cache_name or ""), key_sig)
        now_ts = time.time()
        cached = self._map_query_cache.get(cache_key_tuple)
        if cached is not None:
            cached_ts, cached_value = cached
            if (now_ts - float(cached_ts or 0.0)) <= float(ttl_sec):
                return cached_value
        value = loader()
        self._map_query_cache[cache_key_tuple] = (now_ts, value)
        return value

    @staticmethod
    def _path_stat_fingerprint(path: str | Path | None) -> Tuple[str, int, int]:
        try:
            p = Path(path) if path is not None else None
        except Exception:
            p = None
        if p is None:
            return ("", 0, 0)
        try:
            stat = p.stat()
            return (str(p), int(stat.st_size), int(stat.st_mtime_ns))
        except Exception:
            return (str(p), 0, 0)

    @staticmethod
    def _map_band_filter_signature(band_filter: object) -> Tuple[Tuple[str, str], ...]:
        if isinstance(band_filter, dict):
            try:
                return tuple(sorted((str(k), str(v)) for k, v in band_filter.items()))
            except Exception:
                return tuple()
        return (("value", str(band_filter or "")),)

    def _nets_db_fingerprint(self) -> tuple:
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return ("", 0, 0)
        # WAL commits may leave the main file untouched for many refreshes.
        return (self._path_stat_fingerprint(db_path), self._path_stat_fingerprint(str(db_path) + "-wal"))

    def _station_points_for_map(self) -> List[StationPoint]:
        points = list(self.stations)
        call = str(self.settings.get("operator_callsign", "") or "").strip().upper()
        grid = str(self.settings.get("operator_grid6", "") or self.settings.get("operator_grid", "") or "").strip().upper()
        location = maidenhead_to_latlon(grid) if re.fullmatch(r"[A-R]{2}\d{2}(?:[A-X]{2})?", grid) else None
        if call and location:
            # Saved operator location is authoritative for my station.
            existing = next((point for point in points if point.callsign.upper() == call), None)
            points = [point for point in points if point.callsign.upper() != call]
            own = replace(existing, grid=grid, lat=location[0], lon=location[1]) if existing else StationPoint(callsign=call, grid=grid, lat=location[0], lon=location[1])
            points.insert(0, own)
        return points

    def _set_station_points(self, points: List[StationPoint]) -> None:
        self.stations = list(points)
        self._stations_revision += 1

    def _set_map_runtime_state(self, state: str, detail: str = "", *, error: str = "") -> None:
        normalized = str(state or "cold").strip().lower() or "cold"
        self._map_runtime_state = normalized
        self._map_runtime_detail = str(detail or "").strip()
        self._map_last_error = str(error or "").strip()
        self._update_map_support_card()

    def _application_window(self) -> object:
        ref = getattr(self, "_application_host_ref", None)
        if callable(ref):
            try:
                host = ref()
            except Exception:
                host = None
            if host is not None:
                return host
        return self.window()

    def _bring_application_window_forward(self) -> None:
        """Present the existing main CommsPlan window without touching Map geometry."""
        host_ref = getattr(self, "_application_host_ref", None)
        if not callable(host_ref):
            return
        try:
            main_window = host_ref()
        except Exception:
            main_window = None
        if main_window is None:
            return
        presenter = getattr(main_window, "present_main_window", None)
        if callable(presenter):
            # Never change top-level focus re-entrantly from a QPushButton
            # signal. On macOS/PySide that can synchronously enter application
            # lifecycle callbacks while Qt is still dispatching the click and
            # deadlock in a nested signal connection. A queued meta-call uses
            # the next event-loop turn without allocating or connecting a
            # transient QTimer.
            if isinstance(main_window, QObject):
                try:
                    if QMetaObject.invokeMethod(main_window, "present_main_window", Qt.QueuedConnection):
                        return
                except Exception:
                    log.debug("StationsMap: queued CommsPlan presentation failed", exc_info=True)
            presenter()
            return
        # Compatibility for lightweight hosts and tests. Real MainWindow owns
        # the stronger platform-aware activation contract above.
        try:
            main_window.raise_()
            main_window.activateWindow()
        except Exception as exc:
            log.debug("StationsMap: failed bringing CommsPlan workspace to front: %s", exc)

    def _request_map_refresh(self, *, level: str = "medium", reason: str = "", preserve_view: object = True) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        # Invalidate at input change, before the coalescing timer can fire.
        self._map_payload_generation = getattr(self, "_map_payload_generation", 0) + 1
        self._map_inspector_generation = getattr(self, "_map_inspector_generation", 0) + 1
        requested_rank = self._refresh_level_rank(level)
        if not getattr(self, "_app_active", True):
            self._map_dirty = True
            self._pending_refresh_level = max(getattr(self, "_pending_refresh_level", 0), requested_rank)
            self._pending_refresh_reason = reason or getattr(self, "_pending_refresh_reason", "")
            self._pending_refresh_preserve_view = preserve_view
            self._emit_map_event("render_deferred_inactive", level=self._refresh_level_name(requested_rank), reason=reason)
            return
        if not getattr(self, "_map_visible", False):
            self._map_dirty = True
            self._pending_refresh_level = max(getattr(self, "_pending_refresh_level", 0), requested_rank)
            self._pending_refresh_reason = reason or getattr(self, "_pending_refresh_reason", "")
            self._pending_refresh_preserve_view = preserve_view
            return
        if getattr(self, "_map_page_loading", False):
            self._map_dirty = True
            self._render_requested_during_load = True
            self._render_requested_during_load_level = max(
                getattr(self, "_render_requested_during_load_level", 0),
                requested_rank,
            )
            self._pending_refresh_reason = reason or getattr(self, "_pending_refresh_reason", "")
            self._pending_refresh_preserve_view = preserve_view
            self._emit_map_event("render_queued_while_loading", level=self._refresh_level_name(requested_rank), reason=reason)
            return
        self._pending_refresh_level = max(getattr(self, "_pending_refresh_level", 0), requested_rank)
        if reason:
            self._pending_refresh_reason = reason
        self._pending_refresh_preserve_view = preserve_view
        self._show_map_refresh_pending_feedback(level=self._refresh_level_name(requested_rank), reason=reason)
        delay_ms = 90 if requested_rank <= 1 else 180
        if requested_rank >= 3:
            delay_ms = 30
        self._render_pending = True
        timer = getattr(self, "_map_refresh_timer", None)
        if timer is None:
            QTimer.singleShot(0, self._flush_requested_map_refresh)
            return
        timer.start(delay_ms)
        self._emit_map_event("render_requested", level=self._refresh_level_name(requested_rank), reason=reason)

    def _show_map_refresh_pending_feedback(self, *, level: str, reason: str = "") -> None:
        """Make slow map work visible before the coalesced render starts."""
        if self._map_runtime_state == "degraded":
            return
        detail = self._map_loading_detail_text(level=level, reason=reason)
        self._set_map_runtime_state("loading", detail or "Refreshing map data.")
        label = getattr(self, "_map_loading_label", None)
        if label is not None:
            try:
                label.setText(detail or "Refreshing map data...")
            except Exception:
                pass

    def _flush_requested_map_refresh(self) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        rank = max(int(self._pending_refresh_level or 0), 2 if self._map_dirty else 0)
        reason = self._pending_refresh_reason or "coalesced"
        preserve_view = self._pending_refresh_preserve_view
        self._pending_refresh_level = 0
        self._pending_refresh_reason = ""
        self._pending_refresh_preserve_view = True
        self._render_pending = False
        if not self._map_visible or not self._app_active:
            self._map_dirty = True
            return
        if self._map_page_loading:
            self._map_dirty = True
            self._render_requested_during_load = True
            self._render_requested_during_load_level = max(self._render_requested_during_load_level, rank)
            return
        if rank <= 0:
            return
        now_ts = time.time()
        if rank < 3 and now_ts - self._last_map_render_ts < 0.75:
            self._pending_refresh_level = max(self._pending_refresh_level, rank)
            self._pending_refresh_reason = reason or self._pending_refresh_reason
            self._pending_refresh_preserve_view = preserve_view
            self._render_pending = True
            self._map_refresh_timer.start(220)
            return
        self._last_map_render_ts = now_ts
        self._perform_map_refresh(level=self._refresh_level_name(rank), reason=reason, preserve_view=preserve_view)

    def _perform_map_refresh(self, *, level: str, reason: str, preserve_view: object = True) -> None:
        self._emit_map_event("render_started", level=level, reason=reason)
        loading_detail = self._map_loading_detail_text(level=level, reason=reason)
        if level == "full" or not self._map_initialized:
            self._set_map_runtime_state("loading", loading_detail or "Refreshing the map surface and rebuilding overlays.")
        elif self._map_runtime_state != "degraded":
            self._set_map_runtime_state("loading", loading_detail or "Refreshing map data.")
        label = getattr(self, "_map_loading_label", None)
        if label is not None:
            try:
                label.setText(loading_detail or "Refreshing map data...")
            except Exception:
                pass
        try:
            with perf_span("map.render_call", settings=self.settings, meta={"source": reason, "level": level}, min_ms=10.0):
                self._render_map(preserve_view=preserve_view)
        except Exception as exc:
            self._enter_map_degraded(
                "Map refresh did not complete cleanly. You can retry without restarting CommsPlan.",
                reason=reason,
                exc=exc,
            )
            return
        if not getattr(self, "_map_read_inflight", False) and not self._map_page_loading and self._map_runtime_state != "degraded":
            link_detail = str(getattr(self, "_map_link_status_detail", "") or "").strip()
            detail = self._map_ready_detail_text()
            if link_detail:
                detail = f"{detail} {link_detail}"
            self._set_map_runtime_state(
                "ready",
                detail,
            )
        self._emit_map_event("render_queued", level=level, reason=reason)

    def _map_loading_detail_text(self, *, level: str = "", reason: str = "") -> str:
        mode = self._current_map_mode_key()
        label = self._map_view_status_text().replace("Map View: ", "")
        seconds = int(getattr(self, "recency_seconds", 0) or 0)
        if seconds <= 0:
            return f"Building {label} from available history."
        if seconds >= 7 * 24 * 60 * 60:
            days = max(1, round(seconds / 86400))
            return f"Building {days}-day {label} view; aggregating older traffic before drawing details."
        if seconds > 24 * 60 * 60:
            hours = max(1, round(seconds / 3600))
            return f"Building {hours}-hour {label} view; this may take a moment."
        if mode == "paths":
            return "Building path view for the selected time window."
        return f"Refreshing {label}."

    def _enter_map_degraded(self, detail: str, *, reason: str = "", exc: Exception | None = None) -> None:
        error_text = str(exc or "").strip()
        self._set_map_runtime_state("degraded", detail, error=error_text)
        self._emit_map_event("degraded", reason=reason, error=error_text or detail)

    def _retry_map_render(self) -> None:
        self._emit_map_event("retry_requested")
        self._request_map_refresh(level="full", reason="retry", preserve_view=True)

    def _reload_map_data(self) -> None:
        self._emit_map_event("reload_data_requested")
        self._ensure_initial_data_loaded()
        self._auto_ingest_and_refresh(initial=False)

    def _map_support_summary(self) -> str:
        top_lines = [
            f"State: {self._map_runtime_state.title()}",
            f"Visible: {'Yes' if self._map_visible else 'No'}",
            f"Markers: {int(self._map_marker_count)}",
            f"Links: {int(self._map_link_count)}",
            f"Renderer Loading: {'Yes' if self._map_page_loading else 'No'}",
            f"Native Renderer Ready: {'Yes' if self._map_initialized else 'No'}",
        ]
        sections = [
            (
                "Current Detail",
                bullet_lines(
                    [
                        self._map_runtime_detail,
                        f"Last error: {self._map_last_error}" if self._map_last_error else "",
                        f"Active band filter: {self.selected_band or 'All'}",
                        f"Recency filter: {self.recency_combo.currentText()}" if hasattr(self, "recency_combo") else "",
                        f"Link mode: {self.link_mode_combo.currentText()}" if hasattr(self, "link_mode_combo") else "",
                        f"Group filter: {self.group_filter_combo.currentText()}" if hasattr(self, "group_filter_combo") else "",
                        f"Region filter: {self.region_filter_combo.currentText()}" if hasattr(self, "region_filter_combo") else "",
                    ]
                ),
            ),
        ]
        return build_support_summary("CommsPlan Multi-Rig Map Diagnostics", top_lines, sections=sections)

    def _copy_map_diagnostics(self) -> None:
        QApplication.clipboard().setText(self._map_support_summary())
        if hasattr(self, "_map_copy_summary_btn") and self._map_copy_summary_btn is not None:
            self._map_copy_summary_btn.setText("Copied")
            QTimer.singleShot(1500, lambda: self._map_copy_summary_btn.setText("Copy Diagnostics"))

    def _update_map_support_card(self) -> None:
        card = getattr(self, "_map_support_card", None)
        if card is None:
            return
        ready = self._map_runtime_state == "ready"
        # Once a usable map is on screen, background refresh feedback stays in
        # the compact strip so the native viewport never shifts during updates.
        routine_busy = bool(
            self._map_runtime_state in {"loading", "warming"}
            and getattr(self, "_map_initialized", False)
            and getattr(self, "_map_load_ok", False)
        )
        compact = ready or routine_busy
        label = getattr(self, "_map_support_label", None)
        if label is not None:
            text = self._map_runtime_detail or "Map is standing by."
            if ready:
                link_detail = str(getattr(self, "_map_link_status_detail", "") or "").strip()
                suffix = f" {link_detail}" if link_detail else ""
                label.setText(
                    f"Ready: {int(getattr(self, '_map_marker_count', 0) or 0)} {self._map_marker_noun()}, "
                    f"{int(getattr(self, '_map_link_count', 0) or 0)} links.{suffix}"
                )
            else:
                label.setText(f"Map Status: {self._map_runtime_state.title()}. {text}")
            if ready and getattr(self, "_current_map_mode_key", lambda: "all")() == "mesh_nodes":
                summary = getattr(self, "_mesh_map_summary", {})
                label.setText(
                    f"Ready: {summary.get('located_nodes', 0)}/{summary.get('known_nodes', 0)} nodes · "
                    f"{int(getattr(self, '_map_link_count', 0) or 0)} paths · "
                    f"{summary.get('unlocated_nodes', 0)} unlocated · {summary.get('excluded_nodes', 0)} excluded"
                )
            label.setToolTip(self._map_support_summary())
        support_layout = getattr(self, "_map_support_layout", None)
        if support_layout is not None:
            if compact:
                support_layout.setContentsMargins(8, 3, 8, 3)
            else:
                support_layout.setContentsMargins(10, 8, 10, 8)
        compact_height = 34
        try:
            compact_height = max(compact_height, int(self.fontMetrics().height()) + 14)
        except Exception:
            pass
        try:
            card.setMaximumHeight(compact_height if compact else 16777215)
        except Exception:
            pass
        theme = self._theme_snapshot()
        border = theme.get("border", "#cccccc")
        role = "muted"
        if self._map_runtime_state == "degraded":
            border = theme.get("danger", "#b3261e")
            role = "danger"
        elif self._map_runtime_state in {"loading", "warming"}:
            border = theme.get("warning", "#c99700")
            role = "warning"
        elif self._map_runtime_state == "ready":
            border = theme.get("success", theme.get("accent", "#2a6fd3"))
            role = "success"
        bg = theme.get("surface_alt", theme.get("surface", "#f7f7f7"))
        fg = theme.get("text", "#222222")
        card.setStyleSheet(
            "QFrame {"
            f" background: {bg};"
            f" border: 1px solid {border};"
            " border-radius: 6px;"
            "}"
            " QLabel {"
            f" color: {fg};"
            " border: none;"
            " background: transparent;"
            "}"
        )
        show_support_actions = not compact
        if getattr(self, "_map_retry_btn", None) is not None:
            self._map_retry_btn.setStyleSheet(button_style("warning" if role in {"warning", "danger"} else "secondary", theme))
            self._map_retry_btn.setVisible(show_support_actions)
        if getattr(self, "_map_reload_btn", None) is not None:
            self._map_reload_btn.setStyleSheet(button_style("secondary", theme))
            self._map_reload_btn.setVisible(show_support_actions)
        if getattr(self, "_map_copy_summary_btn", None) is not None:
            self._map_copy_summary_btn.setStyleSheet(button_style("secondary", theme))
            self._map_copy_summary_btn.setVisible(
                show_support_actions and self._map_runtime_state in {"loading", "warming", "degraded"}
            )
        if getattr(self, "_map_support_help_btn", None) is not None:
            self._map_support_help_btn.setStyleSheet(button_style("muted", theme))
            self._map_support_help_btn.setVisible(show_support_actions)

    def _map_marker_noun(self) -> str:
        try:
            if self._effective_map_observation_focus_enabled():
                mode = self._effective_map_report_focus_mode()
                if mode == "rf_pins":
                    return "planning pins"
                if mode == "regional_intelligence":
                    return "regional concern areas"
                if mode in {"hf_reports", "local_reports", "all_reports"}:
                    return "traffic items"
        except Exception:
            pass
        return "stations"

    def _map_ready_detail_text(self) -> str:
        return (
            f"Map is ready with {int(getattr(self, '_map_marker_count', 0) or 0)} {self._map_marker_noun()} "
            f"and {int(getattr(self, '_map_link_count', 0) or 0)} links."
        )

    def _start_map_ingest_lifecycle(self) -> None:
        if self._ingest_started:
            return
        self._ingest_started = True
        self._start_js8_ingest_timer()
        # Initial ingest to catch up since last run (looks back to last exit time if available)
        QTimer.singleShot(500, lambda: self._auto_ingest_and_refresh(initial=True))

    def _maybe_start_map_ingest(self) -> bool:
        if self._ingest_started or not self._deferred_initial_ingest_pending:
            return False
        if not self._map_load_ok:
            return False
        self._deferred_initial_ingest_pending = False
        self._start_map_ingest_lifecycle()
        return True

    def set_map_visible(self, is_visible: bool) -> None:
        is_visible = bool(is_visible)
        renderer = getattr(self, "_native_map_renderer", None)
        if renderer is not None:
            # An unfocused but visible Map must retain its scene. Application
            # activity gates refresh/ingest timers and publication separately.
            renderer.set_map_visible(is_visible)
        if self._map_visible == is_visible:
            return
        self._map_visible = is_visible
        references = getattr(self,"_license_reference_presenter",None)
        if references is not None:
            references.set_active(self._map_visible and self._app_active)
        geography_lane = getattr(self,"_world_geography_lane",None)
        if not is_visible and geography_lane is not None:
            geography_lane.invalidate()
        if not self._map_visible:
            if self._js8_timer is not None:
                self._js8_timer.stop()
            return
        if not self._app_active:
            self._map_dirty = True
            self._set_map_runtime_state("warming", "Preparing the map view.")
            self._emit_map_event("activation_deferred_inactive")
            return
        if self._map_visible and not self._ingest_started:
            self._deferred_initial_ingest_pending = True
            self._maybe_start_map_ingest()
        elif self._map_visible and self._js8_timer is not None and self._ingest_started and not self._js8_timer.isActive():
            self._js8_timer.start()
        if getattr(self, "_map_runtime_state", "") == "degraded":
            self._request_map_refresh(level="medium", reason="reopen_retry")
            return
        if (self._map_initialized and self._map_load_ok and not self._map_dirty
                and not self._pending_refresh_level and not getattr(self, "_pending_map_payload", None)
                and not getattr(self, "_map_read_inflight", False)):
            self._set_map_runtime_state("ready", self._map_ready_detail_text())
            self._emit_map_event("activation_ready")
            return
        self._set_map_runtime_state("warming", "Preparing the map view and refreshing station data.")
        self._emit_map_event("activation_started")
        QTimer.singleShot(0, self._on_map_visible_deferred)

    def set_app_active(self, active: bool) -> None:
        self._app_active = bool(active)
        references = getattr(self,"_license_reference_presenter",None)
        if references is not None:
            references.set_active(self._map_visible and self._app_active)
        renderer = getattr(self, "_native_map_renderer", None)
        if renderer is not None:
            renderer.set_map_visible(self._map_visible)
        if not self._app_active:
            if self._js8_timer is not None:
                self._js8_timer.stop()
            self._emit_map_event("ui_paused_inactive")
            return
        if self._map_visible and not self._is_shutting_down:
            if self._js8_timer is not None and self._ingest_started and not self._js8_timer.isActive():
                self._js8_timer.start()
            if getattr(self, "_map_runtime_state", "") == "degraded":
                self._request_map_refresh(level="medium", reason="resume_retry")
                return
            if (self._map_initialized and self._map_load_ok and not self._map_dirty
                    and not self._pending_refresh_level and not getattr(self, "_pending_map_payload", None)
                and not getattr(self, "_map_read_inflight", False)):
                self._set_map_runtime_state("ready", self._map_ready_detail_text())
                self._emit_map_event("ui_resumed_ready")
                return
            self._set_map_runtime_state("warming", "Resuming map view.")
            self._emit_map_event("ui_resumed")
            QTimer.singleShot(0, self._on_map_visible_deferred)

    def _on_js8_rx_messages(self, messages: List[dict]) -> None:
        """
        Consume js8net rx messages and upsert live observations into js8_links.
        """
        if self._is_shutting_down or self._js8_polling or not self._map_visible:
            return
        if not self.settings:
            return
        indexer = self._get_js8_indexer()
        if indexer is None:
            return
        start_ts = time.time()
        try:
            my_call = (self.settings.get("operator_callsign", "") or "").upper()
        except Exception:
            my_call = ""

        updated = False
        observations: List[tuple] = []
        self._js8_polling = True
        try:
            source_context = self._js8_live_source_context()
            try:
                if any("F!" in str((msg.get("params", {}) or {}).get("TEXT") or msg.get("value") or "").upper() for msg in messages if isinstance(msg, dict)):
                    MessageIngestor(self.settings).ingest_spotter_from_js8_events(
                        messages,
                        source_radio_id=source_context.get("source_radio_id", ""),
                        js8_instance_id=source_context.get("js8_instance_id", ""),
                    )
            except Exception as exc:
                log.debug("StationsMap: Spotter live ingest failed: %s", exc)
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                try:
                    ts = float(msg.get("time") or time.time())
                except Exception:
                    ts = time.time()
                params = msg.get("params", {})
                mtype = (msg.get("type") or "").strip().upper()
                freq_hz = None
                try:
                    fval = params.get("FREQ")
                    if fval not in (None, ""):
                        freq_hz = float(fval)
                except Exception:
                    freq_hz = None
                origin = ""
                dest = ""
                snr = None
                is_spotter = 0
                if mtype == "RX.SPOT":
                    origin = str(params.get("CALL") or "").upper()
                    dest = my_call
                    snr = params.get("SNR")
                    is_spotter = 1
                elif mtype == "RX.DIRECTED":
                    origin = str(params.get("FROM") or "").upper()
                    dest = str(params.get("TO") or params.get("CALL") or "").upper()
                    snr = params.get("SNR") or params.get("EXTRA")
                    if not dest:
                        dest = my_call
                else:
                    continue
                if not origin or not dest:
                    continue
                try:
                    snr_val = float(snr)
                except Exception:
                    snr_val = None
                observations.append(
                    (
                        ts,
                        origin,
                        dest,
                        snr_val,
                        freq_hz,
                        is_spotter,
                        source_context.get("source_id", ""),
                        source_context.get("app_instance_id", ""),
                        source_context.get("source_radio_id", ""),
                    )
                )
                updated = True
        finally:
            self._js8_polling = False
        if observations:
            try:
                indexer.ingest_live_batch(observations)
            except Exception as e:
                log.debug("StationsMap: live ingest batch failed: %s", e)
        if updated:
            self._last_js8_load_ts = max(self._last_js8_load_ts, time.time())
            self._schedule_render()
        elapsed = time.time() - start_ts
        if elapsed > 0.5:
            log.debug("StationsMap: js8 rx ingest took %.2fs", elapsed)

    def _record_exit_time(self):
        try:
            if self.settings:
                now_ts = time.time()
                self.settings.set("last_exit_utc", now_ts)
                self._last_exit_ts = now_ts
        except Exception:
            pass

    def _ingest_js8_logs(self, since_ts: Optional[float] = None, *, force_rebuild: bool = False) -> int:
        """
        Run JS8 log ingestion (DIRECTED/ALL) and persist last load timestamp.
        """
        if not self.settings:
            return 0
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
            result = ingest_js8_links_for_runtime_sources(
                self.settings,
                db_path,
                since_ts=since_ts,
                force_rebuild=force_rebuild,
            )
            self._last_js8_load_ts = result.latest_ts
            try:
                self.settings.set("js8_links_last_load_utc", result.latest_ts)
            except Exception:
                pass
            if result.used_runtime_sources:
                log.info("StationsMap: JS8 traffic ingested from runtime sources (%s rows)", result.inserted)
            else:
                log.info("StationsMap: JS8 traffic ingested from legacy JS8 settings (%s rows)", result.inserted)
            return result.inserted
        except Exception as e:
            log.error("StationsMap: JS8 log ingest failed: %s", e)
            return 0

    def _request_background_ingest(self, *kinds: str) -> bool:
        controller = getattr(self._application_window(), "background_ingest", None)
        if controller is None:
            return False
        try:
            if hasattr(controller, "is_running") and not controller.is_running():
                return False
            self._connect_background_ingest_notifications(controller)
            if hasattr(controller, "request_refresh"):
                controller.request_refresh(*kinds)
                return True
        except Exception as exc:
            log.debug("StationsMap: background ingest request failed: %s", exc)
        return False

    def _connect_background_ingest_notifications(self, controller: object) -> None:
        if self._background_ingest_controller is controller:
            return
        signal = getattr(controller, "job_finished", None)
        if signal is None:
            return
        try:
            signal.connect(self._on_background_ingest_finished)
            self._background_ingest_controller = controller
        except Exception as exc:
            log.debug("StationsMap: background ingest signal connect failed: %s", exc)

    def _on_background_ingest_finished(self, job_name: str) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        if str(job_name or "").strip().lower() in {"js8_links", "varac"}:
            self._schedule_render()

    def _auto_ingest_and_refresh(self, initial: bool = False, *, operator_refresh: bool = False):
        """
        Background ingest and refresh map. Used on timer and manual refresh.
        """
        if getattr(self, "_is_shutting_down", False):
            return
        if not self._app_active:
            self._map_dirty = True
            self._emit_map_event("ingest_deferred_inactive")
            return
        since = None
        if initial:
            since = max(self._last_js8_load_ts, self._last_exit_ts)
        elif self._js8_rx_hub and self._js8_rx_hub.is_active():
            since = self._last_js8_load_ts
        use_background = not bool(operator_refresh)
        if use_background and self._request_background_ingest("js8_links", "varac"):
            pass
        else:
            inserted = self._ingest_js8_logs(
                since_ts=None if operator_refresh else since,
                force_rebuild=bool(operator_refresh),
            )
            if operator_refresh and inserted > 0:
                self._widen_recency_for_manual_link_refresh()
            try:
                ingest_varac_for_runtime_sources(self.settings)
            except Exception:
                pass
        self._schedule_render()

    def _widen_recency_for_manual_link_refresh(self) -> None:
        """
        Manual link refresh means "show what CommsPlan just loaded." If imported or
        migrated traffic is older than the current recency window, widen to Any
        so a successful reload does not look empty.
        """
        combo = getattr(self, "recency_combo", None)
        if combo is None:
            self.recency_seconds = None
            return
        try:
            if self.recency_seconds is None:
                return
            idx = combo.findText("Any") if hasattr(combo, "findText") else -1
            if idx >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)
                self._map_recency_label = "Any"
                self._update_map_since_button_text("Any")
            self.recency_seconds = None
        except Exception:
            self.recency_seconds = None
            self._map_recency_label = "Any"

    def _map_recency_options(self) -> List[Tuple[str, Optional[int]]]:
        return [
            ("Any", None),
            ("15m", 15 * 60),
            ("30m", 30 * 60),
            ("1h", 60 * 60),
            ("3h", 3 * 60 * 60),
            ("6h", 6 * 60 * 60),
            ("12h", 12 * 60 * 60),
            ("24h", 24 * 60 * 60),
            ("3d", 3 * 24 * 60 * 60),
            ("7d", 7 * 24 * 60 * 60),
            ("14d", 14 * 24 * 60 * 60),
            ("30d", 30 * 24 * 60 * 60),
            ("60d", 60 * 24 * 60 * 60),
            ("90d", 90 * 24 * 60 * 60),
        ]

    def _path_recency_options(self) -> List[Tuple[str, Optional[int]]]:
        return [
            (label, seconds)
            for label, seconds in self._map_recency_options()
            if seconds is not None and int(seconds or 0) <= MAP_PATH_MAX_RECENCY_SECONDS
        ]

    def _current_recency_options(self) -> List[Tuple[str, Optional[int]]]:
        return self._map_recency_options()

    def _clamp_path_recency_if_needed(self) -> bool:
        # Compatibility hook: the selected age is never rewritten by a layer.
        # Path readers apply the separate 24h maximum on their copied inputs.
        self._update_map_since_button_text()
        return False

    def _map_recency_display_label(self, value: str) -> str:
        label = str(value or "").strip() or "Any"
        return "Age: Any" if label == "Any" else f"Age: {label}"

    def _map_recency_menu_label(self, value: str) -> str:
        label = str(value or "").strip() or "Any"
        return "Any" if label == "Any" else label

    def _update_map_since_button_text(self, value: Optional[str] = None) -> None:
        button = getattr(self, "_map_since_button", None)
        if button is None:
            return
        label = str(value or "").strip()
        if not label:
            label = str(getattr(self, "_map_recency_label", "") or "").strip()
        if not label:
            combo = getattr(self, "recency_combo", None)
            try:
                label = str(combo.currentText() or "").strip() if combo is not None else ""
            except Exception:
                label = ""
        self._map_recency_label = label or "Any"
        activity_label = label or "Any"
        button.setText(activity_label)
        button.setToolTip("Selected activity age. Observed paths use at most the last 24h; other layers retain this age.")

    def _build_map_since_menu(self) -> None:
        button = getattr(self, "_map_since_button", None)
        if button is None:
            return
        button.clicked.connect(self._show_map_since_popover)

    def _show_map_since_popover(self) -> None:
        button = getattr(self, "_map_since_button", None)
        if button is None:
            return
        existing = getattr(self, "_map_since_popover", None)
        if existing is not None:
            try:
                existing.close()
            except Exception:
                pass
        popover = QDialog(self, Qt.Popup)
        popover.setObjectName("MapSincePopover")
        popover.setWindowTitle("Map Time Window")
        layout = QVBoxLayout(popover)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        title = QLabel("Show map activity age", popover)
        title.setStyleSheet("font-weight: 700;")
        layout.addWidget(title)

        options_by_label = {label: seconds for label, seconds in self._current_recency_options()}
        path_limited = self._current_map_mode_key() == "paths" or (
            bool(getattr(self, "show_link_paths", False))
            and str(getattr(self, "link_mode", "") or "").strip().lower() != "off"
        )
        quick_groups = [
            ("Recent", ("15m", "30m", "1h", "3h", "6h", "12h", "24h")),
        ]
        if not path_limited:
            quick_groups.extend(
                [
                    ("Days", ("3d", "7d", "14d")),
                    ("Archive", ("30d", "60d", "90d", "Any")),
                ]
            )
        for group_label, labels in quick_groups:
            group_title = QLabel(group_label, popover)
            group_title.setStyleSheet(
                f"color: {self._theme_snapshot().get('text_muted', '#5f6b76')}; font-weight: 700;"
            )
            layout.addWidget(group_title)
            row = QGridLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setHorizontalSpacing(6)
            row.setVerticalSpacing(6)
            for idx, label in enumerate(labels):
                if label not in options_by_label:
                    continue
                chip = QPushButton(label, popover)
                chip.setMinimumWidth(58)
                chip.setToolTip(f"Show mapped activity up to {label} old.")
                if label == "Any":
                    chip.setToolTip("Show all indexed mapped activity.")
                chip.clicked.connect(
                    lambda _checked=False, selected=label, dialog=popover: (
                        self._set_map_recency_from_label(selected),
                        dialog.close(),
                    )
                )
                row.addWidget(chip, idx // 4, idx % 4)
            layout.addLayout(row)

        if path_limited:
            note = QLabel("Paths use the last 24h or less; older contacts are shown as station history.", popover)
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {self._theme_snapshot().get('text_muted', '#5f6b76')};")
            layout.addWidget(note)
        else:
            custom_row = QHBoxLayout()
            custom_row.setContentsMargins(0, 2, 0, 0)
            custom_row.addWidget(QLabel("Custom days", popover))
            custom_days = QLineEdit(popover)
            custom_days.setPlaceholderText("days")
            custom_days.setToolTip("Enter a custom number of days, then choose Set Custom.")
            custom_days.setMinimumWidth(88)
            custom_row.addWidget(custom_days)
            custom_btn = QPushButton("Set Custom", popover)

            def _apply_custom_days() -> None:
                text = str(custom_days.text() or "").strip()
                if not text:
                    return
                try:
                    days = max(1, min(365, int(float(text))))
                except Exception:
                    custom_days.selectAll()
                    custom_days.setFocus()
                    return
                self._set_map_recency_from_label(f"{days}d")
                popover.close()

            custom_btn.clicked.connect(_apply_custom_days)
            custom_days.returnPressed.connect(_apply_custom_days)
            custom_row.addWidget(custom_btn)
            layout.addLayout(custom_row)
        self._map_since_popover = popover
        # Qt.Popup placement varies by window manager. Size the complete
        # chooser first, then constrain it to the trigger's active screen.
        popover.ensurePolished()
        popover.adjustSize()
        popup_size = popover.size().expandedTo(popover.sizeHint())
        screen = button.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            below = button.mapToGlobal(button.rect().bottomLeft())
            above = button.mapToGlobal(button.rect().topLeft()) - QPoint(0, popup_size.height())
            max_x = available.right() - popup_size.width() + 1
            max_y = available.bottom() - popup_size.height() + 1
            x = min(max(below.x(), available.left()), max(available.left(), max_x))
            preferred_y = (
                below.y()
                if below.y() + popup_size.height() <= available.bottom() + 1
                else above.y()
            )
            y = min(max(preferred_y, available.top()), max(available.top(), max_y))
            popover.move(QPoint(x, y))
        else:
            popover.move(button.mapToGlobal(button.rect().bottomLeft()))
        popover.show()

    def _set_map_recency_from_label(self, label: str) -> None:
        combo = getattr(self, "recency_combo", None)
        label = str(label or "").strip() or "Any"
        if combo is not None:
            try:
                idx = combo.findText(label)
            except Exception:
                idx = -1
            if idx >= 0:
                try:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(idx)
                    combo.blockSignals(False)
                except Exception:
                    pass
                self._on_recency_changed(idx)
                return
        mapping = dict(self._current_recency_options())
        self.recency_seconds = mapping.get(label)
        if self.recency_seconds is None and str(label or "").strip().lower().endswith("d"):
            try:
                days = int(str(label).strip()[:-1])
                self.recency_seconds = max(1, days) * 24 * 60 * 60
            except Exception:
                self.recency_seconds = None
        self._map_recency_label = label
        self._clamp_path_recency_if_needed()
        self._update_map_since_button_text(str(getattr(self, "_map_recency_label", "") or label))
        self._clear_report_query_caches()
        self._refresh_selected_paths_panel()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason="recency_filter")

    def shutdown(self) -> None:
        self._is_shutting_down = True
        references = getattr(self,"_license_reference_presenter",None)
        if references is not None:
            references.dispose()
        geography_lane = getattr(self,"_world_geography_lane",None)
        if geography_lane is not None:
            geography_lane.shutdown()
        for lane in (self._map_read_lane, self._map_inspector_lane):
            if lane is not None:
                lane.shutdown()
        mesh_timer = getattr(self, "_mesh_map_update_timer", None)
        if mesh_timer is not None:
            mesh_timer.stop()
        try:
            if self._js8_timer:
                self._js8_timer.stop()
        except Exception:
            pass
        try:
            if self._js8_rx_hub and self._js8_rx_registered:
                self._js8_rx_hub.unregister_listener(self._on_js8_rx_messages)
                self._js8_rx_registered = False
        except Exception:
            pass
        renderer = getattr(self, "_native_map_renderer", None)
        self._native_map_renderer = None
        if renderer is not None:
            try:
                renderer.shutdown()
            except Exception:
                pass
        try:
            executor = getattr(self, "_map_payload_executor", None)
            self._map_payload_executor = None
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


    @staticmethod
    def _parse_link_selection(data) -> tuple[str, str]:
        """
        Normalize link-mode selection from the combo. PySide may return list/tuple.
        """
        if isinstance(data, (list, tuple)) and len(data) >= 2:
            return str(data[0]), str(data[1])
        if isinstance(data, str):
            txt = data.strip()
            # Accept stringified tuple/list e.g. "('all', '')" or "['all', '']"
            if txt.startswith(("(", "[")) and "," in txt:
                inner = txt.strip("()[]")
                parts = [p.strip().strip("'\"") for p in inner.split(",")]
                if len(parts) >= 2:
                    return parts[0], parts[1]
        return "off", ""

    @staticmethod
    def _hex_to_rgba(value: str, alpha: float) -> str:
        try:
            raw = (value or "").strip().lstrip("#")
            if len(raw) != 6:
                raise ValueError("invalid hex color")
            r = int(raw[0:2], 16)
            g = int(raw[2:4], 16)
            b = int(raw[4:6], 16)
        except Exception:
            r, g, b = 0, 0, 0
        a = max(0.0, min(1.0, float(alpha)))
        return f"rgba({r}, {g}, {b}, {a:.2f})"

    def _theme_snapshot(self, force_reload: bool = False) -> Dict[str, str]:
        if force_reload and self.settings is not None:
            try:
                if hasattr(self.settings, "reload"):
                    self.settings.reload()
            except Exception:
                pass
        override = getattr(self, "_active_theme_override", None)
        if isinstance(override, Mapping) and override:
            return dict(override)
        return resolve_theme(self.settings)

    def apply_theme(self, theme: Optional[Mapping[str, str]] = None) -> None:
        if getattr(self, "_map_read_inflight", False):
            self._request_map_refresh(level="light", reason="theme_snapshot_changed")
        if isinstance(theme, Mapping) and theme:
            self._active_theme_override = dict(theme)
        else:
            self._active_theme_override = resolve_theme(self.settings)
        theme = self._theme_snapshot()
        reference_panel = getattr(self,"_license_reference_panel",None)
        if reference_panel is not None:
            reference_panel.apply_theme(theme)
        renderer = getattr(self, "_native_map_renderer", None)
        if renderer is not None:
            renderer.apply_theme(theme)
        if self._controls_button is not None:
            self._controls_button.setStyleSheet(button_style("muted", theme))
        show_fio_button = getattr(self, "_show_fio_button", None)
        if show_fio_button is not None:
            show_fio_button.setStyleSheet(button_style("primary", theme))
        if getattr(self, "_help_button", None) is not None:
            self._help_button.setStyleSheet(button_style("secondary", theme))
        if getattr(self, "_paths_help_button", None) is not None:
            self._paths_help_button.setStyleSheet(button_style("secondary", theme))
        if self._refresh_links_button is not None:
            self._refresh_links_button.setStyleSheet(button_style("primary", theme))
        if self._map_add_rf_pin_button is not None:
            self._map_add_rf_pin_button.setStyleSheet(button_style("secondary", theme))
        if self._map_manage_rf_pins_button is not None:
            self._map_manage_rf_pins_button.setStyleSheet(button_style("secondary", theme))
        self._update_now_reachable_button_visual(bool(self._now_reachable_enabled), theme=theme)
        self._update_map_mode_buttons(theme=theme)
        self._update_map_view_status_label(theme=theme)
        self._sync_map_control_button_widths()
        self._update_splitter_indicator_state(theme=theme)
        self._reflow_map_filter_bar()
        StationsMapTab._coordinate_map_panels(self)
        try:
            if self.prop_overlay_enabled:
                target_label, best_band, best_score = getattr(
                    self,
                    "_last_prop_badge_values",
                    ("National", "", 0.0),
                )
                self._update_prop_badge(str(target_label), str(best_band), float(best_score or 0.0), theme=theme)
            else:
                target_ctx = self._prop_target_context()
                target_label = str(target_ctx.get("label") or "National")
                self._update_prop_badge(target_label, "", 0.0, theme=theme)
        except Exception:
            # Keep theme updates resilient if propagation data is unavailable.
            self._update_prop_badge("National", "", 0.0, theme=theme)
        self._update_map_support_card()
        browser_style = (
            "QTextBrowser {"
            f" background-color: {theme['surface']};"
            f" color: {theme['text']};"
            f" border: 1px solid {theme['border']};"
            " border-radius: 4px;"
            "}"
        )
        for browser in (
            getattr(self, "_map_selected_body", None),
            getattr(self, "_map_selected_status_body", None),
            getattr(self, "_map_selected_paths_body", None),
            getattr(self, "_map_selected_messages_body", None),
        ):
            if browser is not None:
                try:
                    browser.setStyleSheet(browser_style)
                except Exception:
                    pass
        selected_payload = getattr(self, "_map_selected_payload", None)
        if isinstance(selected_payload, Mapping) and selected_payload:
            # The documents embed shared-theme colors.  Rebuild all four tabs
            # from the retained value snapshot so foreground and backing
            # QTextBrowser palette always change as one visual transaction.
            try:
                self._show_map_selected_detail(dict(selected_payload))
            except Exception:
                log.debug("StationsMap: failed refreshing selected detail theme", exc_info=True)

    def _open_context_help(self, context_key: str) -> None:
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_context_help"):
            try:
                host.open_context_help(context_key)
            except Exception:
                pass

    def _sync_map_control_button_widths(self) -> None:
        for button in (
            self._refresh_links_button,
            self._now_reachable_button,
            getattr(self, "_map_propagation_button", None),
            getattr(self, "_paths_help_button", None),
        ):
            if button is None:
                continue
            try:
                button.setMinimumWidth(max(button.minimumWidth(), button.sizeHint().width() + 6))
            except Exception:
                continue

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(6)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)
        self._show_fio_button = QPushButton("Show CommsPlan")
        self._show_fio_button.setToolTip("Bring the main CommsPlan window to the front without closing Map.")
        self._show_fio_button.setAccessibleName("Show main CommsPlan window")
        self._show_fio_button.setVisible(True)
        self._show_fio_button.clicked.connect(self._bring_application_window_forward)
        top_row.addWidget(self._show_fio_button, alignment=Qt.AlignLeft)
        self._controls_button = QPushButton("Advanced Map Tools")
        self._controls_button.setToolTip("Refine evidence, inspect paths, adjust basemap detail, and manage planning pins.")
        self._controls_button.setVisible(False)
        self._controls_button.clicked.connect(self._toggle_controls_drawer)
        top_row.addWidget(self._controls_button, alignment=Qt.AlignLeft)
        self._help_button = QPushButton("Help")
        self._help_button.setToolTip("Open focused help for Map controls and overlays.")
        self._help_button.clicked.connect(lambda: self._open_context_help("tab.map"))
        top_row.addWidget(self._help_button, alignment=Qt.AlignLeft)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        map_context_text = (
            "Map uses the current radio and Frequency Plan context when reviewing station and traffic overlays."
        )
        self.plan_context_label = PlanContextLabel(
            "map",
            service=self.plan_context_service,
            fallback_text=map_context_text,
            create_service=self.plan_context_service is not None,
        )
        self.plan_context_label.setToolTip(
            "Use this context to confirm which radio and assigned Frequency Plan Map overlays are being reviewed against."
        )
        self.plan_context_label.setVisible(False)
        layout.addWidget(self.plan_context_label)
        if self.plan_context_service is not None:
            self.plan_context_label.refresh_context(refresh=True)

        self._map_support_card = QFrame(self)
        support_layout = QHBoxLayout(self._map_support_card)
        self._map_support_layout = support_layout
        support_layout.setContentsMargins(10, 8, 10, 8)
        support_layout.setSpacing(8)
        self._map_support_label = QLabel("Map Status: Cold. Map has not been opened yet.")
        self._map_support_label.setWordWrap(True)
        support_layout.addWidget(self._map_support_label, 1)
        self._map_retry_btn = QPushButton("Retry")
        self._map_retry_btn.clicked.connect(self._retry_map_render)
        support_layout.addWidget(self._map_retry_btn)
        self._map_reload_btn = QPushButton("Reload Data")
        self._map_reload_btn.clicked.connect(self._reload_map_data)
        support_layout.addWidget(self._map_reload_btn)
        self._map_copy_summary_btn = QPushButton("Copy Diagnostics")
        self._map_copy_summary_btn.clicked.connect(self._copy_map_diagnostics)
        support_layout.addWidget(self._map_copy_summary_btn)
        self._map_support_help_btn = QPushButton("Help")
        self._map_support_help_btn.clicked.connect(lambda: self._open_context_help("tab.map"))
        support_layout.addWidget(self._map_support_help_btn)
        layout.addWidget(self._map_support_card)

        splitter = QSplitter(Qt.Horizontal, self)
        style_splitter_handles(splitter, self._theme_snapshot(), width=14)
        self._main_splitter = splitter
        layout.addWidget(splitter, stretch=1)

        controls_host = QWidget(self)
        self._controls_host = controls_host
        controls_host_layout = QHBoxLayout(controls_host)
        controls_host_layout.setContentsMargins(0, 0, 0, 0)
        controls_host_layout.setSpacing(6)
        controls_scroll = QScrollArea(controls_host)
        self._controls_scroll = controls_scroll
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        controls_scroll.setMinimumWidth(300)
        controls_scroll.setMaximumWidth(520)

        self._controls_panel = QWidget()
        controls_layout = QVBoxLayout(self._controls_panel)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(8)
        self._controls_top_spacer = QWidget(self._controls_panel)
        self._controls_top_spacer.setFixedHeight(0)
        controls_layout.addWidget(self._controls_top_spacer)
        # The rail is deliberately presentation-only: the existing widgets
        # below remain the single callback/data owners.
        rail = QFrame(controls_host)
        self._map_control_rail = rail
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        rail_layout.setSpacing(4)
        from freqinout.gui.active_page_stack import ActivePageStack
        category_stack = ActivePageStack(self._controls_panel)
        # The active category owns its natural width; a hidden Planning or
        # Paths page must not inflate the rail pane at compact widths.
        category_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        try:
            from PySide6.QtWidgets import QLayout
            category_stack.layout().setSizeConstraint(QLayout.SetNoConstraint)
        except Exception:
            pass
        category_stack.setVisible(False)
        self._map_control_category_stack = category_stack
        self._map_control_requested_category = ""
        self._map_control_rail_buttons = {}
        category_layouts = {}
        for index, category in enumerate(("Layers", "Filters", "Display", "Paths", "Planning")):
            button = QToolButton(rail)
            button.setText(category)
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonTextOnly)
            button.setAccessibleName(f"Map tools: {category}")
            button.setStyleSheet(button_style("muted", self._theme_snapshot()))
            rail_layout.addWidget(button)
            page = QWidget(category_stack)
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 4, 0, 0)
            page_layout.setSpacing(8)
            category_stack.addWidget(page)
            self._map_control_rail_buttons[category] = button
            category_layouts[category] = page_layout
            def select_category(checked=False, *, name=category, page_index=index):
                self._open_map_tool_category(name,toggle=True)
            button.clicked.connect(select_category)
        self._select_map_control_category = lambda name="Layers": next(
            (button.click() for category, button in self._map_control_rail_buttons.items() if category == name), None
        )
        rail_layout.addStretch(1)
        self._map_controls_close_button = QPushButton("Close tools", self._controls_panel)
        self._map_controls_close_button.clicked.connect(lambda: self._set_controls_drawer_open(False))
        controls_layout.addWidget(self._map_controls_close_button)
        controls_layout.addWidget(category_stack, 1)
        controls_host_layout.addWidget(rail, 0)
        controls_host_layout.addWidget(controls_scroll, 1)
        try:
            from PySide6.QtGui import QKeySequence, QShortcut
            escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
            escape_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            escape_shortcut.activated.connect(self._close_active_map_pane)
            self._map_controls_escape_shortcut = escape_shortcut
        except Exception:
            pass
        controls_scroll.setWidget(self._controls_panel)
        splitter.addWidget(controls_host)

        self.link_mode_combo = QComboBox()
        self.link_mode_combo.addItem("Off", ("off", ""))
        self.link_mode_combo.addItem("My Station", ("my_station", ""))
        self.link_mode_combo.addItem("All", ("all", ""))
        self.link_mode_combo.setCurrentText("Off")
        self._map_path_scope_combo = QComboBox()
        self._map_path_scope_combo.addItem("Off", ("off", ""))
        self._map_path_scope_combo.addItem("My Station", ("my_station", ""))
        self._map_path_scope_combo.addItem("All", ("all", ""))
        self._map_path_scope_combo.setCurrentIndex(0)
        self._map_path_scope_combo.setToolTip(
            "Choose My Station for local links, All for the full network topology within current filters, or Off."
        )

        self.group_filter_combo = QComboBox()
        self.group_filter_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.group_filter_combo.setMinimumContentsLength(14)
        self.group_filter_combo.view().setMinimumWidth(220)

        self.region_filter_combo = QComboBox()
        self.region_filter_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.region_filter_combo.setMinimumContentsLength(12)
        self.region_filter_combo.view().setMinimumWidth(200)

        self.band_combo = QComboBox()
        self.recency_combo = QComboBox()
        self.recency_combo.addItems([
            "Any",
            "15m",
            "30m",
            "1h",
            "3h",
            "6h",
            "12h",
            "24h",
            "3d",
            "7d",
            "14d",
            "30d",
            "60d",
            "90d",
        ])
        self.recency_combo.setCurrentText(MAP_DEFAULT_RECENCY_LABEL)
        self.recency_seconds = MAP_DEFAULT_RECENCY_SECONDS
        self._map_recency_label = MAP_DEFAULT_RECENCY_LABEL
        self.recency_combo.setVisible(False)
        self._map_since_button = QToolButton()
        self._map_since_button.setPopupMode(QToolButton.DelayedPopup)
        self._map_since_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._map_since_button.setMinimumWidth(self._map_since_button.fontMetrics().horizontalAdvance("90d") + 32)
        self._map_since_button.setToolTip("Choose how far back mapped stations, traffic, and paths should be considered.")
        self._build_map_since_menu()
        self._update_map_since_button_text(MAP_DEFAULT_RECENCY_LABEL)

        self.relay_target_combo = QComboBox()
        self.relay_target_combo.setEditable(True)
        self.relay_target_combo.setInsertPolicy(QComboBox.NoInsert)
        self.relay_target_combo.setDuplicatesEnabled(False)
        self.relay_target_combo.setMinimumWidth(180)
        try:
            self.relay_target_combo.view().setMinimumWidth(360)
        except Exception:
            pass
        try:
            relay_edit = self.relay_target_combo.lineEdit()
            if relay_edit is not None:
                relay_edit.setPlaceholderText("Search Callsign or Name...")
        except Exception:
            pass
        relay_completer = self.relay_target_combo.completer()
        if relay_completer:
            relay_completer.setFilterMode(Qt.MatchContains)
            relay_completer.setCaseSensitivity(Qt.CaseInsensitive)

        layers_layout = self._add_collapsible_group(category_layouts["Display"], "Display", expanded=True)
        self.show_calls_chk = QCheckBox("Callsigns")
        self.show_regions_chk = QCheckBox("Regions")
        self.show_states_chk = QCheckBox("States")
        self.show_cities_chk = QCheckBox("Cities")
        self.show_grid_labels_chk = QCheckBox("Grids")
        layer_grid = QGridLayout()
        layer_grid.setContentsMargins(0, 0, 0, 0)
        layer_grid.setHorizontalSpacing(8)
        layer_grid.setVerticalSpacing(6)
        layer_grid.addWidget(self.show_calls_chk, 0, 0)
        layer_grid.addWidget(self.show_regions_chk, 0, 1)
        layer_grid.addWidget(self.show_states_chk, 1, 0)
        layer_grid.addWidget(self.show_cities_chk, 1, 1)
        layer_grid.addWidget(self.show_grid_labels_chk, 2, 0)
        layers_layout.addLayout(layer_grid)

        self.city_pop_combo = QComboBox()
        self._city_pop_options = [
            ("1M+", 1_000_000),
            ("750k+", 750_000),
            ("500k+", 500_000),
            ("250k+", 250_000),
            ("100k+", 100_000),
            ("75k+", 75_000),
            ("50k+", 50_000),
            ("25k+", 25_000),
            ("10k+", 10_000),
            ("5k+", 5_000),
            ("<5k", 0),
        ]
        for label, val in self._city_pop_options:
            self.city_pop_combo.addItem(label, val)
        self.city_pop_combo.setCurrentIndex(4)
        pop_grid = QGridLayout()
        pop_grid.setContentsMargins(0, 0, 0, 0)
        pop_grid.setHorizontalSpacing(8)
        pop_grid.addWidget(QLabel("Population"), 0, 0)
        pop_grid.addWidget(self.city_pop_combo, 0, 1)
        layers_layout.addLayout(pop_grid)

        prop_layout = self._add_collapsible_group(category_layouts["Planning"], "Propagation Forecast", expanded=False)
        self.prop_overlay_chk = QCheckBox("Enable Propagation Overlay")
        prop_layout.addWidget(self.prop_overlay_chk)

        self.prop_mode_combo = QComboBox()
        self.prop_mode_combo.addItem("Actual", "actual")
        self.prop_mode_combo.addItem("Blended", "blended")
        self.prop_mode_combo.addItem("Modeled", "model")

        self.prop_window_combo = QComboBox()
        self.prop_window_combo.addItem("1h", 1)
        self.prop_window_combo.addItem("3h", 3)
        self.prop_window_combo.addItem("6h", 6)
        self.prop_window_combo.addItem("12h", 12)
        self.prop_window_combo.addItem("24h", 24)
        self.prop_window_combo.addItem("7 Days", 168)

        self.prop_target_type_combo = QComboBox()
        self.prop_target_type_combo.addItem("Region", "REGION")
        self.prop_target_type_combo.addItem("State", "STATE")
        self.prop_target_type_combo.addItem("Operator", "OPERATOR")

        self.prop_target_value_combo = QComboBox()
        self.prop_target_value_combo.setEditable(True)
        self.prop_target_value_combo.setInsertPolicy(QComboBox.NoInsert)
        self.prop_target_value_combo.setDuplicatesEnabled(False)
        try:
            self.prop_target_value_combo.view().setMinimumWidth(260)
        except Exception:
            pass

        prop_grid = QGridLayout()
        prop_grid.setContentsMargins(0, 0, 0, 0)
        prop_grid.setHorizontalSpacing(8)
        prop_grid.setVerticalSpacing(6)
        prop_grid.addWidget(QLabel("Mode"), 0, 0)
        prop_grid.addWidget(self.prop_mode_combo, 0, 1)
        prop_grid.addWidget(QLabel("Window"), 1, 0)
        prop_grid.addWidget(self.prop_window_combo, 1, 1)
        prop_grid.addWidget(QLabel("Target"), 2, 0)
        prop_grid.addWidget(self.prop_target_type_combo, 2, 1)
        prop_grid.addWidget(QLabel("Value"), 3, 0)
        prop_grid.addWidget(self.prop_target_value_combo, 3, 1)
        prop_layout.addLayout(prop_grid)

        self.prop_badge = QLabel("Best Band: --")
        self.prop_badge.setWordWrap(True)
        theme = self._theme_snapshot()
        self.prop_badge.setStyleSheet(
            f"font-weight: bold; color: {theme.get('info', theme.get('accent', '#1E88E5'))};"
        )
        prop_layout.addWidget(self.prop_badge)

        map_container = QWidget(self)
        self._map_container = map_container
        map_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        map_layout = QVBoxLayout(map_container)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.setSpacing(4)
        splitter.addWidget(map_container)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 1084])

        # Keep filters highly accessible at the top of the map content area.
        self.link_mode_combo.setMinimumWidth(120)
        self._map_path_scope_combo.setMinimumWidth(150)
        self._map_path_scope_combo.setMaximumWidth(210)
        self.group_filter_combo.setMinimumWidth(170)
        self.group_filter_combo.setMaximumWidth(230)
        self.region_filter_combo.setMinimumWidth(150)
        self.band_combo.setMinimumWidth(120)
        self.recency_combo.setMinimumWidth(90)
        self.relay_target_combo.setMinimumWidth(180)
        self._refresh_links_button = QPushButton("Refresh Links")
        self._refresh_links_button.clicked.connect(lambda: self._auto_ingest_and_refresh(initial=False, operator_refresh=True))
        self._map_all_stations_button = QPushButton("All Stations")
        self._map_all_stations_button.setToolTip("Return to the normal station map view.")
        self._map_all_stations_button.clicked.connect(self.focus_all_stations)
        self._map_hf_reports_button = QPushButton("RF/App")
        self._map_hf_reports_button.setToolTip("Traffic subtype: radio and connected-app message intelligence.")
        self._map_hf_reports_button.clicked.connect(self.focus_hf_reports)
        self._map_local_reports_button = QPushButton("Local")
        self._map_local_reports_button.setToolTip("Traffic subtype: local operator and NCS field reports only.")
        self._map_local_reports_button.clicked.connect(self.focus_local_reports)
        self._map_reports_button = QPushButton("Traffic")
        self._map_reports_button.setToolTip("Show recent report traffic. Use Type for All, RF/App, Local, or CommStat.")
        self._map_reports_button.clicked.connect(self.focus_reports)
        self._map_regional_intel_button = QPushButton("Regional Intel")
        self._map_regional_intel_button.setToolTip(
            "Show state and FEMA-region concern from recent and active report evidence."
        )
        self._map_regional_intel_button.clicked.connect(self.focus_regional_intelligence)
        self._map_paths_button = QPushButton("Paths")
        self._map_paths_button.setToolTip("Open the topology-first path view. Use Paths to choose My Station or All network links.")
        self._map_paths_button.clicked.connect(self.focus_paths)
        self._map_propagation_button = QPushButton("RF Planning")
        self._map_propagation_button.setToolTip(
            "Show stations, path links, planning pins, and propagation support for band and routing decisions."
        )
        self._map_propagation_button.clicked.connect(self.focus_propagation)
        self._map_rf_pins_button = QPushButton("Planning Pins")
        self._map_rf_pins_button.setToolTip("Show saved planning/reference pins only. These are not received traffic.")
        self._map_rf_pins_button.clicked.connect(self.focus_rf_pins)
        self._map_mode_combo = QComboBox()
        for label, data in (
            ("All Stations", "all"),
            ("Traffic", "reports"),
            ("Regional Intel", "regional"),
            ("Paths", "paths"),
            ("RF Planning", "propagation"),
            ("Planning Pins", "pins"),
            ("Mesh Nodes", "mesh_nodes"),
            ("Peer Sched Now", "peer"),
        ):
            self._map_mode_combo.addItem(label, data)
        self._map_mode_combo.setToolTip("Choose the main map view.")
        self._map_mode_combo.setMinimumWidth(180)
        self._map_mode_combo.setMaximumWidth(240)
        self._map_country_combo = QComboBox()
        self._map_country_combo.addItem("All countries", "")
        self._map_country_combo.setToolTip(
            "Scope plotted locations by country. This is geographic evidence, not callsign nationality or licensing origin.")
        self._map_country_combo.setMinimumWidth(150)
        self._map_country_combo.setMaximumWidth(230)
        self._map_focus_country_button = QPushButton("Focus map")
        self._map_focus_country_button.setToolTip("Fit the map to the selected country when geographic map data is available.")
        self._map_focus_country_button.setEnabled(False)
        self._map_focus_country_button.setToolTip("Focus the selected country's largest landmass. Filters retain the current camera until this action.")
        self._map_focus_country_button.setProperty("mapUiHook", "focus_country")
        self._map_traffic_subtype_combo = QComboBox()
        for label, data in (
            ("All", "all"),
            ("RF/App", "rf"),
            ("Local", "local"),
            ("CommStat", "commstat"),
            ("Mesh", "mesh"),
        ):
            self._map_traffic_subtype_combo.addItem(label, data)
        self._map_traffic_subtype_combo.setToolTip("Choose which traffic source family the Traffic view should show.")
        self._map_traffic_subtype_combo.setMinimumWidth(115)
        self._map_traffic_subtype_combo.setMaximumWidth(155)
        self._map_clear_filters_button = QPushButton("Clear Filters")
        self._map_clear_filters_button.setToolTip("Clear Group, Since, Topic, search, and advanced filters.")
        self._map_clear_filters_button.clicked.connect(self.clear_map_filters)
        self._map_clear_layers_button = QPushButton("Clear Layers")
        self._map_clear_layers_button.setToolTip(
            "Turn off temporary layers such as paths, RF planning, and pins. Filters stay unchanged."
        )
        self._map_clear_layers_button.clicked.connect(self.clear_map_layers)
        self._now_reachable_button = QPushButton("Peer Sched Now")
        self._now_reachable_button.setCheckable(True)
        self._update_now_reachable_button_visual(False)
        self._sitrep_status_button = self._map_reports_button
        self.map_stations_chk = QCheckBox("Stations")
        self.map_links_chk = QCheckBox("Links")
        self.map_weather_chk = QCheckBox("Weather")
        self.map_alerts_chk = QCheckBox("Alerts/Intel")
        self.map_infrastructure_chk = QCheckBox("Infrastructure/Utilities")
        self.map_stations_chk.setToolTip("Show or hide station markers on the map.")
        self.map_links_chk.setToolTip("Show or hide path lines on the map.")
        self.map_weather_chk.setToolTip("Show or hide mapped Weather / Storm reports.")
        self.map_alerts_chk.setToolTip("Show or hide mapped awareness and warning reports.")
        self.map_infrastructure_chk.setToolTip("Show or hide mapped infrastructure and utility status reports.")
        self._paths_help_button = QPushButton("Paths Help")
        self._paths_help_button.setToolTip("Open focused help for Paths, Paths To, and Peer Sched Now.")
        self._paths_help_button.clicked.connect(lambda: self._open_context_help("map.paths"))
        self._map_add_rf_pin_button = QPushButton("Add Planning Pin")
        self._map_add_rf_pin_button.setToolTip(
            "Add a planning/reference point to the map using a grid, topic, and short note."
        )
        self._map_add_rf_pin_button.clicked.connect(self._on_add_rf_pin_clicked)
        self._map_manage_rf_pins_button = QPushButton("Manage Pins")
        self._map_manage_rf_pins_button.setToolTip("Review or delete saved planning pins.")
        self._map_manage_rf_pins_button.clicked.connect(self._on_manage_rf_pins_clicked)
        self._map_topic_filter_combo = QComboBox()
        self._map_topic_filter_combo.addItem("All Topics")
        self._map_topic_filter_combo.addItems(list(RF_PIN_TOPICS))
        self._map_topic_filter_combo.setToolTip(
            "Filter mapped reports and planning pins by message-intelligence topic."
        )
        self._map_topic_filter_combo.setMinimumWidth(180)
        self._map_topic_filter_combo.setMaximumWidth(260)
        self._map_intel_sensitivity_combo = QComboBox()
        for label, data in (("Current", "current"), ("Active", "active"), ("Extended", "extended")):
            self._map_intel_sensitivity_combo.addItem(label, data)
        self._map_intel_sensitivity_combo.setCurrentIndex(1)
        self._map_intel_sensitivity_combo.setToolTip(
            "Choose how long regional intelligence keeps context before old reports fade."
        )
        self._map_intel_sensitivity_combo.setMinimumWidth(115)
        self._map_intel_sensitivity_combo.setMaximumWidth(150)
        self._map_search_edit = QLineEdit()
        self._map_search_edit.setPlaceholderText("Search map: callsign, group, topic, state/grid, keyword...")
        self._map_search_edit.setClearButtonEnabled(True)
        self._map_search_edit.setToolTip("Search station metadata and mapped traffic summaries without leaving the map.")
        self._map_scope_filter_combo = QComboBox()
        self._map_scope_filter_combo.addItem("Stations + Traffic", "all")
        self._map_scope_filter_combo.addItem("Stations Only", "stations")
        self._map_scope_filter_combo.addItem("Traffic Only", "reports")
        self._map_scope_filter_combo.setToolTip("Choose whether advanced filters show stations, traffic, or both.")
        self._map_state_filter_combo = QComboBox()
        self._map_state_filter_combo.setEditable(True)
        self._map_state_filter_combo.addItem("All", "")
        for prefix, regions in (("US", US_STATE_NAMES), ("CA", CANADA_PROVINCE_NAMES)):
            for code in sorted(regions):
                self._map_state_filter_combo.addItem(f"{prefix} · {code}", code)
        self._map_state_filter_combo.setToolTip("Region supplied with station/report records. Country uses plotted coordinates.")
        self._map_source_filter_combo = QComboBox()
        for label, data in (
            ("All Sources", ""),
            ("HF Apps", "hf_apps"),
            ("Spotter", "spotter"),
            ("CommStat", "commstat"),
            ("JS8Call", "js8call"),
            ("VarAC", "varac"),
            ("Mesh", "mesh"),
            ("FastLight", "fastlight"),
            ("Local Traffic", "local_report"),
            ("Condition Alerts", "condition_alert"),
            ("Planning Pins", "rf_pin"),
        ):
            self._map_source_filter_combo.addItem(label, data)
        self._map_source_filter_combo.setToolTip("Filter mapped traffic by the source that created the report.")
        self._map_status_filter_combo = QComboBox()
        for label, data in (
            ("All Statuses", ""),
            ("Needs Review", "needs_review"),
            ("Normal / Green", "normal"),
            ("Unconfirmed / Unknown", "unconfirmed"),
        ):
            self._map_status_filter_combo.addItem(label, data)
        self._map_status_filter_combo.setToolTip("Filter reports by operational status.")
        self._map_trust_filter_combo = QComboBox()
        for label, data in (
            ("All Auth/Trust", ""),
            ("Verified / Trusted", "verified"),
            ("Unverified", "unverified"),
            ("Confirmed", "confirmed"),
            ("Unconfirmed", "unconfirmed"),
        ):
            self._map_trust_filter_combo.addItem(label, data)
        self._map_trust_filter_combo.setToolTip("Filter reports by verification or confirmation state.")
        for button in (
            self._refresh_links_button,
            self._map_all_stations_button,
            self._map_hf_reports_button,
            self._map_local_reports_button,
            self._map_reports_button,
            self._map_regional_intel_button,
            self._map_paths_button,
            self._map_propagation_button,
            self._map_rf_pins_button,
            self._map_clear_filters_button,
            self._map_clear_layers_button,
            self._now_reachable_button,
            self._paths_help_button,
            self._map_add_rf_pin_button,
            self._map_manage_rf_pins_button,
        ):
            try:
                button.setMinimumWidth(button.sizeHint().width() + 6)
            except Exception:
                pass
        self._now_reachable_label = QLabel("")
        self._now_reachable_label.setWordWrap(True)
        self._now_reachable_label.setVisible(False)
        self._map_view_status_label = None
        filter_bar = QFrame(map_container)
        self._map_filter_bar = filter_bar
        def filter_field(
            label_text: str,
            widget: QWidget,
            minimum_width: int = 0,
            maximum_width: int = 0,
        ) -> QWidget:
            field = QWidget(filter_bar)
            field.setProperty("mapFilterRole", label_text)
            field_layout = QHBoxLayout(field)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(6)
            label = QLabel(label_text, field)
            label.setStyleSheet("font-weight: 700;")
            label.setMinimumWidth(label.sizeHint().width())
            field_layout.addWidget(label)
            field_layout.addWidget(widget, stretch=1)
            if minimum_width:
                field.setMinimumWidth(minimum_width)
            if maximum_width:
                field.setMaximumWidth(maximum_width)
            return field

        mode_actions_row = QWidget(filter_bar)
        mode_actions_layout = QGridLayout(mode_actions_row)
        mode_actions_layout.setContentsMargins(0, 0, 0, 0)
        mode_actions_layout.setSpacing(8)
        mode_action_buttons = (
            self._map_all_stations_button,
            self._map_reports_button,
            self._map_regional_intel_button,
            self._map_paths_button,
            self._map_propagation_button,
            self._map_rf_pins_button,
            self._now_reachable_button,
        )
        for idx, button in enumerate(mode_action_buttons):
            mode_actions_layout.addWidget(button, idx // 4, idx % 4)
        for col in range(4):
            mode_actions_layout.setColumnStretch(col, 1)
        views_layout = self._add_collapsible_group(category_layouts["Layers"], "View shortcuts", expanded=False)
        views_layout.addWidget(mode_actions_row)
        # Layer choices have one visible owner: the compact checkbox strip.
        views_layout.parentWidget().parentWidget().hide()
        path_tools_layout = self._add_collapsible_group(category_layouts["Paths"], "Path tools", expanded=True)
        path_tools_grid = QGridLayout()
        path_tools_grid.setContentsMargins(0, 0, 0, 0)
        path_tools_grid.setHorizontalSpacing(8)
        path_tools_grid.setVerticalSpacing(6)
        path_tools_grid.addWidget(QLabel("Region"), 0, 0)
        path_tools_grid.addWidget(self.region_filter_combo, 0, 1)
        path_tools_grid.addWidget(QLabel("Band"), 1, 0)
        path_tools_grid.addWidget(self.band_combo, 1, 1)
        self.link_mode_combo.hide()  # Scope is owned by the visible Paths selector.
        path_tools_grid.addWidget(QLabel("Paths to"), 3, 0)
        path_tools_grid.addWidget(self.relay_target_combo, 3, 1)
        path_tools_layout.addLayout(path_tools_grid)
        path_tools_layout.addWidget(self._refresh_links_button)
        path_tools_layout.addWidget(self._paths_help_button)

        intelligence_layout = self._add_collapsible_group(category_layouts["Layers"], "Layers", expanded=True)
        self._map_intelligence_layers_section = intelligence_layout.parentWidget().parentWidget()
        intelligence_layout.addWidget(self.map_stations_chk)
        intelligence_layout.addWidget(self.map_links_chk)
        intelligence_layout.addWidget(self.map_weather_chk)
        intelligence_layout.addWidget(self.map_alerts_chk)
        intelligence_layout.addWidget(self.map_infrastructure_chk)
        self._map_intelligence_layers_section.hide()

        advanced_layout = self._add_collapsible_group(category_layouts["Filters"], "Filters", expanded=True)
        advanced_grid = QGridLayout()
        advanced_grid.setContentsMargins(0, 0, 0, 0)
        advanced_grid.setHorizontalSpacing(8)
        advanced_grid.setVerticalSpacing(6)
        self._map_scope_filter_combo.hide()  # Stations/Traffic checkboxes own this choice.
        self._map_region_filter_label = QLabel("Region")
        advanced_grid.addWidget(self._map_region_filter_label, 1, 0)
        advanced_grid.addWidget(self._map_state_filter_combo, 1, 1)
        advanced_grid.addWidget(QLabel("Source"), 2, 0)
        advanced_grid.addWidget(self._map_source_filter_combo, 2, 1)
        advanced_grid.addWidget(QLabel("Status"), 3, 0)
        advanced_grid.addWidget(self._map_status_filter_combo, 3, 1)
        advanced_grid.addWidget(QLabel("Trust"), 4, 0)
        advanced_grid.addWidget(self._map_trust_filter_combo, 4, 1)
        advanced_layout.addLayout(advanced_grid)

        pins_tools_layout = self._add_collapsible_group(category_layouts["Planning"], "Planning pins", expanded=True)
        pins_tools_layout.addWidget(self._map_add_rf_pin_button)
        pins_tools_layout.addWidget(self._map_manage_rf_pins_button)
        display_actions = self._add_collapsible_group(category_layouts["Display"], "Map navigation", expanded=True)
        self._map_save_starting_view_button = QPushButton("Use current view as starting area")
        self._map_save_starting_view_button.setToolTip("Save this center and zoom for the next session. This does not change geographic filters.")
        self._map_save_starting_view_button.setProperty("mapUiHook", "save_starting_view")
        display_actions.addWidget(self._map_save_starting_view_button)
        self._map_category_layouts = category_layouts
        for page_layout in category_layouts.values():
            page_layout.addStretch(1)

        self._map_intel_sensitivity_field = filter_field("Sensitivity", self._map_intel_sensitivity_combo, 155, 190)
        self._map_traffic_subtype_field = filter_field("Type", self._map_traffic_subtype_combo, 130, 180)
        self._map_path_scope_field = filter_field("Paths", self._map_path_scope_combo, 150, 220)
        self._map_view_field = filter_field("View", self._map_mode_combo, 180, 250)
        self._map_country_field = filter_field("Country", self._map_country_combo, 165, 250)
        filter_grid = QGridLayout(filter_bar)
        self._map_filter_grid = filter_grid
        filter_grid.setContentsMargins(0, 0, 0, 0)
        filter_grid.setHorizontalSpacing(10)
        filter_grid.setVerticalSpacing(6)
        # Keep direct operational questions in reading order. The category
        # panes own less-common type, topic, and sensitivity choices.
        self._map_filter_fields = (
            self._map_view_field,
            filter_field("Group", self.group_filter_combo, 170, 230),
            filter_field("Activity window", self._map_since_button, 165, 210),
            self._map_country_field,
            self._map_path_scope_field,
        )
        self._map_search_field = filter_field("Search", self._map_search_edit)
        filters_layout = self._add_collapsible_group(category_layouts["Filters"], "Message and evidence", expanded=True)
        filters_layout.addWidget(filter_field("Topic", self._map_topic_filter_combo, 185, 260))
        filters_layout.addWidget(self._map_traffic_subtype_field)
        filters_layout.addWidget(self._map_intel_sensitivity_field)
        display_actions.addWidget(self._map_focus_country_button)
        self._map_restore_starting_view_button = QPushButton("Restore starting area")
        display_actions.addWidget(self._map_restore_starting_view_button)
        self._map_starting_view_status = QLabel("Save a starting area without excluding other countries.")
        self._map_starting_view_status.setWordWrap(True)
        display_actions.addWidget(self._map_starting_view_status)
        self._map_country_combo.currentIndexChanged.connect(self._on_map_country_changed)
        self._map_focus_country_button.clicked.connect(self._focus_map_country)
        self._map_save_starting_view_button.clicked.connect(self._save_map_starting_view)
        self._map_restore_starting_view_button.clicked.connect(self._restore_map_starting_view)
        self._reflow_map_filter_bar()
        self._map_layer_controls = MapLayerControls(map_container)
        self._map_layer_controls.set_visible_layers(("stations", "paths", "traffic"))
        self._map_layer_controls.layer_toggled.connect(self._on_map_layer_toggled)
        category_layouts["Layers"].insertWidget(0, self._map_layer_controls)
        self._mesh_recent_chk = QCheckBox("Recent (24h)", map_container)
        self._mesh_recent_chk.setToolTip("Unchecked: all known contacts. Checked: contacts heard in the last 24 hours. Advertised positions may be old.")
        self._mesh_recent_chk.setVisible(False)
        self._mesh_recent_chk.toggled.connect(self._on_mesh_recent_changed)
        map_layout.addWidget(self._mesh_recent_chk)
        # Common selections span the workspace and keep their width when a
        # tool pane opens; disclosure must not squeeze the operational toolbar.
        layout.insertWidget(layout.indexOf(splitter), filter_bar)

        self._map_canvas_splitter = QSplitter(Qt.Horizontal, map_container)
        style_splitter_handles(self._map_canvas_splitter, self._theme_snapshot(), width=12)
        map_layout.addWidget(self._map_canvas_splitter, stretch=1)

        # The production Map renderer is an ordinary Qt Quick widget backed by
        # Qt Location. It lives in this final splitter parent for its complete
        # lifetime and never starts Chromium or attaches a browser surface.
        self._map_stack = None
        self._map_loading_label = None
        self._native_map_renderer = None
        self._ensure_native_map_renderer()

        self._build_map_selected_detail_panel(self._map_canvas_splitter)
        self._map_canvas_splitter.setStretchFactor(0, 1)
        self._map_canvas_splitter.setStretchFactor(1, 0)
        self._map_canvas_splitter.setSizes([980, 0])

        self.show_calls_chk.stateChanged.connect(self._on_show_calls_changed)
        self.show_regions_chk.stateChanged.connect(self._on_show_regions_changed)
        self.show_states_chk.stateChanged.connect(self._on_show_states_changed)
        self.show_cities_chk.stateChanged.connect(self._on_show_cities_changed)
        self.show_grid_labels_chk.stateChanged.connect(self._on_show_grid_labels_changed)
        self.map_stations_chk.stateChanged.connect(self._on_map_stations_changed)
        self.map_links_chk.stateChanged.connect(self._on_map_links_changed)
        self.map_weather_chk.stateChanged.connect(self._on_map_weather_changed)
        self.map_alerts_chk.stateChanged.connect(self._on_map_alerts_changed)
        self.map_infrastructure_chk.stateChanged.connect(self._on_map_infrastructure_changed)
        self.city_pop_combo.currentIndexChanged.connect(self._on_city_pop_changed)
        self.link_mode_combo.currentIndexChanged.connect(self._on_link_mode_changed)
        self._map_path_scope_combo.currentIndexChanged.connect(self._on_map_path_scope_changed)
        self._map_topic_filter_combo.currentIndexChanged.connect(self._on_map_topic_filter_changed)
        self._map_intel_sensitivity_combo.currentIndexChanged.connect(self._on_map_intel_sensitivity_changed)
        self._map_traffic_subtype_combo.currentIndexChanged.connect(self._on_map_traffic_subtype_changed)
        self._map_mode_combo.currentIndexChanged.connect(self._on_map_mode_combo_changed)
        self._map_search_edit.textChanged.connect(self._on_map_search_text_changed)
        for combo in (
            self._map_scope_filter_combo,
            self._map_state_filter_combo,
            self._map_source_filter_combo,
            self._map_status_filter_combo,
            self._map_trust_filter_combo,
        ):
            combo.currentIndexChanged.connect(self._on_advanced_map_filter_changed)
        try:
            state_edit = self._map_state_filter_combo.lineEdit()
            if state_edit is not None:
                state_edit.editingFinished.connect(self._on_advanced_map_filter_changed)
        except Exception:
            pass
        self.group_filter_combo.currentIndexChanged.connect(self._on_group_filter_changed)
        self.region_filter_combo.currentIndexChanged.connect(self._on_region_filter_changed)
        self.band_combo.currentIndexChanged.connect(self._on_band_changed)
        self.recency_combo.currentIndexChanged.connect(self._on_recency_changed)
        self.relay_target_combo.currentIndexChanged.connect(
            lambda _idx: self._on_relay_target_changed(self.relay_target_combo.currentText())
        )
        try:
            line_edit = self.relay_target_combo.lineEdit()
            if line_edit is not None:
                line_edit.editingFinished.connect(
                    lambda: self._on_relay_target_changed(self.relay_target_combo.currentText())
                )
        except Exception:
            pass
        if self._now_reachable_button is not None:
            self._now_reachable_button.toggled.connect(self._on_now_reachable_toggled)
        self.prop_overlay_chk.stateChanged.connect(self._on_prop_overlay_changed)
        self.prop_mode_combo.currentIndexChanged.connect(self._on_prop_mode_changed)
        self.prop_window_combo.currentIndexChanged.connect(self._on_prop_window_changed)
        self.prop_target_type_combo.currentIndexChanged.connect(self._on_prop_target_type_changed)
        self.prop_target_value_combo.currentTextChanged.connect(self._on_prop_target_value_changed)

        from freqinout.gui.map_license_reference_panel import MapLicenseReferencePanel
        from freqinout.gui.map_license_reference_presenter import MapLicenseReferencePresenter
        self._license_reference_panel = MapLicenseReferencePanel(self)
        category_layouts["Layers"].insertWidget(1, self._license_reference_panel)
        self._license_reference_presenter = MapLicenseReferencePresenter(self, self._license_reference_panel)
        self._sync_city_pop_enabled()
        try:
            self._on_link_mode_changed(self.link_mode_combo.currentIndex())
        except Exception:
            pass
        # The persistent category rail owns control disclosure.
        self._sync_controls_top_alignment()
        QTimer.singleShot(0, self._update_drawer_mode)

    def _build_map_selected_detail_panel(self, parent: QWidget) -> None:
        panel = QFrame(parent)
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setMinimumWidth(260)
        panel.setMaximumWidth(460)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(7)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        title = QLabel("Selection")
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: 800;")
        header.addWidget(title, 1)
        close_btn = QPushButton("Close")
        close_btn.setToolTip("Hide the selected map detail panel.")
        close_btn.clicked.connect(self._clear_map_selected_detail)
        header.addWidget(close_btn, 0)
        layout.addLayout(header)

        subtitle = QLabel("")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        tabs = QTabWidget(panel)
        tabs.setDocumentMode(True)
        tabs.setMinimumHeight(tabs.fontMetrics().lineSpacing() * 5)
        tabs.hide()
        overview_body = QTextBrowser(tabs)
        status_body = QTextBrowser(tabs)
        paths_body = QTextBrowser(tabs)
        messages_body = QTextBrowser(tabs)
        for browser in (overview_body, status_body, paths_body, messages_body):
            browser.setOpenExternalLinks(False)
            browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tabs.addTab(overview_body, "Overview")
        tabs.addTab(status_body, "Status")
        tabs.addTab(paths_body, "Paths")
        tabs.addTab(messages_body, "Inbox")

        action_grid = QGridLayout()
        action_grid.setContentsMargins(0, 0, 0, 0)
        action_grid.setHorizontalSpacing(6)
        action_grid.setVerticalSpacing(6)
        center_btn = QPushButton("Center")
        paths_btn = QPushButton("Show Paths To")
        group_btn = QPushButton("Group")
        topic_btn = QPushButton("Topic")
        messages_btn = QPushButton("Inbox")
        spotter_btn = QPushButton("Compose Message")
        sop_btn = QPushButton("SOP")
        center_btn.setToolTip("Center the map on this selected station, report, or area.")
        paths_btn.setToolTip("Show observed direct or shared-contact paths from my station to the selected station.")
        group_btn.setToolTip("Filter the map to this station or report group.")
        topic_btn.setToolTip("Filter the map to this report topic.")
        messages_btn.setToolTip("Open Messages Inbox filtered to the evidence for this map selection.")
        spotter_btn.setToolTip("Open Compose and prefill the selected station callsign.")
        sop_btn.setToolTip("Open SOP guidance related to this selection.")
        center_btn.clicked.connect(self._center_map_selected_detail)
        paths_btn.clicked.connect(self._show_paths_for_selected_station)
        group_btn.clicked.connect(lambda: self._handle_map_detail_action({
            "action": "filter_group",
            "group": str(self._map_selected_payload.get("group") or ""),
        }))
        topic_btn.clicked.connect(lambda: self._handle_map_detail_action({
            "action": "filter_topic",
            "topic": (
                self._map_preferred_topic_for_values(self._map_selected_payload.get("topics"))
                or str(self._map_selected_payload.get("topic") or "")
            ),
        }))
        messages_btn.clicked.connect(self._open_map_selected_messages)
        spotter_btn.clicked.connect(self._compose_message_for_selected_station)
        sop_btn.clicked.connect(self._open_map_selected_sop)
        from PySide6.QtWidgets import QMenu
        more_btn = QPushButton("More…", panel)
        more_menu = QMenu(more_btn)
        self._map_selected_more_actions = []
        action_owner = QWidget(panel)
        action_owner.hide()
        for btn in (group_btn, topic_btn, messages_btn, spotter_btn, sop_btn):
            btn.setParent(action_owner)
            btn.hide()
            action = more_menu.addAction(btn.text(), btn.click)
            self._map_selected_more_actions.append((action, btn))
        def sync_more_actions():
            for action, btn in self._map_selected_more_actions:
                action.setText(btn.text())
                action.setEnabled(btn.isEnabled())
                action.setVisible(not btn.isHidden())
        more_menu.aboutToShow.connect(sync_more_actions)
        more_btn.setMenu(more_menu)
        details_btn = QPushButton("Details", panel)
        details_btn.setCheckable(True)
        def disclose_details(visible):
            tabs.setVisible(visible)
            self._map_canvas_layout_signature = None
            self._sync_map_canvas_splitter()
        details_btn.toggled.connect(disclose_details)
        for idx, btn in enumerate((center_btn, paths_btn, more_btn, details_btn)):
            action_grid.addWidget(btn, idx // 2, idx % 2)
        layout.addLayout(action_grid)
        layout.addWidget(tabs, 1)
        layout.addStretch(0)
        self._map_selected_details_button = details_btn

        self._map_selected_panel = panel
        self._map_selected_title = title
        self._map_selected_subtitle = subtitle
        self._map_selected_tabs = tabs
        self._map_selected_body = overview_body
        self._map_selected_status_body = status_body
        self._map_selected_paths_body = paths_body
        self._map_selected_messages_body = messages_body
        self._map_selected_center_btn = center_btn
        self._map_selected_paths_btn = paths_btn
        self._map_selected_group_btn = group_btn
        self._map_selected_topic_btn = topic_btn
        self._map_selected_messages_btn = messages_btn
        self._map_selected_spotter_btn = spotter_btn
        self._map_selected_sop_btn = sop_btn
        parent.addWidget(panel)
        panel.setVisible(False)

    def _clear_map_selected_detail(self) -> None:
        self._map_selected_payload = {}
        self._map_inspector_generation = getattr(self, "_map_inspector_generation", 0) + 1
        panel = getattr(self, "_map_selected_panel", None)
        if panel is not None:
            panel.setVisible(False)
        self._sync_map_canvas_splitter()

    def _map_selected_panel_target_width(self, total_width: int) -> int:
        total = max(1, int(total_width or 0))
        if total < 760:
            return max(0, min(300, total // 3))
        if total < 1050:
            return min(340, max(300, total // 3))
        return min(430, max(360, total // 4))

    def _sync_map_canvas_splitter(self) -> None:
        StationsMapTab._coordinate_map_panels(self)

    def _show_map_selected_detail(self, payload: Dict[str, object]) -> None:
        panel = getattr(self, "_map_selected_panel", None)
        if panel is None:
            return
        same_selection = getattr(self, "_map_selected_payload", {}) == dict(payload or {})
        self._map_selected_payload = dict(payload or {})
        self._map_inspector_generation = getattr(self, "_map_inspector_generation", 0) + 1
        rows = self._map_payload_rows(payload)
        title = self._map_detail_clean_text(payload.get("title") or "Selection")
        route = self._map_detail_clean_text(payload.get("route") or "")
        summary = self._map_detail_clean_text(payload.get("summary") or "", multiline=True)
        display_title = self._map_selected_display_title(payload, title, rows)
        body_html = "<p>Updating selection details…</p>"
        self._queue_map_inspector_snapshot()

        if self._map_selected_title is not None:
            self._map_selected_title.setText(display_title)
        if self._map_selected_subtitle is not None:
            from freqinout.gui.map_marker_presentation import plain_tooltip_text
            brief = " · ".join(str(payload.get(key) or rows.get(key) or "").strip()
                               for key in ("name", "state", "grid")
                               if str(payload.get(key) or rows.get(key) or "").strip())
            if not brief:
                lines = plain_tooltip_text(payload.get("title") or "").splitlines()
                brief = " · ".join(lines[1:4])
            self._map_selected_subtitle.setText(route or brief)
            self._map_selected_subtitle.setVisible(bool(route or brief))
        if not same_selection:
            self._map_inspector_has_content = False
            for body in (self._map_selected_body, self._map_selected_status_body,
                         self._map_selected_paths_body, self._map_selected_messages_body):
                if body is not None:
                    body.setHtml(body_html)

        lat, lon = self._map_selected_latlon(payload)
        group = str(payload.get("group") or "").strip()
        topic = self._map_preferred_topic_for_values(payload.get("topics")) or str(payload.get("topic") or "").strip()
        source_family = self._map_payload_source_family(payload)
        kind = str(payload.get("type") or "").strip().lower()
        message_context = self._map_selected_message_context(payload)
        callsign = self._map_selected_station_callsign()
        action_callsign = self._map_selected_action_callsign(payload)
        is_mesh_node = self._map_payload_is_mesh_node(payload)
        is_mesh_cluster = is_mesh_node and self._map_payload_is_mesh_cluster(payload, rows)
        is_mesh_infra = is_mesh_node and self._map_payload_is_mesh_infrastructure(payload, rows)
        can_show_paths = bool(action_callsign and not self._map_selected_station_is_self(action_callsign))
        can_show_paths = is_mesh_node or can_show_paths
        if self._map_selected_tabs is not None:
            self._map_selected_tabs.setTabEnabled(1, True)
            self._map_selected_tabs.setTabEnabled(2, can_show_paths)
            self._map_selected_tabs.setTabEnabled(3, bool(str(message_context.get("target") or "").strip()))
        if self._map_selected_center_btn is not None:
            self._map_selected_center_btn.setEnabled(bool(lat != 0.0 or lon != 0.0))
        if self._map_selected_paths_btn is not None:
            self._map_selected_paths_btn.setText("Mesh Paths" if is_mesh_node else "Show Paths To")
            self._map_selected_paths_btn.setVisible(bool(action_callsign or is_mesh_node))
            self._map_selected_paths_btn.setEnabled(can_show_paths)
            self._map_selected_paths_btn.setToolTip(
                f"Show observed mesh path details for {action_callsign}."
                if is_mesh_node and can_show_paths
                else (
                    "Mesh path detail appears when route data is available."
                    if is_mesh_node
                    else (
                        f"Show observed direct or relay paths from my station to {action_callsign}."
                        if can_show_paths
                        else "Paths are not shown for your own station."
                    )
                )
            )
            self._update_selected_paths_button_visual()
        if self._map_selected_group_btn is not None:
            self._map_selected_group_btn.setVisible(bool(group))
        if self._map_selected_topic_btn is not None:
            self._map_selected_topic_btn.setVisible(bool(topic))
        if self._map_selected_messages_btn is not None:
            target = str(message_context.get("target") or "").strip()
            self._map_selected_messages_btn.setVisible(bool(target))
            self._map_selected_messages_btn.setText("Local Traffic" if target == "local_reports" else "Inbox")
        if self._map_selected_spotter_btn is not None:
            can_message = bool(action_callsign) and not self._map_selected_station_is_self(action_callsign)
            if is_mesh_node:
                can_message = can_message and not is_mesh_cluster and not is_mesh_infra
            self._map_selected_spotter_btn.setText("Message Node" if is_mesh_node else "Compose Message")
            self._map_selected_spotter_btn.setVisible(bool(can_message) if is_mesh_node else bool(action_callsign))
            self._map_selected_spotter_btn.setEnabled(can_message)
            self._map_selected_spotter_btn.setToolTip(
                f"Open mesh compose for {action_callsign}."
                if is_mesh_node and can_message
                else (
                    "Mesh direct compose is available when this node has an address CommsPlan can target."
                    if is_mesh_node
                    else (
                        f"Open Compose and prefill {action_callsign}."
                        if can_message
                        else "Compose is disabled for your own station."
                    )
                )
            )
        if self._map_selected_sop_btn is not None:
            sop_context = self._map_selected_sop_context(payload)
            self._map_selected_sop_btn.setVisible(
                bool(
                    str(sop_context.get("group") or "").strip()
                    or str(sop_context.get("topic") or "").strip()
                    or source_family == "condition_alert"
                    or kind == "report"
                )
            )

        panel.setVisible(True)
        self._sync_map_canvas_splitter()

    @staticmethod
    def _map_payload_rows(payload: Dict[str, object]) -> Dict[str, str]:
        rows = payload.get("rows")
        out: Dict[str, str] = {}
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            value = StationsMapTab._map_detail_clean_text(row.get("value"), multiline=True)
            if label and value:
                out[label.lower()] = value
        return out

    @classmethod
    def _map_payload_is_mesh_node(cls, payload: Dict[str, object]) -> bool:
        source_family = cls._map_payload_source_family(payload)
        source_ref = str(payload.get("source_ref") or "").strip()
        source_refs = payload.get("source_refs")
        mesh_family = source_family in {"mesh", "meshcore", "meshtastic", "mesh_client", "local_mesh"}
        if not mesh_family:
            return False
        if payload.get("is_mesh_node"):
            return True
        if source_ref.startswith("mesh-node:"):
            return True
        if isinstance(source_refs, (list, tuple, set)):
            if any(str(ref or "").startswith("mesh-node:") for ref in source_refs):
                return True
        text = " ".join(
            str(value or "")
            for value in (
                payload.get("title"),
                payload.get("summary"),
                payload.get("details"),
                payload.get("tooltip"),
            )
        ).lower()
        return "mesh node" in text

    @classmethod
    def _map_mesh_node_labels(
        cls,
        payload: Dict[str, object],
        rows: Optional[Dict[str, str]] = None,
        *,
        limit: int = 14,
    ) -> List[str]:
        rows = rows or cls._map_payload_rows(payload)
        candidates: List[object] = [
            payload.get("label") if payload.get("is_mesh_node") else None,
            payload.get("callsigns"),
            payload.get("call_label"),
            payload.get("callsign"),
            payload.get("reported_by"),
            payload.get("summary"),
            payload.get("details"),
            payload.get("tooltip"),
            payload.get("title"),
            rows.get("from"),
            rows.get("reporter"),
            rows.get("summary"),
            rows.get("reports"),
            rows.get("source"),
        ]
        labels: List[str] = []
        seen: Set[str] = set()

        def add_label(value: object) -> None:
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    add_label(item)
                return
            text = cls._map_detail_clean_text(value, multiline=True)
            if not text:
                return
            for part in re.split(r"[\n;]+", text):
                line = part.strip()
                if not line:
                    continue
                match = re.search(r"mesh\s+node:\s*(.+)$", line, re.I)
                label = match.group(1).strip() if match else line
                label = re.sub(r"\s*\|\s*now\b.*$", "", label, flags=re.I).strip()
                label = re.sub(r"^\s*(from|source|reporter):\s*", "", label, flags=re.I).strip()
                if not label:
                    continue
                lower = label.lower()
                if lower.startswith(("meshcore ", "topics:", "area:", "summary:", "mapped location")):
                    continue
                if "," in label and " " in label:
                    for token in label.split(","):
                        add_label(token)
                    continue
                key = re.sub(r"[^a-z0-9]+", "", lower)
                if not key or key in seen:
                    continue
                seen.add(key)
                labels.append(label)

        for candidate in candidates:
            add_label(candidate)
            if len(labels) >= limit:
                break
        return labels[:1] if payload.get("is_mesh_node") and payload.get("label") else labels[:limit]

    @classmethod
    def _map_payload_is_mesh_cluster(cls, payload: Dict[str, object], rows: Optional[Dict[str, str]] = None) -> bool:
        rows = rows or cls._map_payload_rows(payload)
        count_values = (
            payload.get("count"),
            payload.get("cluster_count"),
            payload.get("report_count"),
            rows.get("nodes"),
            rows.get("source"),
        )
        for raw in count_values:
            text = str(raw or "").strip()
            match = re.search(r"\b(\d+)\b", text)
            if match:
                try:
                    if int(match.group(1)) > 1:
                        return True
                except Exception:
                    pass
        labels = cls._map_mesh_node_labels(payload, rows, limit=3)
        return len(labels) > 1

    @classmethod
    def _map_payload_is_mesh_infrastructure(cls, payload: Dict[str, object], rows: Optional[Dict[str, str]] = None) -> bool:
        rows = rows or cls._map_payload_rows(payload)
        text = " ".join(
            str(value or "")
            for value in (
                payload.get("role"),
                payload.get("node_role"),
                payload.get("title"),
                payload.get("summary"),
                payload.get("details"),
                rows.get("role"),
                rows.get("summary"),
            )
        ).lower()
        return any(word in text for word in ("router", "repeater", "relay", "infrastructure", "telemetry"))

    def _effective_city_label_config(
        self,
        *,
        observation_focus_enabled: Optional[bool] = None,
        observation_focus_mode: Optional[str] = None,
    ) -> tuple[bool, int]:
        focus_enabled = (
            self._effective_map_observation_focus_enabled()
            if observation_focus_enabled is None
            else bool(observation_focus_enabled)
        )
        focus_mode = (
            self._effective_map_observation_focus_mode()
            if observation_focus_mode is None
            else str(observation_focus_mode or "").strip().lower()
        )
        try:
            city_min = int(getattr(self, "city_pop_min", 100000) or 100000)
        except Exception:
            city_min = 100000
        if focus_enabled and focus_mode == "mesh_nodes":
            return True, min(city_min, 1000)
        return bool(getattr(self, "show_city_labels", False)), city_min

    @staticmethod
    def _map_commstat_scope_note(scope: object, state_confidence: object = "", geo_confidence: object = "") -> str:
        scope_text = str(scope or "").strip()
        normalized = re.sub(r"[^a-z0-9]+", " ", scope_text.lower()).strip()
        notes: List[str] = []
        if normalized in {"other location", "other", "county", "my county", "community", "my community", "region", "my region", "event"}:
            notes.append("CommStat report location may differ from the reporting station.")
        state_conf = str(state_confidence or "").strip().lower()
        geo_conf = str(geo_confidence or "").strip().lower()
        if state_conf == "remarks" or geo_conf.startswith("grid") and "remarks" in geo_conf:
            notes.append("State was inferred from the report text.")
        return " ".join(dict.fromkeys(notes))

    def _map_payload_latlon(self, payload: Dict[str, object]) -> tuple[float, float]:
        lat = self._safe_float(
            self._map_detail_first_value(
                payload.get("lat"),
                payload.get("latitude"),
                payload.get("station_lat"),
                payload.get("report_lat"),
            ),
            0.0,
        )
        lon = self._safe_float(
            self._map_detail_first_value(
                payload.get("lon"),
                payload.get("lng"),
                payload.get("longitude"),
                payload.get("station_lon"),
                payload.get("station_lng"),
                payload.get("report_lon"),
                payload.get("report_lng"),
            ),
            0.0,
        )
        if lat or lon:
            return lat, lon
        for nested_key in ("payload", "event", "station", "report"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                lat, lon = self._map_payload_latlon(nested)
                if lat or lon:
                    return lat, lon
        rows = self._map_payload_rows(payload)
        raw_candidates = (
            payload.get("grid"),
            payload.get("locator"),
            payload.get("maidenhead"),
            rows.get("grid"),
            rows.get("locator"),
            rows.get("maidenhead"),
            rows.get("area"),
            rows.get("location"),
        )
        for raw in raw_candidates:
            text = str(raw or "").strip().upper()
            if not text:
                continue
            for token in re.split(r"[^A-Z0-9]+", text):
                ll = None
                if re.match(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$", token):
                    ll = maidenhead_to_latlon(token)
                if ll:
                    return float(ll[0]), float(ll[1])
        return 0.0, 0.0

    def _map_selected_latlon(self, payload: Dict[str, object]) -> tuple[float, float]:
        lat, lon = self._map_payload_latlon(payload)
        if lat or lon:
            return lat, lon
        callsign = self._map_selected_station_callsign()
        if not callsign:
            return 0.0, 0.0
        for station in list(getattr(self, "stations", []) or []):
            station_call = str(getattr(station, "callsign", "") or "").strip().upper()
            if station_call != callsign:
                continue
            lat = self._safe_float(getattr(station, "lat", 0.0), 0.0)
            lon = self._safe_float(getattr(station, "lon", 0.0), 0.0)
            if lat or lon:
                return lat, lon
            grid = str(getattr(station, "grid", "") or "").strip()
            if grid:
                ll = maidenhead_to_latlon(grid)
                if ll:
                    return float(ll[0]), float(ll[1])
        return 0.0, 0.0

    @staticmethod
    def _map_detail_chip_html(values: object, *, limit: int = 5) -> str:
        if isinstance(values, str):
            raw_values = [part.strip() for part in values.replace(";", ",").split(",")]
        elif isinstance(values, (list, tuple, set)):
            raw_values = [str(part).strip() for part in values]
        else:
            raw_values = []
        labels = [label.lstrip("@") for label in raw_values if label]
        if not labels:
            return ""
        shown = labels[:limit]
        chips = "".join(f"<span class='fio-chip'>{html.escape(label)}</span>" for label in shown)
        if len(labels) > limit:
            chips += f"<span class='fio-chip muted'>+{len(labels) - limit}</span>"
        return f"<div class='fio-chip-row'>{chips}</div>"

    @staticmethod
    def _map_detail_values(values: object, *, limit: int = 6) -> List[str]:
        raw_values: List[str] = []
        if isinstance(values, str):
            raw_values = [part.strip() for part in values.replace(";", ",").split(",")]
        elif isinstance(values, (list, tuple, set)):
            for value in values:
                if isinstance(value, str):
                    raw_values.extend(part.strip() for part in value.replace(";", ",").split(","))
                else:
                    raw_values.append(str(value or "").strip())
        labels: List[str] = []
        seen: Set[str] = set()
        for raw in raw_values:
            label = str(raw or "").strip().lstrip("@")
            if not label:
                continue
            key = label.upper()
            if key in seen:
                continue
            seen.add(key)
            labels.append(label)
        if len(labels) > limit:
            return labels[:limit] + [f"+{len(labels) - limit} more"]
        return labels

    @classmethod
    def _map_detail_list_row_html(cls, label: str, values: object, *, limit: int = 6) -> str:
        labels = cls._map_detail_values(values, limit=limit)
        if not labels:
            return ""
        return cls._map_detail_row_html(label, ", ".join(labels))

    @staticmethod
    def _regional_source_category(source_family: object, evidence_type: object = "") -> str:
        source = str(source_family or "").strip().lower()
        kind = str(evidence_type or "").strip().lower()
        if source == "commstat":
            return "CommStat"
        if source == "local_report":
            return "Local"
        if kind in {"signal", "path"}:
            return "RF Signal"
        if source in {"flmsg", "flamp", "spotter", "js8spotter", "js8call", "js8"}:
            return "RF Reports"
        return "Other"

    @classmethod
    def _regional_source_mix(cls, evidence: object) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        if not isinstance(evidence, (list, tuple)):
            return counts
        for item in evidence:
            if not isinstance(item, dict):
                continue
            category = cls._regional_source_category(item.get("source_family"), item.get("evidence_type"))
            counts[category] = counts.get(category, 0) + 1
        return counts

    @staticmethod
    def _regional_source_mix_text(source_mix: object) -> str:
        if not isinstance(source_mix, dict):
            return ""
        order = ("RF Reports", "RF Signal", "CommStat", "Local", "Other")
        parts = [
            f"{label} {int(source_mix.get(label, 0) or 0)}"
            for label in order
            if int(source_mix.get(label, 0) or 0) > 0
        ]
        return ", ".join(parts)

    @staticmethod
    def _regional_topic_rows_text(topics: object) -> str:
        if not isinstance(topics, (list, tuple)):
            return ""
        parts: List[str] = []
        for item in topics[:5]:
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic") or "").strip()
            if not topic:
                continue
            level = str(item.get("level") or "").strip()
            count = int(item.get("evidence_count", 0) or 0)
            label = f"{topic} ({level})" if level else topic
            if count:
                label += f" x{count}"
            parts.append(label)
        return ", ".join(parts)

    @classmethod
    def _regional_evidence_lines(cls, evidence: object, *, limit: int = 6) -> List[str]:
        if not isinstance(evidence, (list, tuple)):
            return []
        lines: List[str] = []
        for item in evidence[:limit]:
            if not isinstance(item, dict):
                continue
            source = cls._regional_source_category(item.get("source_family"), item.get("evidence_type"))
            reporter = str(item.get("reporter_callsign") or "").strip().upper()
            topic = str(item.get("topic") or "").strip()
            age = item.get("age_hours")
            try:
                age_text = f"{float(age):.1f}h ago"
            except Exception:
                age_text = ""
            summary = cls._map_detail_clean_text(item.get("summary") or "")
            prefix = " | ".join(part for part in (source, reporter, topic, age_text) if part)
            line = f"{prefix}: {summary}" if summary and prefix else summary or prefix
            if line:
                lines.append(line)
        return lines

    @staticmethod
    def _map_detail_row_html(label: str, value: object) -> str:
        text = StationsMapTab._map_detail_clean_text(value)
        if not text:
            return ""
        return (
            "<div class='fio-detail-row'>"
            f"<span class='fio-detail-label'>{html.escape(label)}:</span> "
            f"<span class='fio-detail-value'>{html.escape(text)}</span>"
            "</div>"
        )

    @staticmethod
    def _map_detail_payload_provenance(payload: Dict[str, object]) -> Dict[str, object]:
        provenance = payload.get("provenance")
        if isinstance(provenance, Mapping):
            return dict(provenance)
        if isinstance(provenance, str) and provenance.strip():
            try:
                parsed = json.loads(provenance)
            except Exception:
                parsed = {}
            if isinstance(parsed, Mapping):
                return dict(parsed)
        provenance_json = payload.get("provenance_json")
        if isinstance(provenance_json, str) and provenance_json.strip():
            try:
                parsed = json.loads(provenance_json)
            except Exception:
                parsed = {}
            if isinstance(parsed, Mapping):
                return dict(parsed)
        return {}

    @staticmethod
    def _map_detail_optional_bool(value: object) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "y", "direct"}:
            return True
        if text in {"0", "false", "no", "n", "multi-hop", "multihop"}:
            return False
        return None

    @staticmethod
    def _map_detail_hop_count(value: object, path_hops: object = None) -> Optional[int]:
        if value is not None and str(value).strip() != "":
            try:
                return max(0, int(float(str(value).strip())))
            except Exception:
                pass
        if isinstance(path_hops, (list, tuple, set)):
            return len([hop for hop in path_hops if str(hop or "").strip()])
        return None

    @staticmethod
    def _map_detail_routing_context(payload: Dict[str, object]) -> Dict[str, object]:
        rows = StationsMapTab._map_payload_rows(payload)
        provenance = StationsMapTab._map_detail_payload_provenance(payload)
        raw_routing = provenance.get("routing")
        routing: Dict[str, object] = dict(raw_routing) if isinstance(raw_routing, Mapping) else {}
        payload_routing = payload.get("routing")
        if isinstance(payload_routing, Mapping):
            routing.update({key: value for key, value in payload_routing.items() if value not in (None, "")})
        for key in ("route_type", "direct_receive", "via_node", "hop_count", "path_hops", "snr", "rssi"):
            if key not in routing or routing.get(key) in (None, "", ()):
                value = payload.get(key)
                if value in (None, ""):
                    value = rows.get(key.replace("_", " "))
                if value not in (None, ""):
                    routing[key] = value
        return routing

    @staticmethod
    def _map_detail_routing_section_html(payload: Dict[str, object]) -> str:
        routing = StationsMapTab._map_detail_routing_context(payload)
        if not routing:
            return ""
        route_type = StationsMapTab._map_detail_clean_text(routing.get("route_type")).replace("_", " ")
        direct_receive = StationsMapTab._map_detail_optional_bool(routing.get("direct_receive"))
        path_hops = routing.get("path_hops")
        hop_count = StationsMapTab._map_detail_hop_count(routing.get("hop_count"), path_hops)
        via_node = StationsMapTab._map_detail_clean_text(routing.get("via_node"))
        snr = StationsMapTab._map_detail_clean_text(routing.get("snr"))
        rssi = StationsMapTab._map_detail_clean_text(routing.get("rssi"))

        heard = ""
        if direct_receive is True:
            heard = "Direct"
        elif hop_count is not None:
            heard = f"{hop_count} hop" if hop_count == 1 else f"{hop_count} hops"
        elif direct_receive is False:
            heard = "Multi-hop"

        path_text = ""
        if isinstance(path_hops, (list, tuple, set)):
            hops = [StationsMapTab._map_detail_clean_text(hop) for hop in path_hops]
            hops = [hop for hop in hops if hop]
            if hops:
                if len(hops) <= 3 and all(len(hop) <= 12 for hop in hops):
                    path_text = " -> ".join(hops)
                else:
                    path_text = f"{len(hops)} path hints"

        signal = ", ".join(part for part in (f"RSSI {rssi}" if rssi else "", f"SNR {snr}" if snr else "") if part)
        rows_html = [
            StationsMapTab._map_detail_row_html("Heard", heard),
            StationsMapTab._map_detail_row_html("Path", route_type.title() if route_type else ""),
            StationsMapTab._map_detail_row_html("Via", via_node),
            StationsMapTab._map_detail_row_html("Path Hints", path_text),
            StationsMapTab._map_detail_row_html("Signal", signal),
        ]
        rows_html = [row for row in rows_html if row]
        if not rows_html:
            return ""
        return (
            "<div class='fio-detail-section'><div class='fio-detail-heading'>Mesh Routing</div>"
            + "".join(rows_html)
            + "</div>"
        )

    @staticmethod
    def _map_detail_location_confidence_text(payload: Dict[str, object]) -> str:
        confidence = StationsMapTab._map_detail_clean_text(
            payload.get("location_confidence") or payload.get("geo_confidence")
        ).lower()
        provenance = StationsMapTab._map_detail_payload_provenance(payload)
        location_source = payload.get("location_source")
        if not isinstance(location_source, Mapping):
            raw_location_source = provenance.get("location_source")
            location_source = dict(raw_location_source) if isinstance(raw_location_source, Mapping) else {}
        source_type = StationsMapTab._map_detail_clean_text(location_source.get("type")).lower().replace("-", "_")
        source_label = StationsMapTab._map_detail_clean_text(location_source.get("label"))
        if source_type == "advertised" or payload.get("is_mesh_node"):
            return "Advertised"
        if confidence in {"declared", "explicit-latlon", "gps"}:
            return "GPS"
        if confidence == "grid":
            return "Grid"
        if confidence in {"sender_lookup", "sender lookup"} or source_type == "sender_node":
            suffix = f" from {source_label}" if source_label else ""
            return f"Sender location{suffix}"
        if confidence in {"route_derived", "route-derived"} or source_type == "route_derived":
            suffix = f" via {source_label}" if source_label else ""
            return f"Route-derived approx{suffix}"
        if confidence == "state":
            return "State only"
        return ""

    @staticmethod
    def _map_detail_clean_text(value: object, *, multiline: bool = False) -> str:
        text = str(value or "")
        for _ in range(3):
            unescaped = html.unescape(text)
            if unescaped == text:
                break
            text = unescaped
        if not text:
            return ""
        text = re.sub(r"(?i)<br\s*/?>", "\n" if multiline else " | ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if multiline:
            lines = [" ".join(line.split()) for line in text.split("\n")]
            return "\n".join(line for line in lines if line).strip()
        return " ".join(text.split()).strip()

    @classmethod
    def _map_compact_tooltip_html(cls, lines: object, *, limit: int = 4) -> str:
        if isinstance(lines, (list, tuple, set)):
            raw_lines = list(lines)
        else:
            raw_lines = [lines]
        clean_lines: List[str] = []
        for raw_line in raw_lines:
            text = cls._map_detail_clean_text(raw_line)
            if not text:
                continue
            clean_lines.append(text)
            if len(clean_lines) >= limit:
                break
        return "<br/>".join(html.escape(line) for line in clean_lines)

    @staticmethod
    def _station_detected_capability_text(radio_modes: object, app_uses: object) -> str:
        def _clean_items(values: object) -> List[str]:
            if isinstance(values, str):
                raw_values = [part.strip() for part in values.split(",")]
            elif isinstance(values, (list, tuple, set)):
                raw_values = list(values)
            else:
                raw_values = []
            out: List[str] = []
            seen: Set[str] = set()
            for value in raw_values:
                text = str(value or "").strip()
                if not text:
                    continue
                key = text.upper()
                if key in seen:
                    continue
                seen.add(key)
                out.append(text)
            return out

        modes = _clean_items(radio_modes)
        uses = _clean_items(app_uses)
        parts: List[str] = []
        if modes:
            parts.append(f"Traffic: {', '.join(modes)}")
        if uses:
            parts.append(f"Uses: {', '.join(uses)}")
        return "; ".join(parts)

    @staticmethod
    def _map_detail_callsigns_from_text(value: object) -> List[str]:
        text = StationsMapTab._map_detail_clean_text(value).upper()
        if not text:
            return []
        callsigns: List[str] = []
        seen: set[str] = set()
        for token in re.split(r"[^A-Z0-9/>]+", text):
            candidate = token.strip().lstrip("@").rstrip(">")
            if not re.fullmatch(r"[A-Z0-9]{3,10}", candidate):
                continue
            if not any(ch.isdigit() for ch in candidate):
                continue
            if candidate in {
                "AGE",
                "ALL",
                "AMRRON",
                "ANY",
                "ANYNET",
                "AREA",
                "AUTH",
                "CALL",
                "COMMS",
                "COMMSTAT",
                "FORM",
                "FROM",
                "GRID",
                "GROUP",
                "GROUPS",
                "MAGNET",
                "MODE",
                "MODES",
                "NAME",
                "REPORTER",
                "ROUTE",
                "SITREP",
                "SOURCE",
                "STATUS",
                "TO",
                "TOPIC",
                "TOPICS",
                "UPDATED",
            }:
                continue
            if re.fullmatch(r"MR\d{1,2}[A-Z]*", candidate):
                continue
            if candidate not in seen:
                callsigns.append(candidate)
                seen.add(candidate)
        return callsigns

    def _map_selected_display_title(self, payload: Dict[str, object], title: str, rows: Dict[str, str]) -> str:
        if self._map_payload_is_mesh_node(payload):
            labels = self._map_mesh_node_labels(payload, rows, limit=2)
            count = int(self._safe_float(payload.get("count"), 0.0) or 0)
            if count > 1:
                first = labels[0] if labels else ""
                return f"Mesh Nodes: {first} +{count - 1}" if first else f"Mesh Nodes: {count}"
            if labels:
                return f"Mesh Node: {labels[0]}"
            return title or "Mesh Node"
        station_callsign = self._map_detail_clean_text(payload.get("callsign"))
        kind = str(payload.get("type") or payload.get("_map_item_kind") or "").strip().lower()
        has_station_fields = bool(station_callsign) and any(
            key in payload for key in ("is_own_station", "name", "state", "grid")
        )
        # Older station projections omit ``type``.  Do not let their familiar
        # structured fields relabel an explicit report or event selection.
        is_station = kind == "station" or (not kind and has_station_fields)
        if is_station and station_callsign:
            return station_callsign
        if title and not title.lower().startswith("message reports"):
            return title
        kind = str(payload.get("type") or payload.get("_map_item_kind") or "").strip().lower()
        raw_callsigns = payload.get("callsigns")
        callsign_text = ""
        if isinstance(raw_callsigns, (list, tuple, set)):
            callsign_text = " ".join(str(value or "") for value in raw_callsigns)
        else:
            callsign_text = str(raw_callsigns or "")
        callsigns = self._map_detail_callsigns_from_text(
            self._map_detail_first_value(
                payload.get("callsign"),
                payload.get("call"),
                payload.get("call_label"),
                callsign_text,
                rows.get("from"),
                rows.get("call label"),
                rows.get("reporter"),
                payload.get("route"),
                rows.get("route"),
                payload.get("summary"),
                rows.get("summary"),
                rows.get("reports"),
            )
        )
        topic = self._map_preferred_topic_for_values(payload.get("topics")) or str(payload.get("topic") or "").strip()
        if not topic and kind != "station":
            raw_topics = payload.get("topics")
            if isinstance(raw_topics, (list, tuple, set)) and raw_topics:
                topic = str(next(iter(raw_topics)) or "").strip()
        if len(callsigns) == 1:
            if topic:
                return f"{callsigns[0]} {topic} Reports"
            return f"{callsigns[0]} {title or 'Reports'}"
        group = str(payload.get("group") or rows.get("group") or "").strip().lstrip("@")
        if group and topic:
            return f"{group} {topic} Reports"
        if group:
            return f"{group} Reports"
        if topic:
            return f"{topic} Reports"
        return title or "Selection"

    @staticmethod
    def _map_detail_first_value(*values: object) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _map_preferred_topic_for_values(self, topics: object) -> str:
        """Return the map topic that should drive labels and message handoff.

        A cluster may contain several topics, often sorted alphabetically. If
        the operator has an active topic filter, that topic is the mental model
        for the view even when the underlying report also mentions Comms,
        Weather, or Water.
        """
        selected = self._selected_map_topic_filter()
        values: List[str] = []
        if isinstance(topics, (list, tuple, set)):
            values = [str(topic or "").strip() for topic in topics if str(topic or "").strip()]
        elif str(topics or "").strip():
            values = [str(topics or "").strip()]
        if selected:
            if not values or self._map_text_matches_query(selected, *values):
                return selected
        return values[0] if values else ""

    def _map_detail_shell_html(self, heading: str, rows: List[tuple[str, object]], *, note: str = "") -> str:
        theme = self._theme_snapshot()
        detail_text = theme["text"]
        detail_muted = theme["text_muted"]
        detail_info = theme["info"]
        detail_surface = theme["surface_alt"]
        css = """
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
          .fio-detail-card { color: %s; }
          .fio-detail-heading { font-weight: 800; margin: 0 0 8px 0; }
          .fio-detail-row { margin: 4px 0; }
          .fio-detail-label { color: %s; font-weight: 700; }
          .fio-detail-value { color: %s; white-space: normal; }
          .fio-note { margin-top: 8px; padding: 8px; border-left: 3px solid %s; background: %s; white-space: pre-line; }
        </style>
        """ % (detail_text, detail_muted, detail_text, detail_info, detail_surface)
        parts = [css, "<div class='fio-detail-card'>", f"<div class='fio-detail-heading'>{html.escape(heading)}</div>"]
        row_count = 0
        for label, value in rows:
            row_html = StationsMapTab._map_detail_row_html(label, value)
            if row_html:
                row_count += 1
                parts.append(row_html)
        note_text = StationsMapTab._map_detail_clean_text(note, multiline=True)
        if note_text:
            parts.append(f"<div class='fio-note'>{html.escape(note_text)}</div>")
        elif row_count == 0:
            parts.append("<p>No detail is available yet.</p>")
        parts.append("</div>")
        return "".join(parts)

    def _map_selected_source_label(self, payload: Dict[str, object], rows: Optional[Dict[str, str]] = None) -> str:
        rows = rows if rows is not None else self._map_payload_rows(payload)
        source_family = self._map_payload_source_family(payload)
        raw_source_label = str(rows.get("source") or "").strip()
        if raw_source_label.lower() in {"fused", "mixed", "multiple_sources", "multiple sources"}:
            return self._map_report_source_label(raw_source_label)
        return raw_source_label or self._map_report_source_label(source_family)

    @staticmethod
    def _map_station_summary_is_noise(value: object) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if text.endswith("?"):
            return True
        lowered = text.lower()
        prompt_stems = (
            "is ",
            "are ",
            "do ",
            "does ",
            "can ",
            "has ",
            "have ",
            "phone ",
            "power ",
            "water ",
            "internet ",
            "landline ",
        )
        prompt_words = ("functioning", "working", "operational", "available")
        return lowered.startswith(prompt_stems) and any(word in lowered for word in prompt_words)

    def _map_selected_status_html(self, payload: Dict[str, object], *, summary: str = "") -> str:
        rows = self._map_payload_rows(payload)
        kind = str(payload.get("type") or "").strip().lower()
        if self._map_payload_is_mesh_node(payload):
            labels = self._map_mesh_node_labels(payload, rows, limit=14)
            label_text = ", ".join(labels[:6]) + ("..." if len(labels) > 6 else "")
            count = int(self._safe_float(payload.get("count"), 0.0) or 0) or (len(labels) if labels else "")
            routing = self._map_detail_routing_context(payload)
            hop_count = self._map_detail_hop_count(routing.get("hop_count"), routing.get("path_hops"))
            direct_receive = self._map_detail_optional_bool(routing.get("direct_receive"))
            if direct_receive is True:
                heard = "Direct"
            elif hop_count is not None:
                heard = f"{hop_count} hop" if hop_count == 1 else f"{hop_count} hops"
            else:
                heard = ""
            return self._map_detail_shell_html(
                "Mesh Status",
                [
                    ("Nodes", count),
                    ("Selected", label_text),
                    ("Heard", heard),
                    ("Signal", self._map_detail_first_value(rows.get("signal"), payload.get("rssi"), payload.get("snr"))),
                    ("Location", self._map_detail_location_confidence_text(payload)),
                    ("Source", self._map_selected_source_label(payload, rows)),
                    ("Newest", self._map_detail_first_value(rows.get("newest"), rows.get("age"), payload.get("age"))),
                    ("Topics", self._map_detail_first_value(rows.get("topics"), payload.get("topic"))),
                ],
                note="Advertised positions may be old. Cached outgoing routes do not prove packet delivery or current reachability.",
            )
        status = self._map_detail_first_value(
            rows.get("sitrep"),
            rows.get("status"),
            rows.get("severity"),
            payload.get("severity"),
            "Unknown",
        )
        source = self._map_selected_source_label(payload, rows)
        group = str(payload.get("group") or rows.get("group") or "").strip().lstrip("@")
        area = self._map_detail_first_value(rows.get("area"), rows.get("location"), payload.get("area"))
        updated = self._map_detail_first_value(rows.get("updated"), rows.get("age"), payload.get("age"))
        form = self._map_detail_first_value(rows.get("mcf"), rows.get("form"), rows.get("reports"), payload.get("title"))
        evidence = self._map_detail_first_value(summary, rows.get("activity"), rows.get("schedule"))
        location_confidence = self._map_detail_location_confidence_text(payload)
        # Raw form prompts are useful in the message body, but as map status they obscure why the pin is colored.
        if self._map_station_summary_is_noise(evidence):
            evidence = ""
        if kind == "station":
            heading = "Station Status"
            note = evidence or "No latest status report detail is available for this station yet."
        else:
            heading = "Report Status"
            note = evidence
        return self._map_detail_shell_html(
            heading,
            [
                ("Status", status),
                ("Group", group),
                ("Area", area),
                ("Form", form),
                ("Updated", updated),
                ("Source", source),
                ("Location", location_confidence),
            ],
            note=note,
        )

    def _peer_schedule_hint_for_callsign(self, callsign: str) -> str:
        callsign = str(callsign or "").strip().upper()
        if not callsign:
            return ""
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            rows = self._load_peer_schedule_presence(now_utc)
        except Exception:
            rows = []
        for row in rows or []:
            if str(row.get("callsign") or "").strip().upper() != callsign:
                continue
            band = str(row.get("band") or "").strip().upper()
            freq = str(row.get("frequency") or "").strip()
            mode = str(row.get("mode") or "").strip().upper()
            try:
                minutes_to_end = int(row.get("minutes_to_end") or 0)
            except Exception:
                minutes_to_end = 0
            parts = [part for part in (band, freq, mode) if part]
            if minutes_to_end > 0:
                parts.append(f"active {minutes_to_end}m")
            return " ".join(parts)
        return ""

    def _path_to_propagation_hint(self, callsign: str, payload: Dict[str, object]) -> str:
        user_ll = self._get_user_latlon()
        if not user_ll:
            return ""
        target_ll = self._map_selected_latlon(payload)
        if not (target_ll[0] or target_ll[1]) and callsign:
            target_ll, _state = self._operator_target_point(callsign)
        if not target_ll:
            return ""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        best_band = ""
        best_score = 0.0
        distance_km = self._haversine_km(user_ll[0], user_ll[1], target_ll[0], target_ll[1])
        for band in PROP_BANDS:
            score = self._modeled_band_score(band, target_ll[0], target_ll[1], now_utc, distance_km)
            if score > best_score:
                best_band = band
                best_score = score
        if not best_band:
            return ""
        return f"{best_band} modeled {self._score_level(best_score)} now"

    def _path_to_planning_rows(self, callsign: str, payload: Dict[str, object], rows: Dict[str, str]) -> List[tuple[str, str]]:
        schedule = self._map_detail_first_value(
            self._peer_schedule_hint_for_callsign(callsign),
            rows.get("schedule"),
            payload.get("qsy_text"),
        )
        if schedule:
            return [
                ("Peer Schedule", schedule),
                ("Planning", "Use the peer schedule first; it is the strongest hint for where this station is expected now."),
            ]
        propagation = self._path_to_propagation_hint(callsign, payload)
        if propagation:
            return [
                ("Propagation", propagation),
                ("Planning", "No peer schedule is known; use RF Planning as the fallback."),
            ]
        return [
            ("Planning", "No peer schedule is known; use RF Planning as the propagation fallback."),
        ]

    def _map_selected_paths_html(self, payload: Dict[str, object]) -> str:
        if payload.get("is_mesh_node"):
            route = payload.get("outgoing_route") or {}
            return self._map_detail_shell_html("Mesh Paths", [
                ("Node", payload.get("label")),
                ("Cached outgoing", f"{route.get('hop_count')} hops" if route.get("known") else "Unknown"),
                ("Updated", route.get("updated_utc") or "Unknown"),
                ("Layer", "Showing" if self.show_link_paths else "Hidden"),
            ], note="↝ Cached outgoing route. → Observed flood hop order. Missing or ambiguous hops break the line; delivery is unverified.")
        rows = self._map_payload_rows(payload)
        callsign = self._map_selected_action_callsign(payload)
        mode = str(getattr(self, "link_mode", "") or "").strip().lower()
        active_scope = self._current_path_scope_label()
        if not callsign:
            return self._map_detail_shell_html(
                "Path Topology",
                [("Scope", active_scope)],
                note="Select a station to review observed paths for that station.",
            )
        active = self._selected_station_paths_active(callsign)
        note = (
            "Paths for this station are displayed on the map. Click Hide Paths to turn that layer off."
            if active
            else "Click Show Paths To to display observed direct or shared-contact paths from my station to this station."
        )
        planning_rows = self._path_to_planning_rows(callsign, payload, rows)
        path_snapshot = self._map_path_snapshot(callsign)
        direct = str(path_snapshot.get("direct") or "").strip()
        relay = str(path_snapshot.get("relay") or "").strip()
        relay_edges = path_snapshot.get("relay_edges")
        shared = path_snapshot.get("shared")
        shared_text = ""
        if isinstance(shared, (list, tuple)) and shared:
            shared_text = ", ".join(str(value or "").strip() for value in shared[:8] if str(value or "").strip())
        relay_detail = ""
        if isinstance(relay_edges, (list, tuple)) and relay_edges:
            relay_detail = "\n".join(str(value or "").strip() for value in relay_edges[:4] if str(value or "").strip())
        return self._map_detail_shell_html(
            "Path Topology",
            [
                ("Station", callsign),
                ("Direct Path", direct or "No direct path in this window"),
                ("Best Relay", relay or "No relay chain found in this window"),
                ("Relay Detail", relay_detail),
                ("Shared Contacts", shared_text),
                ("Layer", "Showing" if bool(getattr(self, "show_link_paths", False)) else "Hidden"),
                ("Window", self._map_message_context_age_label(self._effective_path_recency_seconds())),
                ("Scope", active_scope if mode != "off" else "Off"),
                *planning_rows,
                ("Meaning", "Arrows show who reported hearing whom; color shows reported signal quality."),
            ],
            note=note,
        )

    def _map_selected_messages_html(self, payload: Dict[str, object]) -> str:
        context = self._map_selected_message_context(payload)
        target = str(context.get("target") or "").strip()
        if not target:
            return self._map_detail_shell_html(
                "Related Messages",
                [],
                note="This map item does not have a message history view.",
            )
        group = str(context.get("group_filter") or context.get("query") or "").strip().lstrip("@")
        topic = str(context.get("topic_filter") or "").strip()
        source_family = str(context.get("source_family") or "").strip()
        query = str(context.get("query_filter") or context.get("callsign") or "").strip().lstrip("@")
        try:
            age_seconds = int(context.get("age_filter_seconds") or 0)
        except Exception:
            age_seconds = 0
        age_label = self._map_message_context_age_label(age_seconds)
        status_filter = "Non-green/status evidence" if bool(context.get("concern_only")) else ""
        destination = "Local report history" if target == "local_reports" else "Message Inbox"
        callsign = self._map_selected_action_callsign(payload)
        message_snapshot = self._map_message_snapshot(callsign, context) if callsign else {}
        source_mix = message_snapshot.get("source_mix") if isinstance(message_snapshot, dict) else {}
        source_mix_text = ""
        if isinstance(source_mix, dict):
            source_mix_text = ", ".join(
                f"{source} {count}" for source, count in source_mix.items() if int(count or 0) > 0
            )
        topics = message_snapshot.get("topics") if isinstance(message_snapshot, dict) else []
        topic_summary = ", ".join(str(value or "").strip() for value in topics if str(value or "").strip()) if isinstance(topics, (list, tuple)) else ""
        newest_ts = message_snapshot.get("newest_ts") if isinstance(message_snapshot, dict) else 0
        return self._map_detail_shell_html(
            "Related Messages",
            [
                ("Open", destination),
                ("Age", age_label),
                ("Status Filter", status_filter),
                ("Matching Traffic", message_snapshot.get("count") if isinstance(message_snapshot, dict) else ""),
                ("Unread", message_snapshot.get("unread") if isinstance(message_snapshot, dict) else ""),
                ("Newest", self._map_report_age_text(newest_ts) if newest_ts else ""),
                ("Source Mix", source_mix_text),
                ("Recent Topics", topic_summary),
                ("Group", group),
                ("Topic", topic),
                ("Source", self._map_report_source_label(source_family) if source_family else ""),
                ("Search", query),
            ],
            note="Use the Messages action below to open the filtered traffic behind this map item.",
        )

    def _map_message_context_age_label(self, seconds: int) -> str:
        value = int(seconds or 0)
        if value <= 0:
            return "Any"
        for label, option_seconds in self._map_recency_options():
            if option_seconds == value:
                return self._map_recency_display_label(label)
        if value % (24 * 60 * 60) == 0:
            days = value // (24 * 60 * 60)
            return f"Age: {days}d"
        if value % (60 * 60) == 0:
            hours = value // (60 * 60)
            return f"Age: {hours}h"
        if value % 60 == 0:
            minutes = value // 60
            return f"Age: {minutes}m"
        return f"Age: {value}s"

    def _map_selected_detail_html(self, payload: Dict[str, object], *, summary: str = "") -> str:
        rows = self._map_payload_rows(payload)
        kind = str(payload.get("type") or "").strip().lower()
        source_family = self._map_payload_source_family(payload)
        source_label = self._map_selected_source_label(payload, rows)
        group = str(payload.get("group") or rows.get("group") or "").strip().lstrip("@")
        topic = self._map_preferred_topic_for_values(payload.get("topics")) or str(payload.get("topic") or "").strip()
        if not topic and kind != "station":
            raw_topics = payload.get("topics")
            if isinstance(raw_topics, (list, tuple, set)) and raw_topics:
                topic = str(next(iter(raw_topics)) or "").strip()
        group_chips = payload.get("groups") if payload.get("groups") else group
        topic_chips = payload.get("topics") if payload.get("topics") else topic
        summary_text = self._map_detail_clean_text(
            summary
            or payload.get("summary")
            or rows.get("summary")
            or rows.get("reports")
            or rows.get("activity")
            or "",
            multiline=True,
        )

        theme = self._theme_snapshot()
        detail_text = theme["text"]
        detail_muted = theme["text_muted"]
        detail_info = theme["info"]
        detail_surface = theme["surface_alt"]
        css = """
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
          .fio-detail-card { color: %s; }
          .fio-detail-section { margin: 0 0 10px 0; }
          .fio-detail-heading { font-weight: 700; margin: 0 0 4px 0; }
          .fio-detail-row { margin: 4px 0; }
          .fio-detail-label { color: %s; font-weight: 700; }
          .fio-detail-value { color: %s; white-space: normal; }
          .fio-chip-row { margin: 4px 0 6px 0; }
          .fio-chip { display: inline-block; margin: 0 4px 4px 0; padding: 2px 7px; border-radius: 8px; background: %s; color: %s; font-weight: 700; }
          .fio-chip.muted { background: %s; color: %s; }
          .fio-summary { margin-top: 6px; padding: 8px; border-left: 3px solid %s; background: %s; white-space: pre-line; }
          .fio-evidence-list { margin: 6px 0 0 0; padding-left: 18px; }
          .fio-evidence-list li { margin: 3px 0; }
        </style>
        """ % (
            detail_text,
            detail_muted,
            detail_text,
            self._hex_to_rgba(detail_info, 0.16),
            detail_info,
            self._hex_to_rgba(detail_surface, 0.85),
            detail_muted,
            detail_info,
            self._hex_to_rgba(detail_info, 0.10),
        )
        parts: List[str] = [css, "<div class='fio-detail-card'>"]
        if kind == "regional_intelligence":
            source_mix = payload.get("source_mix")
            if not source_mix:
                source_mix = self._regional_source_mix(payload.get("evidence"))
            evidence_lines = self._regional_evidence_lines(payload.get("evidence"))
            parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Regional Intelligence</div>")
            parts.append(self._map_detail_row_html("Status", self._map_detail_first_value(rows.get("status"), rows.get("level"), payload.get("level"))))
            parts.append(self._map_detail_row_html("Area", self._map_detail_first_value(rows.get("area"), payload.get("state"))))
            parts.append(self._map_detail_row_html("Window", rows.get("window") or payload.get("age_window")))
            parts.append(self._map_detail_row_html("Why", rows.get("why") or self._regional_topic_rows_text(payload.get("top_topics")) or rows.get("topics")))
            parts.append(self._map_detail_row_html("Trend", self._map_detail_first_value(rows.get("trend"), payload.get("trend"))))
            parts.append(self._map_detail_row_html("Newest", self._map_detail_first_value(rows.get("newest"), payload.get("newest_age_hours"))))
            parts.append(self._map_detail_row_html("Topics", self._regional_topic_rows_text(payload.get("top_topics")) or rows.get("topics")))
            parts.append(self._map_detail_row_html("Evidence", rows.get("evidence")))
            parts.append(self._map_detail_row_html("Sources", rows.get("sources") or self._regional_source_mix_text(source_mix)))
            parts.append(self._map_detail_row_html("Next", "Use Messages to review reports, or Center to inspect this area on the map."))
            parts.append("</div>")
            if evidence_lines:
                parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Evidence</div><ul class='fio-evidence-list'>")
                for line in evidence_lines:
                    parts.append(f"<li>{html.escape(line)}</li>")
                parts.append("</ul></div>")
        elif source_family == "condition_alert":
            parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Condition Alert</div>")
            parts.append(self._map_detail_row_html("Level", self._map_detail_first_value(rows.get("level"), rows.get("severity"), rows.get("status"), "Review")))
            parts.append(self._map_detail_row_html("Group", group))
            parts.append(self._map_detail_row_html("Route", str(payload.get("route") or rows.get("route") or "").replace(" | ", " -> ")))
            parts.append(self._map_detail_row_html("Age", rows.get("age") or rows.get("updated")))
            parts.append(self._map_detail_row_html("Source", source_label))
            parts.append("</div>")
        elif kind == "station":
            action_callsign = self._map_selected_action_callsign(payload)
            station_snapshot = self._map_station_activity_snapshot(action_callsign) if action_callsign else {}
            snapshot_modes = self._map_tool_list_text(station_snapshot.get("modes", [])) if station_snapshot else ""
            snapshot_tools = self._map_tool_list_text(station_snapshot.get("tools", [])) if station_snapshot else ""
            payload_modes = self._map_tool_list_text(payload.get("modes", []))
            payload_tools = self._map_tool_list_text(payload.get("app_uses", []))
            last_seen_ts = station_snapshot.get("last_seen_ts") if station_snapshot else 0
            last_seen = self._map_report_age_text(last_seen_ts) if last_seen_ts else ""
            last_seen_source = str(station_snapshot.get("last_seen_source") or "").strip() if station_snapshot else ""
            parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Station Activity</div>")
            parts.append(self._map_detail_row_html("Callsign", payload.get("title")))
            parts.append(self._map_detail_row_html("Name", rows.get("name")))
            parts.append(self._map_detail_row_html("Area", rows.get("area")))
            parts.append(self._map_detail_row_html("FEMA Region", rows.get("fema region")))
            parts.append(self._map_detail_row_html("Groups", rows.get("groups")))
            parts.append(self._map_detail_row_html("Detected Tools", self._map_detail_first_value(snapshot_tools, payload_tools, rows.get("detected"))))
            parts.append(self._map_detail_row_html("Detected Modes", self._map_detail_first_value(snapshot_modes, payload_modes, rows.get("modes"))))
            parts.append(self._map_detail_row_html("Activity", rows.get("activity")))
            parts.append(self._map_detail_row_html("Last Seen", last_seen))
            parts.append(self._map_detail_row_html("Last Seen Via", last_seen_source))
            parts.append(self._map_detail_row_html("Status", self._map_detail_first_value(rows.get("sitrep"), rows.get("status"), rows.get("severity"))))
            parts.append(self._map_detail_row_html("Marker", rows.get("marker")))
            parts.append(self._map_detail_row_html("Updated", self._map_detail_first_value(rows.get("updated"), rows.get("age"))))
            parts.append(self._map_detail_row_html("Source", source_label))
            parts.append(self._map_detail_row_html("Schedule", rows.get("schedule")))
            parts.append(self._map_detail_row_html("JS8 Heard", rows.get("js8 heard")))
            parts.append(self._map_detail_row_html("JS8 Contact", rows.get("js8 contact")))
            parts.append(self._map_detail_row_html("JS8 SNR", rows.get("js8 snr")))
            parts.append(self._map_detail_row_html("VarAC Heard", rows.get("varac heard")))
            parts.append(self._map_detail_row_html("Trust", rows.get("trust")))
            parts.append("</div>")
        else:
            if source_family == "spotter":
                parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Spotter Report</div>")
                parts.append(self._map_detail_row_html("MCF", self._map_detail_first_value(rows.get("mcf"), rows.get("form"), rows.get("reports"), payload.get("title"))))
                parts.append(self._map_detail_row_html("Route", str(payload.get("route") or rows.get("route") or "").replace(" | ", " -> ")))
                parts.append(self._map_detail_row_html("Age", rows.get("age")))
                parts.append(self._map_detail_row_html("Area", self._map_detail_first_value(rows.get("area"), rows.get("location"))))
                parts.append(self._map_detail_row_html("Trust", self._map_detail_first_value(rows.get("auth"), rows.get("trust"), rows.get("status"))))
                parts.append("</div>")
            elif source_family == "commstat":
                reported_for = self._map_detail_first_value(rows.get("reported for"), rows.get("area"), rows.get("location"))
                report_scope = self._map_detail_first_value(rows.get("report scope"), rows.get("scope"), payload.get("scope"))
                reported_by = self._map_detail_first_value(rows.get("reporter"), rows.get("from"), rows.get("reported by"), payload.get("call_label"), payload.get("callsign"), payload.get("from_call"))
                status_text = self._map_detail_first_value(rows.get("status"), rows.get("severity"))
                topic_text = self._map_detail_first_value(
                    rows.get("topics"),
                    rows.get("topic"),
                    payload.get("topic"),
                    ", ".join(str(value or "").strip() for value in (payload.get("topics") or []) if str(value or "").strip())
                    if isinstance(payload.get("topics"), (list, tuple, set))
                    else "",
                )
                why_text = " | ".join(part for part in (status_text, topic_text, reported_for) if part)
                action_callsign = self._map_selected_action_callsign(payload)
                reporter_snapshot = self._map_station_activity_snapshot(action_callsign) if action_callsign else {}
                reporter_modes = self._map_tool_list_text(reporter_snapshot.get("modes", [])) if reporter_snapshot else ""
                reporter_tools = self._map_tool_list_text(reporter_snapshot.get("tools", [])) if reporter_snapshot else ""
                reporter_last_ts = reporter_snapshot.get("last_seen_ts") if reporter_snapshot else 0
                reporter_seen = self._map_report_age_text(reporter_last_ts) if reporter_last_ts else ""
                reporter_source = str(reporter_snapshot.get("last_seen_source") or "").strip() if reporter_snapshot else ""
                location_note = self._map_commstat_scope_note(
                    report_scope,
                    self._map_detail_first_value(rows.get("state confidence"), payload.get("state_confidence")),
                    self._map_detail_first_value(rows.get("geo confidence"), payload.get("geo_confidence")),
                )
                parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>CommStat Activity</div>")
                parts.append(self._map_detail_row_html("Route", str(payload.get("route") or rows.get("route") or "").replace(" | ", " -> ")))
                parts.append(self._map_detail_row_html("Age", rows.get("age")))
                parts.append(self._map_detail_row_html("Reach", self._map_detail_first_value(rows.get("reach"), rows.get("transport"), rows.get("source"), source_label)))
                parts.append(self._map_detail_row_html("Reported For", reported_for))
                parts.append(self._map_detail_row_html("Reported By", reported_by))
                parts.append(self._map_detail_row_html("Report Scope", report_scope))
                parts.append(self._map_detail_row_html("Location Note", location_note))
                parts.append(self._map_detail_row_html("Reporter Tools", reporter_tools))
                parts.append(self._map_detail_row_html("Reporter Modes", reporter_modes))
                parts.append(self._map_detail_row_html("Reporter Last Seen", reporter_seen))
                parts.append(self._map_detail_row_html("Reporter Seen Via", reporter_source))
                parts.append(self._map_detail_row_html("Why", why_text))
                parts.append(self._map_detail_row_html("Status", status_text))
                parts.append(self._map_detail_row_html("Topics", topic_text))
                parts.append("</div>")
            elif source_family == "local_report":
                parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Local Report</div>")
                parts.append(self._map_detail_row_html("Reporter", self._map_detail_first_value(rows.get("reporter"), rows.get("from"), payload.get("title"))))
                parts.append(self._map_detail_row_html("Area", self._map_detail_first_value(rows.get("area"), rows.get("location"))))
                parts.append(self._map_detail_row_html("Age", rows.get("age")))
                parts.append(self._map_detail_row_html("Status", self._map_detail_first_value(rows.get("confirmed"), rows.get("status"), rows.get("severity"))))
                parts.append(self._map_detail_row_html("Source", source_label))
                parts.append("</div>")
            elif source_family == "rf_pin":
                parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Planning Pin</div>")
                parts.append(self._map_detail_row_html("Purpose", self._map_detail_first_value(rows.get("purpose"), rows.get("reports"), payload.get("title"))))
                parts.append(self._map_detail_row_html("Area", self._map_detail_first_value(rows.get("area"), rows.get("location"))))
                parts.append(self._map_detail_row_html("Band", rows.get("band")))
                parts.append(self._map_detail_row_html("Group", group))
                parts.append(self._map_detail_row_html("Updated", self._map_detail_first_value(rows.get("updated"), rows.get("age"))))
                parts.append("</div>")
            elif self._map_payload_is_mesh_node(payload):
                labels = self._map_mesh_node_labels(payload, rows, limit=14)
                count = int(self._safe_float(payload.get("count"), 0.0) or 0) or (len(labels) if labels else "")
                parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Mesh Topology</div>")
                parts.append(self._map_detail_row_html("Source", source_label))
                parts.append(self._map_detail_row_html("Nodes", count))
                parts.append(self._map_detail_row_html("Newest", self._map_detail_first_value(rows.get("newest"), rows.get("age"), payload.get("age"))))
                parts.append(self._map_detail_row_html("Location", self._map_detail_location_confidence_text(payload)))
                parts.append(self._map_detail_row_html("Map Use", "Inspect node area, routing clues, and local mesh coverage."))
                parts.append("</div>")
                if labels:
                    parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Nodes</div><ul class='fio-evidence-list'>")
                    for label in labels:
                        parts.append(f"<li>{html.escape(label)}</li>")
                    parts.append("</ul></div>")
            else:
                parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Operational Report</div>")
                parts.append(self._map_detail_row_html("Source", source_label))
                parts.append(self._map_detail_row_html("Route", str(payload.get("route") or rows.get("route") or "").replace(" | ", " -> ")))
                report_count = self._map_detail_first_value(rows.get("report count"), rows.get("count"))
                if not report_count:
                    report_count_match = re.search(r"\bmessage reports:\s*(\d+)\b", f"{payload.get('title') or ''} {rows.get('reports') or ''}", re.I)
                    report_count = report_count_match.group(1) if report_count_match else ""
                parts.append(self._map_detail_row_html("Reports", report_count))
                parts.append(self._map_detail_row_html("Age", rows.get("age")))
                parts.append(self._map_detail_row_html("Status", rows.get("severity")))
                parts.append("</div>")

        if group_chips:
            parts.append(self._map_detail_list_row_html("Groups", group_chips))
        if topic_chips:
            parts.append(self._map_detail_list_row_html("Topics", topic_chips))

        location_values = [
            rows.get("area"),
            rows.get("location"),
        ]
        location_text = next((value for value in location_values if value), "")
        if location_text and kind != "station" and source_family != "commstat":
            parts.append("<div class='fio-detail-section'><div class='fio-detail-heading'>Location</div>")
            parts.append(self._map_detail_row_html("Area", location_text))
            parts.append("</div>")

        if kind == "station" and self._map_station_summary_is_noise(summary_text):
            summary_text = ""

        routing_section = self._map_detail_routing_section_html(payload)
        if routing_section:
            parts.append(routing_section)

        if summary_text:
            parts.append(f"<div class='fio-summary'>{html.escape(summary_text)}</div>")
        elif not any("fio-detail-row" in part for part in parts):
            parts.append("<p>No additional detail is available for this selection.</p>")
        parts.append("</div>")
        return "".join(parts)

    def _center_map_selected_detail(self) -> None:
        payload = dict(getattr(self, "_map_selected_payload", {}) or {})
        lat, lon = self._map_selected_latlon(payload)
        renderer = getattr(self, "_native_map_renderer", None)
        if (lat == 0.0 and lon == 0.0) or renderer is None:
            return
        try:
            renderer.center_on(lat, lon, zoom=6)
        except Exception:
            pass

    def _map_selected_station_callsign(self) -> str:
        payload = dict(getattr(self, "_map_selected_payload", {}) or {})
        rows = self._map_payload_rows(payload)
        for value in (
            payload.get("callsign"),
            payload.get("call"),
            payload.get("call_label"),
            payload.get("title"),
            rows.get("callsign"),
            rows.get("call"),
            rows.get("call label"),
            rows.get("from"),
            rows.get("reporter"),
        ):
            callsign = self._map_callsign_from_value(value)
            if callsign:
                return callsign
        return ""

    def _map_selected_action_callsign(self, payload: Optional[Dict[str, object]] = None) -> str:
        payload = dict(payload if payload is not None else getattr(self, "_map_selected_payload", {}) or {})
        rows = self._map_payload_rows(payload)
        if str(payload.get("type") or "").strip().lower() == "station":
            callsign = self._map_selected_station_callsign()
            if callsign:
                return callsign
        for value in (
            payload.get("reported_by"),
            payload.get("reporter_callsign"),
            payload.get("from_call"),
            payload.get("sender"),
            rows.get("reported by"),
            rows.get("reporter"),
            rows.get("from"),
            payload.get("call_label"),
            payload.get("callsign"),
            payload.get("call"),
        ):
            callsign = self._map_callsign_from_value(value)
            if callsign:
                return callsign
        return ""

    @staticmethod
    def _map_callsign_from_value(value: object) -> str:
        text = str(value or "").strip().upper().lstrip("@").rstrip(">")
        if not text:
            return ""
        if re.fullmatch(r"[A-Z0-9]{3,10}", text) and looks_like_callsign_text(text):
            return text
        first_line = text.splitlines()[0].strip().lstrip("@").rstrip(">")
        for candidate in re.split(r"[\s|,;/()<>]+", first_line):
            candidate = candidate.strip().upper().lstrip("@").rstrip(">")
            if candidate and re.fullmatch(r"[A-Z0-9]{3,10}", candidate) and looks_like_callsign_text(candidate):
                return candidate
        return ""

    @staticmethod
    def _map_callsign_base(value: object) -> str:
        callsign = StationsMapTab._map_callsign_from_value(value)
        if not callsign:
            return ""
        try:
            return JS8LogLinkIndexer._base_callsign(callsign)
        except Exception:
            return callsign

    @staticmethod
    def _map_tool_list_text(values: Iterable[object]) -> str:
        labels: List[str] = []
        seen: Set[str] = set()
        for value in values:
            label = str(value or "").strip()
            if not label:
                continue
            key = label.upper()
            if key in seen:
                continue
            seen.add(key)
            labels.append(label)
        return ", ".join(labels)

    def _map_station_activity_snapshot(self, callsign: str) -> Dict[str, object]:
        target = self._map_callsign_base(callsign)
        if not target:
            return {}
        age_seconds = int(getattr(self, "recency_seconds", 0) or 0)
        cache_key = ("map_station_activity_snapshot", target, age_seconds)
        cached = self._query_cache_get(cache_key, ttl_sec=5.0)
        if isinstance(cached, dict):
            return dict(cached)
        snapshot: Dict[str, object] = {
            "tools": [],
            "modes": [],
            "last_seen_ts": 0.0,
            "last_seen_source": "",
            "message_count": 0,
            "unread_count": 0,
            "source_mix": {},
        }

        def _remember_seen(ts_value: object, source: str) -> None:
            try:
                ts = float(ts_value or 0.0)
            except Exception:
                ts = 0.0
            if ts > float(snapshot.get("last_seen_ts") or 0.0):
                snapshot["last_seen_ts"] = ts
                snapshot["last_seen_source"] = source

        db_path = get_config_dir() / "config" / "freqinout_nets.db"
        if not db_path.exists():
            return snapshot
        cutoff = time.time() - age_seconds if age_seconds > 0 else 0.0
        tools: Set[str] = set()
        modes: Set[str] = set()
        source_mix: Dict[str, int] = {}
        conn = None
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            cur.execute("SELECT last_seen_ts FROM js8_callsign_stats WHERE UPPER(callsign)=?", (target,))
            row = cur.fetchone()
            if row:
                ts = float(row[0] or 0.0)
                if ts <= 0 or cutoff <= 0 or ts >= cutoff:
                    modes.add("JS8Call")
                    _remember_seen(ts, "JS8Call")
            cur.execute(
                "SELECT MAX(ts), COUNT(*) FROM js8_links WHERE ts >= ? AND (UPPER(origin)=? OR UPPER(destination)=?)",
                (cutoff, target, target),
            )
            row = cur.fetchone()
            if row and int(row[1] or 0) > 0:
                modes.add("JS8Call")
                _remember_seen(row[0], "JS8Call")
            cur.execute(
                "SELECT MAX(event_ts), COUNT(*) FROM commstat_artifacts WHERE event_ts >= ? AND UPPER(from_call)=?",
                (cutoff, target),
            )
            row = cur.fetchone()
            if row and int(row[1] or 0) > 0:
                tools.add("CommStat")
                source_mix["CommStat"] = int(row[1] or 0)
                _remember_seen(row[0], "CommStat")
            cur.execute(
                "SELECT MAX(utc_ts), COUNT(*), SUM(CASE WHEN read_ts IS NULL OR read_ts <= 0 THEN 1 ELSE 0 END) "
                "FROM js8_messages WHERE utc_ts >= ? AND (UPPER(from_call)=? OR UPPER(to_call)=?)",
                (cutoff, target, target),
            )
            row = cur.fetchone()
            if row and int(row[1] or 0) > 0:
                count = int(row[1] or 0)
                modes.add("JS8Call")
                snapshot["message_count"] = int(snapshot.get("message_count") or 0) + count
                snapshot["unread_count"] = int(snapshot.get("unread_count") or 0) + int(row[2] or 0)
                source_mix["JS8Call"] = source_mix.get("JS8Call", 0) + count
                _remember_seen(row[0], "JS8Call")
            cur.execute("SELECT last_seen_ts FROM varac_callsign_stats WHERE UPPER(callsign)=?", (target,))
            row = cur.fetchone()
            if row:
                ts = float(row[0] or 0.0)
                if ts <= 0 or cutoff <= 0 or ts >= cutoff:
                    modes.add("VarAC")
                    _remember_seen(ts, "VarAC")
            cur.execute(
                "SELECT MAX(ts), COUNT(*) FROM varac_links WHERE ts >= ? AND (UPPER(origin)=? OR UPPER(destination)=?)",
                (cutoff, target, target),
            )
            row = cur.fetchone()
            if row and int(row[1] or 0) > 0:
                modes.add("VarAC")
                _remember_seen(row[0], "VarAC")
            cur.execute(
                "SELECT MAX(last_seen_ts), COUNT(*) FROM fldigi_checkins WHERE last_seen_ts >= ? AND UPPER(callsign)=?",
                (cutoff, target),
            )
            row = cur.fetchone()
            if row and int(row[1] or 0) > 0:
                modes.add("FLDigi")
                tools.add("Fast Light")
                _remember_seen(row[0], "Fast Light")
        except Exception as exc:
            log.debug("StationsMap: failed loading selected station activity for %s: %s", target, exc)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        try:
            if target in self._load_commstat_reporter_activity():
                tools.add("CommStat")
        except Exception:
            pass
        snapshot["tools"] = sorted(tools)
        snapshot["modes"] = sorted(modes)
        snapshot["source_mix"] = source_mix
        self._query_cache_set(cache_key, dict(snapshot))
        return snapshot

    def _map_path_snapshot(self, callsign: str) -> Dict[str, object]:
        target = self._map_callsign_base(callsign)
        my_call = self._map_callsign_base(self._operator_callsign_for_map_actions())
        if not target or not my_call or target == my_call:
            return {}
        age_seconds = self._effective_path_recency_seconds()
        cache_key = ("map_path_snapshot", my_call, target, age_seconds)
        cached = self._query_cache_get(cache_key, ttl_sec=5.0)
        if isinstance(cached, dict):
            return dict(cached)
        cutoff = time.time() - age_seconds if age_seconds > 0 else 0.0
        best: Dict[tuple[str, str], Dict[str, object]] = {}
        db_path = get_config_dir() / "config" / "freqinout_nets.db"
        if not db_path.exists():
            return {}
        conn = None
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            for table in ("js8_links", "varac_links"):
                try:
                    cur.execute(
                        f"SELECT ts, origin, destination, snr, band FROM {table} WHERE ts >= ? ORDER BY ts DESC LIMIT 20000",
                        (cutoff,),
                    )
                    link_rows = cur.fetchall()
                except Exception:
                    link_rows = []
                source = "JS8Call" if table == "js8_links" else "VarAC"
                for ts, origin, destination, snr, band in link_rows:
                    a = self._map_callsign_base(origin)
                    b = self._map_callsign_base(destination)
                    if not a or not b or a == b:
                        continue
                    key = tuple(sorted((a, b)))
                    try:
                        snr_val = float(snr)
                    except Exception:
                        snr_val = None
                    prev = best.get(key)
                    prev_snr = prev.get("snr") if isinstance(prev, dict) else None
                    if prev is None or (
                        snr_val is not None and (prev_snr is None or snr_val > prev_snr)
                    ) or float(ts or 0.0) > float(prev.get("ts") or 0.0):
                        best[key] = {
                            "snr": snr_val,
                            "ts": float(ts or 0.0),
                            "band": str(band or "").strip().upper(),
                            "source": source,
                        }
        except Exception as exc:
            log.debug("StationsMap: failed loading selected path snapshot for %s: %s", target, exc)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

        def _edge_text(data: Dict[str, object], a: str, b: str) -> str:
            snr = data.get("snr")
            snr_text = ""
            if snr is not None:
                try:
                    snr_text = f"SNR {float(snr):.1f}"
                except Exception:
                    snr_text = ""
            parts = [f"{a} -> {b}", snr_text, str(data.get("band") or ""), str(data.get("source") or "")]
            return " | ".join(part for part in parts if str(part or "").strip())

        graph: Dict[str, Set[str]] = {}
        for a, b in best:
            graph.setdefault(a, set()).add(b)
            graph.setdefault(b, set()).add(a)
        shared = sorted(
            graph.get(my_call, set()) & graph.get(target, set()),
            key=lambda node: (
                float(best.get(tuple(sorted((my_call, node))), {}).get("snr") or -999),
                float(best.get(tuple(sorted((target, node))), {}).get("snr") or -999),
            ),
            reverse=True,
        )
        queue_nodes = deque([(my_call, [my_call])])
        seen = {my_call}
        relay_path: List[str] = []
        while queue_nodes:
            node, path = queue_nodes.popleft()
            if node == target:
                relay_path = path
                break
            if len(path) >= 4:
                continue
            neighbors = sorted(
                graph.get(node, set()) - seen,
                key=lambda other: (
                    float(best.get(tuple(sorted((node, other))), {}).get("snr") or -999),
                    float(best.get(tuple(sorted((node, other))), {}).get("ts") or 0.0),
                ),
                reverse=True,
            )
            for other in neighbors:
                seen.add(other)
                queue_nodes.append((other, path + [other]))
        direct_key = tuple(sorted((my_call, target)))
        direct_data = best.get(direct_key)
        relay_edges = [
            _edge_text(best.get(tuple(sorted((a, b))), {}), a, b)
            for a, b in zip(relay_path, relay_path[1:])
            if best.get(tuple(sorted((a, b))))
        ]
        result = {
            "direct": _edge_text(direct_data, my_call, target) if isinstance(direct_data, dict) else "",
            "relay": " -> ".join(relay_path) if len(relay_path) > 2 else "",
            "relay_edges": relay_edges,
            "shared": shared[:8],
            "link_count": len(best),
        }
        self._query_cache_set(cache_key, dict(result))
        return result

    def _map_message_snapshot(self, callsign: str, context: Dict[str, object]) -> Dict[str, object]:
        target = self._map_callsign_base(callsign)
        if not target:
            return {}
        try:
            age_seconds = int(context.get("age_filter_seconds") or getattr(self, "recency_seconds", 0) or 0)
        except Exception:
            age_seconds = int(getattr(self, "recency_seconds", 0) or 0)
        cache_key = ("map_message_snapshot", target, age_seconds)
        cached = self._query_cache_get(cache_key, ttl_sec=5.0)
        if isinstance(cached, dict):
            return dict(cached)
        cutoff = time.time() - age_seconds if age_seconds > 0 else 0.0
        result: Dict[str, object] = {"count": 0, "unread": 0, "newest_ts": 0.0, "source_mix": {}, "topics": []}
        source_mix: Dict[str, int] = {}
        topic_counts: Dict[str, int] = {}

        def _add_source(source: str, count: int) -> None:
            source_mix[source] = source_mix.get(source, 0) + int(count or 0)

        def _remember_ts(value: object) -> None:
            try:
                ts = float(value or 0.0)
            except Exception:
                ts = 0.0
            if ts > float(result.get("newest_ts") or 0.0):
                result["newest_ts"] = ts

        db_path = get_config_dir() / "config" / "freqinout_nets.db"
        if not db_path.exists():
            return result
        conn = None
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(utc_ts), COUNT(*), SUM(CASE WHEN read_ts IS NULL OR read_ts <= 0 THEN 1 ELSE 0 END) "
                "FROM js8_messages WHERE utc_ts >= ? AND (UPPER(from_call)=? OR UPPER(to_call)=?)",
                (cutoff, target, target),
            )
            row = cur.fetchone()
            if row and int(row[1] or 0) > 0:
                count = int(row[1] or 0)
                result["count"] = int(result.get("count") or 0) + count
                result["unread"] = int(result.get("unread") or 0) + int(row[2] or 0)
                _add_source("JS8Call", count)
                _remember_ts(row[0])
            cur.execute(
                "SELECT MAX(event_ts), COUNT(*) FROM commstat_artifacts WHERE event_ts >= ? AND UPPER(from_call)=?",
                (cutoff, target),
            )
            row = cur.fetchone()
            if row and int(row[1] or 0) > 0:
                count = int(row[1] or 0)
                result["count"] = int(result.get("count") or 0) + count
                _add_source("CommStat", count)
                _remember_ts(row[0])
            cur.execute(
                "SELECT body_text, remarks_text FROM commstat_artifacts WHERE event_ts >= ? AND UPPER(from_call)=? "
                "ORDER BY event_ts DESC LIMIT 50",
                (cutoff, target),
            )
            for body, remarks in cur.fetchall():
                try:
                    for topic in normalize_topic_terms(f"{body or ''} {remarks or ''}"):
                        topic_counts[topic] = topic_counts.get(topic, 0) + 1
                except Exception:
                    continue
        except Exception as exc:
            log.debug("StationsMap: failed loading selected message snapshot for %s: %s", target, exc)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        result["source_mix"] = source_mix
        result["topics"] = [topic for topic, _count in sorted(topic_counts.items(), key=lambda item: item[1], reverse=True)[:5]]
        self._query_cache_set(cache_key, dict(result))
        return result

    def _operator_callsign_for_map_actions(self) -> str:
        try:
            return str(self.settings.get("operator_callsign", "") or "").strip().upper().lstrip("@")
        except Exception:
            return ""

    def _map_selected_station_is_self(self, callsign: str = "") -> bool:
        selected = str(callsign or self._map_selected_station_callsign() or "").strip().upper().lstrip("@")
        operator = self._operator_callsign_for_map_actions()
        if not selected or not operator:
            return False
        try:
            selected = JS8LogLinkIndexer._base_callsign(selected)
            operator = JS8LogLinkIndexer._base_callsign(operator)
        except Exception:
            pass
        return bool(selected and operator and selected == operator)

    def _selected_station_paths_active(self, callsign: str = "") -> bool:
        target = (callsign or self._map_selected_action_callsign() or "").strip().upper()
        if not target:
            return False
        mode = str(getattr(self, "link_mode", "") or "").strip().lower()
        value = str(getattr(self, "link_value", "") or "").strip().upper()
        relay_target = str(getattr(self, "relay_target", "") or "").strip().upper()
        return bool(getattr(self, "show_link_paths", False) and (
            (mode == "relay_target" and (relay_target == target or value == target))
            or (mode == "station" and value == target)
        ))

    def _set_selected_station_path_target(self, callsign: str) -> bool:
        target = self._map_callsign_from_value(callsign)
        if not target or self._map_selected_station_is_self(target):
            return False
        self.show_link_paths = True
        self.link_mode = "relay_target"
        self.link_value = target
        self.relay_target = target
        self._paths_focus_station = target
        links_chk = getattr(self, "map_links_chk", None)
        if links_chk is not None:
            try:
                links_chk.blockSignals(True)
                links_chk.setChecked(True)
                links_chk.blockSignals(False)
            except Exception:
                pass
        relay_combo = getattr(self, "relay_target_combo", None)
        if relay_combo is not None:
            try:
                relay_combo.blockSignals(True)
                relay_idx = relay_combo.findData(target)
                if relay_idx >= 0:
                    relay_combo.setCurrentIndex(relay_idx)
                elif relay_combo.isEditable():
                    relay_combo.setEditText(target)
                relay_combo.blockSignals(False)
            except Exception:
                try:
                    relay_combo.blockSignals(False)
                except Exception:
                    pass
        self._sync_path_scope_combo(("relay_target", target))
        return True

    def _refresh_selected_paths_panel(self) -> None:
        payload = dict(getattr(self, "_map_selected_payload", {}) or {})
        body = getattr(self, "_map_selected_paths_body", None)
        if body is not None and payload:
            self._queue_map_inspector_snapshot()
        self._update_selected_paths_button_visual()

    def _update_selected_paths_button_visual(self, theme: Optional[Dict[str, str]] = None) -> None:
        btn = getattr(self, "_map_selected_paths_btn", None)
        if btn is None:
            return
        if theme is None:
            theme = self._theme_snapshot()
        if self._map_payload_is_mesh_node(getattr(self, "_map_selected_payload", {}) or {}):
            btn.setText("Hide Mesh Paths" if self.show_link_paths else "Mesh Paths")
            btn.setStyleSheet(button_style("eligible_info" if self.show_link_paths else "muted", theme))
            return
        active = self._selected_station_paths_active()
        btn.setText("Hide Paths" if active else "Show Paths To")
        btn.setStyleSheet(button_style("eligible_info" if active else "muted", theme))

    def _sync_link_mode_combo_to_off(self) -> None:
        combo = getattr(self, "link_mode_combo", None)
        if combo is None:
            self._sync_path_scope_combo(("off", ""))
            return
        try:
            idx = combo.findData(("off", ""))
            if idx < 0:
                idx = combo.findText("Off")
            if idx >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)
        except Exception:
            pass
        self._sync_path_scope_combo(("off", ""))

    def _sync_path_scope_combo(self, data: object = None) -> None:
        combo = getattr(self, "_map_path_scope_combo", None)
        if combo is None:
            return
        mode, value = self._parse_link_selection(data if data is not None else (getattr(self, "link_mode", ""), getattr(self, "link_value", "")))
        label = ""
        target_data: object = (mode or "off", value or "")
        if mode == "station":
            callsign = str(value or self._paths_focus_station or "").strip().upper()
            label = f"Selected: {callsign}" if callsign else "Selected Station"
            target_data = ("station", callsign)
        elif mode == "relay_target":
            callsign = str(value or self.relay_target or self._paths_focus_station or "").strip().upper()
            label = f"Paths To: {callsign}" if callsign else "Paths To Station"
            target_data = ("relay_target", callsign)
        elif mode in {"group", "region"}:
            label = f"{mode.title()}: {value}"
            target_data = (mode, value)
        elif mode == "all":
            label = "All"
            target_data = ("all", "")
        elif mode == "my_station":
            label = "My Station"
            target_data = ("my_station", "")
        else:
            label = "Off"
            target_data = ("off", "")
        try:
            combo.blockSignals(True)
            if mode in {"station", "relay_target", "group", "region"}:
                existing = combo.findData(target_data)
                if existing < 0:
                    # Keep station-specific path review visible without filling the main dropdown with every callsign.
                    combo.addItem(label, target_data)
                    existing = combo.findData(target_data)
                combo.setCurrentIndex(existing)
            else:
                idx = combo.findData(target_data)
                if idx < 0:
                    idx = combo.findText(label)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        except Exception:
            pass
        finally:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def _restore_path_focus_if_needed(self) -> None:
        if str(getattr(self, "_observation_focus_mode", "") or "") != "paths":
            self._paths_previous_observation_focus = None
            return
        previous_focus = getattr(self, "_paths_previous_observation_focus", None)
        if isinstance(previous_focus, tuple) and len(previous_focus) >= 2:
            self._observation_focus_enabled = bool(previous_focus[0])
            self._observation_focus_mode = str(previous_focus[1] or "")
        else:
            self._observation_focus_enabled = False
            self._observation_focus_mode = ""
        self._paths_previous_observation_focus = None

    def _remember_map_path_scope(self) -> None:
        mode = str(getattr(self, "link_mode", "off") or "off")
        if mode != "off":
            self._saved_map_path_scope = (mode, self.link_value,
                                          self.relay_target, self._paths_focus_station)

    def _restore_map_path_scope(self, default: str = "my_station") -> None:
        if str(getattr(self, "link_mode", "off") or "off") == "off":
            saved = getattr(self, "_saved_map_path_scope", None)
            self.link_mode, self.link_value, self.relay_target, self._paths_focus_station = (
                saved or (default, "", "", ""))
        self._sync_path_scope_combo((self.link_mode, self.link_value))
        combo = getattr(self, "link_mode_combo", None)
        if combo is not None:
            idx = combo.findData((self.link_mode, self.link_value))
            if idx >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)

    def _effective_path_recency_seconds(self) -> int:
        selected = getattr(self, "recency_seconds", None)
        return min(int(selected), MAP_PATH_MAX_RECENCY_SECONDS) if selected else MAP_PATH_MAX_RECENCY_SECONDS

    def _set_path_layer_off(self, *, remember: bool = True) -> None:
        if remember:
            self._remember_map_path_scope()
        else:
            self._saved_map_path_scope = None
        self.show_link_paths = False
        self.link_mode = "off"
        self.link_value = ""
        self.relay_target = ""
        self._paths_focus_station = ""
        self._restore_path_focus_if_needed()
        links_chk = getattr(self, "map_links_chk", None)
        if links_chk is not None:
            try:
                links_chk.blockSignals(True)
                links_chk.setChecked(False)
                links_chk.blockSignals(False)
            except Exception:
                pass
        self._sync_link_mode_combo_to_off()
        relay_combo = getattr(self, "relay_target_combo", None)
        if relay_combo is not None:
            try:
                relay_combo.blockSignals(True)
                if relay_combo.isEditable():
                    relay_combo.setEditText("")
                else:
                    relay_combo.setCurrentIndex(0)
                relay_combo.blockSignals(False)
            except Exception:
                pass

    def _show_paths_for_selected_station(self) -> None:
        if self._map_payload_is_mesh_node(getattr(self, "_map_selected_payload", {}) or {}):
            selected = {"mesh_nodes"} if self.show_link_paths else {"mesh_nodes", "paths"}
            self._apply_map_layer_selection(selected)
            return
        callsign = self._map_selected_action_callsign()
        if not callsign or self._map_selected_station_is_self(callsign):
            return
        try:
            turning_off = self._selected_station_paths_active(callsign)
            self.show_link_paths = not turning_off
            if turning_off:
                self._set_path_layer_off()
                reason = "selected_detail_paths_off"
            else:
                if str(getattr(self, "_observation_focus_mode", "") or "") != "paths":
                    self._paths_previous_observation_focus = (
                        bool(getattr(self, "_observation_focus_enabled", False)),
                        str(getattr(self, "_observation_focus_mode", "") or ""),
                    )
                if not self._set_selected_station_path_target(callsign):
                    return
                self._sitrep_status_only_enabled = False
                self._observation_focus_enabled = True
                self._observation_focus_mode = "paths"
                self.show_station_markers = True
                self.show_link_paths = True
                tabs = getattr(self, "_map_selected_tabs", None)
                if tabs is not None:
                    try:
                        tabs.setCurrentIndex(2)
                    except Exception:
                        pass
                reason = "selected_detail_paths"
            self._refresh_selected_paths_panel()
            self._update_map_mode_buttons()
            self._update_map_view_status_label()
            self._update_clear_filter_buttons_visual()
            self._request_map_refresh(level="medium", reason=reason)
        except Exception as exc:
            log.debug("StationsMap: failed showing paths for selected station %s: %s", callsign, exc)

    def _compose_message_for_selected_station(self) -> None:
        callsign = self._map_selected_action_callsign()
        if not callsign or self._map_selected_station_is_self(callsign):
            return
        main_window = self._application_window()
        if main_window is None or not hasattr(main_window, "open_messages_section"):
            return
        try:
            main_window.open_messages_section("compose")
        except Exception as exc:
            log.debug("StationsMap: failed opening Compose from selected station: %s", exc)
            return
        self._bring_application_window_forward()

        def _prefill_spotter_target() -> None:
            tab = getattr(main_window, "message_viewer_tab", None)
            if tab is None:
                return
            try:
                payload = dict(getattr(self, "_map_selected_payload", {}) or {})
                rows = self._map_payload_rows(payload)
                intent = {
                    "source": "map",
                    "transport": "js8",
                    "recipient_callsign": callsign,
                    "title": str(payload.get("title") or callsign),
                    "group": str(payload.get("group") or rows.get("groups") or rows.get("group") or ""),
                    "last_heard_band": str(payload.get("last_band") or payload.get("last_contact_band") or ""),
                    "last_heard_source": str(payload.get("source_family") or payload.get("source") or rows.get("source") or ""),
                    "last_heard_age_label": str(rows.get("js8 heard") or rows.get("updated") or ""),
                    "age_filter_seconds": int(getattr(self, "recency_seconds", 0) or 0),
                }
                intent = compose_intent_from_mapping(intent).as_dict()
                prefill = getattr(tab, "prefill_compose_intent", None)
                if callable(prefill):
                    prefill(intent)
                else:
                    selector = getattr(tab, "compose_mode_selector", None)
                    if selector is not None and selector.count() > 1:
                        selector.setCurrentRow(1)
                    if callsign and hasattr(tab, "compose_js8_target_edit"):
                        tab.compose_js8_target_edit.setText(callsign)
                    update = getattr(tab, "_update_compose_preview", None)
                    if callable(update):
                        update()
            except Exception as exc:
                log.debug("StationsMap: failed prefilling Spotter compose target %s: %s", callsign, exc)

        QTimer.singleShot(0, _prefill_spotter_target)

    def _compose_spotter_for_selected_station(self) -> None:
        self._compose_message_for_selected_station()

    @staticmethod
    def _canonical_map_source_family(value: object) -> str:
        family = source_family_key(value)
        if family == "JS8SPOTTER":
            return "spotter"
        if family == "COMMSTAT":
            return "commstat"
        if family == "CONDITION_ALERT":
            return "condition_alert"
        if family == "RF_PIN":
            return "rf_pin"
        raw = str(value or "").strip().lower()
        if raw in {"local", "local_report", "local report"}:
            return "local_report"
        if raw in {"mesh", "meshcore", "meshtastic", "mesh_client", "local_mesh"}:
            return raw
        if raw in {"flmsg", "flamp", "js8call", "varac", "manual", "fused", "mixed"}:
            return raw
        return "" if family == "UNKNOWN" else family.lower()

    @classmethod
    def _map_payload_source_family(cls, payload: Dict[str, object]) -> str:
        source_family = cls._canonical_map_source_family(payload.get("source_family"))
        if source_family:
            return source_family
        source = str(payload.get("source") or "").strip()
        normalized_source = cls._canonical_map_source_family(source)
        if normalized_source:
            return normalized_source
        source = source.lower()
        if "spotter" in source:
            return "spotter"
        if "commstat" in source:
            return "commstat"
        if "local" in source:
            return "local_report"
        return ""

    def _map_payload_has_message_context(self, payload: Dict[str, object]) -> bool:
        return bool(str(self._map_selected_message_context(payload).get("target") or "").strip())

    @staticmethod
    def _map_context_callsign(value: object) -> str:
        raw = StationsMapTab._map_detail_clean_text(value).strip()
        if raw.startswith("@"):
            return ""
        text = raw.lstrip("@").rstrip(">").upper()
        if re.fullmatch(r"MR\d{1,2}[A-Z]*", text):
            return ""
        if re.fullmatch(r"[A-R]{2}\d{2}(?:[A-X]{2})?", text):
            return ""
        return text if looks_like_callsign_text(text) else ""

    @staticmethod
    def _map_context_group(value: object) -> str:
        raw = StationsMapTab._map_detail_clean_text(value).strip()
        explicit_group = raw.startswith("@")
        text = raw.lstrip("@").rstrip(">")
        if not text:
            return ""
        if explicit_group:
            return text
        if re.fullmatch(r"(?i)MR\d{1,2}[A-Z]*", text):
            return text.upper()
        if looks_like_callsign_text(text):
            return ""
        return text

    @classmethod
    def _map_context_query_text(cls, values: Iterable[object], *, limit: int = 5) -> str:
        terms: List[str] = []
        seen: Set[str] = set()
        for value in values:
            if isinstance(value, (list, tuple, set)):
                nested_values = value
            else:
                nested_values = [value]
            for nested in nested_values:
                text = cls._map_detail_clean_text(nested).strip().lstrip("@").rstrip(">")
                if not text:
                    continue
                for token in re.split(r"[\s,;/|]+", text):
                    token = token.strip().lstrip("@").rstrip(">")
                    if not token:
                        continue
                    if re.fullmatch(r"(?i)MR\d{1,2}[A-Z]*", token):
                        continue
                    if re.fullmatch(r"(?i)[A-R]{2}\d{2}(?:[A-X]{2})?", token):
                        continue
                    if not looks_like_callsign_text(token):
                        continue
                    key = token.upper()
                    if key in seen:
                        continue
                    seen.add(key)
                    terms.append(key)
                    if len(terms) >= limit:
                        return " ".join(terms)
        return " ".join(terms)

    def _map_selected_message_context(self, payload: Dict[str, object]) -> Dict[str, object]:
        rows = self._map_payload_rows(payload)
        kind = str(payload.get("type") or "").strip().lower()
        source_family = self._map_payload_source_family(payload)
        active_topic = self._selected_map_topic_filter()
        topic = active_topic or str(payload.get("topic") or "").strip()
        if kind == "station":
            topic = active_topic
        summary = self._map_detail_clean_text(payload.get("summary") or "", multiline=True)
        title = self._map_detail_clean_text(payload.get("title") or "")
        group_candidates: List[object] = [payload.get("group"), rows.get("group")]
        raw_groups = payload.get("groups")
        if isinstance(raw_groups, (list, tuple, set)):
            group_candidates.extend(raw_groups)
        group = next((candidate for value in group_candidates if (candidate := self._map_context_group(value))), "")
        callsign_candidates: List[object] = [
            payload.get("callsign"),
            payload.get("call"),
            payload.get("call_label"),
            payload.get("station"),
            rows.get("callsign"),
            rows.get("call"),
            rows.get("call label"),
            rows.get("from"),
            rows.get("reporter"),
            rows.get("reports"),
            payload.get("from"),
            payload.get("from_call"),
            payload.get("to"),
            payload.get("to_call"),
            rows.get("to"),
            title,
        ]
        selected_call = next((candidate for value in callsign_candidates if (candidate := self._map_context_callsign(value))), "")
        query_filter = self._map_context_query_text(
            callsign_candidates
            + [
                payload.get("callsigns"),
                payload.get("route"),
                rows.get("route"),
                rows.get("reports"),
                rows.get("summary"),
                payload.get("state"),
                payload.get("grid"),
                rows.get("area"),
                rows.get("location"),
                summary,
            ]
        )
        if selected_call and selected_call not in query_filter.upper().split():
            query_filter = " ".join(part for part in (selected_call, query_filter) if part)
        if not topic and kind != "station":
            raw_topics = payload.get("topics")
            if isinstance(raw_topics, (list, tuple, set)) and raw_topics:
                topic = str(next(iter(raw_topics)) or "").strip()

        if kind == "regional_intelligence":
            area_type = str(payload.get("area_type") or "").strip().lower()
            state = str(payload.get("state") or "").strip().upper()
            fema_region = str(payload.get("fema_region") or "").strip().upper()
            if not area_type:
                if state:
                    area_type = "state"
                elif fema_region:
                    area_type = "fema_region"
            if not state and area_type != "national":
                state = str(fema_region or rows.get("area") or "").strip().upper()
            if state:
                state = re.split(r"[\s/|,]+", state)[0].strip().upper()
            if state == "NATIONAL":
                state = ""
            query_filter = ""
            state_filter = ""
            fema_region_filter = ""
            if area_type == "fema_region":
                fema_region_filter = fema_region
            elif area_type == "state":
                state_filter = state
                query_filter = state
            elif area_type != "national":
                query_filter = state
            return {
                "target": "messages",
                "group_filter": group,
                "topic_filter": topic,
                "query_filter": query_filter,
                "source_family": "",
                "age_filter_seconds": int(getattr(self, "recency_seconds", 0) or 0),
                "concern_only": True,
                "state_filter": state_filter,
                "fema_region_filter": fema_region_filter,
            }
        if source_family == "rf_pin":
            return {"target": ""}
        if source_family == "local_report":
            callsign = selected_call or self._map_detail_first_value(rows.get("reporter"), rows.get("from"), title)
            query = " ".join(part for part in (group, summary) if part)
            return {
                "target": "local_reports",
                "callsign": callsign.strip().upper(),
                "topic_filter": topic,
                "query": query,
            }
        if kind == "station":
            callsign = self._map_detail_first_value(
                payload.get("callsign"),
                payload.get("call"),
                payload.get("call_label"),
                title,
                rows.get("callsign"),
                rows.get("call"),
                rows.get("call label"),
                rows.get("from"),
                rows.get("reporter"),
            )
            callsign = selected_call or self._map_detail_clean_text(callsign).upper().lstrip("@").rstrip(">")
            return {
                "target": "messages",
                "group_filter": group,
                "topic_filter": topic,
                "query_filter": query_filter or callsign,
                "source_family": "",
                "age_filter_seconds": int(getattr(self, "recency_seconds", 0) or 0),
            }
        inbox_source = source_family
        source_key = inbox_source.strip().lower().replace(" ", "_")
        if source_key in {"condition_alert", "fused", "mixed", "multiple_sources"} or (kind == "report" and topic and not source_key):
            inbox_source = ""
        final_query = query_filter or selected_call
        grid_filter = self._map_detail_first_value(payload.get("grid"), rows.get("grid"))
        if kind == "report" and not final_query:
            area_query = self._map_detail_first_value(payload.get("state"), payload.get("grid"), rows.get("area"), rows.get("location"))
            final_query = re.split(r"[\s/|,]+", str(area_query or "").strip().upper())[0] if area_query else ""
        severity_text = " ".join(
            str(value or "").strip().lower()
            for value in (payload.get("severity"), rows.get("severity"), rows.get("status"), title, summary)
            if str(value or "").strip()
        )
        concern_only = kind == "report" and any(
            term in severity_text for term in ("red", "yellow", "orange", "severe", "caution", "degraded", "warning", "watch")
        )
        context = {
            "target": "messages",
            "group_filter": group,
            "topic_filter": topic,
            "query_filter": final_query,
            "source_family": inbox_source,
            "age_filter_seconds": int(getattr(self, "recency_seconds", 0) or 0),
        }
        if grid_filter:
            context["grid_filter"] = grid_filter
        if concern_only:
            context["concern_only"] = True
        return context

    def _map_selected_sop_context(self, payload: Dict[str, object]) -> Dict[str, str]:
        rows = self._map_payload_rows(payload)
        group = str(payload.get("group") or rows.get("group") or "").strip().lstrip("@")
        if not group:
            raw_groups = payload.get("groups")
            if isinstance(raw_groups, (list, tuple, set)) and raw_groups:
                group = str(next(iter(raw_groups)) or "").strip().lstrip("@")
        topic = str(payload.get("topic") or "").strip()
        if not topic:
            raw_topics = payload.get("topics")
            if isinstance(raw_topics, (list, tuple, set)) and raw_topics:
                topic = str(next(iter(raw_topics)) or "").strip()
        return {
            "group": group,
            "topic": topic,
            "source_family": self._map_payload_source_family(payload),
        }

    def _open_map_selected_messages(self) -> None:
        payload = dict(getattr(self, "_map_selected_payload", {}) or {})
        main_window = self._application_window()
        if main_window is None:
            return
        context = self._map_selected_message_context(payload)
        target = str(context.get("target") or "").strip()
        try:
            if target == "local_reports" and hasattr(main_window, "open_local_reports"):
                main_window.open_local_reports(
                    callsign=str(context.get("callsign") or ""),
                    topic_filter=str(context.get("topic_filter") or ""),
                    query=str(context.get("query") or ""),
                )
            elif target == "messages" and hasattr(main_window, "open_messages_section"):
                main_window.open_messages_section(
                    "inbox",
                    group_filter=str(context.get("group_filter") or ""),
                    topic_filter=str(context.get("topic_filter") or ""),
                    query_filter=str(context.get("query_filter") or ""),
                    source_family=str(context.get("source_family") or ""),
                    age_filter_seconds=context.get("age_filter_seconds") or 0,
                    concern_only=context.get("concern_only") or False,
                    state_filter=str(context.get("state_filter") or ""),
                    grid_filter=str(context.get("grid_filter") or ""),
                    fema_region_filter=str(context.get("fema_region_filter") or ""),
                )
            else:
                return
            self._bring_application_window_forward()
        except Exception as exc:
            log.debug("StationsMap: failed opening Messages from selection: %s", exc)

    def _open_map_selected_sop(self) -> None:
        payload = dict(getattr(self, "_map_selected_payload", {}) or {})
        main_window = self._application_window()
        if main_window is None:
            return
        try:
            context = self._map_selected_sop_context(payload)
            index_by_label = getattr(main_window, "_screen_index_by_label", {}) or {}
            idx = index_by_label.get("SOP", -1)
            if idx >= 0 and hasattr(main_window, "_set_screen"):
                main_window._set_screen(idx)
            sop_tab = getattr(main_window, "sop_tab", None)
            focus = getattr(sop_tab, "focus_traffic_context", None) if sop_tab is not None else None
            if callable(focus):
                QTimer.singleShot(
                    0,
                    lambda: focus(
                        group=str(context.get("group") or ""),
                        topic=str(context.get("topic") or ""),
                        source_family=str(context.get("source_family") or ""),
                    ),
                )
        except Exception as exc:
            log.debug("StationsMap: failed opening SOP from selection: %s", exc)

    def _add_collapsible_group(self, parent_layout: QVBoxLayout, title: str, expanded: bool) -> QVBoxLayout:
        section = QFrame(self)
        section.setFrameShape(QFrame.StyledPanel)
        section.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(6, 6, 6, 6)
        section_layout.setSpacing(6)
        header = QToolButton(section)
        header.setText(title)
        header.setCheckable(True)
        header.setChecked(bool(expanded))
        header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        header.setStyleSheet("font-weight: 600;")
        body = QWidget(section)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(6)

        def _toggle(opened: bool) -> None:
            body.setVisible(bool(opened))
            header.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)

        header.toggled.connect(_toggle)
        _toggle(bool(expanded))
        section_layout.addWidget(header)
        section_layout.addWidget(body)
        parent_layout.addWidget(section)
        return body_layout

    def _toggle_controls_drawer(self) -> None:
        if self._controls_drawer_open:
            self._set_controls_drawer_open(False)
            return
        requested = str(getattr(self, "_map_control_requested_category", "") or "Layers")
        selector = getattr(self, "_select_map_control_category", None)
        if callable(selector):
            selector(requested)
        else:
            self._set_controls_drawer_open(True)

    def _install_splitter_indicator(self) -> None:
        splitter = self._main_splitter
        if splitter is None:
            return
        try:
            handle = splitter.handle(1)
        except Exception:
            handle = None
        if handle is None:
            return
        btn = QToolButton(handle)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip("Collapse map controls")
        btn.setText("")
        btn.setAutoRaise(True)
        btn.setIconSize(btn.sizeHint())
        btn.clicked.connect(self._toggle_controls_drawer)
        btn.raise_()
        self._controls_handle_button = btn
        self._update_splitter_indicator_state()
        self._position_splitter_indicator()

    def _position_splitter_indicator(self) -> None:
        btn = self._controls_handle_button
        splitter = self._main_splitter
        if btn is None or splitter is None:
            return
        try:
            handle = splitter.handle(1)
        except Exception:
            return
        if handle is None:
            return
        w = max(10, handle.width() - 2)
        h = handle.height()
        bw = min(w, 12)
        bh = min(max(26, h // 8), 42)
        x = max(0, (handle.width() - bw) // 2)
        y = max(4, (h - bh) // 2)
        btn.setGeometry(x, y, bw, bh)
        btn.setIconSize(QSize(max(8, bw - 3), max(10, bh - 12)))

    def _update_splitter_indicator_state(self, theme: Optional[Dict[str, str]] = None) -> None:
        btn = self._controls_handle_button
        splitter = self._main_splitter
        if btn is None or splitter is None:
            return
        if theme is None:
            theme = self._theme_snapshot()
        style = self.style() if hasattr(self, "style") else None
        if style is not None:
            icon = (
                style.standardIcon(QStyle.SP_ArrowLeft)
                if self._controls_drawer_open
                else style.standardIcon(QStyle.SP_ArrowRight)
            )
            btn.setIcon(icon)
        btn.setToolTip("Collapse map controls" if self._controls_drawer_open else "Expand map controls")
        btn.setVisible(True)
        try:
            handle = splitter.handle(1)
            if handle is not None:
                handle.setCursor(Qt.SplitHCursor)
                handle.setToolTip("Drag to resize, or click chevron to show/hide map controls")
                border_color = self._hex_to_rgba(theme.get("border", "#2A313A"), 0.80)
                handle_bg = self._hex_to_rgba(theme.get("surface_alt", "#202632"), 0.55)
                button_bg = self._hex_to_rgba(theme.get("surface", "#171B21"), 0.90)
                button_hover = self._hex_to_rgba(theme.get("surface_alt", "#202632"), 0.95)
                button_border = self._hex_to_rgba(theme.get("focus", theme.get("accent", "#4C9BD3")), 0.75)
                handle.setStyleSheet(
                    "QSplitterHandle {"
                    f" background: {handle_bg};"
                    f" border-left: 1px solid {border_color};"
                    f" border-right: 1px solid {border_color};"
                    " }"
                )
                btn.setStyleSheet(
                    "QToolButton {"
                    f" background: {button_bg};"
                    f" border: 1px solid {button_border};"
                    " border-radius: 4px; padding: 1px; }"
                    f"QToolButton:hover {{ background: {button_hover}; }}"
                )
        except Exception:
            pass

    def _sync_controls_top_alignment(self) -> None:
        if self._controls_top_spacer is None or self._map_filter_bar is None:
            return
        self._controls_top_spacer.setFixedHeight(0)

    def _reflow_map_filter_bar(self) -> None:
        """Lay out principal map filters from measured available width only.

        This intentionally owns no data refresh: resize, theme, and text-scale
        changes only move already-created controls and keep Search/Clear visible.
        """
        grid = getattr(self, "_map_filter_grid", None)
        fields = tuple(getattr(self, "_map_filter_fields", ()) or ())
        role_order = {
            "view": 0,
            "topic": 1,
            "group": 2,
            "age": 3,
            "type": 4,
            "sensitivity": 5,
            "paths": 6,
        }

        def field_order(item: tuple[int, QWidget]) -> tuple[int, int]:
            index, field = item
            role = str(field.property("mapFilterRole") or field.objectName() or "").strip().lower()
            return role_order.get(role, len(role_order) + index), index

        visible_fields = tuple(
            field
            for _index, field in sorted(
                ((index, field) for index, field in enumerate(fields) if not field.isHidden()),
                key=field_order,
            )
        )
        search = getattr(self, "_map_search_field", None)
        clear = getattr(self, "_map_clear_filters_button", None)
        clear_layers = getattr(self, "_map_clear_layers_button", None)
        reachable = getattr(self, "_now_reachable_label", None)
        if grid is None or not visible_fields or search is None or clear is None or clear_layers is None:
            return
        bar = getattr(self, "_map_filter_bar", None)
        available = int(bar.width() if bar is not None else self.width())
        if available <= 1:
            return
        # Derive responsive breakpoints from the controls' live font/content
        # widths instead of assuming a particular desktop scale.
        field_width = max(
            max(
                int(field.minimumWidth() or 0),
                int(field.minimumSizeHint().width() or 0),
                int(field.sizeHint().width() or 0),
            )
            for field in visible_fields
        )
        field_width = max(
            field_width,
            int(clear.sizeHint().width() or 0),
            int(clear_layers.sizeHint().width() or 0),
        )
        spacing = max(0, int(grid.horizontalSpacing() or 0))
        columns = 4 if available >= (field_width * 4) + (spacing * 3) else (
            2 if available >= (field_width * 2) + spacing else 1
        )
        layout_signature = (columns, tuple(id(field) for field in visible_fields))
        previous_columns = getattr(self, "_map_filter_columns", None)
        if layout_signature == getattr(self, "_map_filter_layout_signature", None):
            return
        self._map_filter_columns = columns
        self._map_filter_layout_signature = layout_signature
        while grid.count():
            grid.takeAt(0)
        if previous_columns is not None:
            for column in range(int(grid.columnCount())):
                grid.setColumnStretch(column, 0)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        for index, field in enumerate(visible_fields):
            grid.addWidget(field, index // columns, index % columns)
        action_row = (len(visible_fields) + columns - 1) // columns
        if columns == 1:
            grid.addWidget(search, action_row, 0)
            grid.addWidget(clear, action_row + 1, 0)
            grid.addWidget(clear_layers, action_row + 2, 0)
            info_row = action_row + 3
        elif columns == 2:
            grid.addWidget(search, action_row, 0, 1, 2)
            grid.addWidget(clear, action_row + 1, 0, alignment=Qt.AlignBottom)
            grid.addWidget(clear_layers, action_row + 1, 1, alignment=Qt.AlignBottom)
            info_row = action_row + 2
        else:
            grid.addWidget(search, action_row, 0, 1, 2)
            grid.addWidget(clear, action_row, 2, alignment=Qt.AlignBottom)
            grid.addWidget(clear_layers, action_row, 3, alignment=Qt.AlignBottom)
            info_row = action_row + 1
        grid.addWidget(reachable, info_row, 0, 1, columns, alignment=Qt.AlignLeft)

    def _open_map_tool_category(self, name, *, toggle=False) -> None:
        names=tuple(self._map_control_rail_buttons)
        if name not in names:
            return
        active=getattr(self,"_map_control_requested_category","")
        scroll=self._controls_scroll.verticalScrollBar()
        positions=getattr(self,"_map_tool_scroll_positions",{})
        if active and self._controls_drawer_open:
            positions[active]=scroll.value()
        self._map_tool_scroll_positions=positions
        if (toggle and active==name and getattr(self,"_controls_requested_open",False)
                and not getattr(self,"_map_controls_suspended_for_inspector",False)):
            self._set_controls_drawer_open(False)
            return
        if getattr(self,"_map_controls_suspended_for_inspector",False):
            self._map_selected_panel.hide()
        self._map_control_focus_widget=self._map_control_rail_buttons[name]
        self._map_control_requested_category=name
        self._map_control_category_stack.setCurrentIndex(names.index(name))
        self._set_controls_drawer_open(True)
        def restore():
            if (not getattr(self,"_is_shutting_down",False) and self.isVisible()
                    and self._controls_drawer_open and self._map_control_requested_category==name):
                scroll.setValue(positions.get(name,0))
        QTimer.singleShot(0,restore)

    def _close_active_map_pane(self) -> None:
        panel = getattr(self,"_map_selected_panel",None)
        if panel is not None and not panel.isHidden():
            self._clear_map_selected_detail()
        else:
            self._set_controls_drawer_open(False)

    def _set_controls_drawer_open(self, open_drawer: bool) -> None:
        self._controls_requested_open = bool(open_drawer)
        if open_drawer and not getattr(self, "_map_control_requested_category", ""):
            self._map_control_requested_category = "Layers"
        StationsMapTab._coordinate_map_panels(self)
        if not open_drawer:
            focus = getattr(self, "_map_control_focus_widget", None)
            if focus is not None:
                try:
                    focus.setFocus()
                except RuntimeError:
                    pass

    def _coordinate_map_panels(self) -> None:
        """One cache-only owner for intent, effective visibility and inspector fit."""
        main = getattr(self, "_main_splitter", None)
        if main is None:
            return
        rail = getattr(self, "_map_control_rail", None)
        rail_width = max(76, rail.sizeHint().width() if rail is not None else 76)
        total = max(1, main.contentsRect().width())
        fm = self.fontMetrics() if hasattr(self, "fontMetrics") else None
        unit = fm.horizontalAdvance("M") if fm else 10
        panel_width = max(300, min(400, unit * 28))
        inspector = getattr(self, "_map_selected_panel", None)
        inspector_visible = bool(inspector is not None and not inspector.isHidden())
        # Inspector fit is independent of the three-pane tools breakpoint.
        # Hide tools before moving the selection below an otherwise useful map.
        inspector_min = max(280, inspector.minimumSizeHint().width() if inspector is not None else 280)
        inspector_width = max(inspector_min, min(460, unit * 24))
        map_min = max(280, unit * 24)
        suspended = bool(inspector_visible and
                         total < rail_width + panel_width + inspector_width + map_min + 36)
        requested = bool(getattr(self, "_controls_requested_open", getattr(self, "_controls_drawer_open", False)))
        effective = requested and not suspended
        previous_effective = bool(getattr(self,"_controls_drawer_open",False))
        scroll_owner = getattr(self,"_controls_scroll",None)
        positions = getattr(self,"_map_tool_scroll_positions",{})
        remembered_category = str(getattr(self,"_map_control_requested_category","") or "")
        if previous_effective and not effective and hasattr(scroll_owner,"verticalScrollBar"):
            positions[remembered_category] = scroll_owner.verticalScrollBar().value()
            self._map_tool_scroll_positions = positions
        self._map_controls_suspended_for_inspector = suspended
        self._controls_drawer_open = effective
        category = str(getattr(self, "_map_control_requested_category", "Layers") or "Layers")
        for name, button in getattr(self, "_map_control_rail_buttons", {}).items():
            selected = effective and name == category
            button.blockSignals(True)
            button.setChecked(selected)
            button.blockSignals(False)
            button.setAccessibleDescription("Expanded" if selected else ("Temporarily closed while selection is open" if suspended and name == category else "Collapsed"))
            button.setStyleSheet(button_style("eligible_info" if selected else "muted", self._theme_snapshot()))
        stack = getattr(self, "_map_control_category_stack", None)
        if stack is not None:
            stack.setVisible(effective)
        scroll = getattr(self, "_controls_scroll", None)
        if scroll is not None:
            scroll.setMinimumWidth(panel_width if effective else 0)
            scroll.setMaximumWidth(max(panel_width, 520) if effective else 0)
            scroll.setVisible(effective)
        signature = (effective, panel_width, rail_width, total)
        if signature != getattr(self, "_map_pane_layout_signature", None):
            self._map_pane_layout_signature = signature
            tools = rail_width + (panel_width + 6 if effective else 0)
            main.setSizes([tools, max(1,total-tools)])
        if effective and not previous_effective and hasattr(scroll_owner,"verticalScrollBar"):
            def restore_scroll():
                if (not getattr(self,"_is_shutting_down",False)
                        and getattr(self,"_controls_drawer_open",False)
                        and getattr(self,"_map_control_requested_category","")==remembered_category):
                    scroll_owner.verticalScrollBar().setValue(positions.get(remembered_category,0))
            QTimer.singleShot(0,restore_scroll)
        button = getattr(self, "_controls_button", None)
        if button is not None:
            button.setVisible(False)
        canvas = getattr(self, "_map_canvas_splitter", None)
        if canvas is not None and inspector is not None:
            compact = canvas.contentsRect().width() < inspector_width + map_min + canvas.handleWidth()
            orientation = Qt.Vertical if compact else Qt.Horizontal
            canvas.setOrientation(orientation)
            inspector.setMinimumWidth(0 if compact else inspector_min)
            inspector.setMaximumWidth(16777215 if compact else inspector_width)
            dimension = canvas.height() if compact else canvas.width()
            details = getattr(self, "_map_selected_details_button", None)
            expanded = bool(details and details.isChecked())
            side = (max(inspector.minimumSizeHint().height(), int(dimension * 0.55)) if expanded
                    else inspector.minimumSizeHint().height()) if compact else inspector_width
            if not inspector_visible:
                side = 0
            sig = (inspector_visible,compact,dimension,side,expanded)
            if sig != getattr(self,"_map_canvas_layout_signature",None):
                self._map_canvas_layout_signature = sig
                canvas.setSizes([max(1,dimension-side),side])
        update = getattr(self,"_update_splitter_indicator_state",None)
        if callable(update):
            update()

    def _update_drawer_mode(self) -> None:
        self._drawer_mode = self.width() < self._controls_drawer_threshold
        self._sync_controls_top_alignment()
        StationsMapTab._coordinate_map_panels(self)

    def _schedule_map_geometry_reflow(self) -> None:
        if self._is_shutting_down:
            return
        timer = getattr(self, "_map_geometry_timer", None)
        if isinstance(timer, QTimer):
            timer.start(0)
            return
        self._flush_map_geometry_reflow()

    def _flush_map_geometry_reflow(self) -> None:
        if self._is_shutting_down or self.width() <= 1 or self.height() <= 1:
            return
        self._reflow_map_filter_bar()
        self._update_drawer_mode()
        self._sync_map_canvas_splitter()

    def on_first_visible_layout_ready(self) -> None:
        """Settle responsive geometry after the page has its real stack viewport."""
        self._flush_map_geometry_reflow()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Splitter and grid writes can cause another resize while the native
        # scene settles. Coalesce the storm into one idempotent geometry pass.
        self._schedule_map_geometry_reflow()

    def _sync_city_pop_enabled(self) -> None:
        enabled = bool(self.show_cities or self.show_states)
        self.city_pop_combo.setEnabled(enabled)

    def _query_cache_get(self, key: tuple, ttl_sec: Optional[float] = None):
        ttl = self._query_cache_ttl_sec if ttl_sec is None else max(0.0, float(ttl_sec))
        item = self._query_cache.get(key)
        if not item:
            return None
        ts, value = item
        if (time.time() - float(ts)) > ttl:
            self._query_cache.pop(key, None)
            return None
        return value

    def _query_cache_set(self, key: tuple, value: Any) -> None:
        self._query_cache[key] = (time.time(), value)
        # Keep cache bounded.
        if len(self._query_cache) > 200:
            try:
                oldest_key = min(self._query_cache.items(), key=lambda kv: kv[1][0])[0]
                self._query_cache.pop(oldest_key, None)
            except Exception:
                pass

    def _clear_report_query_caches(self) -> None:
        """Clear map/report caches after user-visible report inputs change."""
        self._map_read_cache_revision = getattr(self, "_map_read_cache_revision", 0) + 1
        try:
            self._map_query_cache.clear()
        except Exception:
            pass
        try:
            self._query_cache.clear()
        except Exception:
            pass

    def _load_operator_activity_summary(self) -> Dict[str, Dict[str, object]]:
        cache_key = ("operator_activity_summary",)
        cached = self._query_cache_get(cache_key, ttl_sec=2.0)
        if isinstance(cached, dict):
            return {str(k): (dict(v) if isinstance(v, dict) else {}) for k, v in cached.items()}
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception as e:
            log.debug("StationsMap: failed to resolve operator activity DB path: %s", e)
            return {}
        if not db_path.exists():
            return {}
        conn = None
        try:
            conn = connect_sqlite_readonly(db_path)
            summary = load_operator_activity_summary(conn)
        except Exception as e:
            log.debug("StationsMap: failed to load operator activity summary: %s", e)
            return {}
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        self._query_cache_set(cache_key, {k: dict(v) for k, v in summary.items()})
        return summary

    def _load_js8_direct_contact_summary(self, my_call: str) -> Dict[str, Dict[str, object]]:
        my_call = (my_call or "").strip().upper()
        if not my_call:
            return {}
        cache_key = ("js8_direct_contact_summary", my_call)
        cached = self._query_cache_get(cache_key, ttl_sec=2.0)
        if isinstance(cached, dict):
            return {str(k): (dict(v) if isinstance(v, dict) else {}) for k, v in cached.items()}
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception as e:
            log.debug("StationsMap: failed to resolve direct-contact DB path: %s", e)
            return {}
        if not db_path.exists():
            return {}
        conn = None
        try:
            conn = connect_sqlite_readonly(db_path)
            summary = load_js8_direct_contact_summary(conn, my_call)
        except Exception as e:
            log.debug("StationsMap: failed to load JS8 direct-contact summary: %s", e)
            return {}
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        self._query_cache_set(cache_key, {k: dict(v) for k, v in summary.items()})
        return summary

    # ------------- Data helpers ------------- #
    def _load_operator_history(self):
        """
        Load operator_checkins (callsign, name, state, grid, group1-3) and plot as stations.
        Grid is preferred; if missing, fall back to state centroid when available.
        """
        perf_start = time.perf_counter()
        pts: List[StationPoint] = []
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception as e:
            log.error("StationsMap: failed to resolve DB path: %s", e)
            self._set_station_points(pts)
            emit_span(
                "map.load_operator_history",
                (time.perf_counter() - perf_start) * 1000.0,
                settings=self.settings,
                meta={"stations": 0, "status": "resolve_db_failed"},
                min_ms=5.0,
            )
            return
        if not db_path.exists():
            self._set_station_points(pts)
            emit_span(
                "map.load_operator_history",
                (time.perf_counter() - perf_start) * 1000.0,
                settings=self.settings,
                meta={"stations": 0, "status": "db_missing"},
                min_ms=5.0,
            )
            return
        raw_rows = []
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    callsign,
                    IFNULL(name,''),
                    IFNULL(state,''),
                    IFNULL(grid,''),
                    IFNULL(group1,''),
                    IFNULL(group2,''),
                    IFNULL(group3,''),
                    groups_json,
                    COALESCE(trusted,0)
                FROM operator_checkins
                ORDER BY callsign COLLATE NOCASE
                """
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log.error("StationsMap: failed to load operator history: %s", e)
            emit_span(
                "map.load_operator_history",
                (time.perf_counter() - perf_start) * 1000.0,
                settings=self.settings,
                meta={"stations": 0, "status": "query_failed"},
                min_ms=5.0,
            )
            raise

        my_call = (self.settings.get("operator_callsign", "") or "").strip().upper()
        my_grid = (self.settings.get("operator_grid6", "") or "").strip().upper()

        for cs, name, state, grid, g1, g2, g3, gj, trusted in rows:
            cs = (cs or "").strip().upper()
            if not cs:
                continue
            grid = (grid or "").strip().upper()
            if my_call and cs == my_call and my_grid:
                grid = my_grid
            state = (state or "").strip().upper()
            state_abbr = state
            if state_abbr and len(state_abbr) > 2:
                if state_abbr in US_STATE_ABBR_FROM_NAME:
                    state_abbr = US_STATE_ABBR_FROM_NAME[state_abbr]
                elif state_abbr in CANADA_PROV_ABBR_FROM_NAME:
                    state_abbr = CANADA_PROV_ABBR_FROM_NAME[state_abbr]
            latlon = None
            placement = ""
            grid4 = grid[:4] if len(grid) >= 4 else ""
            if len(grid) >= 6:
                latlon = maidenhead_to_latlon(grid)
                placement = "grid6"
            if not latlon and grid4 and state_abbr:
                bounds = maidenhead_grid4_bounds(grid4)
                state_center = STATE_CENTERS.get(state_abbr)
                if bounds and state_center:
                    lat = min(max(state_center[0], bounds[0]), bounds[2])
                    lon = min(max(state_center[1], bounds[1]), bounds[3])
                    latlon = (lat, lon)
                    placement = "state+grid4"
            if not latlon and state_abbr:
                latlon = STATE_CENTERS.get(state_abbr)
                placement = "state"
            if not latlon and grid4:
                latlon = maidenhead_to_latlon(grid4)
                placement = "grid4"
            if not latlon:
                continue
            lat, lon = latlon
            if not self._is_usa_canada(lat, lon):
                continue
            log.debug(
                "StationsMap: station placement %s cs=%s state=%s grid=%s",
                placement or "unknown",
                cs,
                state_abbr or state,
                grid,
            )
            groups: List[str] = []
            try:
                if gj:
                    parsed = json.loads(gj)
                    if isinstance(parsed, list):
                        groups = [str(x) for x in parsed if str(x).strip()]
            except Exception:
                groups = []
            if not groups:
                groups = [g for g in (g1, g2, g3) if g]
            group = groups[0] if groups else ""
            pts.append(
                StationPoint(
                    callsign=cs,
                    grid=grid,
                    name=(name or "").strip(),
                    state=state_abbr or state,
                    group=(group or "").strip(),
                    groups=groups,
                    trusted=bool(trusted),
                    lat=lat,
                    lon=lon,
                )
            )

        self._set_station_points(pts)
        # store raw operator rows for path filters
        op_rows = []
        for r in rows:
            cs_val = (r[0] or "").strip()
            if not cs_val:
                continue
            parsed_groups: List[str] = []
            try:
                if len(r) > 7 and r[7]:
                    maybe = json.loads(r[7])
                    if isinstance(maybe, list):
                        parsed_groups = [str(g) for g in maybe if str(g).strip()]
            except Exception:
                parsed_groups = []
            op_rows.append(
                {
                    "callsign": cs_val.upper(),
                    "name": (r[1] or "").strip(),
                    "state": (r[2] or "").strip().upper(),
                    "group1": (r[4] or "").strip(),
                    "group2": (r[5] or "").strip(),
                    "group3": (r[6] or "").strip(),
                    "groups": parsed_groups,
                }
            )
        self.operator_rows = op_rows
        self._rebuild_operator_index()
        if hasattr(self, "link_mode_combo"):
            self._refresh_link_mode_options()
        if hasattr(self, "group_filter_combo"):
            self._refresh_group_filter_options()
        if hasattr(self, "region_filter_combo"):
            self._refresh_region_filter_options()
        if hasattr(self, "relay_target_combo"):
            self._refresh_relay_targets()
        if hasattr(self, "prop_target_type_combo") and self.prop_target_type_combo is not None:
            self._refresh_prop_target_controls()
        if self._now_reachable_enabled:
            self._now_reachable_meta = self._compute_now_reachable_snapshot()
            self._now_reachable_callsigns = set(self._now_reachable_meta.keys())
            self._update_now_reachable_summary()
        emit_span(
            "map.load_operator_history",
            (time.perf_counter() - perf_start) * 1000.0,
            settings=self.settings,
            meta={"stations": len(self.stations), "rows": len(rows)},
            min_ms=5.0,
        )

    def update_stations(self, stations: List[Dict]):
        pts: List[StationPoint] = []
        for s in stations:
            cs = (s.get("callsign") or "").strip().upper()
            grid = (s.get("grid") or "").strip().upper()
            heard_by = (s.get("heard_by") or "").strip().upper() or None
            if not cs or not grid:
                continue
            ll = maidenhead_to_latlon(grid)
            if not ll:
                continue
            lat, lon = ll
            if not self._is_usa_canada(lat, lon):
                continue
            pts.append(StationPoint(callsign=cs, grid=grid, heard_by=heard_by, lat=lat, lon=lon))
        self._set_station_points(pts)
        self._request_map_refresh(level="full", reason="operator_history_loaded")

    def _daily_schedule_freqs(self) -> List[float]:
        """
        Return unique list of frequencies (MHz) from daily_schedule_tab if present.
        """
        freqs: List[float] = []
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return freqs
        if not db_path.exists():
            return freqs
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_schedule_tab'")
            if not cur.fetchone():
                conn.close()
                return freqs
            cur.execute("SELECT DISTINCT frequency FROM daily_schedule_tab WHERE frequency IS NOT NULL")
            for (f,) in cur.fetchall():
                try:
                    freqs.append(round(float(f), 3))
                except Exception:
                    continue
            conn.close()
        except Exception as e:
            log.warning("StationsMap: failed reading daily_schedule_tab: %s", e)
        return sorted({v for v in freqs if v})

    def _refresh_band_options(self):
        current = self.band_combo.currentData()
        current_text = self.band_combo.currentText()
        priority = ["20M", "40M", "80M"]
        remaining = ["160M", "60M", "30M", "17M", "15M", "12M", "10M", "6M", "2M"]
        bands = ["All"] + priority + remaining
        self.band_combo.blockSignals(True)
        self.band_combo.clear()
        self.band_combo.addItem("All", {"type": "all"})
        for b in bands[1:]:
            self.band_combo.addItem(b, {"type": "band", "value": b})
        for freq in getattr(self, "_map_schedule_freqs", []):
            label = f"{freq:.3f} MHz"
            self.band_combo.addItem(label, {"type": "freq", "value": freq})
        idx = self.band_combo.findData(current) if current else 0
        if idx < 0 and current:
            self.band_combo.addItem(current_text, current)
            idx = self.band_combo.count() - 1
        self.band_combo.setCurrentIndex(max(0, idx))
        self.band_combo.blockSignals(False)

    def _rebuild_operator_index(self):
        """
        Build a quick lookup for operator metadata (state, groups, FEMA region).
        """
        idx: Dict[str, Dict] = {}
        groups: Set[str] = set()
        for r in self.operator_rows:
            cs = (r.get("callsign") or "").upper()
            if not cs:
                continue
            state_raw = (r.get("state") or "").upper()
            state_abbr = state_raw
            if state_abbr and len(state_abbr) > 2:
                if state_abbr in US_STATE_ABBR_FROM_NAME:
                    state_abbr = US_STATE_ABBR_FROM_NAME[state_abbr]
                elif state_abbr in CANADA_PROV_ABBR_FROM_NAME:
                    state_abbr = CANADA_PROV_ABBR_FROM_NAME[state_abbr]
            region = STATE_TO_FEMA_REGION.get(state_abbr)
            group_set = {
                str(g).strip().upper()
                for g in (
                    r.get("group1") or "",
                    r.get("group2") or "",
                    r.get("group3") or "",
                    *(r.get("groups") or []),
                )
                if str(g).strip()
            }
            idx[cs] = {"state": state_abbr, "region": region, "groups": group_set}
            groups.update(group_set)
        self.operator_index = idx
        self._operator_groups = sorted(groups)
        self._operator_regions = sorted({v.get("region") for v in idx.values() if v.get("region")})

    def _marker_station_matches_filters(
        self,
        callsign: str,
        *,
        group_filter: str = "",
        region_filter: str = "",
        my_call: str = "",
        allow_self: bool = False,
    ) -> bool:
        cs = (callsign or "").strip().upper()
        if not cs:
            return False
        if allow_self and my_call and cs == my_call:
            return True
        meta = self.operator_index.get(cs, {})
        group_key = StationsMapTab._normalize_map_group_value(group_filter)
        if group_key:
            groups = {
                StationsMapTab._normalize_map_group_value(g)
                for g in (meta.get("groups") or set())
                if str(g).strip()
            }
            if not StationsMapTab._map_values_match_group_filter(sorted(groups), group_key):
                return False
        region_key = (region_filter or "").strip().upper()
        if region_key and str(meta.get("region") or "").strip().upper() != region_key:
            return False
        return True

    def _refresh_group_filter_options(self):
        current = self.group_filter_combo.currentData() if hasattr(self, "group_filter_combo") else None
        group_values = sorted({str(g).strip().upper() for g in (self._operator_groups + self._sitrep_report_groups) if str(g).strip()})
        self.group_filter_combo.blockSignals(True)
        self.group_filter_combo.clear()
        self.group_filter_combo.addItem("All", "")
        for g in group_values:
            self.group_filter_combo.addItem(g, g)
        if current and self.group_filter_combo.findData(current) < 0:
            self.group_filter_combo.addItem(str(current), current)
        if current and self.group_filter_combo.findData(current) >= 0:
            self.group_filter_combo.setCurrentIndex(self.group_filter_combo.findData(current))
        else:
            self.group_filter_combo.setCurrentIndex(0)
        self.group_filter_combo.blockSignals(False)

    def _refresh_region_filter_options(self):
        current = self.region_filter_combo.currentData() if hasattr(self, "region_filter_combo") else None
        self.region_filter_combo.blockSignals(True)
        self.region_filter_combo.clear()
        self.region_filter_combo.addItem("All", "")
        for reg in self._operator_regions:
            self.region_filter_combo.addItem(f"Region {reg}", reg)
        if current and self.region_filter_combo.findData(current) < 0:
            self.region_filter_combo.addItem(str(current), current)
        if current and self.region_filter_combo.findData(current) >= 0:
            self.region_filter_combo.setCurrentIndex(self.region_filter_combo.findData(current))
        else:
            self.region_filter_combo.setCurrentIndex(0)
        self.region_filter_combo.blockSignals(False)

    def _refresh_link_mode_options(self):
        current_data = (self.link_mode, self.link_value)
        self.link_mode_combo.blockSignals(True)
        self.link_mode_combo.clear()
        self.link_mode_combo.addItem("Off", ("off", ""))
        self.link_mode_combo.addItem("My Station", ("my_station", ""))
        self.link_mode_combo.addItem("All", ("all", ""))
        restore_idx = self.link_mode_combo.findData(current_data) if current_data else -1
        if restore_idx >= 0:
            self.link_mode_combo.setCurrentIndex(restore_idx)
        else:
            self.link_mode_combo.setCurrentIndex(max(0, self.link_mode_combo.findData(("off", ""))))
        self.link_mode_combo.blockSignals(False)
        self._sync_path_scope_combo((self.link_mode, self.link_value))

    def _ensure_map_link_mode(self, preferred_mode: str = "my_station") -> None:
        """Keep path/link review usable when a map focus expects HF traffic links."""
        combo = getattr(self, "link_mode_combo", None)
        if combo is None:
            self.link_mode = preferred_mode
            self.link_value = ""
            self._sync_path_scope_combo((self.link_mode, self.link_value))
            return
        current_mode, current_value = self.link_mode, self.link_value
        if current_mode and current_mode != "off":
            self.link_mode = current_mode
            self.link_value = current_value
            self._sync_path_scope_combo((self.link_mode, self.link_value))
            return
        idx = combo.findData((preferred_mode, ""))
        if idx < 0:
            idx = combo.findText("My Station" if preferred_mode == "my_station" else "All")
        if idx >= 0:
            try:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
            finally:
                combo.blockSignals(False)
        self.link_mode, self.link_value = self._parse_link_selection(combo.currentData())
        self._sync_path_scope_combo((self.link_mode, self.link_value))

    def _refresh_relay_targets(self):
        current_text = self.relay_target_combo.currentText() if hasattr(self, "relay_target_combo") else ""
        current_call = self._relay_target_callsign_from_text(current_text)
        entries: list[tuple[str, str, str, str]] = []
        for row in self.operator_rows:
            cs = (row.get("callsign") or "").strip().upper()
            if not cs:
                continue
            if self._now_reachable_enabled and self._now_reachable_callsigns and cs not in self._now_reachable_callsigns:
                continue
            name = (row.get("name") or "").strip()
            state = self._normalize_state_abbr(row.get("state") or "")
            groups = row.get("groups") if isinstance(row.get("groups"), list) else []
            if not groups:
                groups = [
                    (row.get("group1") or "").strip(),
                    (row.get("group2") or "").strip(),
                    (row.get("group3") or "").strip(),
                ]
            group_list = [g for g in groups if g]
            group_text = ", ".join(group_list[:3]) if group_list else "--"
            entries.append((cs, name, state, group_text))
        entries = sorted(entries, key=lambda it: (it[0], it[1], it[2], it[3]))
        self.relay_target_combo.blockSignals(True)
        self.relay_target_combo.clear()
        self.relay_target_combo.addItem("")
        for cs, name, state, group_text in entries:
            name_txt = name if name else "--"
            state_txt = state if state else "--"
            status = ""
            if self._now_reachable_enabled:
                meta = self._now_reachable_meta.get(cs, {})
                qsy_text = str(meta.get("qsy_text") or "").strip()
                if qsy_text:
                    status = f" | {qsy_text}"
            label = f"{cs} | {name_txt} | {state_txt} | {group_text}{status}"
            self.relay_target_combo.addItem(label, cs)
        if current_call:
            idx = self.relay_target_combo.findData(current_call)
            if idx >= 0:
                self.relay_target_combo.setCurrentIndex(idx)
            elif current_text:
                self.relay_target_combo.setEditText(current_text)
        elif current_text:
                self.relay_target_combo.setEditText(current_text)
        self.relay_target_combo.blockSignals(False)

    def _parse_frequency_mhz(self, value) -> Optional[float]:
        try:
            txt = str(value).strip()
            if not txt:
                return None
            match = re.search(r"[-+]?\d+(?:[.,]\d+)?", txt)
            if not match:
                return None
            return float(match.group(0).replace(",", "."))
        except Exception:
            return None

    @staticmethod
    def _day_to_index(day_val: str) -> Optional[int]:
        txt = (day_val or "").strip().lower()
        if not txt:
            return None
        names = [
            "sunday",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
        ]
        for idx, name in enumerate(names):
            if txt.startswith(name[:3]) or name.startswith(txt[:3]):
                return idx
        return None

    def _schedule_day_indices(self, day_val: str) -> List[int]:
        txt = (day_val or "ALL").strip().upper()
        if txt in {"", "ALL", "DAILY"}:
            return list(range(7))
        parts = re.split(r"[,/;|]+", txt)
        out: List[int] = []
        for part in parts:
            idx = self._day_to_index(part)
            if idx is not None and idx not in out:
                out.append(idx)
        return out

    def _schedule_active_now(
        self,
        day_val: str,
        today_name: str,
        yesterday_name: str,
        now_min: int,
        start_min: int,
        end_min: int,
    ) -> bool:
        """
        Backward-compatible helper for tests and legacy callers.
        """
        if start_min < 0 or start_min > 1439 or end_min < 0 or end_min > 1439:
            return False
        if start_min == end_min:
            return False
        day_indices = self._schedule_day_indices(day_val)
        if not day_indices:
            return False
        today_idx = self._day_to_index(today_name)
        yesterday_idx = self._day_to_index(yesterday_name)
        if today_idx is None:
            return False
        if start_min < end_min:
            return today_idx in day_indices and start_min <= now_min < end_min
        if today_idx in day_indices and now_min >= start_min:
            return True
        return yesterday_idx in day_indices and now_min < end_min if yesterday_idx is not None else False

    def _minutes_until_end(self, now_min: int, start_min: int, end_min: int) -> Optional[int]:
        """
        Backward-compatible helper for tests and legacy callers.
        """
        if start_min < 0 or start_min > 1439 or end_min < 0 or end_min > 1439:
            return None
        if start_min == end_min:
            return None
        if start_min < end_min:
            if start_min <= now_min < end_min:
                return end_min - now_min
            return None
        if now_min >= start_min:
            return (24 * 60 - now_min) + end_min
        if now_min < end_min:
            return end_min - now_min
        return None

    def _schedule_minutes_to_end(
        self,
        day_val: str,
        now_utc: datetime.datetime,
        start_min: int,
        end_min: int,
    ) -> Optional[int]:
        if start_min < 0 or start_min > 1439 or end_min < 0 or end_min > 1439:
            return None
        if start_min == end_min:
            return None
        day_indices = self._schedule_day_indices(day_val)
        if not day_indices:
            return None
        now_day_idx = (now_utc.weekday() + 1) % 7  # Sunday=0
        now_min = now_utc.hour * 60 + now_utc.minute
        candidates: List[int] = []
        for day_idx in day_indices:
            if start_min < end_min:
                if day_idx == now_day_idx and start_min <= now_min < end_min:
                    candidates.append(end_min - now_min)
                continue
            # Overnight interval
            if day_idx == now_day_idx and now_min >= start_min:
                candidates.append((24 * 60 - now_min) + end_min)
            if ((day_idx + 1) % 7) == now_day_idx and now_min < end_min:
                candidates.append(end_min - now_min)
        if not candidates:
            return None
        return max(0, min(candidates))

    def _load_my_active_schedule_freqs(self, now_utc: datetime.datetime) -> Set[float]:
        out: Set[float] = set()
        try:
            nets_db_path = get_config_dir() / "config" / "freqinout_nets.db"
            settings_db_path = get_config_dir() / "config" / "freqinout.db"
        except Exception:
            return out

        def _add_rows(db_path: Path, table_name: str) -> None:
            if not db_path.exists():
                return
            try:
                conn = connect_sqlite_readonly(db_path)
                cur = conn.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                )
                if not cur.fetchone():
                    conn.close()
                    return
                cur.execute(
                    f"SELECT day_utc, start_utc, end_utc, frequency FROM {table_name}"
                )
                for day_utc, start_utc, end_utc, frequency in cur.fetchall():
                    start_min = self._parse_hhmm_minutes(start_utc)
                    end_min = self._parse_hhmm_minutes(end_utc)
                    if start_min is None or end_min is None:
                        continue
                    minutes_to_end = self._schedule_minutes_to_end(
                        str(day_utc or "ALL"),
                        now_utc,
                        start_min,
                        end_min,
                    )
                    if minutes_to_end is None:
                        continue
                    freq_mhz = self._parse_frequency_mhz(frequency)
                    if freq_mhz is None:
                        continue
                    out.add(round(freq_mhz, 6))
                conn.close()
            except Exception:
                pass

        try:
            _add_rows(settings_db_path, "daily_schedule_tab")
            _add_rows(nets_db_path, "daily_schedule_tab")
            _add_rows(nets_db_path, "net_schedule_tab")
            _add_rows(settings_db_path, "net_schedule_tab")
        except Exception:
            return out
        return out

    def _current_scheduler_frequency_for_map(self):
        # Cached scheduler entry only; capture on the owner thread before reads.
        return current_scheduler_freq(self._application_window())

    def _compute_now_reachable_snapshot(self) -> Dict[str, Dict]:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        active_freqs = self._load_my_active_schedule_freqs(now_utc)
        try:
            sched_freq = self._current_scheduler_frequency_for_map()
            sched_freq_mhz = self._parse_frequency_mhz(sched_freq)
            if sched_freq_mhz is not None:
                active_freqs.add(round(sched_freq_mhz, 6))
        except Exception:
            pass
        if not active_freqs:
            return {}
        active_peer = self._load_peer_schedule_presence(now_utc)
        out: Dict[str, Dict] = {}
        for entry in active_peer:
            callsign = (entry.get("callsign") or "").strip().upper()
            if not callsign:
                continue
            freq_mhz = self._parse_frequency_mhz(entry.get("frequency"))
            if freq_mhz is None:
                continue
            if not any(abs(freq_mhz - mine) <= 0.001 for mine in active_freqs):
                continue
            minutes_to_end = entry.get("minutes_to_end")
            try:
                mins = int(minutes_to_end) if minutes_to_end is not None else None
            except Exception:
                mins = None
            qsy_soon = bool(mins is not None and mins <= 10)
            if mins is None:
                qsy_text = "QSY ?"
            elif qsy_soon:
                qsy_text = f"QSY in {mins}m"
            else:
                qsy_text = f"Stable {mins}m"
            out[callsign] = {
                "frequency": round(freq_mhz, 6),
                "minutes_to_end": mins,
                "qsy_soon": qsy_soon,
                "qsy_text": qsy_text,
            }
        return out

    def _update_now_reachable_summary(self) -> None:
        if self._now_reachable_label is None:
            return
        # Reachable details are shown in map legend/tooltip; keep header compact.
        self._now_reachable_label.setText("")
        self._now_reachable_label.setVisible(False)

    def _update_now_reachable_button_visual(self, enabled: bool, theme: Optional[Dict[str, str]] = None) -> None:
        if self._now_reachable_button is None:
            return
        if theme is None:
            theme = self._theme_snapshot()
        self._now_reachable_button.setText("Peer Sched Now")
        if enabled:
            self._now_reachable_button.setStyleSheet(button_style("eligible_info", theme))
            self._now_reachable_button.setToolTip(
                "Peer Sched Now is ON: map filters to peers whose peer schedule currently matches your active schedule frequency."
            )
        else:
            self._now_reachable_button.setStyleSheet(button_style("muted", theme))
            self._now_reachable_button.setToolTip(
                "Show peers whose peer schedule currently matches your active schedule frequency."
            )

    def _current_map_mode_key(self) -> str:
        if bool(getattr(self, "_now_reachable_enabled", False)):
            return "peer"
        if self._effective_map_observation_focus_enabled():
            focus_mode = self._effective_map_observation_focus_mode()
            if focus_mode == "hf_reports":
                return "hf"
            if focus_mode == "local_reports":
                return "local"
            if focus_mode == "all_reports":
                return "reports"
            if focus_mode == "regional_intelligence":
                return "regional"
            if focus_mode == "paths":
                return "paths"
            if focus_mode == "propagation":
                return "propagation"
            if focus_mode == "rf_pins":
                return "pins"
            if focus_mode == "mesh_nodes":
                return "mesh_nodes"
        if bool(getattr(self, "_sitrep_status_only_enabled", False)):
            self._sitrep_status_only_enabled = False
        return "all"

    def _implicit_map_observation_focus_enabled(self) -> bool:
        """Traffic filters should work from All Stations without a separate mode click."""
        return bool(self._selected_map_topic_filter() or self._selected_map_search_text())

    def _effective_map_observation_focus_enabled(self) -> bool:
        return bool(
            getattr(self, "_observation_focus_enabled", False)
            or self._implicit_map_observation_focus_enabled()
        )

    def _effective_map_observation_focus_mode(self) -> str:
        mode = str(getattr(self, "_observation_focus_mode", "") or "").strip().lower()
        if bool(getattr(self, "_observation_focus_enabled", False)) and mode:
            return mode
        if self._implicit_map_observation_focus_enabled():
            return "all_reports"
        return mode or "all_reports"

    def _effective_map_report_focus_mode(self) -> str:
        """Return the report layer to use when filters ask a traffic question.

        Path/RF-planning layers are visual overlays. If the operator enters a
        topic or search term while one of those overlays is active, keep the
        overlay state but still render the traffic that answers the filter.
        Planning Pins are intentionally excluded because they are saved
        reference points, not received traffic.
        """
        mode = self._effective_map_observation_focus_mode()
        if not self._implicit_map_observation_focus_enabled():
            return mode
        if mode in {"rf_pins", "mesh_nodes"}:
            return mode
        if mode not in {"hf_reports", "local_reports", "all_reports"}:
            return "all_reports"
        return mode

    def _map_report_refinement_active(self) -> bool:
        """True when the user is asking the map to answer a traffic question."""
        return bool(
            self._implicit_map_observation_focus_enabled()
            or (
                bool(getattr(self, "_observation_focus_enabled", False))
                and str(getattr(self, "_observation_focus_mode", "") or "").strip().lower()
                in {"hf_reports", "local_reports", "all_reports"}
            )
        )

    def _map_layer_selection(self) -> frozenset[str]:
        mode = self._current_map_mode_key()
        if mode == "mesh_nodes":
            return frozenset({mode, "paths"} if self.show_link_paths else {mode})
        if mode in {"regional", "pins"}:
            return frozenset({mode})
        selected = set()
        if mode in {"propagation", "peer"}:
            selected.add(mode)
        if self.show_station_markers or self._links_active():
            selected.add("stations")
        if self._links_active():
            selected.add("paths")
        if (mode in {"hf", "local", "reports"} or self._map_report_refinement_active()
                or any(bool(getattr(self, name, False)) for name in (
                    "show_weather_reports", "show_alert_reports", "show_infrastructure_reports"))):
            selected.add("traffic")
        return frozenset(selected)

    def _on_map_layer_toggled(self, layer: str, checked: bool) -> None:
        selected = toggle_map_layer(self._map_layer_selection(), layer, checked)
        self._apply_map_layer_selection(selected)

    def _apply_map_layer_selection(self, selected) -> None:
        """Resolve a checkbox transaction before requesting one coherent refresh."""
        self._remember_map_path_scope()
        selected = set(selected)
        old_peer = bool(self._now_reachable_enabled)
        self._now_reachable_enabled = "peer" in selected
        if not self._now_reachable_enabled:
            self._now_reachable_meta = {}
            self._now_reachable_callsigns = set()
        traffic_mode = {"rf": "hf_reports", "local": "local_reports"}.get(
            self._selected_map_traffic_subtype(), "all_reports"
        )
        mode = next((mode for key, mode in (
            ("regional", "regional_intelligence"), ("pins", "rf_pins"),
            ("mesh_nodes", "mesh_nodes"), ("propagation", "propagation"),
            ("traffic", traffic_mode), ("paths", "paths"),
        ) if key in selected), "")
        self._sitrep_status_only_enabled = False
        self._observation_focus_enabled = bool(mode)
        self._observation_focus_mode = mode
        self._paths_previous_observation_focus = None
        self.show_station_markers = "stations" in selected
        self.show_link_paths = "paths" in selected
        self.show_weather_reports = False
        self.show_alert_reports = "traffic" in selected
        self.show_infrastructure_reports = bool({"traffic", "mesh_nodes"} & selected)
        self.show_rf_pins = "pins" in selected
        self.prop_overlay_enabled = False
        if self.show_link_paths:
            if self.link_mode in {"", "off"}:
                self._restore_map_path_scope("all" if "propagation" in selected else "my_station")
            self._clamp_path_recency_if_needed()
            self._sync_path_scope_combo((self.link_mode, self.link_value))
        else:
            self.link_mode, self.link_value = "off", ""
            self.relay_target = ""
            self._paths_focus_station = ""
            self._sync_link_mode_combo_to_off()
        # The hidden legacy Show selector is a layer scope, not a data filter.
        # Keep it consistent with the visible layer transaction.
        scope = getattr(self, "_map_scope_filter_combo", None)
        if scope is not None:
            self._set_combo_by_text_or_data(scope, "")
        # Preserve the operator's topic/search, band, age and path choices.
        for name, value in (
            ("map_stations_chk", self.show_station_markers),
            ("map_links_chk", self.show_link_paths), ("map_weather_chk", False),
            ("map_alerts_chk", self.show_alert_reports),
            ("map_infrastructure_chk", self.show_infrastructure_reports),
            ("prop_overlay_chk", False), ("_now_reachable_button", self._now_reachable_enabled),
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.blockSignals(True)
                widget.setChecked(value)
                widget.blockSignals(False)
        if "regional" in selected:
            self.show_states = True
        self._clear_report_query_caches()
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_now_reachable_summary()
        self._request_map_refresh(level="medium", reason="visible_layers")

    def _update_map_mode_buttons(self, theme: Optional[Dict[str, str]] = None) -> None:
        if theme is None:
            theme = self._theme_snapshot()
        mode_key = self._current_map_mode_key()
        buttons = (
            (getattr(self, "_map_all_stations_button", None), "all"),
            (getattr(self, "_map_hf_reports_button", None), "hf"),
            (getattr(self, "_map_local_reports_button", None), "local"),
            (getattr(self, "_map_reports_button", None), "reports"),
            (getattr(self, "_map_regional_intel_button", None), "regional"),
            (getattr(self, "_map_paths_button", None), "paths"),
            (getattr(self, "_map_propagation_button", None), "propagation"),
            (getattr(self, "_map_rf_pins_button", None), "pins"),
            (getattr(self, "_now_reachable_button", None), "peer"),
        )
        for button, key in buttons:
            if button is None:
                continue
            active = key == mode_key
            if key == "paths":
                active = active or bool(
                    getattr(self, "show_link_paths", False)
                    and str(getattr(self, "link_mode", "") or "").strip().lower() != "off"
                )
            button.setStyleSheet(button_style("eligible_info" if active else "muted", theme))
        layers = getattr(self, "_map_layer_controls", None)
        if layers is not None:
            layers.set_selection(self._map_layer_selection())
        self._sync_map_mode_combo(mode_key)
        self._update_map_compact_control_visibility(mode_key)
        self._update_selected_paths_button_visual(theme)
        self._update_clear_filter_buttons_visual(theme)

    def _update_map_compact_control_visibility(self, mode_key: str = "") -> None:
        key = str(mode_key or self._current_map_mode_key() or "").strip().lower()
        recent = getattr(self, "_mesh_recent_chk", None)
        if recent is not None:
            recent.setVisible(key == "mesh_nodes")
        filter_bar = getattr(self, "_map_filter_bar", None)
        if filter_bar is not None:
            filter_bar.setVisible(True)
        for field in tuple(getattr(self, "_map_filter_fields", ()) or ()):
            if field is not None:
                operational = str(field.property("mapFilterRole") or "") in {"Group", "Activity window"}
                field.setEnabled(not operational or key != "mesh_nodes")
                if operational:
                    field.setToolTip("Operational evidence filter; Mesh known contacts use Recent (24h)." if key == "mesh_nodes" else "Filters operational evidence; license references have separate scope.")
        sensitivity_field = getattr(self, "_map_intel_sensitivity_field", None)
        if sensitivity_field is not None:
            sensitivity_field.setVisible(key == "regional")
        traffic_subtype_field = getattr(self, "_map_traffic_subtype_field", None)
        if traffic_subtype_field is not None:
            traffic_subtype_field.setVisible(key in {"reports", "hf", "local"} or "traffic" in self._map_layer_selection())
        path_scope_field = getattr(self, "_map_path_scope_field", None)
        if path_scope_field is not None:
            path_scope_field.setVisible(
                key in {"paths", "propagation", "peer"}
                or bool(key != "mesh_nodes" and
                    getattr(self, "show_link_paths", False)
                    and str(getattr(self, "link_mode", "") or "").strip().lower() != "off"
                )
            )
        intelligence_section = getattr(self, "_map_intelligence_layers_section", None)
        if intelligence_section is not None:
            intelligence_section.setVisible(False)
        if getattr(self, "_map_filter_grid", None) is not None:
            self._schedule_map_geometry_reflow()

    def _sync_map_mode_combo(self, mode_key: str) -> None:
        combo = getattr(self, "_map_mode_combo", None)
        if combo is None:
            return
        key = str(mode_key or "all").strip().lower()
        if key in {"hf", "local"}:
            key = "reports"
        try:
            for idx in range(combo.count()):
                if str(combo.itemData(idx) or "").strip().lower() == key:
                    if combo.currentIndex() == idx:
                        return
                    combo.blockSignals(True)
                    combo.setCurrentIndex(idx)
                    combo.blockSignals(False)
                    return
        except Exception:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def _selected_map_traffic_subtype(self) -> str:
        combo = getattr(self, "_map_traffic_subtype_combo", None)
        if combo is None:
            return "all"
        try:
            value = str(combo.currentData() or "").strip().lower()
        except Exception:
            value = ""
        return value if value in {"all", "rf", "local", "commstat", "mesh"} else "all"

    def _set_map_traffic_subtype(self, subtype: str) -> None:
        combo = getattr(self, "_map_traffic_subtype_combo", None)
        if combo is None:
            return
        target = str(subtype or "all").strip().lower()
        try:
            for idx in range(combo.count()):
                if str(combo.itemData(idx) or "").strip().lower() == target:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(idx)
                    combo.blockSignals(False)
                    return
        except Exception:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def _map_view_status_text(self) -> str:
        mode_key = self._current_map_mode_key()
        if mode_key == "peer":
            return "Map View: Peer Schedule Now"
        if mode_key in {"hf", "local", "reports"}:
            subtype = self._selected_map_traffic_subtype()
            if mode_key == "hf":
                subtype = "rf"
            elif mode_key == "local":
                subtype = "local"
            label = {
                "rf": "RF/App",
                "local": "Local",
                "commstat": "CommStat",
                "mesh": "Mesh",
            }.get(subtype, "All")
            return f"Map View: Traffic | {label}"
        if mode_key == "regional":
            sensitivity = self._selected_map_intel_sensitivity().title()
            topic = self._selected_map_topic_filter()
            topic_label = topic or "All Topics"
            return f"Map View: Regional Intelligence | {sensitivity} | {topic_label}"
        if mode_key == "paths":
            return f"Map View: Paths - {self._current_path_scope_label()}"
        if mode_key == "propagation":
            return "Map View: RF Planning"
        if mode_key == "pins":
            return "Map View: Planning Pins"
        return "Map View: All Stations"

    def _current_path_scope_label(self) -> str:
        mode = str(getattr(self, "link_mode", "") or "").strip().lower()
        value = str(getattr(self, "link_value", "") or "").strip().upper()
        if not bool(getattr(self, "show_link_paths", False)) or mode == "off":
            return "Off"
        if mode == "station":
            return f"Selected {value}" if value else "Selected Station"
        if mode == "relay_target":
            target = value or str(getattr(self, "relay_target", "") or "").strip().upper()
            return f"Paths To {target}" if target else "Paths To Station"
        if mode == "all":
            return "All"
        if mode == "my_station":
            return "My Station"
        if mode == "group":
            return f"Group {value}" if value else "Group"
        return "On"

    def _map_link_direction_markers_enabled(self) -> bool:
        if not bool(getattr(self, "show_link_paths", False)):
            return False
        link_mode = str(getattr(self, "link_mode", "") or "").strip().lower()
        if link_mode == "off":
            return False
        mode_key = self._current_map_mode_key()
        if mode_key == "paths":
            return link_mode in {"my_station", "station", "all", "group", "relay_target"}
        return link_mode in {"station", "relay_target"}

    def _update_map_view_status_label(self, theme: Optional[Dict[str, str]] = None) -> None:
        label = getattr(self, "_map_view_status_label", None)
        if label is None:
            return
        if theme is None:
            theme = self._theme_snapshot()
        text = self._map_view_status_text()
        label.setText(text)
        active = text != "Map View: All Stations"
        color = theme.get("accent", "#0078A8") if active else theme.get("text_secondary", "#5B6773")
        label.setStyleSheet(f"font-weight: bold; color: {color};")
        label.setToolTip(
            "Shows the current map review context. Filters choose which records are eligible; "
            "layers choose what is drawn. Paths can show My Station, a selected station, or the network."
        )

    def _on_now_reachable_toggled(self, checked: bool) -> None:
        self._now_reachable_enabled = bool(checked)
        if self._now_reachable_enabled and self._sitrep_status_only_enabled:
            self._sitrep_status_only_enabled = False
            self._observation_focus_enabled = False
            self._observation_focus_mode = ""
            self._update_map_mode_buttons()
        if self._now_reachable_enabled:
            # Ensure links are visible for the operator-centric view.
            try:
                mode, _ = self._parse_link_selection(self.link_mode_combo.currentData())
                if str(mode).lower() == "off":
                    self.link_mode_combo.setCurrentText("My Station")
            except Exception:
                pass
        else:
            self._now_reachable_meta = {}
            self._now_reachable_callsigns = set()
        self._update_now_reachable_button_visual(self._now_reachable_enabled)
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_now_reachable_summary()
        self._refresh_relay_targets()
        self._request_map_refresh(level="medium", reason="reachable_toggle")

    def _on_map_mode_combo_changed(self, _idx: int) -> None:
        combo = getattr(self, "_map_mode_combo", None)
        if combo is None:
            return
        try:
            key = str(combo.currentData() or "").strip().lower()
        except Exception:
            key = ""
        if not key or key == self._current_map_mode_key():
            return
        if key == "all":
            self.focus_all_stations()
        elif key == "reports":
            self.focus_reports()
        elif key == "regional":
            self.focus_regional_intelligence()
        elif key == "paths":
            self.focus_paths()
        elif key == "propagation":
            self.focus_propagation()
        elif key == "pins":
            self.focus_rf_pins()
        elif key == "mesh_nodes":
            self.focus_mesh_nodes()
        elif key == "hf":
            self.focus_hf_reports()
        elif key == "local":
            self.focus_local_reports()
        elif key == "peer":
            self.focus_peer_sched_now()

    def focus_peer_sched_now(self) -> None:
        button = getattr(self, "_now_reachable_button", None)
        if button is not None:
            try:
                if not button.isChecked():
                    button.setChecked(True)
                    return
            except Exception:
                pass
        self._on_now_reachable_toggled(True)

    def focus_all_stations(self) -> None:
        """Return to the normal station map view."""
        self._remember_map_path_scope()
        self._sitrep_status_only_enabled = False
        self._observation_focus_enabled = False
        self._observation_focus_mode = ""
        self._now_reachable_enabled = False
        self._now_reachable_meta = {}
        self._now_reachable_callsigns = set()
        self.show_station_markers = True
        self.show_link_paths = False
        self.link_mode = "off"
        self.link_value = ""
        self.relay_target = ""
        self._paths_focus_station = ""
        self._paths_previous_observation_focus = None
        self.prop_overlay_enabled = False
        for button in (getattr(self, "_now_reachable_button", None),):
            if button is None:
                continue
            try:
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
            except Exception:
                pass
        prop_chk = getattr(self, "prop_overlay_chk", None)
        if prop_chk is not None:
            try:
                prop_chk.blockSignals(True)
                prop_chk.setChecked(False)
                prop_chk.blockSignals(False)
            except Exception:
                pass
        links_chk = getattr(self, "map_links_chk", None)
        if links_chk is not None:
            try:
                links_chk.blockSignals(True)
                links_chk.setChecked(False)
                links_chk.blockSignals(False)
            except Exception:
                pass
        stations_chk = getattr(self, "map_stations_chk", None)
        if stations_chk is not None:
            try:
                stations_chk.blockSignals(True)
                stations_chk.setChecked(True)
                stations_chk.blockSignals(False)
            except Exception:
                pass
        self._sync_link_mode_combo_to_off()
        self._sync_path_scope_combo(("off", ""))
        self._update_selected_paths_button_visual()
        self._update_now_reachable_button_visual(False)
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_clear_filter_buttons_visual()
        self._update_now_reachable_summary()
        self._refresh_relay_targets()
        self._request_map_refresh(level="medium", reason="all_stations_map_focus")

    def _toggle_active_map_layer_off(self, *, reason: str) -> bool:
        """Turn off the currently active visual layer without clearing filters.

        Filters answer the user's question. Layers decide how that answer is
        drawn, so clicking an active layer chip again should remove only that
        drawing mode and leave group/topic/search refinements in place.
        """
        mode_key = self._current_map_mode_key()
        if mode_key in {"all", "reports", "hf", "local"} and not bool(getattr(self, "show_link_paths", False)):
            return False
        self.clear_map_layers(reason=reason)
        return True

    def focus_paths(self) -> None:
        """Open the station path/link view without report or planning overlays."""
        if self._current_map_mode_key() == "paths":
            self._set_path_layer_off()
            self._update_selected_paths_button_visual()
            self._update_map_mode_buttons()
            self._update_map_view_status_label()
            self._update_clear_filter_buttons_visual()
            self._request_map_refresh(level="medium", reason="paths_map_focus_off")
            return
        if str(getattr(self, "_observation_focus_mode", "") or "") != "paths":
            self._paths_previous_observation_focus = (
                bool(getattr(self, "_observation_focus_enabled", False)),
                str(getattr(self, "_observation_focus_mode", "") or ""),
            )
        self._sitrep_status_only_enabled = False
        self._observation_focus_enabled = True
        self._observation_focus_mode = "paths"
        self._now_reachable_enabled = False
        self._now_reachable_meta = {}
        self._now_reachable_callsigns = set()
        self.show_station_markers = True
        self.show_link_paths = True
        self.show_weather_reports = False
        self.show_alert_reports = False
        self.show_infrastructure_reports = False
        self.show_rf_pins = False
        self.prop_overlay_enabled = False
        for widget, value in (
            (getattr(self, "_now_reachable_button", None), False),
            (getattr(self, "map_stations_chk", None), True),
            (getattr(self, "map_links_chk", None), True),
            (getattr(self, "map_weather_chk", None), False),
            (getattr(self, "map_alerts_chk", None), False),
            (getattr(self, "map_infrastructure_chk", None), False),
            (getattr(self, "prop_overlay_chk", None), False),
        ):
            if widget is None:
                continue
            try:
                widget.blockSignals(True)
                widget.setChecked(value)
                widget.blockSignals(False)
            except Exception:
                pass
        self._clamp_path_recency_if_needed()
        self._restore_map_path_scope("my_station")
        self._update_now_reachable_button_visual(False)
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_now_reachable_summary()
        self._refresh_relay_targets()
        self._request_map_refresh(level="medium", reason="paths_map_focus")

    def focus_propagation(self) -> None:
        """Open the RF planning map view for path, pin, and band decisions."""
        if self._current_map_mode_key() == "propagation":
            self._toggle_active_map_layer_off(reason="propagation_map_focus_off")
            return
        self._sitrep_status_only_enabled = False
        self._observation_focus_enabled = True
        self._observation_focus_mode = "propagation"
        self._now_reachable_enabled = False
        self._now_reachable_meta = {}
        self._now_reachable_callsigns = set()
        self.show_station_markers = True
        self.show_link_paths = True
        self.show_weather_reports = False
        self.show_alert_reports = False
        self.show_infrastructure_reports = False
        self.show_rf_pins = False
        self.prop_overlay_enabled = False
        for widget, value in (
            (getattr(self, "_now_reachable_button", None), False),
            (getattr(self, "map_stations_chk", None), True),
            (getattr(self, "map_links_chk", None), True),
            (getattr(self, "map_weather_chk", None), False),
            (getattr(self, "map_alerts_chk", None), False),
            (getattr(self, "map_infrastructure_chk", None), False),
            (getattr(self, "prop_overlay_chk", None), False),
        ):
            if widget is None:
                continue
            try:
                widget.blockSignals(True)
                widget.setChecked(value)
                widget.blockSignals(False)
            except Exception:
                pass
        self._restore_map_path_scope("all")
        self._update_now_reachable_button_visual(False)
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_now_reachable_summary()
        self._refresh_relay_targets()
        self._request_map_refresh(level="full", reason="propagation_map_focus")

    def _set_combo_by_text_or_data(self, combo: QComboBox, value: str, *, fallback_index: int = 0) -> bool:
        target = str(value or "").strip()
        try:
            combo.blockSignals(True)
            if target:
                for idx in range(combo.count()):
                    data = str(combo.itemData(idx) or "").strip()
                    text = str(combo.itemText(idx) or "").strip()
                    if target.lower() in {data.lower(), text.lower()}:
                        combo.setCurrentIndex(idx)
                        return True
            combo.setCurrentIndex(max(0, min(int(fallback_index), combo.count() - 1)))
            return False
        except Exception:
            return False
        finally:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def _set_report_focus_mode(
        self,
        mode: str,
        *,
        group_filter: str = "",
        topic_filter: str = "",
        query_filter: str = "",
        state_filter: str = "",
        grid_filter: str = "",
        source_filter: Optional[str] = None,
        reason: str = "",
    ) -> None:
        """Open a temporary map focus for HF, local, or combined report review."""
        context = map_context_from_mapping(
            {
                "group_filter": group_filter,
                "topic_filter": topic_filter,
                "query_filter": query_filter,
                "state_filter": state_filter,
                "grid_filter": grid_filter,
                "source_family": source_filter or "",
            }
        )
        group_filter = context.group_filter
        topic_filter = context.topic_filter
        query_filter = context.query_filter
        state_filter = context.state_filter
        grid_filter = context.grid_filter
        if source_filter is not None:
            source_filter = context.source_family
        self._sitrep_status_only_enabled = False
        self._observation_focus_enabled = True
        self._observation_focus_mode = str(mode or "all_reports").strip().lower()
        self.show_station_markers = False
        self.show_link_paths = False
        self.show_weather_reports = False
        self.show_alert_reports = True
        self.show_infrastructure_reports = True
        self.show_rf_pins = False
        self.prop_overlay_enabled = False
        if self._now_reachable_enabled and self._now_reachable_button is not None:
            self._now_reachable_button.blockSignals(True)
            self._now_reachable_button.setChecked(False)
            self._now_reachable_button.blockSignals(False)
            self._now_reachable_enabled = False
            self._now_reachable_meta = {}
            self._now_reachable_callsigns = set()
            self._update_now_reachable_button_visual(False)
            self._update_now_reachable_summary()
            self._refresh_relay_targets()
        for widget, value in (
            (getattr(self, "map_stations_chk", None), False),
            (getattr(self, "map_links_chk", None), False),
            (getattr(self, "map_weather_chk", None), False),
            (getattr(self, "map_alerts_chk", None), True),
            (getattr(self, "map_infrastructure_chk", None), True),
            (getattr(self, "prop_overlay_chk", None), False),
        ):
            if widget is None:
                continue
            try:
                widget.blockSignals(True)
                widget.setChecked(value)
                widget.blockSignals(False)
            except Exception:
                pass
        try:
            if hasattr(self, "group_filter_combo"):
                self._set_combo_by_text_or_data(self.group_filter_combo, group_filter)
            if getattr(self, "_map_topic_filter_combo", None) is not None:
                self._set_combo_by_text_or_data(self._map_topic_filter_combo, topic_filter)
            if getattr(self, "_map_state_filter_combo", None) is not None:
                self._set_combo_by_text_or_data(self._map_state_filter_combo, state_filter)
            if source_filter is not None and getattr(self, "_map_source_filter_combo", None) is not None:
                self._set_combo_by_text_or_data(self._map_source_filter_combo, source_filter)
            if getattr(self, "_map_search_edit", None) is not None:
                search_text = " ".join(
                    part
                    for part in (
                        str(query_filter or "").strip(),
                        str(grid_filter or "").strip().upper(),
                    )
                    if part
                ).strip()
                self._map_search_edit.blockSignals(True)
                self._map_search_edit.setText(search_text)
                self._map_search_edit.blockSignals(False)
            if hasattr(self, "band_combo"):
                self.band_combo.blockSignals(True)
                self.band_combo.setCurrentIndex(0)
                self.band_combo.blockSignals(False)
        except Exception:
            pass
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._request_map_refresh(level="medium", reason=reason or f"{self._observation_focus_mode}_map_focus")

    def _apply_map_traffic_subtype(
        self,
        *,
        group_filter: str = "",
        topic_filter: str = "",
        query_filter: str = "",
        state_filter: str = "",
        grid_filter: str = "",
        reason: str = "traffic_subtype",
    ) -> None:
        subtype = self._selected_map_traffic_subtype()
        if subtype == "rf":
            self._set_report_focus_mode(
                "hf_reports",
                group_filter=group_filter,
                topic_filter=topic_filter,
                query_filter=query_filter,
                state_filter=state_filter,
                grid_filter=grid_filter,
                source_filter="",
                reason=reason,
            )
            return
        if subtype == "local":
            self._set_report_focus_mode(
                "local_reports",
                group_filter=group_filter,
                topic_filter=topic_filter,
                query_filter=query_filter,
                state_filter=state_filter,
                grid_filter=grid_filter,
                source_filter="",
                reason=reason,
            )
            return
        self._set_report_focus_mode(
            "all_reports",
            group_filter=group_filter,
            topic_filter=topic_filter,
            query_filter=query_filter,
            state_filter=state_filter,
            grid_filter=grid_filter,
            source_filter="commstat" if subtype == "commstat" else ("mesh" if subtype == "mesh" else ""),
            reason=reason,
        )

    def _on_map_traffic_subtype_changed(self, _idx: int) -> None:
        if getattr(self, "_map_layer_controls", None) is not None:
            source = getattr(self, "_map_source_filter_combo", None)
            if source is not None:
                self._set_combo_by_text_or_data(source, {"commstat": "commstat", "mesh": "mesh"}.get(self._selected_map_traffic_subtype(), ""))
            self._apply_map_layer_selection(set(self._map_layer_selection()) | {"traffic"})
            return
        self._clear_report_query_caches()
        self._apply_map_traffic_subtype(reason="traffic_subtype")

    def focus_hf_reports(
        self,
        *,
        group_filter: str = "",
        topic_filter: str = "",
        query_filter: str = "",
        state_filter: str = "",
        grid_filter: str = "",
    ) -> None:
        """Open a map focus for HF-derived Spotter/SitRep field reports."""
        if self._current_map_mode_key() == "hf" and not any((group_filter, topic_filter, query_filter, state_filter, grid_filter)):
            self.focus_all_stations()
            return
        self._set_map_traffic_subtype("rf")
        self._apply_map_traffic_subtype(
            group_filter=group_filter,
            topic_filter=topic_filter,
            query_filter=query_filter,
            state_filter=state_filter,
            grid_filter=grid_filter,
            reason="hf_reports_map_focus",
        )

    def focus_local_reports(
        self,
        *,
        group_filter: str = "",
        topic_filter: str = "",
        query_filter: str = "",
        state_filter: str = "",
        grid_filter: str = "",
    ) -> None:
        """Open a map focus for confirmed local operator and NCS reports."""
        if self._current_map_mode_key() == "local" and not any((group_filter, topic_filter, query_filter, state_filter, grid_filter)):
            self.focus_all_stations()
            return
        self._set_map_traffic_subtype("local")
        self._apply_map_traffic_subtype(
            group_filter=group_filter,
            topic_filter=topic_filter,
            query_filter=query_filter,
            state_filter=state_filter,
            grid_filter=grid_filter,
            reason="local_reports_map_focus",
        )

    def focus_reports(
        self,
        *,
        group_filter: str = "",
        topic_filter: str = "",
        query_filter: str = "",
        state_filter: str = "",
        grid_filter: str = "",
    ) -> None:
        """Open a map focus for HF and confirmed local reports together."""
        if self._current_map_mode_key() == "reports" and not any((group_filter, topic_filter, query_filter, state_filter, grid_filter)):
            self.focus_all_stations()
            return
        self._apply_map_traffic_subtype(
            group_filter=group_filter,
            topic_filter=topic_filter,
            query_filter=query_filter,
            state_filter=state_filter,
            grid_filter=grid_filter,
            reason="traffic_map_focus",
        )

    def focus_regional_intelligence(self) -> None:
        """Open the regional situation view for state/FEMA concern rollups."""
        if self._current_map_mode_key() == "regional":
            self.focus_all_stations()
            return
        self._sitrep_status_only_enabled = False
        self._observation_focus_enabled = True
        self._observation_focus_mode = "regional_intelligence"
        self._now_reachable_enabled = False
        self._now_reachable_meta = {}
        self._now_reachable_callsigns = set()
        self.show_station_markers = False
        self.show_link_paths = False
        self.show_weather_reports = False
        self.show_alert_reports = False
        self.show_infrastructure_reports = False
        self.show_rf_pins = False
        self.show_states = True
        self.show_regions = True
        self.prop_overlay_enabled = False
        for widget, value in (
            (getattr(self, "_now_reachable_button", None), False),
            (getattr(self, "show_states_chk", None), True),
            (getattr(self, "show_regions_chk", None), True),
            (getattr(self, "map_stations_chk", None), False),
            (getattr(self, "map_links_chk", None), False),
            (getattr(self, "map_weather_chk", None), False),
            (getattr(self, "map_alerts_chk", None), False),
            (getattr(self, "map_infrastructure_chk", None), False),
            (getattr(self, "prop_overlay_chk", None), False),
        ):
            if widget is None:
                continue
            try:
                widget.blockSignals(True)
                widget.setChecked(value)
                widget.blockSignals(False)
            except Exception:
                pass
        self._sync_link_mode_combo_to_off()
        self._sync_path_scope_combo(("off", ""))
        self._update_now_reachable_button_visual(False)
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_now_reachable_summary()
        self._refresh_relay_targets()
        self._request_map_refresh(level="full", reason="regional_intelligence_map_focus")

    def focus_rf_pins(self) -> None:
        """Open a map focus for saved planning/reference pins."""
        if self._current_map_mode_key() == "pins":
            self._toggle_active_map_layer_off(reason="planning_pins_map_focus_off")
            return
        self._sitrep_status_only_enabled = False
        self._observation_focus_enabled = True
        self._observation_focus_mode = "rf_pins"
        self._now_reachable_enabled = False
        self._now_reachable_meta = {}
        self._now_reachable_callsigns = set()
        self.show_station_markers = False
        self.show_link_paths = False
        self.show_weather_reports = False
        self.show_alert_reports = False
        self.show_infrastructure_reports = False
        self.show_rf_pins = True
        self.prop_overlay_enabled = False
        for widget, value in (
            (getattr(self, "_now_reachable_button", None), False),
            (getattr(self, "map_stations_chk", None), False),
            (getattr(self, "map_links_chk", None), False),
            (getattr(self, "map_weather_chk", None), False),
            (getattr(self, "map_alerts_chk", None), False),
            (getattr(self, "map_infrastructure_chk", None), False),
            (getattr(self, "prop_overlay_chk", None), False),
        ):
            if widget is None:
                continue
            try:
                widget.blockSignals(True)
                widget.setChecked(value)
                widget.blockSignals(False)
            except Exception:
                pass
        self._update_now_reachable_button_visual(False)
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_now_reachable_summary()
        self._refresh_relay_targets()
        self._request_map_refresh(level="medium", reason="planning_pins_map_focus")

    def focus_mesh_nodes(self) -> None:
        """Open a topology-first view of known mesh node locations."""
        if self._current_map_mode_key() == "mesh_nodes":
            self._toggle_active_map_layer_off(reason="mesh_nodes_map_focus_off")
            return
        self._sitrep_status_only_enabled = False
        self._observation_focus_enabled = True
        self._observation_focus_mode = "mesh_nodes"
        self._now_reachable_enabled = False
        self._now_reachable_meta = {}
        self._now_reachable_callsigns = set()
        self.show_station_markers = False
        self.show_link_paths = False
        self.show_weather_reports = False
        self.show_alert_reports = False
        self.show_infrastructure_reports = True
        self.show_rf_pins = False
        self.prop_overlay_enabled = False
        self._mesh_nodes_auto_fit_pending = True
        for widget, value in (
            (getattr(self, "_now_reachable_button", None), False),
            (getattr(self, "map_stations_chk", None), False),
            (getattr(self, "map_links_chk", None), False),
            (getattr(self, "map_weather_chk", None), False),
            (getattr(self, "map_alerts_chk", None), False),
            (getattr(self, "map_infrastructure_chk", None), True),
            (getattr(self, "prop_overlay_chk", None), False),
        ):
            if widget is None:
                continue
            try:
                widget.blockSignals(True)
                widget.setChecked(value)
                widget.blockSignals(False)
            except Exception:
                pass
        self._update_now_reachable_button_visual(False)
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_now_reachable_summary()
        self._refresh_relay_targets()
        self._request_map_refresh(level="medium", reason="mesh_nodes_map_focus")

    def focus_spotter_reports(self) -> None:
        """Compatibility alias for the previous Spotter map action."""
        self.focus_hf_reports()

    def _on_add_rf_pin_clicked(self) -> None:
        dialog = _RfPinDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        payload = dialog.pin_payload()
        if not payload.get("grid") and not payload.get("state"):
            QMessageBox.warning(
                self,
                "Add Planning Pin",
                "Add a grid square or state/province so CommsPlan can place the planning pin in context.",
            )
            return
        if not payload.get("grid"):
            QMessageBox.warning(
                self,
                "Add Planning Pin",
                "A state-only pin can be saved later when rollup markers are supported. Add a grid square for this planning pin.",
            )
            return
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
            save_rf_pin(db_path, payload)
        except Exception as exc:
            log.warning("StationsMap: failed to save RF pin: %s", exc, exc_info=True)
            QMessageBox.warning(self, "Add Planning Pin", f"CommsPlan could not save this planning pin.\n{exc}")
            return
        self._clear_report_query_caches()
        if not bool(getattr(self, "_observation_focus_enabled", False)):
            self._set_report_focus_mode("rf_pins")
        else:
            self._request_map_refresh(level="medium", reason="rf_pin_saved")
        label = str(payload.get("label") or "Planning Pin")
        status = getattr(self, "_map_view_status_label", None)
        if status is not None:
            status.setText(f"Planning Pin saved: {label}")

    def _on_manage_rf_pins_clicked(self) -> None:
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception as exc:
            QMessageBox.warning(self, "Manage Planning Pins", f"CommsPlan could not open planning pin storage.\n{exc}")
            return
        dialog = _RfPinManagerDialog(db_path, self)
        dialog.exec()
        if not dialog.changed:
            return
        self._clear_report_query_caches()
        self._request_map_refresh(level="medium", reason="rf_pin_changed")

    def _include_legacy_spotter_report_layers(self) -> bool:
        """Return False when Traffic > Local should exclude HF Spotter-only traffic layers."""
        if not self._effective_map_observation_focus_enabled():
            return True
        focus_mode = self._effective_map_report_focus_mode()
        return focus_mode not in {"local_reports", "rf_pins", "mesh_nodes"}

    @staticmethod
    def _observation_focus_scopes_station_markers(
        observation_focus_enabled: bool,
        observation_focus_mode: object,
    ) -> bool:
        """
        Traffic-focused map modes can narrow station markers to stations with
        matching observations. Planning Pins are saved planning/reference
        markers, not received traffic, so they never scope station markers.
        """
        mode = str(observation_focus_mode or "").strip().lower()
        return bool(observation_focus_enabled) and mode in {
            "hf_reports",
            "local_reports",
            "all_reports",
        }

    def _relay_target_callsign_from_text(self, text: str) -> str:
        txt = (text or "").strip()
        if not txt:
            return ""
        if hasattr(self, "relay_target_combo"):
            idx = self.relay_target_combo.findText(txt, Qt.MatchFixedString)
            if idx >= 0:
                data = self.relay_target_combo.itemData(idx)
                if data:
                    return str(data).strip().upper()
        first = txt.split("|", 1)[0].strip().upper()
        if first in self.operator_index:
            return first
        exact_name = txt.upper()
        for row in self.operator_rows:
            name = (row.get("name") or "").strip().upper()
            if name and name == exact_name:
                return (row.get("callsign") or "").strip().upper()
        return first

    @staticmethod
    def _expand_observed_path_directions(selected_links, directional_best, positions):
        """Keep route selection undirected, but retain each observed traffic direction."""
        selected_pairs = {tuple(sorted((link["origin"], link["destination"]))) for link in selected_links}
        links = []
        for (origin, destination), record in directional_best.items():
            if tuple(sorted((origin, destination))) not in selected_pairs:
                continue
            first, last = positions.get(origin), positions.get(destination)
            if first is None or last is None:
                continue
            links.append({**record, "lat1": first[0], "lon1": first[1], "lat2": last[0], "lon2": last[1]})
        return links

    def _load_js8_links(
        self,
        band_filter=None,
        my_call: str = "",
        link_selection: Optional[tuple[str, str]] = None,
        relay_target: Optional[str] = None,
        group_filter: str = "",
        region_filter: str = "",
        reachable_callsigns: Optional[Set[str]] = None,
        max_age_sec: Optional[int] = None,
    ) -> tuple[List[Dict], Dict[str, Dict]]:
        """
        Load recent JS8 links from js8_links table, returning only pairs with known positions.
        Returns (links, station_stats) where station_stats keyed by callsign contains:
          last_seen (ts), last_spotter (ts), avg_snr, max_snr
        """
        links: List[Dict] = []
        if isinstance(link_selection, (list, tuple)) and len(link_selection) >= 2:
            mode, selection_value = link_selection[0], link_selection[1]
        else:
            mode, selection_value = "off", ""
        mode = str(mode or "off").strip().lower()
        selection_value = (
            (selection_value or "").strip().upper()
            if mode in {"region", "group", "station"}
            else (selection_value or "")
        )
        relay_target = (relay_target or "").strip().upper()
        group_filter = (group_filter or "").strip().upper()
        region_filter = (region_filter or "").strip().upper()
        reachable_calls = {c.strip().upper() for c in (reachable_callsigns or set()) if c}
        band_sig = ""
        try:
            band_sig = json.dumps(band_filter or {"type": "all"}, sort_keys=True, default=str)
        except Exception:
            band_sig = str(band_filter or {"type": "all"})
        cache_key = (
            "js8_links",
            band_sig,
            (my_call or "").strip().upper(),
            mode,
            selection_value,
            relay_target,
            group_filter,
            region_filter,
            ",".join(sorted(reachable_calls)),
            int(max_age_sec or 0),
            len(self.stations),
            self._get_user_latlon(),
            len(self.operator_index),
        )
        cached = self._query_cache_get(cache_key, ttl_sec=2.0)
        if isinstance(cached, tuple) and len(cached) == 2:
            cached_links, cached_stats = cached
            if isinstance(cached_links, list) and isinstance(cached_stats, dict):
                self._map_last_link_source_rows = max(
                    int(getattr(self, "_map_last_link_source_rows", 0) or 0),
                    len(cached_links),
                )
                return (
                    [dict(x) for x in cached_links if isinstance(x, dict)],
                    {
                        str(k): (dict(v) if isinstance(v, dict) else v)
                        for k, v in cached_stats.items()
                    },
                )
        if mode == "off" and not relay_target:
            self._map_last_link_source_rows = 0
            self._map_last_link_missing_position_rows = 0
            return links, {}

        # Build position map from current stations
        pos_map: Dict[str, tuple[float, float]] = {}
        for pt in self._station_points_for_map():
            pos_map[pt.callsign.upper()] = (pt.lat, pt.lon)

        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception as e:
            log.error("StationsMap: failed to resolve DB path for links: %s", e)
            return links, {}
        if not db_path.exists():
            return links, {}

        ts_cut = None
        if max_age_sec and max_age_sec > 0:
            ts_cut = time.time() - max_age_sec

        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            try:
                cols = {str(row[1]) for row in cur.execute("PRAGMA table_info(js8_links)").fetchall()}
            except Exception:
                cols = set()
            relay_select = ", is_relay, relay_via" if {"is_relay", "relay_via"}.issubset(cols) else ", 0, ''"
            direction_select = ", direction_known" if "direction_known" in cols else ", 0"
            where_parts: List[str] = []
            params: List[object] = []
            if ts_cut:
                where_parts.append("ts >= ?")
                params.append(ts_cut)
            if relay_target and my_call:
                # Path-to mode needs the recent local graph, not only edges touching
                # my station or the target. Example: me -> A -> B -> target.
                pass
            where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
            cur.execute(
                "SELECT ts, origin, destination, snr, band, freq_hz, is_spotter"
                f"{relay_select}{direction_select} FROM js8_links{where_sql} ORDER BY ts DESC LIMIT 20000",
                tuple(params),
            )
            rows = cur.fetchall()
            note_map_read_cap("js8_links", len(rows), 20000)
            conn.close()
        except Exception as e:
            log.error("StationsMap: failed to load js8_links: %s", e)
            return links, {}
        self._map_last_link_source_rows = len(rows)
        self._map_last_link_missing_position_rows = 0

        # Defensive recency filter in Python too (covers odd SQLite typing differences across platforms)
        if ts_cut:
            before = len(rows)
            rows = [r for r in rows if r and len(r) > 0 and isinstance(r[0], (int, float)) and r[0] >= ts_cut]
            if log.isEnabledFor(logging.DEBUG):
                log.debug("StationsMap: recency filter %s removed %s rows", max_age_sec, before - len(rows))

        # keep best SNR per pair with filters
        best: Dict[tuple[str, str], Dict[str, object]] = {}
        directional_best: Dict[tuple[str, str], Dict[str, object]] = {}
        stat: Dict[str, Dict] = {}
        relay_best: Dict[tuple[str, str], Dict[str, object]] = {}
        my_partners: Set[str] = set()
        target_partners: Set[str] = set()
        direct_snrs: Dict[tuple[str, str], List[float]] = {}
        direct_counts: Dict[tuple[str, str], int] = {}

        def _freq_to_band(freq_mhz: Optional[float]) -> str:
            if freq_mhz is None:
                return ""
            bands = [
                ("160M", 1.8, 2.0),
                ("80M", 3.5, 4.0),
                ("60M", 5.0, 5.5),
                ("40M", 7.0, 7.3),
                ("30M", 10.1, 10.15),
                ("20M", 14.0, 14.35),
                ("17M", 18.068, 18.168),
                ("15M", 21.0, 21.45),
                ("12M", 24.89, 24.99),
                ("10M", 28.0, 29.7),
                ("6M", 50.0, 54.0),
                ("2M", 144.0, 148.0),
            ]
            for name, lo, hi in bands:
                if lo <= freq_mhz <= hi:
                    return name
            return ""

        def _station_matches_filters(cs: str) -> bool:
            if not cs:
                return False
            if group_filter:
                groups = self.operator_index.get(cs, {}).get("groups", set())
                if group_filter not in groups:
                    return False
            if region_filter:
                region = self.operator_index.get(cs, {}).get("region")
                if region != region_filter:
                    return False
            return True

        for row in rows:
            if not row or len(row) < 7:
                continue
            ts, o, d, snr, band, freq_hz, is_spotter = row[:7]
            is_relay = row[7] if len(row) >= 8 else 0
            relay_via = row[8] if len(row) >= 9 else ""
            o = (o or "").upper()
            d = (d or "").upper()
            if o == "" or d == "" or o not in pos_map or d not in pos_map:
                self._map_last_link_missing_position_rows += 1
                continue
            bf = band_filter or {"type": "all"}
            try:
                freq_mhz = float(freq_hz) / 1_000_000.0 if freq_hz is not None else None
            except Exception:
                freq_mhz = None
            band_val = (band or "").upper() or _freq_to_band(freq_mhz)
            if bf.get("type") == "band":
                if band_val != str(bf.get("value")).upper():
                    continue
            elif bf.get("type") == "freq":
                target_f = bf.get("value")
                if freq_mhz is None or target_f is None or abs(freq_mhz - target_f) > 0.001:
                    continue

            match_o = _station_matches_filters(o)
            match_d = _station_matches_filters(d)

            include = False
            if relay_target:
                include = bool(my_call)
            elif mode == "my_station":
                include = bool(my_call) and my_call in {o, d}
            elif mode == "station" and selection_value:
                include = selection_value in {o, d}
            elif mode == "all":
                include = True
            elif mode == "region" and selection_value:
                region_o = self.operator_index.get(o, {}).get("region")
                region_d = self.operator_index.get(d, {}).get("region")
                if region_o == selection_value and region_d == selection_value:
                    include = True
                elif my_call and my_call in {o, d}:
                    other = d if o == my_call else o
                    include = self.operator_index.get(other, {}).get("region") == selection_value
            elif mode == "group" and selection_value:
                groups_o = self.operator_index.get(o, {}).get("groups", set())
                groups_d = self.operator_index.get(d, {}).get("groups", set())
                if selection_value in groups_o and selection_value in groups_d:
                    include = True
                elif my_call and my_call in {o, d}:
                    other = d if o == my_call else o
                    include = selection_value in self.operator_index.get(other, {}).get("groups", set())
            if include and group_filter:
                if my_call and my_call in {o, d}:
                    other = d if o == my_call else o
                    include = group_filter in self.operator_index.get(other, {}).get("groups", set())
                elif mode == "station" and selection_value and selection_value in {o, d}:
                    other = d if o == selection_value else o
                    include = (
                        group_filter in self.operator_index.get(selection_value, {}).get("groups", set())
                        or group_filter in self.operator_index.get(other, {}).get("groups", set())
                    )
                else:
                    include = group_filter in self.operator_index.get(o, {}).get("groups", set()) and group_filter in self.operator_index.get(d, {}).get("groups", set())
            if include and region_filter:
                if my_call and my_call in {o, d}:
                    other = d if o == my_call else o
                    include = self.operator_index.get(other, {}).get("region") == region_filter
                elif mode == "station" and selection_value and selection_value in {o, d}:
                    other = d if o == selection_value else o
                    include = (
                        self.operator_index.get(selection_value, {}).get("region") == region_filter
                        or self.operator_index.get(other, {}).get("region") == region_filter
                    )
                else:
                    region_o = self.operator_index.get(o, {}).get("region")
                    region_d = self.operator_index.get(d, {}).get("region")
                    include = region_o == region_filter and region_d == region_filter
            if include and reachable_calls:
                include = (o in reachable_calls or d in reachable_calls)
                if include and my_call:
                    include = my_call in {o, d}
            if not include:
                include = False

            key = tuple(sorted((o, d)))
            try:
                snr_val = float(snr)
            except Exception:
                snr_val = None

            record_stats = False
            if mode == "all":
                record_stats = match_o
            elif mode == "my_station":
                record_stats = bool(my_call) and o != my_call and my_call in {o, d} and match_o
            if snr_val is not None and record_stats and my_call and my_call in {o, d} and o != my_call:
                direct_snrs.setdefault(key, []).append(snr_val)
                direct_counts[key] = direct_counts.get(key, 0) + 1

            def _record_station(cs: str, other: str, ts_val, snr_value, spotted: bool, is_origin: bool, band_name: str):
                s = stat.setdefault(
                    cs,
                    {
                        "last_seen": 0,
                        "last_spotter": 0,
                        "snrs": [],
                        "snrs_excl_my": [],
                        "snr_excl_my_count": 0,
                        "last_band": "",
                        "last_band_ts": 0,
                    },
                )
                if ts_val and ts_val > s["last_seen"]:
                    s["last_seen"] = ts_val
                if spotted and ts_val and ts_val > s["last_spotter"]:
                    s["last_spotter"] = ts_val
                if snr_value is not None and is_origin:
                    s["snrs"].append(snr_value)
                    if my_call and other != my_call:
                        s["snrs_excl_my"].append(snr_value)
                        s["snr_excl_my_count"] += 1
                if is_origin and band_name and ts_val and ts_val > s.get("last_band_ts", 0):
                    s["last_band"] = band_name
                    s["last_band_ts"] = ts_val
            if record_stats:
                _record_station(o, d, ts, snr_val, bool(is_spotter), True, band_val)

            if not include:
                continue

            direction_key = (o, d)
            direction_known = bool(row[9]) if len(row) > 9 else False
            previous = directional_best.get(direction_key)
            if previous is None or (direction_known and not previous.get("direction_known")) or (
                direction_known == bool(previous.get("direction_known"))
                and snr_val is not None and (previous.get("snr") is None or snr_val > previous["snr"])
            ):
                directional_best[direction_key] = {
                    "origin": o, "destination": d, "snr": snr_val, "ts": ts,
                    "is_relay": bool(is_relay), "relay_via": relay_via or "",
                    "direction_known": direction_known,
                }

            if relay_target:
                prev = relay_best.get(key)
                prev_snr = prev.get("snr") if isinstance(prev, dict) else None
                if key not in relay_best or (snr_val is not None and (prev_snr is None or snr_val > prev_snr)):
                    relay_best[key] = {
                        "origin": o,
                        "destination": d,
                        "snr": snr_val,
                        "ts": ts,
                        "is_relay": bool(is_relay),
                        "relay_via": relay_via or "",
                    }
                if my_call and my_call in {o, d}:
                    other = d if o == my_call else o
                    my_partners.add(other)
                if relay_target in {o, d}:
                    other = d if o == relay_target else o
                    target_partners.add(other)
            else:
                prev = best.get(key)
                prev_snr = prev.get("snr") if isinstance(prev, dict) else None
                if key not in best or (snr_val is not None and (prev_snr is None or snr_val > prev_snr)):
                    best[key] = {
                        "origin": o,
                        "destination": d,
                        "snr": snr_val,
                        "ts": ts,
                        "is_relay": bool(is_relay),
                        "relay_via": relay_via or "",
                    }

        def _add_link(key_map: Dict[tuple[str, str], Dict[str, object]], a: str, b: str):
            k = tuple(sorted((a, b)))
            if k not in key_map:
                return
            data = key_map[k]
            origin = str(data.get("origin") or a or "").strip().upper()
            destination = str(data.get("destination") or b or "").strip().upper()
            p1 = pos_map.get(origin)
            p2 = pos_map.get(destination)
            if not p1 or not p2:
                return
            links.append(
                {
                    "origin": origin,
                    "destination": destination,
                    "lat1": p1[0],
                    "lon1": p1[1],
                    "lat2": p2[0],
                    "lon2": p2[1],
                    "snr": data.get("snr"),
                    "ts": data.get("ts"),
                    "is_relay": bool(data.get("is_relay")),
                    "relay_via": str(data.get("relay_via") or ""),
                }
            )

        def _best_relay_path_nodes(max_hops: int = 3) -> List[str]:
            if not relay_target or not my_call:
                return []
            graph: Dict[str, Set[str]] = {}
            for a, b in relay_best:
                graph.setdefault(a, set()).add(b)
                graph.setdefault(b, set()).add(a)
            if my_call not in graph or relay_target not in graph:
                return []
            queue_nodes = deque([(my_call, [my_call])])
            seen = {my_call}
            while queue_nodes:
                node, path = queue_nodes.popleft()
                if node == relay_target:
                    return path
                if len(path) > max_hops:
                    continue
                neighbors = sorted(
                    graph.get(node, set()) - seen,
                    key=lambda other: (
                        float(relay_best.get(tuple(sorted((node, other))), {}).get("snr") or -999),
                        float(relay_best.get(tuple(sorted((node, other))), {}).get("ts") or 0),
                    ),
                    reverse=True,
                )
                for other in neighbors:
                    seen.add(other)
                    queue_nodes.append((other, path + [other]))
            return []

        if relay_target and my_call:
            direct_key = tuple(sorted((my_call, relay_target)))
            _add_link(relay_best, my_call, relay_target)
            mutual = my_partners & target_partners
            if direct_key in relay_best:
                for other in sorted(mutual):
                    _add_link(relay_best, my_call, other)
                    _add_link(relay_best, relay_target, other)
            else:
                path_nodes = _best_relay_path_nodes()
                for a, b in zip(path_nodes, path_nodes[1:]):
                    _add_link(relay_best, a, b)
        else:
            for (o, d), _data in best.items():
                _add_link(best, o, d)

        links = self._expand_observed_path_directions(links, directional_best, pos_map)

        # finalize stats: avg/max
        stats_out: Dict[str, Dict] = {}
        for cs, data in stat.items():
            snrs = data.get("snrs", [])
            avg_snr = sum(snrs) / len(snrs) if snrs else None
            max_snr = max(snrs) if snrs else None
            snrs_excl_my = data.get("snrs_excl_my", [])
            if not my_call:
                snrs_excl_my = snrs
            avg_snr_excl_my = sum(snrs_excl_my) / len(snrs_excl_my) if snrs_excl_my else None
            direct_snr = None
            direct_count = 0
            if my_call:
                key = tuple(sorted((my_call, cs)))
                vals = direct_snrs.get(key, [])
                if vals:
                    direct_snr = sum(vals) / len(vals)
                    direct_count = direct_counts.get(key, 0)
            try:
                seen_fmt = datetime.datetime.utcfromtimestamp(data.get("last_seen") or 0).strftime("%Y-%m-%d %H:%M:%S UTC") if data.get("last_seen") else ""
            except Exception:
                seen_fmt = ""
            try:
                spotter_fmt = datetime.datetime.utcfromtimestamp(data.get("last_spotter") or 0).strftime("%Y-%m-%d %H:%M:%S UTC") if data.get("last_spotter") else ""
            except Exception:
                spotter_fmt = ""
            stats_out[cs] = {
                "last_seen": data.get("last_seen") or 0,
                "last_spotter": data.get("last_spotter") or 0,
                "last_seen_fmt": seen_fmt,
                "last_spotter_fmt": spotter_fmt,
                "avg_snr": avg_snr,
                "max_snr": max_snr,
                "direct_snr": direct_snr,
                "avg_snr_excl_my": avg_snr_excl_my,
                "direct_count": direct_count,
                "avg_snr_count": data.get("snr_excl_my_count", 0),
                "last_band": data.get("last_band") or "",
            }
        self._query_cache_set(
            cache_key,
            (
                [dict(x) for x in links],
                {str(k): (dict(v) if isinstance(v, dict) else v) for k, v in stats_out.items()},
            ),
        )
        return links, stats_out

    def _load_varac_links(
        self,
        band_filter=None,
        my_call: str = "",
        link_selection: Optional[tuple[str, str]] = None,
        group_filter: str = "",
        region_filter: str = "",
        reachable_callsigns: Optional[Set[str]] = None,
        max_age_sec: Optional[int] = None,
    ) -> List[Dict]:
        band_sig = ""
        try:
            band_sig = json.dumps(band_filter or {"type": "all"}, sort_keys=True, default=str)
        except Exception:
            band_sig = str(band_filter or {"type": "all"})
        cache_key = (
            "varac_links",
            band_sig,
            (my_call or "").strip().upper(),
            str(link_selection or ""),
            (group_filter or "").strip().upper(),
            (region_filter or "").strip().upper(),
            ",".join(sorted({c.strip().upper() for c in (reachable_callsigns or set()) if c})),
            int(max_age_sec or 0),
            len(self.stations),
            self._get_user_latlon(),
            len(self.operator_index),
        )
        cached = self._query_cache_get(cache_key, ttl_sec=2.0)
        if isinstance(cached, list):
            return [dict(x) for x in cached if isinstance(x, dict)]
        links: List[Dict] = []
        pos_map: Dict[str, tuple[float, float]] = {}
        for pt in self._station_points_for_map():
            pos_map[pt.callsign.upper()] = (pt.lat, pt.lon)

        if isinstance(link_selection, (list, tuple)) and len(link_selection) >= 2:
            mode, selection_value = link_selection[0], link_selection[1]
        else:
            mode, selection_value = "off", ""
        mode = str(mode or "off").strip().lower()
        selection_value = (
            (selection_value or "").strip().upper()
            if mode in {"region", "group", "station", "relay_target"}
            else (selection_value or "")
        )
        relay_target = selection_value if mode == "relay_target" else ""
        group_filter = (group_filter or "").strip().upper()
        region_filter = (region_filter or "").strip().upper()
        reachable_calls = {c.strip().upper() for c in (reachable_callsigns or set()) if c}
        if mode == "off" and not relay_target:
            return links

        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return links
        if not db_path.exists():
            return links

        ts_cut = None
        if max_age_sec and max_age_sec > 0:
            ts_cut = time.time() - max_age_sec
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            where_parts: List[str] = []
            params: List[object] = []
            if ts_cut:
                where_parts.append("ts >= ?")
                params.append(ts_cut)
            if relay_target and my_call:
                # Path-to mode needs the recent local graph, not only edges touching
                # my station or the target. Example: me -> A -> B -> target.
                pass
            where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
            cur.execute(
                f"SELECT ts, origin, destination, snr, band, freq_hz FROM varac_links{where_sql} ORDER BY ts DESC LIMIT 20000",
                tuple(params),
            )
            rows = cur.fetchall()
            note_map_read_cap("varac_links", len(rows), 20000)
            conn.close()
        except Exception:
            return links

        def _freq_to_band(freq_mhz: Optional[float]) -> str:
            if freq_mhz is None:
                return ""
            bands = [
                ("160M", 1.8, 2.0),
                ("80M", 3.5, 4.0),
                ("60M", 5.0, 5.5),
                ("40M", 7.0, 7.3),
                ("30M", 10.1, 10.15),
                ("20M", 14.0, 14.35),
                ("17M", 18.068, 18.168),
                ("15M", 21.0, 21.45),
                ("12M", 24.89, 24.99),
                ("10M", 28.0, 29.7),
                ("6M", 50.0, 54.0),
                ("2M", 144.0, 148.0),
            ]
            for name, lo, hi in bands:
                if lo <= freq_mhz <= hi:
                    return name
            return ""

        def _station_matches_filters(cs: str) -> bool:
            if not cs:
                return False
            if group_filter:
                groups = self.operator_index.get(cs, {}).get("groups", set())
                if group_filter not in groups:
                    return False
            if region_filter:
                region = self.operator_index.get(cs, {}).get("region")
                if region != region_filter:
                    return False
            return True

        relay_best: Dict[tuple[str, str], Dict[str, object]] = {}
        best: Dict[tuple[str, str], Dict[str, object]] = {}
        directional_best: Dict[tuple[str, str], Dict[str, object]] = {}
        my_partners: Set[str] = set()
        target_partners: Set[str] = set()

        for ts, o, d, snr, band, freq_hz in rows:
            o = (o or "").upper()
            d = (d or "").upper()
            if o == "" or d == "" or o not in pos_map or d not in pos_map:
                continue
            bf = band_filter or {"type": "all"}
            try:
                freq_mhz = float(freq_hz) / 1_000_000.0 if freq_hz is not None else None
            except Exception:
                freq_mhz = None
            band_val = (band or "").upper() or _freq_to_band(freq_mhz)
            if bf.get("type") == "band":
                if band_val != str(bf.get("value")).upper():
                    continue
            elif bf.get("type") == "freq":
                target_f = bf.get("value")
                if freq_mhz is None or target_f is None or abs(freq_mhz - target_f) > 0.001:
                    continue

            include = False
            match_o = _station_matches_filters(o)
            match_d = _station_matches_filters(d)
            if mode == "my_station":
                include = bool(my_call) and my_call in {o, d}
            elif mode == "station" and selection_value:
                include = selection_value in {o, d}
            elif relay_target:
                include = bool(my_call)
            elif mode == "all":
                include = True
            elif mode == "region" and selection_value:
                region_o = self.operator_index.get(o, {}).get("region")
                region_d = self.operator_index.get(d, {}).get("region")
                if region_o == selection_value and region_d == selection_value:
                    include = True
                elif my_call and my_call in {o, d}:
                    other = d if o == my_call else o
                    include = self.operator_index.get(other, {}).get("region") == selection_value
            elif mode == "group" and selection_value:
                groups_o = self.operator_index.get(o, {}).get("groups", set())
                groups_d = self.operator_index.get(d, {}).get("groups", set())
                if selection_value in groups_o and selection_value in groups_d:
                    include = True
                elif my_call and my_call in {o, d}:
                    other = d if o == my_call else o
                    include = selection_value in self.operator_index.get(other, {}).get("groups", set())
            if include and group_filter:
                if my_call and my_call in {o, d}:
                    other = d if o == my_call else o
                    include = group_filter in self.operator_index.get(other, {}).get("groups", set())
                elif mode == "station" and selection_value and selection_value in {o, d}:
                    other = d if o == selection_value else o
                    include = (
                        group_filter in self.operator_index.get(selection_value, {}).get("groups", set())
                        or group_filter in self.operator_index.get(other, {}).get("groups", set())
                    )
                else:
                    include = group_filter in self.operator_index.get(o, {}).get("groups", set()) and group_filter in self.operator_index.get(d, {}).get("groups", set())
            if include and region_filter:
                if my_call and my_call in {o, d}:
                    other = d if o == my_call else o
                    include = self.operator_index.get(other, {}).get("region") == region_filter
                elif mode == "station" and selection_value and selection_value in {o, d}:
                    other = d if o == selection_value else o
                    include = (
                        self.operator_index.get(selection_value, {}).get("region") == region_filter
                        or self.operator_index.get(other, {}).get("region") == region_filter
                    )
                else:
                    region_o = self.operator_index.get(o, {}).get("region")
                    region_d = self.operator_index.get(d, {}).get("region")
                    include = region_o == region_filter and region_d == region_filter
            if include and reachable_calls:
                include = (o in reachable_calls or d in reachable_calls)
                if include and my_call:
                    include = my_call in {o, d}
            if not include:
                include = False

            if not include:
                continue
            p1 = pos_map.get(o)
            p2 = pos_map.get(d)
            if not p1 or not p2:
                continue
            try:
                snr_val = float(snr)
            except Exception:
                snr_val = None
            direction_key = (o, d)
            previous = directional_best.get(direction_key)
            if previous is None or (snr_val is not None and (previous.get("snr") is None or snr_val > previous["snr"])):
                directional_best[direction_key] = {"origin": o, "destination": d, "snr": snr_val, "ts": ts}
            if relay_target:
                key = tuple(sorted((o, d)))
                prev = relay_best.get(key)
                prev_snr = prev.get("snr") if isinstance(prev, dict) else None
                if key not in relay_best or (snr_val is not None and (prev_snr is None or snr_val > prev_snr)):
                    relay_best[key] = {"origin": o, "destination": d, "snr": snr_val, "ts": ts}
                if my_call and my_call in {o, d}:
                    my_partners.add(d if o == my_call else o)
                if relay_target in {o, d}:
                    target_partners.add(d if o == relay_target else o)
                continue
            key = tuple(sorted((o, d)))
            prev = best.get(key)
            prev_snr = prev.get("snr") if isinstance(prev, dict) else None
            if key not in best or (snr_val is not None and (prev_snr is None or snr_val > prev_snr)):
                best[key] = {"origin": o, "destination": d, "snr": snr_val, "ts": ts}

        def _add_link_from_best(data: Dict[str, object], a: str, b: str) -> None:
            origin = str(data.get("origin") or a or "").strip().upper()
            destination = str(data.get("destination") or b or "").strip().upper()
            p1 = pos_map.get(origin)
            p2 = pos_map.get(destination)
            if not p1 or not p2:
                return
            links.append(
                {
                    "origin": origin,
                    "destination": destination,
                    "lat1": p1[0],
                    "lon1": p1[1],
                    "lat2": p2[0],
                    "lon2": p2[1],
                    "snr": data.get("snr"),
                    "ts": data.get("ts"),
                }
            )

        def _add_relay_link(a: str, b: str) -> None:
            key = tuple(sorted((a, b)))
            data = relay_best.get(key)
            if not data:
                return
            origin = str(data.get("origin") or a or "").strip().upper()
            destination = str(data.get("destination") or b or "").strip().upper()
            p1 = pos_map.get(origin)
            p2 = pos_map.get(destination)
            if not p1 or not p2:
                return
            links.append(
                {
                    "origin": origin,
                    "destination": destination,
                    "lat1": p1[0],
                    "lon1": p1[1],
                    "lat2": p2[0],
                    "lon2": p2[1],
                    "snr": data.get("snr"),
                    "ts": data.get("ts"),
                }
            )

        def _best_relay_path_nodes(max_hops: int = 3) -> List[str]:
            if not relay_target or not my_call:
                return []
            graph: Dict[str, Set[str]] = {}
            for a, b in relay_best:
                graph.setdefault(a, set()).add(b)
                graph.setdefault(b, set()).add(a)
            if my_call not in graph or relay_target not in graph:
                return []
            queue_nodes = deque([(my_call, [my_call])])
            seen = {my_call}
            while queue_nodes:
                node, path = queue_nodes.popleft()
                if node == relay_target:
                    return path
                if len(path) > max_hops:
                    continue
                neighbors = sorted(
                    graph.get(node, set()) - seen,
                    key=lambda other: (
                        float(relay_best.get(tuple(sorted((node, other))), {}).get("snr") or -999),
                        float(relay_best.get(tuple(sorted((node, other))), {}).get("ts") or 0),
                    ),
                    reverse=True,
                )
                for other in neighbors:
                    seen.add(other)
                    queue_nodes.append((other, path + [other]))
            return []

        if relay_target and my_call:
            direct_key = tuple(sorted((my_call, relay_target)))
            _add_relay_link(my_call, relay_target)
            mutual = my_partners & target_partners
            if direct_key in relay_best:
                for other in sorted(mutual):
                    _add_relay_link(my_call, other)
                    _add_relay_link(relay_target, other)
            else:
                path_nodes = _best_relay_path_nodes()
                for a, b in zip(path_nodes, path_nodes[1:]):
                    _add_relay_link(a, b)
        else:
            for (o, d), data in best.items():
                _add_link_from_best(data, o, d)

        links = self._expand_observed_path_directions(links, directional_best, pos_map)
        self._query_cache_set(cache_key, [dict(x) for x in links])
        return links

    def _load_varac_stats(self, max_age_sec: Optional[int] = None) -> Dict[str, Dict]:
        cache_key = ("varac_stats", int(max_age_sec) if max_age_sec else None)
        cached = self._query_cache_get(cache_key)
        if isinstance(cached, dict):
            return {
                str(k): dict(v) if isinstance(v, dict) else v
                for k, v in cached.items()
            }
        stats: Dict[str, Dict] = {}
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return stats
        if not db_path.exists():
            return stats
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT callsign, last_seen_ts, last_band, last_freq_hz, last_snr
                FROM varac_callsign_stats
                """
            )
            rows = cur.fetchall()
            cur.execute(
                """
                SELECT callsign, AVG(snr) FROM (
                    SELECT origin AS callsign, snr FROM varac_links WHERE snr IS NOT NULL
                    UNION ALL
                    SELECT destination AS callsign, snr FROM varac_links WHERE snr IS NOT NULL
                )
                GROUP BY callsign
                """
            )
            avg_rows = cur.fetchall()
            conn.close()
        except Exception:
            return stats
        ts_cut = None
        if max_age_sec and max_age_sec > 0:
            ts_cut = time.time() - max_age_sec
        avg_map = {str(c or "").strip().upper(): float(a) for c, a in avg_rows if c and a is not None}
        def _freq_to_band(freq_hz: Optional[float]) -> str:
            if not freq_hz:
                return ""
            try:
                mhz = float(freq_hz) / 1_000_000.0
            except Exception:
                return ""
            bands = [
                ("160M", 1.8, 2.0),
                ("80M", 3.5, 4.0),
                ("60M", 5.0, 5.5),
                ("40M", 7.0, 7.3),
                ("30M", 10.1, 10.15),
                ("20M", 14.0, 14.35),
                ("17M", 18.068, 18.168),
                ("15M", 21.0, 21.45),
                ("12M", 24.89, 24.99),
                ("10M", 28.0, 29.7),
                ("6M", 50.0, 54.0),
                ("2M", 144.0, 148.0),
            ]
            for name, lo, hi in bands:
                if lo <= mhz <= hi:
                    return name
            return ""
        for cs, last_seen_ts, last_band, last_freq_hz, last_snr in rows:
            last_seen_val = float(last_seen_ts or 0.0)
            if ts_cut and last_seen_val and last_seen_val < ts_cut:
                continue
            band_val = (last_band or "").strip().upper()
            if band_val in {"NA", "N/A"}:
                band_val = ""
            if not band_val and last_freq_hz not in (None, ""):
                try:
                    band_val = _freq_to_band(float(last_freq_hz))
                except Exception:
                    band_val = ""
            stats[(cs or "").strip().upper()] = {
                "last_seen_ts": last_seen_val,
                "last_band": band_val,
                "last_freq_hz": float(last_freq_hz) if last_freq_hz not in (None, "") else None,
                "last_snr": float(last_snr) if last_snr not in (None, "") else None,
                "avg_snr": avg_map.get((cs or "").strip().upper()),
            }
        self._query_cache_set(cache_key, dict(stats))
        return stats

    def _load_js8_presence(self) -> Set[str]:
        cache_key = ("js8_presence",)
        cached = self._query_cache_get(cache_key)
        if isinstance(cached, (set, list, tuple)):
            return {str(c) for c in cached}
        calls: Set[str] = set()
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return calls
        if not db_path.exists():
            return calls
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT origin FROM js8_links")
            calls.update({str(r[0]).strip().upper() for r in cur.fetchall() if r and r[0]})
            cur.execute("SELECT DISTINCT destination FROM js8_links")
            calls.update({str(r[0]).strip().upper() for r in cur.fetchall() if r and r[0]})
            conn.close()
        except Exception:
            return calls
        self._query_cache_set(cache_key, set(calls))
        return calls

    def _load_fldigi_presence(self) -> Set[str]:
        cache_key = ("fldigi_presence",)
        cached = self._query_cache_get(cache_key)
        if isinstance(cached, (set, list, tuple)):
            return {str(c) for c in cached}
        calls: Set[str] = set()
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return calls
        if not db_path.exists():
            return calls
        def _base_callsign(val: str) -> str:
            cs_norm = (val or "").strip().upper()
            if not cs_norm:
                return ""
            cs_norm = re.sub(r"/(P|M|MM|QRP|SOTA|ROVER|[A-Z0-9]{1,4})$", "", cs_norm)
            match = re.search(r"\b[A-Z]{1,2}\d[A-Z0-9]{1,4}\b", cs_norm)
            if match:
                return match.group(0)
            return cs_norm
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            cur.execute("SELECT callsign FROM fldigi_checkins")
            checkins = {
                _base_callsign(str(r[0]).strip().upper())
                for r in cur.fetchall()
                if r and r[0]
            }
            cur.execute("SELECT callsign FROM fldigi_file_senders")
            senders = {
                _base_callsign(str(r[0]).strip().upper())
                for r in cur.fetchall()
                if r and r[0]
            }
            conn.close()
        except Exception:
            return calls
        out = {c for c in (checkins | senders) if c}
        self._query_cache_set(cache_key, set(out))
        return out

    def _load_commstat_reporter_activity(self, max_age_sec: Optional[int] = None) -> Dict[str, Dict[str, object]]:
        cache_key = ("commstat_reporter_activity", int(max_age_sec or 0), self._nets_db_fingerprint())
        cached = self._query_cache_get(cache_key)
        if isinstance(cached, dict):
            return {str(k): dict(v) if isinstance(v, dict) else {} for k, v in cached.items()}
        out: Dict[str, Dict[str, object]] = {}
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return out
        if not db_path.exists():
            return out
        cutoff = time.time() - int(max_age_sec or 0) if int(max_age_sec or 0) > 0 else 0.0
        try:
            with connect_sqlite_readonly(db_path) as conn:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='commstat_artifacts'"
                ).fetchone()
                if not exists:
                    self._query_cache_set(cache_key, out)
                    return out
                cols = {
                    str(row[1] or "").strip()
                    for row in conn.execute("PRAGMA table_info(commstat_artifacts)").fetchall()
                    if len(row) > 1 and str(row[1] or "").strip()
                }
                event_ts_expr = "event_ts_utc" if "event_ts_utc" in cols else "event_ts" if "event_ts" in cols else "0"
                rows = conn.execute(
                    f"""
                    SELECT from_call, report_group, transport_mode, reach_mode, {event_ts_expr}
                    FROM commstat_artifacts
                    WHERE COALESCE(from_call, '') != ''
                    """
                ).fetchall()
        except Exception as exc:
            log.debug("StationsMap: failed to load CommStat reporter activity: %s", exc)
            self._query_cache_set(cache_key, out)
            return out
        for from_call, report_group, transport_mode, reach_mode, event_ts in rows:
            call = str(from_call or "").strip().upper()
            if not call:
                continue
            parsed_ts = parse_utc_timestamp(event_ts)
            ts_val = float(parsed_ts or self._safe_float(event_ts, 0.0))
            if cutoff and (ts_val <= 0.0 or ts_val < cutoff):
                continue
            current = out.get(call, {})
            if ts_val >= self._safe_float(current.get("last_seen_ts"), 0.0):
                out[call] = {
                    "last_seen_ts": ts_val,
                    "report_group": str(report_group or "").strip(),
                    "transport_mode": str(transport_mode or "").strip(),
                    "reach_mode": str(reach_mode or "").strip(),
                }
        self._query_cache_set(cache_key, dict(out))
        return out

    @staticmethod
    def _settings_bool(settings: SettingsManager, key: str, default: bool) -> bool:
        try:
            value = settings.get(key, default)
        except Exception:
            value = default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        txt = str(value or "").strip().lower()
        if txt in {"1", "true", "yes", "on", "enabled"}:
            return True
        if txt in {"0", "false", "no", "off", "disabled"}:
            return False
        return bool(default)

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return float(default)

    @staticmethod
    def _sitrep_status_label(status_key: str) -> str:
        key = (status_key or "").strip().lower()
        if key == "red":
            return "Not Functioning"
        if key == "yellow":
            return "Partially Functioning"
        if key == "green":
            return "Functioning"
        return "Unknown"

    @staticmethod
    def _sitrep_status_chip(status_key: str) -> str:
        key = (status_key or "").strip().lower()
        if key == "red":
            return "R"
        if key == "yellow":
            return "Y"
        if key == "green":
            return "G"
        return "?"

    @staticmethod
    def _source_short_label(source: str) -> str:
        return source_short_label(source)

    @classmethod
    def _encode_source_chips(cls, source_summary: Dict[str, str]) -> str:
        if not source_summary:
            return ""
        parts: List[str] = []
        for source in sorted(source_summary.keys()):
            key = str(source_summary.get(source) or "unknown").strip().lower()
            if key not in {"red", "yellow", "green", "unknown", "not_reported"}:
                key = "unknown"
            if key == "not_reported":
                key = "unknown"
            parts.append(f"{cls._source_short_label(source)}:{cls._sitrep_status_chip(key)}")
        return " ".join(parts)

    @staticmethod
    def _decode_source_summary(value: object) -> Dict[str, str]:
        txt = str(value or "").strip()
        if not txt:
            return {}
        try:
            obj = json.loads(txt)
            if isinstance(obj, dict):
                out: Dict[str, str] = {}
                for raw_src, raw_status in obj.items():
                    src = str(raw_src or "").strip().upper()
                    status = str(raw_status or "").strip().lower()
                    if not src:
                        continue
                    if status not in {"red", "yellow", "green", "unknown", "not_reported"}:
                        status = "unknown"
                    out[src] = "unknown" if status == "not_reported" else status
                return out
        except Exception:
            pass
        return {}

    @staticmethod
    def _sitrep_conflict(source_summary: Dict[str, str]) -> bool:
        vals = {
            str(v or "").strip().lower()
            for v in source_summary.values()
            if str(v or "").strip().lower() in {"red", "yellow", "green", "unknown"}
        }
        return len(vals) > 1

    @staticmethod
    def _sitrep_age_text(ts_val: float) -> str:
        ts = float(ts_val or 0.0)
        if ts <= 0:
            return ""
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        age = max(0, int(now - ts))
        if age < 60:
            return f"{age}s"
        mins, sec = divmod(age, 60)
        if mins < 60:
            return f"{mins}m {sec}s"
        hrs, mins = divmod(mins, 60)
        if hrs < 24:
            return f"{hrs}h {mins}m"
        days, hrs = divmod(hrs, 24)
        return f"{days}d {hrs}h"

    @staticmethod
    def _active_sitrep_status_key(status_key: str, updated_ts: float, *, now_ts: Optional[float] = None) -> str:
        key = (status_key or "").strip().lower()
        if key not in {"red", "yellow", "green", "unknown", "not_reported"}:
            return "unknown"
        if key == "not_reported":
            return "unknown"
        if key in {"red", "yellow"}:
            try:
                ts = float(updated_ts or 0.0)
            except Exception:
                ts = 0.0
            if ts > 0:
                current = time.time() if now_ts is None else float(now_ts)
                if current - ts > SITREP_ACTIVE_STATUS_MAX_AGE_SECONDS:
                    return "unknown"
        return key

    def _load_spotter_station_status(self) -> Dict[str, Dict]:
        unified_enabled = self._settings_bool(self.settings, "sitrep_unified_map_enabled", True)
        cache_key = ("spotter_station_status", unified_enabled, self._nets_db_fingerprint())
        cached = self._query_cache_get(cache_key)
        if isinstance(cached, dict):
            return {
                str(k): dict(v) if isinstance(v, dict) else v
                for k, v in cached.items()
            }
        statuses: Dict[str, Dict] = {}
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return statuses
        if not db_path.exists():
            return statuses
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT from_call, status_key, status_label, updated_utc_ts, updated_utc_str, response_code,
                           status_source, status_source_detail
                    FROM spotter_station_status
                    """
                )
            except sqlite3.OperationalError:
                try:
                    cur.execute(
                        """
                        SELECT from_call, status_key, status_label, updated_utc_ts, updated_utc_str, response_code,
                               '' AS status_source, '' AS status_source_detail
                        FROM spotter_station_status
                        """
                    )
                    rows = cur.fetchall()
                except sqlite3.OperationalError:
                    rows = []
            else:
                rows = cur.fetchall()
            fused_rows = []
            if unified_enabled:
                try:
                    cur.execute(
                        """
                        SELECT
                            callsign,
                            effective_status,
                            latest_event_ts,
                            latest_event_ts_utc,
                            latest_report_group,
                            latest_transport_mode,
                            latest_state_code,
                            latest_state_confidence,
                            latest_geo_confidence,
                            latest_brevity_summary,
                            source_summary_json
                        FROM sitrep_latest_by_callsign
                        """
                    )
                    fused_rows = cur.fetchall()
                except Exception:
                    fused_rows = []
            conn.close()
        except Exception:
            return statuses

        for from_call, status_key, status_label, updated_utc_ts, updated_utc_str, response_code, status_source, status_source_detail in rows:
            call = (from_call or "").strip().upper()
            if not call:
                continue
            key = (status_key or "").strip().lower()
            updated_ts = self._safe_float(updated_utc_ts, 0.0)
            key = self._active_sitrep_status_key(key, updated_ts)
            updated_str = (updated_utc_str or "").strip()
            if not updated_str and updated_ts > 0:
                try:
                    updated_str = datetime.datetime.utcfromtimestamp(updated_ts).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    updated_str = ""
            source = (status_source or "").strip().upper()
            source_summary = {source: key} if source else {}
            source_chips = self._encode_source_chips(source_summary)
            status_label_text = (
                self._sitrep_status_label(key) if key == "unknown"
                else ((status_label or "").strip() or self._sitrep_status_label(key))
            )
            source_detail = (status_source_detail or "").strip()
            if not source_detail and source_chips:
                source_detail = source_chips
            statuses[call] = {
                "status_key": key,
                "status_label": status_label_text,
                "updated_utc_ts": updated_ts,
                "updated_utc_str": updated_str,
                "response_code": (response_code or "").strip(),
                "status_source": source_family_label(source),
                "status_source_detail": source_detail,
                "status_source_chips": source_chips,
                "status_conflict": False,
                "status_age": self._sitrep_age_text(updated_ts),
                "status_source_count": len(source_summary),
                "report_group": "",
                "transport_label": "",
                "state_code": "",
                "state_confidence": "",
                "geo_confidence": "",
                "brevity_summary": "",
            }

        sitrep_report_groups: Set[str] = set()
        for (
            callsign,
            effective_status,
            latest_event_ts,
            latest_event_ts_utc,
            latest_report_group,
            latest_transport_mode,
            latest_state_code,
            latest_state_confidence,
            latest_geo_confidence,
            latest_brevity_summary,
            source_summary_json,
        ) in fused_rows:
            call = (callsign or "").strip().upper()
            if not call:
                continue
            key = str(effective_status or "").strip().lower()
            summary = self._decode_source_summary(source_summary_json)
            if not summary:
                summary = {"FUSED": key}
            source_count = len(summary)
            source_chips = self._encode_source_chips(summary)
            summary_source = str(next(iter(summary.keys()), "FUSED") or "").strip()
            if source_count > 1 or summary_source.upper() == "FUSED":
                source = "Multiple Sources"
            else:
                source = source_family_label(summary_source)
            updated_ts = self._safe_float(latest_event_ts, 0.0)
            key = self._active_sitrep_status_key(key, updated_ts)
            updated_str = (latest_event_ts_utc or "").strip()
            if not updated_str and updated_ts > 0:
                try:
                    updated_str = datetime.datetime.utcfromtimestamp(updated_ts).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    updated_str = ""
            candidate = {
                "status_key": key,
                "status_label": self._sitrep_status_label(key),
                "updated_utc_ts": updated_ts,
                "updated_utc_str": updated_str,
                "response_code": "",
                "status_source": source,
                "status_source_detail": "Unified SitRep",
                "status_source_chips": source_chips,
                "status_conflict": self._sitrep_conflict(summary),
                "status_age": self._sitrep_age_text(updated_ts),
                "status_source_count": source_count,
                "report_group": str(latest_report_group or "").strip().upper(),
                "transport_label": transport_label(latest_transport_mode),
                "state_code": str(latest_state_code or "").strip().upper(),
                "state_confidence": str(latest_state_confidence or "").strip().lower(),
                "geo_confidence": str(latest_geo_confidence or "").strip().lower(),
                "brevity_summary": str(latest_brevity_summary or "").strip(),
            }
            if candidate["report_group"]:
                sitrep_report_groups.add(candidate["report_group"])
            existing = statuses.get(call)
            if existing is None:
                statuses[call] = candidate
                continue
            existing_source = str(existing.get("status_source") or "").strip().upper()
            existing_ts = self._safe_float(existing.get("updated_utc_ts"), 0.0)
            # Preserve manual operator override until newer unified data arrives.
            if existing_source == "MANUAL" and existing_ts >= updated_ts:
                continue
            if updated_ts >= existing_ts:
                statuses[call] = candidate
        report_groups_sorted = sorted(sitrep_report_groups)
        if report_groups_sorted != self._sitrep_report_groups:
            self._sitrep_report_groups = report_groups_sorted
            if hasattr(self, "group_filter_combo"):
                self._refresh_group_filter_options()

        self._query_cache_set(cache_key, dict(statuses))
        return statuses

    def _spotter_form_codes_for_flag(self, flag: str) -> Optional[set[str]]:
        try:
            return form_codes_enabled_for(self.settings, flag=flag)
        except Exception:
            return None

    def _load_spotter_map_activity(self) -> Dict[str, Dict]:
        map_codes = self._spotter_form_codes_for_flag("map")
        if map_codes is not None:
            weather_codes = self._spotter_weather_form_codes_for_map() or set()
            map_codes = set(map_codes) - set(weather_codes)
        if map_codes is not None and not map_codes:
            return {}
        cache_key = ("spotter_map_activity", tuple(sorted(map_codes)) if map_codes is not None else "__legacy__")
        cached = self._query_cache_get(cache_key)
        if isinstance(cached, dict):
            return {str(k): dict(v) if isinstance(v, dict) else v for k, v in cached.items()}
        out: Dict[str, Dict] = {}
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return out
        if not db_path.exists():
            return out
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            base_sql = """
                SELECT from_call, form_id, utc_ts, utc_str, decoded_text
                FROM spotter_traffic
            """
            params: tuple = ()
            if map_codes is not None:
                form_ids = sorted(code[2:] for code in map_codes if code.startswith("F!"))
                placeholders = ",".join(["?"] * len(form_ids))
                base_sql += f" WHERE form_id IN ({placeholders})"
                params = tuple(form_ids)
            else:
                base_sql += " WHERE 1=0"
            cur.execute(base_sql + " ORDER BY COALESCE(utc_ts, 0) DESC, id DESC LIMIT 1000", params)
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log.debug("StationsMap: failed to load mapped Spotter form activity: %s", e)
            return out
        for from_call, form_id, utc_ts, utc_str, decoded_text in rows:
            call = (from_call or "").strip().upper()
            if not call or call in out:
                continue
            form = str(form_id or "").strip().upper()
            if form and not form.startswith("F!"):
                form = f"F!{form}"
            first_line = ""
            if decoded_text:
                first_line = str(decoded_text or "").strip().splitlines()[0][:80]
            out[call] = {
                "form_id": form,
                "utc_ts": self._safe_float(utc_ts, 0.0),
                "utc_str": str(utc_str or "").strip(),
                "summary": first_line,
            }
        self._query_cache_set(cache_key, dict(out))
        return out

    def _spotter_weather_form_codes_for_map(self) -> Optional[set[str]]:
        try:
            return forms_enabled_for(self.settings, purpose=PURPOSE_WEATHER, flag="map")
        except Exception:
            return set()

    def _spotter_alert_form_codes_for_map(self) -> set[str]:
        try:
            return forms_enabled_for(self.settings, purpose=PURPOSE_HAZARD, flag="map") or set()
        except Exception:
            return set()

    def _spotter_infrastructure_form_codes_for_map(self) -> set[str]:
        try:
            codes = forms_enabled_for(self.settings, purpose=PURPOSE_INFRASTRUCTURE, flag="map") or set()
            status_codes = forms_enabled_for(self.settings, flag="status") or set()
            codes |= {
                code
                for code in status_codes
                if code in {"F!301", "F!304", "F!306", "F!701B", "F!701C"}
            }
            return codes
        except Exception:
            return set()

    @staticmethod
    def _classify_weather_text(text: object) -> tuple[str, str]:
        lower = str(text or "").lower()
        severe_terms = (
            "tornado",
            "warning",
            "flash flood",
            "flooding",
            "wildfire",
            "evacuation",
            "damaging wind",
            "hail",
            "severe",
        )
        caution_terms = (
            "watch",
            "thunderstorm",
            "lightning",
            "heavy rain",
            "high wind",
            "ice",
            "freezing",
            "snow",
            "smoke",
            "heat",
        )
        severity = "routine"
        if any(term in lower for term in severe_terms):
            severity = "severe"
        elif any(term in lower for term in caution_terms):
            severity = "caution"

        strong_wind = any(term in lower for term in ("high wind", "damaging wind", "gust", "wind gust"))
        if any(term in lower for term in ("wildfire", "fire", "smoke")):
            icon = "fire"
        elif any(term in lower for term in ("flood", "high water", "washed out")):
            icon = "flood"
        elif any(term in lower for term in ("tornado", "thunderstorm", "lightning", "hail")):
            icon = "storm"
        elif any(term in lower for term in ("snow", "ice", "freezing", "sleet")):
            icon = "snow"
        elif any(term in lower for term in ("heat", "hot", "temperature")):
            icon = "heat"
        elif strong_wind:
            icon = "wind"
        elif any(term in lower for term in ("rain", "precip", "drizzle")):
            icon = "rain"
        elif any(term in lower for term in ("wind", "breeze")):
            icon = "wind"
        else:
            icon = "general"
        return icon, severity

    @staticmethod
    def _summarize_weather_text(text: object, *, max_len: int = 150) -> str:
        lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
        if not lines:
            return "Weather report received"
        preferred_terms = (
            "weather",
            "storm",
            "rain",
            "wind",
            "snow",
            "ice",
            "flood",
            "fire",
            "smoke",
            "temperature",
            "visibility",
            "remarks",
            "narrative",
            "status",
        )
        picked: List[str] = []
        for line in lines:
            lower = line.lower()
            if "(no response)" in lower or "unknown" == lower:
                continue
            if any(term in lower for term in preferred_terms):
                picked.append(line)
            if len(picked) >= 3:
                break
        if not picked:
            picked = [line for line in lines[:3] if "(no response)" not in line.lower()]
        summary = "; ".join(picked).strip() or "Weather report received"
        return summary[: max_len - 1].rstrip() + "..." if len(summary) > max_len else summary

    def _load_spotter_weather_reports(self) -> List[Dict[str, object]]:
        weather_codes = self._spotter_weather_form_codes_for_map()
        if weather_codes is not None and not weather_codes:
            return []
        cache_key = ("spotter_weather_reports", tuple(sorted(weather_codes)) if weather_codes is not None else "__none__")
        cached = self._query_cache_get(cache_key)
        if isinstance(cached, list):
            return [dict(row) for row in cached if isinstance(row, dict)]
        out: List[Dict[str, object]] = []
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return out
        if not db_path.exists():
            return out
        cutoff = time.time() - WEATHER_REPORT_MAX_AGE_SEC
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            form_ids = sorted(code[2:] for code in (weather_codes or set()) if str(code).startswith("F!"))
            if not form_ids:
                conn.close()
                return out
            placeholders = ",".join(["?"] * len(form_ids))
            cur.execute(
                f"""
                SELECT from_call, form_id, utc_ts, utc_str, decoded_text, raw_text
                FROM spotter_traffic
                WHERE form_id IN ({placeholders})
                  AND COALESCE(utc_ts, 0) >= ?
                ORDER BY COALESCE(utc_ts, 0) DESC, id DESC
                LIMIT 1500
                """,
                tuple(form_ids) + (cutoff,),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log.debug("StationsMap: failed to load Spotter weather reports: %s", e)
            return out
        for from_call, form_id, utc_ts, utc_str, decoded_text, raw_text in rows:
            call = str(from_call or "").strip().upper()
            if not call:
                continue
            text = str(decoded_text or raw_text or "")
            icon, severity = self._classify_weather_text(text)
            form = str(form_id or "").strip().upper()
            if form and not form.startswith("F!"):
                form = f"F!{form}"
            out.append(
                {
                    "callsign": call,
                    "form_id": form,
                    "utc_ts": self._safe_float(utc_ts, 0.0),
                    "utc_str": str(utc_str or "").strip(),
                    "summary": self._summarize_weather_text(text),
                    "icon": icon,
                    "severity": severity,
                }
            )
        self._query_cache_set(cache_key, list(out))
        return out

    @staticmethod
    def _classify_alert_text(text: object) -> tuple[str, str]:
        lower = str(text or "").lower()
        if any(term in lower for term in ("warning", "evacuation", "immediate", "urgent", "emergency", "severe")):
            severity = "severe"
        elif any(term in lower for term in ("watch", "alert", "awareness", "prepare", "activation", "expected")):
            severity = "caution"
        else:
            severity = "routine"
        if any(term in lower for term in ("evacuation", "shelter")):
            icon = "evacuation"
        elif any(term in lower for term in ("rfi", "request for information", "information needed")):
            icon = "rfi"
        elif any(term in lower for term in ("warning", "alert", "awareness", "emergency")):
            icon = "warning"
        else:
            icon = "notice"
        return icon, severity

    @staticmethod
    def _classify_infrastructure_text(text: object) -> tuple[str, str]:
        lower = str(text or "").lower()
        if any(term in lower for term in ("not functioning", "down", "outage", "failed", "unavailable", "not available")):
            severity = "severe"
        elif any(term in lower for term in ("partially", "degraded", "unstable", "intermittent", "limited", "reduced")):
            severity = "caution"
        elif any(term in lower for term in ("functioning", "stable", "normal", "available")):
            severity = "routine"
        else:
            severity = "unknown"
        if any(term in lower for term in ("road", "bridge", "transport", "closure", "highway", "route")):
            icon = "transport"
        elif any(term in lower for term in ("general utility", "local services", "utility status")):
            icon = "utility"
        elif any(term in lower for term in ("water", "public water", "sewage", "waste")):
            icon = "water"
        elif any(term in lower for term in ("internet", "phone", "cell", "communications", "radio", "comms")):
            icon = "comms"
        elif any(term in lower for term in ("power", "grid", "generator", "electric")):
            icon = "power"
        else:
            icon = "utility"
        return icon, severity

    @staticmethod
    def _summarize_operational_text(text: object, *, max_len: int = 160) -> str:
        lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
        useful = [
            line
            for line in lines
            if "(no response)" not in line.lower()
            and line.lower() not in {"unknown", "n/a"}
        ]
        summary = "; ".join(useful[:3]).strip() or "Report received"
        return summary[: max_len - 1].rstrip() + "..." if len(summary) > max_len else summary

    def _load_spotter_layer_reports(
        self,
        *,
        layer_name: str,
        form_codes: set[str],
        max_age_sec: int,
        classifier,
        summarizer,
    ) -> List[Dict[str, object]]:
        if not form_codes:
            return []
        cache_key = (f"spotter_{layer_name}_reports", tuple(sorted(form_codes)), int(max_age_sec or 0))
        cached = self._query_cache_get(cache_key)
        if isinstance(cached, list):
            return [dict(row) for row in cached if isinstance(row, dict)]
        out: List[Dict[str, object]] = []
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return out
        if not db_path.exists():
            return out
        cutoff = time.time() - max_age_sec if max_age_sec and max_age_sec > 0 else 0.0
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            form_ids = sorted(code[2:] for code in form_codes if str(code).startswith("F!"))
            if not form_ids:
                conn.close()
                return out
            placeholders = ",".join(["?"] * len(form_ids))
            cur.execute(
                f"""
                SELECT from_call, form_id, utc_ts, utc_str, decoded_text, raw_text
                FROM spotter_traffic
                WHERE form_id IN ({placeholders})
                  AND COALESCE(utc_ts, 0) >= ?
                ORDER BY COALESCE(utc_ts, 0) DESC, id DESC
                LIMIT 1500
                """,
                tuple(form_ids) + (cutoff,),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log.debug("StationsMap: failed to load Spotter %s reports: %s", layer_name, e)
            return out
        for from_call, form_id, utc_ts, utc_str, decoded_text, raw_text in rows:
            call = str(from_call or "").strip().upper()
            if not call:
                continue
            text = str(decoded_text or raw_text or "")
            icon, severity = classifier(text)
            form = str(form_id or "").strip().upper()
            if form and not form.startswith("F!"):
                form = f"F!{form}"
            out.append(
                {
                    "callsign": call,
                    "form_id": form,
                    "utc_ts": self._safe_float(utc_ts, 0.0),
                    "utc_str": str(utc_str or "").strip(),
                    "summary": summarizer(text),
                    "icon": icon,
                    "severity": severity,
                }
            )
        self._query_cache_set(cache_key, list(out))
        return out

    def _load_spotter_alert_reports(self) -> List[Dict[str, object]]:
        return self._load_spotter_layer_reports(
            layer_name="alert",
            form_codes=self._spotter_alert_form_codes_for_map(),
            max_age_sec=ALERT_REPORT_MAX_AGE_SEC,
            classifier=self._classify_alert_text,
            summarizer=self._summarize_operational_text,
        )

    def _load_spotter_infrastructure_reports(self) -> List[Dict[str, object]]:
        return self._load_spotter_layer_reports(
            layer_name="infrastructure",
            form_codes=self._spotter_infrastructure_form_codes_for_map(),
            max_age_sec=INFRASTRUCTURE_REPORT_MAX_AGE_SEC,
            classifier=self._classify_infrastructure_text,
            summarizer=self._summarize_operational_text,
        )

    def _load_observation_operational_reports(
        self,
        *,
        layer_name: str,
        max_age_sec: int,
    ) -> List[Dict[str, object]]:
        """Load read-only observation projection rows for map review layers."""
        if not self._effective_map_observation_focus_enabled():
            return []
        focus_mode = (
            self._effective_map_report_focus_mode()
            if layer_name == "report_focus"
            else self._effective_map_observation_focus_mode()
        )
        topic_filter = self._selected_map_topic_filter()
        search_text = self._selected_map_search_text()
        group_filter = self._selected_map_group_filter()
        region_filter = self._selected_map_region_filter()
        advanced_sig = self._map_advanced_filters_signature()
        cache_key = (
            f"observation_{layer_name}_reports",
            int(max_age_sec or 0),
            focus_mode,
            topic_filter,
            search_text,
            group_filter,
            region_filter,
            advanced_sig,
        )
        cached = self._query_cache_get(cache_key, ttl_sec=6.0)
        if isinstance(cached, list):
            return [dict(row) for row in cached if isinstance(row, dict)]
        out: List[Dict[str, object]] = []
        focus_is_report_review = focus_mode in {"hf_reports", "local_reports", "all_reports", "mesh_nodes"}
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return out
        if not db_path.exists():
            return out
        since_utc = ""
        if max_age_sec and max_age_sec > 0:
            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=max_age_sec)
            since_utc = cutoff.replace(microsecond=0).isoformat()
        wanted_sources = self._observation_focus_sources(focus_mode)
        try:
            view_rows = map_observation_rows(
                db_path,
                ObservationQuery(
                    source_families=tuple(sorted(wanted_sources)),
                    since_utc=since_utc,
                    limit=1500,
                ),
                layer_enabled=True,
                readonly=True,
                allow_unconfirmed_local=False,
                exercise_layer=False,
            )
        except Exception as e:
            log.debug("StationsMap: failed to load observation %s reports: %s", layer_name, e)
            raise

        # Enrich only the bounded observation result, never the whole archive.
        metadata_lookup = self._message_file_metadata_lookup(
            db_path, paths=tuple(self._observation_file_path(row.observation) for row in view_rows)
        )
        commstat_lookup = self._commstat_artifact_metadata_lookup(
            db_path, source_refs=tuple(str(row.observation.source_ref or "") for row in view_rows)
        )
        for view_row in view_rows:
            obs = view_row.observation
            source_family = str(obs.source_family or "").strip().lower()
            if source_family not in wanted_sources:
                continue
            source_ref = str(getattr(obs, "source_ref", "") or "").strip().lower()
            if focus_mode == "mesh_nodes" and not source_ref.startswith("mesh-node:"):
                continue
            if focus_mode != "mesh_nodes" and source_ref.startswith("mesh-node:"):
                continue
            metadata = self._metadata_for_observation(obs, metadata_lookup)
            if source_family == "commstat":
                artifact_meta = commstat_lookup.get(str(obs.source_ref or "").strip())
                if isinstance(artifact_meta, dict):
                    merged_metadata = dict(metadata)
                    merged_metadata.update({k: v for k, v in artifact_meta.items() if v not in (None, "", (), [])})
                    metadata = merged_metadata
            meta_grid_for_position = str(metadata.get("grid") or "").strip().upper()
            meta_has_usable_position = self._map_grid_looks_usable(meta_grid_for_position)
            if not self._observation_matches_map_scope(
                obs,
                group_filter=group_filter,
                region_filter=region_filter,
            ):
                continue
            if not self._observation_matches_advanced_filters(obs, metadata):
                continue
            if not self._observation_matches_map_search(obs, search_text, metadata):
                continue
            eligibility = view_row.map_eligibility
            eligibility_allowed = bool(eligibility is not None and eligibility.allowed)
            if (
                not eligibility_allowed
                and source_family == "condition_alert"
                and str(obs.from_call or "").strip()
            ):
                # Condition alerts often carry the sender/target and condition
                # level, not a report grid. Let the event builder place them
                # from the station/roster lookup when available.
                eligibility_allowed = True
            if not eligibility_allowed and layer_name == "report_focus" and meta_has_usable_position:
                # FLMsg/FLAmp/Spotter metadata often has the report grid even
                # when the raw observation row was created before the file was
                # decoded. In report views, prefer the decoded message metadata
                # so topic/search filters can still place the traffic on the map.
                eligibility_allowed = True
            if not eligibility_allowed:
                continue
            topics = {str(topic).strip() for topic in obs.observed_topics if str(topic).strip()}
            topics.update(str(topic).strip() for topic in (metadata.get("topics") or ()) if str(topic).strip())
            if not self._observation_matches_topic_filter(obs, metadata, topic_filter):
                continue
            if layer_name == "report_focus" and source_family == "condition_alert":
                include = True
                icon, severity = "warning", "caution"
            elif layer_name == "report_focus":
                classifier = self._classify_infrastructure_text
                text = " ".join(part for part in (obs.subject, obs.summary, " ".join(sorted(topics))) if part)
                icon, severity = classifier(text)
                include = bool(
                    getattr(obs, "operator_attention", False)
                    or topics
                    or str(obs.subject or "").strip()
                    or str(obs.summary or "").strip()
                )
            elif layer_name == "alert" and source_family == "condition_alert":
                include = True
                icon, severity = "warning", "caution"
            elif source_family == "condition_alert":
                include = False
                icon, severity = "warning", "caution"
            elif layer_name == "alert":
                include = bool(
                    topics.intersection(ALERT_MAP_TOPICS)
                    or str(obs.status or "").strip().upper() in {"WATCH", "PRIORITY", "EMERGENCY", "RED", "YELLOW"}
                )
                classifier = self._classify_alert_text
                text = " ".join(part for part in (obs.subject, obs.summary, " ".join(sorted(topics))) if part)
                icon, severity = classifier(text)
            else:
                classifier = self._classify_infrastructure_text
                text = " ".join(part for part in (obs.subject, obs.summary, " ".join(sorted(topics))) if part)
                icon, severity = classifier(text)
                if focus_mode == "rf_pins":
                    include = source_family == "rf_pin"
                elif focus_mode == "mesh_nodes":
                    include = True
                elif focus_is_report_review:
                    # Report review modes should reflect the same human-readable
                    # traffic universe as Messages. A mapped FLMsg/FLAmp/Spotter/
                    # CommStat report with useful intelligence should not vanish
                    # merely because its topics are outside a narrow legacy layer.
                    include = bool(
                        getattr(obs, "operator_attention", False)
                        or topics
                        or str(obs.subject or "").strip()
                        or str(obs.summary or "").strip()
                    )
                    if topics.intersection(ALERT_MAP_TOPICS) and layer_name == "infrastructure":
                        # Alert-worthy observations already render in the alert
                        # layer; avoid drawing the same report twice.
                        include = False
                else:
                    include = bool(topics.intersection(INFRASTRUCTURE_MAP_TOPICS))
            if not include:
                continue
            # A reported condition owns its color. Text classification chooses
            # the topic symbol and is only a fallback when no status was reported.
            report_status = StationsMapTab._map_report_status_key(metadata)
            if report_status:
                severity = report_status
            topic_icon = self._map_icon_for_topics(sorted(topics), preferred_topic=topic_filter)
            if topic_icon and icon not in {"warning"}:
                icon = topic_icon
            form = str((obs.provenance or {}).get("form_name", "") or "").strip()
            display_type = str(metadata.get("display_type") or metadata.get("msg_type") or "").strip()
            if display_type and display_type.upper() not in {"FLMSG", "FLAMP"}:
                form = display_type
            obs_grid = str(obs.grid or "").strip().upper()
            meta_grid = meta_grid_for_position if self._map_grid_looks_usable(meta_grid_for_position) else ""
            effective_grid = meta_grid or (obs_grid if self._map_grid_looks_usable(obs_grid) else "")
            use_observation_coordinates = not meta_grid
            meta_state = str(metadata.get("state") or "").strip().upper()
            meta_title = str(metadata.get("title") or "").strip()
            report_ts = self._safe_float(metadata.get("report_ts"), 0.0)
            provenance = obs.provenance if isinstance(obs.provenance, Mapping) else {}
            scope_text = str(provenance.get("scope") or metadata.get("scope") or "").strip()
            state_confidence = str(provenance.get("state_confidence") or "").strip()
            geo_confidence = str(provenance.get("geo_confidence") or obs.location_confidence or "").strip()
            effective_state = meta_state or str(obs.state or "").strip().upper()
            if source_family == "commstat":
                resolved_state, inferred_state_conf, inferred_geo_conf = resolve_commstat_reported_for_state(
                    state_code=effective_state,
                    grid=effective_grid,
                    scope=scope_text,
                    remarks=str(
                        provenance.get("body_text")
                        or provenance.get("remarks_text")
                        or obs.subject
                        or obs.summary
                        or ""
                    ).strip(),
                )
                if resolved_state:
                    effective_state = str(resolved_state or "").strip().upper()
                    state_confidence = state_confidence or inferred_state_conf
                    geo_confidence = geo_confidence or inferred_geo_conf
            group_values: List[str] = []
            for raw_group in (
                metadata.get("to_call"),
                obs.to_target,
                *(obs.groups or ()),
            ):
                group = str(raw_group or "").strip().upper().lstrip("@").rstrip(">")
                if group and group not in group_values:
                    group_values.append(group)
            search_parts = [
                metadata.get("search_text"),
                meta_title,
                metadata.get("display_type"),
                metadata.get("msg_type"),
                obs.subject,
                obs.summary,
                form,
                obs.from_call,
                obs.to_target,
                " ".join(group_values),
                " ".join(sorted(topics)),
                effective_grid,
                effective_state,
            ]
            eligibility_reason = (
                getattr(eligibility, "reason_text", "") if eligibility is not None else "placed from message metadata"
            )
            out.append(
                {
                    "callsign": str(metadata.get("from_call") or obs.from_call or "").strip().upper(),
                    "from_call": str(metadata.get("from_call") or obs.from_call or "").strip().upper(),
                    "form_id": form,
                    "utc_ts": report_ts or self._observation_ts(obs.event_utc or obs.received_utc),
                    "utc_str": str(obs.event_utc or obs.received_utc or "").strip(),
                    "summary": str(meta_title or obs.subject or obs.summary or "Observation received").strip(),
                    "title": str(meta_title or obs.subject or "").strip(),
                    "icon": icon,
                    "severity": severity,
                    "status": report_status,
                    "lat": obs.lat if use_observation_coordinates else None,
                    "lon": obs.lon if use_observation_coordinates else None,
                    "grid": effective_grid,
                    "source_family": obs.source_family,
                    "source_label": str(metadata.get("source_label") or "").strip()
                    or self._map_report_source_label(obs.source_family, obs.source_app),
                    "source_app": obs.source_app,
                    "source_ref": str(getattr(obs, "source_ref", "") or "").strip(),
                    "metadata_path": self._observation_file_path(obs),
                    "to_target": str(metadata.get("to_call") or obs.to_target or "").strip(),
                    "reported_by": str(metadata.get("from_call") or obs.from_call or "").strip().upper(),
                    "reported_for_state": effective_state,
                    "reported_for_grid": effective_grid,
                    "groups": group_values,
                    "topics": sorted(topics),
                    "state": effective_state,
                    "scope": scope_text,
                    "state_confidence": state_confidence,
                    "geo_confidence": geo_confidence,
                    "search_text": " ".join(str(part or "") for part in search_parts if str(part or "").strip()),
                    "location_confidence": obs.location_confidence,
                    "provenance": dict(provenance),
                    "location_source": dict(provenance.get("location_source") or {})
                    if isinstance(provenance.get("location_source"), Mapping)
                    else {},
                    "routing": dict(provenance.get("routing") or {})
                    if isinstance(provenance.get("routing"), Mapping)
                    else {},
                    "auth_state": obs.auth_state,
                    "trusted_state": obs.trusted_state,
                    "confirmed_state": obs.confirmed_state,
                    "eligibility": eligibility_reason,
                }
            )
        self._query_cache_set(cache_key, list(out))
        return out

    def _observed_message_file_paths(self, db_path: Path) -> Set[str]:
        cache_key = ("observed_message_file_paths", str(db_path), self._nets_db_fingerprint())
        cached = self._query_cache_get(cache_key, ttl_sec=8.0)
        if isinstance(cached, set):
            return {str(path) for path in cached if str(path)}
        paths: Set[str] = set()
        try:
            with connect_sqlite_readonly(str(db_path)) as conn:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='observation_projection'"
                ).fetchone()
                if not exists:
                    self._query_cache_set(cache_key, paths)
                    return paths
                rows = conn.execute(
                    """
                    SELECT source_ref, provenance_json
                    FROM observation_projection
                    WHERE source_ref LIKE 'file:%'
                       OR source_ref LIKE '%/%'
                       OR provenance_json LIKE '%file_path%'
                       OR provenance_json LIKE '%"path"%'
                    """
                ).fetchall()
        except Exception:
            self._query_cache_set(cache_key, paths)
            return paths
        for source_ref, provenance_json in rows:
            source_ref_text = str(source_ref or "").strip()
            if source_ref_text.startswith("file:"):
                paths.add(source_ref_text[5:])
            elif "/" in source_ref_text:
                paths.add(source_ref_text)
            try:
                provenance = json.loads(provenance_json or "{}")
            except Exception:
                provenance = {}
            if isinstance(provenance, dict):
                for key in ("file_path", "path"):
                    path = str(provenance.get(key) or "").strip()
                    if path:
                        paths.add(path)
        if not paths:
            try:
                legacy_rows = conn.execute(
                    """
                    SELECT provenance_json
                    FROM observations
                    WHERE provenance_json LIKE '%file_path%'
                       OR provenance_json LIKE '%"path"%'
                    """
                ).fetchall()
            except Exception:
                legacy_rows = []
            for (provenance_json,) in legacy_rows:
                try:
                    provenance = json.loads(provenance_json or "{}")
                except Exception:
                    provenance = {}
                if isinstance(provenance, dict):
                    for key in ("file_path", "path"):
                        path = str(provenance.get(key) or "").strip()
                        if path:
                            paths.add(path)
        self._query_cache_set(cache_key, set(paths))
        return paths

    def _metadata_matches_map_scope(
        self,
        meta: Dict[str, object],
        *,
        group_filter: str = "",
        region_filter: str = "",
    ) -> bool:
        group_key = self._normalize_map_group_value(group_filter)
        if group_key:
            values = [
                str(meta.get("to_call") or ""),
                str(meta.get("from_call") or ""),
            ]
            if not self._map_values_match_group_filter(values, group_key):
                return False
        region_key = str(region_filter or "").strip().upper()
        if not re.fullmatch(r"R\d{1,2}", region_key):
            region_key = ""
        elif len(region_key) == 2:
            region_key = f"R0{region_key[-1]}"
        if region_key:
            candidates = [
                str(meta.get("from_call") or "").strip().upper(),
                str(meta.get("to_call") or "").strip().upper().lstrip("@").rstrip(">"),
            ]
            matched = False
            for callsign in candidates:
                if not callsign:
                    continue
                try:
                    operator_meta = self.operator_index.get(callsign, {}) if hasattr(self, "operator_index") else {}
                except Exception:
                    operator_meta = {}
                if isinstance(operator_meta, dict) and str(operator_meta.get("region") or "").strip().upper() == region_key:
                    matched = True
                    break
            if not matched:
                return False
        return True

    def _metadata_matches_map_search(self, meta: Dict[str, object], search_text: str) -> bool:
        if not str(search_text or "").strip():
            return True
        return self._map_text_matches_query(
            search_text,
            meta.get("from_call"),
            meta.get("to_call"),
            meta.get("title"),
            meta.get("display_type"),
            meta.get("msg_type"),
            meta.get("status"),
            meta.get("state"),
            meta.get("grid"),
            meta.get("source_label"),
            meta.get("search_text"),
        )

    def _metadata_matches_topic_filter(self, meta: Dict[str, object], topic_filter: str) -> bool:
        if not str(topic_filter or "").strip():
            return True
        evidence_values = (
            meta.get("title"),
            meta.get("display_type"),
            meta.get("msg_type"),
            meta.get("search_text"),
        )
        topics = {
            str(value or "").strip().lower()
            for value in normalize_topic_terms(" ".join(str(value or "") for value in evidence_values))
        }
        topic_key = str(topic_filter or "").strip().lower()
        return topic_key in topics or self._map_text_matches_query(topic_filter, *evidence_values)

    def _observation_matches_topic_filter(
        self,
        obs,
        metadata: Optional[Dict[str, object]],
        topic_filter: str,
    ) -> bool:
        """Match map observations with the same topic clues used by Messages."""
        if not str(topic_filter or "").strip():
            return True
        provenance = getattr(obs, "provenance", {}) or {}
        if not isinstance(provenance, dict):
            provenance = {}
        meta = metadata or {}
        return self._map_observation_has_direct_topic_evidence(obs, meta, provenance, topic_filter)

    @staticmethod
    def _map_observation_has_direct_topic_evidence(
        obs,
        metadata: Optional[Dict[str, object]],
        provenance: Optional[Dict[str, object]],
        topic_filter: str,
    ) -> bool:
        """Require actual report content for a selected topic, not tags alone.

        Some historical Spotter rows carry a mapped topic for a bare form stub
        such as "MCF103 (#ABCD)" or "MCF304 (#ABCD)". Those tags are useful
        diagnostics, but they should not put a station on a Fire-filtered map
        unless the decoded title/body/search text contains Fire evidence.
        """
        topic = str(topic_filter or "").strip()
        if not topic:
            return True
        meta = metadata or {}
        prov = provenance or {}
        evidence_values = [
            getattr(obs, "subject", ""),
            getattr(obs, "summary", ""),
            prov.get("form_name", ""),
            prov.get("message_type", ""),
            prov.get("search_text", ""),
            meta.get("title"),
            meta.get("search_text"),
        ]
        topics = {
            str(value or "").strip().lower()
            for value in normalize_topic_terms(" ".join(str(value or "") for value in evidence_values))
        }
        topic_key = topic.lower()
        if topic_key in topics:
            return True
        return StationsMapTab._map_text_matches_query(topic, *evidence_values)

    @staticmethod
    def _map_topic_icon(topic: object) -> str:
        value = str(topic or "").strip().lower()
        if not value:
            return ""
        direct = {
            "weather": "storm",
            "fire": "fire",
            "medical": "medical",
            "power": "power",
            "water": "water",
            "fuel": "fuel",
            "food": "food",
            "travel/roads": "transport",
            "travel": "transport",
            "roads": "transport",
            "comms": "comms",
            "communications": "comms",
            "security": "security",
            "shelter": "shelter",
            "logistics": "logistics",
            "infrastructure": "utility",
            "general intel": "warning",
            "intel": "warning",
            "alerts/intel": "warning",
        }
        if value in direct:
            return direct[value]
        if "wildfire" in value or "fire" in value or "smoke" in value:
            return "fire"
        if any(token in value for token in ("storm", "weather", "wx", "tornado", "hurricane")):
            return "storm"
        if "flood" in value or "water" in value:
            return "water"
        if "power" in value or "grid" in value or "outage" in value:
            return "power"
        if "road" in value or "travel" in value or "bridge" in value:
            return "transport"
        if "comms" in value or "radio" in value or "internet" in value:
            return "comms"
        if "medical" in value or "hospital" in value or "ems" in value:
            return "medical"
        if "security" in value or "threat" in value:
            return "security"
        if "shelter" in value:
            return "shelter"
        if "food" in value:
            return "food"
        if "fuel" in value:
            return "fuel"
        if "logistics" in value or "resource" in value:
            return "logistics"
        if "infrastructure" in value or "utility" in value:
            return "utility"
        return ""

    @classmethod
    def _map_icon_for_topics(cls, topics: object, *, preferred_topic: str = "") -> str:
        preferred = cls._map_topic_icon(preferred_topic)
        if preferred:
            return preferred
        values: List[str] = []
        if isinstance(topics, (list, tuple, set)):
            values = [str(topic or "").strip() for topic in topics if str(topic or "").strip()]
        elif str(topics or "").strip():
            values = [str(topics or "").strip()]
        for taxonomy_topic in TOPIC_TAXONOMY:
            if any(str(taxonomy_topic).lower() == value.lower() for value in values):
                icon = cls._map_topic_icon(taxonomy_topic)
                if icon:
                    return icon
        for value in values:
            icon = cls._map_topic_icon(value)
            if icon:
                return icon
        return ""

    def _map_event_topic_and_icon(
        self,
        topics: object,
        fallback_icon: object = "",
        *,
        preferred_topic: str = "",
    ) -> tuple[str, str]:
        selected_topic = ""
        try:
            selected_topic = self._selected_map_topic_filter()
        except Exception:
            selected_topic = ""
        primary_topic = (
            str(preferred_topic or "").strip()
            or str(selected_topic or "").strip()
            or self._map_preferred_topic_for_values(topics)
        )
        event_icon = self._map_icon_for_topics(topics, preferred_topic=primary_topic)
        if not event_icon:
            fallback = str(fallback_icon or "general").strip().lower() or "general"
            event_icon = fallback if self._map_topic_icon(fallback) or fallback in {"pin"} else "general"
        return primary_topic, event_icon

    def _message_metadata_source_allowed(self, source_family: str, focus_mode: str) -> bool:
        canonical = self._canonical_map_source_family(source_family)
        wanted = self._observation_focus_sources(focus_mode)
        if focus_mode == "rf_pins":
            return False
        if canonical in wanted:
            return True
        if focus_mode in {"hf_reports", "all_reports"} and canonical in {"flmsg", "flamp"}:
            return True
        return False

    def _load_message_metadata_operational_reports(
        self,
        *,
        layer_name: str,
        max_age_sec: int,
    ) -> List[Dict[str, object]]:
        """Load indexed file/message metadata as map report events.

        The Inbox already normalizes FLMsg/FLAmp/Spotter traffic into
        message_file_metadata. Treat that index as a report source so map
        filters behave like a refinement view over all known traffic, not just
        rows that already passed through the observation projector.
        """
        if not self._effective_map_observation_focus_enabled():
            return []
        focus_mode = (
            self._effective_map_report_focus_mode()
            if layer_name == "report_focus"
            else self._effective_map_observation_focus_mode()
        )
        if focus_mode == "local_reports":
            return []
        topic_filter = self._selected_map_topic_filter()
        search_text = self._selected_map_search_text()
        group_filter = self._selected_map_group_filter()
        region_filter = self._selected_map_region_filter()
        advanced_sig = self._map_advanced_filters_signature()
        cache_key = (
            f"message_metadata_{layer_name}_reports",
            int(max_age_sec or 0),
            focus_mode,
            topic_filter,
            search_text,
            group_filter,
            region_filter,
            advanced_sig,
            self._nets_db_fingerprint(),
        )
        cached = self._query_cache_get(cache_key, ttl_sec=6.0)
        if isinstance(cached, list):
            return [dict(row) for row in cached if isinstance(row, dict)]
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return []
        if not db_path.exists():
            return []
        metadata_lookup = self._message_file_metadata_lookup(db_path)
        now_ts = time.time()
        out: List[Dict[str, object]] = []
        for path, meta in metadata_lookup.items():
            if not isinstance(meta, dict):
                continue
            source_family = str(meta.get("source_family") or meta.get("source_label") or "").strip().lower()
            if not self._message_metadata_source_allowed(source_family, focus_mode):
                continue
            report_ts = self._safe_float(meta.get("report_ts"), 0.0)
            if max_age_sec and max_age_sec > 0:
                if report_ts <= 0.0 or (now_ts - report_ts) > max_age_sec:
                    continue
            if not self._metadata_matches_map_scope(meta, group_filter=group_filter, region_filter=region_filter):
                continue
            if not self._metadata_matches_map_search(meta, search_text):
                continue
            if not self._metadata_matches_topic_filter(meta, topic_filter):
                continue
            topics = [str(topic or "").strip() for topic in (meta.get("topics") or ()) if str(topic or "").strip()]
            title = str(meta.get("title") or "").strip()
            text = " ".join(
                part
                for part in (
                    title,
                    str(meta.get("display_type") or ""),
                    str(meta.get("msg_type") or ""),
                    str(meta.get("search_text") or ""),
                    " ".join(topics),
                )
                if str(part or "").strip()
            )
            icon, severity = self._classify_infrastructure_text(text)
            topic_icon = self._map_icon_for_topics(topics, preferred_topic=topic_filter)
            if topic_icon:
                icon = topic_icon
            event = {
                "callsign": str(meta.get("from_call") or "").strip().upper(),
                "from_call": str(meta.get("from_call") or "").strip().upper(),
                "reported_by": str(meta.get("from_call") or "").strip().upper(),
                "to_target": str(meta.get("to_call") or "").strip().lstrip("@").rstrip(">"),
                "form_id": str(meta.get("display_type") or meta.get("msg_type") or "").strip(),
                "utc_ts": report_ts,
                "summary": title or str(meta.get("search_text") or "Message report").strip() or "Message report",
                "title": title,
                "icon": icon,
                "severity": severity,
                "grid": str(meta.get("grid") or "").strip().upper(),
                "state": str(meta.get("state") or "").strip().upper(),
                "reported_for_grid": str(meta.get("grid") or "").strip().upper(),
                "reported_for_state": str(meta.get("state") or "").strip().upper(),
                "source_family": self._canonical_map_source_family(source_family),
                "source_label": str(meta.get("source_label") or "").strip()
                or self._map_report_source_label(source_family, ""),
                "source_app": "",
                "groups": [str(meta.get("to_call") or "").strip().lstrip("@").rstrip(">")],
                "topics": topics,
                "search_text": str(meta.get("search_text") or "").strip(),
                "location_confidence": "message metadata",
                "metadata_path": path,
                "source_ref": f"file:{path}",
            }
            if self._map_event_matches_advanced_filters(event):
                out.append(event)
        self._query_cache_set(cache_key, list(out))
        return out

    @staticmethod
    def _observation_focus_sources(focus_mode: str) -> Set[str]:
        mode = str(focus_mode or "").strip().lower()
        if mode == "hf_reports":
            return {"spotter", "commstat", "js8call", "varac", "flmsg", "flamp", "condition_alert"}
        if mode == "local_reports":
            return {"local_report"}
        if mode == "rf_pins":
            return {"rf_pin"}
        if mode == "mesh_nodes":
            return {"mesh", "meshcore", "meshtastic", "mesh_client", "local_mesh"}
        return {
            "spotter",
            "commstat",
            "js8call",
            "varac",
            "flmsg",
            "flamp",
            "local_report",
            "condition_alert",
            "mesh",
            "meshcore",
            "meshtastic",
            "mesh_client",
            "local_mesh",
        }

    def _observation_matches_map_scope(self, obs, *, group_filter: str = "", region_filter: str = "") -> bool:
        group_key = self._normalize_map_group_value(group_filter)
        region_key = str(region_filter or "").strip().upper()
        if group_key:
            groups = {
                self._normalize_map_group_value(g)
                for g in (getattr(obs, "groups", ()) or ())
                if str(g or "").strip()
            }
            to_target = self._normalize_map_group_value(getattr(obs, "to_target", ""))
            if to_target:
                groups.add(to_target)
            if not self._map_values_match_group_filter(sorted(groups), group_key):
                return False
        if region_key:
            candidates = [
                str(getattr(obs, "from_call", "") or "").strip().upper(),
                str(getattr(obs, "to_target", "") or "").strip().upper().lstrip("@").rstrip(">"),
            ]
            matched = False
            for callsign in candidates:
                if not callsign:
                    continue
                meta = self.operator_index.get(callsign, {}) if hasattr(self, "operator_index") else {}
                if str(meta.get("region") or "").strip().upper() == region_key:
                    matched = True
                    break
            if not matched:
                return False
        return True

    def _observation_station_scope_calls(self, *, max_age_sec: int = 0) -> Set[str]:
        """Return callsigns represented by the current report/map filters."""
        if not self._effective_map_observation_focus_enabled():
            return set()
        focus_mode = self._effective_map_report_focus_mode()
        topic_filter = self._selected_map_topic_filter()
        search_text = self._selected_map_search_text()
        group_filter = self._selected_map_group_filter()
        region_filter = self._selected_map_region_filter()
        cache_key = (
            "observation_station_scope_calls",
            int(max_age_sec or 0),
            focus_mode,
            topic_filter,
            search_text,
            group_filter,
            region_filter,
            self._map_advanced_filters_signature(),
        )
        cached = self._query_cache_get(cache_key, ttl_sec=6.0)
        if isinstance(cached, set):
            return {str(call).strip().upper() for call in cached if str(call).strip()}
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return set()
        if not db_path.exists():
            return set()
        since_utc = ""
        if max_age_sec and max_age_sec > 0:
            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=max_age_sec)
            since_utc = cutoff.replace(microsecond=0).isoformat()
        try:
            calls: Set[str] = set()
            view_rows = map_observation_rows(
                db_path,
                ObservationQuery(
                    source_families=tuple(sorted(self._observation_focus_sources(focus_mode))),
                    since_utc=since_utc,
                    limit=2500,
                ),
                layer_enabled=True,
                readonly=True,
                allow_unconfirmed_local=False,
                exercise_layer=False,
            )
        except Exception as exc:
            log.debug("StationsMap: failed to load observation station scope: %s", exc)
            raise
        wanted_sources = self._observation_focus_sources(focus_mode)
        metadata_lookup = self._message_file_metadata_lookup(
            db_path, paths=tuple(self._observation_file_path(row.observation) for row in view_rows)
        )
        for view_row in view_rows:
            obs = view_row.observation
            source_family = str(obs.source_family or "").strip().lower()
            if source_family not in wanted_sources:
                continue
            metadata = self._metadata_for_observation(obs, metadata_lookup)
            if not self._observation_matches_map_scope(
                obs,
                group_filter=group_filter,
                region_filter=region_filter,
            ):
                continue
            if not self._observation_matches_advanced_filters(obs):
                continue
            if not self._observation_matches_map_search(obs, search_text, metadata):
                continue
            topics = {str(topic).strip() for topic in obs.observed_topics if str(topic).strip()}
            topics.update(str(topic).strip() for topic in (metadata.get("topics") or ()) if str(topic).strip())
            if not self._observation_matches_topic_filter(obs, metadata, topic_filter):
                continue
            for value in (
                metadata.get("from_call"),
                obs.from_call,
            ):
                call = str(value or "").strip().upper().lstrip("@").rstrip(">")
                if call:
                    calls.add(call)
                    base_call = JS8LogLinkIndexer._base_callsign(call)
                    if base_call:
                        calls.add(base_call)
        for event in self._load_message_metadata_operational_reports(
            layer_name="report_focus",
            max_age_sec=max_age_sec,
        ):
            if not isinstance(event, dict):
                continue
            for value in (
                event.get("callsign"),
                event.get("from_call"),
            ):
                call = str(value or "").strip().upper().lstrip("@").rstrip(">")
                if not call:
                    continue
                calls.add(call)
                base_call = JS8LogLinkIndexer._base_callsign(call)
                if base_call:
                    calls.add(base_call)
        self._query_cache_set(cache_key, set(calls))
        return calls

    def _selected_map_topic_filter(self) -> str:
        combo = getattr(self, "_map_topic_filter_combo", None)
        if combo is None:
            return ""
        data_value = ""
        try:
            data = combo.currentData()
        except Exception:
            data = None
        if data not in (None, ""):
            data_value = str(data or "").strip()
        try:
            text_value = str(combo.currentText() or "").strip()
        except Exception:
            text_value = ""
        value = data_value or text_value
        if not value or value.lower() in {"all", "all topics"}:
            return ""
        return value

    def _selected_map_intel_sensitivity(self) -> str:
        combo = getattr(self, "_map_intel_sensitivity_combo", None)
        if combo is None:
            return "active"
        try:
            data = combo.currentData()
        except Exception:
            data = None
        try:
            text = str(combo.currentText() or "").strip()
        except Exception:
            text = ""
        value = str(data if data not in (None, "") else text or "active").strip().lower()
        return value if value in {"current", "active", "extended"} else "active"

    def _regional_intelligence_payload(
        self,
        *,
        topic_filter: str = "",
        group_filter: str = "",
        region_filter: str = "",
        search_text: str = "",
        state_filter: str = "",
        sensitivity: str = "active",
        max_age_sec: int = 0,
    ) -> Dict[str, object]:
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception as exc:
            log.debug("StationsMap: regional intelligence config path unavailable: %s", exc)
            return {"enabled": False, "states": {}, "regions": {}, "summary": "Regional intelligence unavailable."}
        if not db_path.exists():
            return {"enabled": False, "states": {}, "regions": {}, "summary": "Regional intelligence data is not available."}
        try:
            snapshot = build_regional_intelligence_from_db(
                db_path,
                sensitivity=sensitivity,
                topic_filter=topic_filter,
                operating_group=group_filter,
                search_text=search_text,
                state=state_filter,
                max_age_sec=max_age_sec,
                limit=5000,
                readonly=True,
                station_index=getattr(self, "operator_index", {}) or {},
            )
        except Exception as exc:
            log.warning("StationsMap: failed to build regional intelligence: %s", exc, exc_info=True)
            raise

        def rollup_center(rollup: RegionalAreaRollup) -> Tuple[float, float]:
            if rollup.area_type == "fema_region":
                centers = [STATE_CENTERS[state] for state in FEMA_REGIONS.get(rollup.area_id, []) if state in STATE_CENTERS]
                if centers:
                    return (
                        sum(lat for lat, _lon in centers) / len(centers),
                        sum(lon for _lat, lon in centers) / len(centers),
                    )
            return STATE_CENTERS.get(rollup.area_id, (0.0, 0.0))

        def serialize_rollup(rollup: RegionalAreaRollup) -> Dict[str, object]:
            topics = [
                {
                    "topic": topic.topic,
                    "level": topic.level,
                    "score": topic.score,
                    "evidence_count": topic.evidence_count,
                    "reporter_count": topic.reporter_count,
                    "newest_age_hours": topic.newest_age_hours,
                }
                for topic in rollup.top_topics
            ]
            evidence = [
                {
                    "source_family": item.source_family,
                    "source_ref": item.source_ref,
                    "evidence_type": item.evidence_type,
                    "topic": item.topic,
                    "severity_hint": item.severity_hint,
                    "reporter_callsign": item.reporter_callsign,
                    "target": item.target,
                    "state": item.state,
                    "fema_region": item.fema_region,
                    "summary": item.summary,
                    "age_hours": item.age_hours,
                    "score": item.score,
                }
                for item in rollup.evidence[:8]
            ]
            source_mix = self._regional_source_mix(evidence)
            lat, lon = rollup_center(rollup)
            return {
                "area_type": rollup.area_type,
                "area_id": rollup.area_id,
                "label": rollup.label,
                "fema_region": rollup.fema_region,
                "state_list": list(FEMA_REGIONS.get(rollup.area_id, ())) if rollup.area_type == "fema_region" else [rollup.area_id],
                "level": rollup.level,
                "score": rollup.score,
                "evidence_count": rollup.evidence_count,
                "reporter_count": rollup.reporter_count,
                "signal_count": rollup.signal_count,
                "newest_age_hours": rollup.newest_age_hours,
                "trend": rollup.trend,
                "top_topics": topics,
                "evidence": evidence,
                "source_mix": source_mix,
                "lat": lat,
                "lon": lon,
            }

        region_key = str(region_filter or "").strip().upper()
        state_rollups = tuple(
            rollup
            for rollup in snapshot.state_rollups
            if not region_key or rollup.fema_region.upper() == region_key
        )
        fema_rollups = tuple(
            rollup
            for rollup in snapshot.fema_rollups
            if not region_key or rollup.area_id.upper() == region_key
        )
        states = {rollup.area_id: serialize_rollup(rollup) for rollup in state_rollups}
        regions = {rollup.area_id: serialize_rollup(rollup) for rollup in fema_rollups}
        top_states = sorted(state_rollups, key=lambda item: (-item.score, item.area_id))[:5]
        if top_states:
            summary = ", ".join(f"{rollup.area_id} {rollup.level}" for rollup in top_states)
        else:
            summary = "No active regional concerns from current evidence."
        return {
            "enabled": True,
            "sensitivity": snapshot.sensitivity,
            "topic_filter": topic_filter,
            "recency_seconds": int(max_age_sec or 0),
            "generated_utc": snapshot.generated_utc,
            "states": states,
            "regions": regions,
            "summary": summary,
        }

    def _regional_intelligence_density_events(
        self,
        regional_payload: Mapping[str, object],
        station_lookup: Mapping[str, StationPoint],
    ) -> List[Dict[str, object]]:
        if not isinstance(regional_payload, Mapping) or not regional_payload.get("enabled"):
            return []
        states = regional_payload.get("states")
        if not isinstance(states, Mapping):
            return []
        grouped: Dict[tuple[str, str, str, str], Dict[str, object]] = {}
        for rollup in states.values():
            if not isinstance(rollup, Mapping):
                continue
            evidence_rows = rollup.get("evidence")
            if not isinstance(evidence_rows, list):
                continue
            for item in evidence_rows:
                if not isinstance(item, Mapping):
                    continue
                hint = str(item.get("severity_hint") or "").strip().lower()
                if hint == "normal":
                    continue
                call = str(item.get("reporter_callsign") or "").strip().upper()
                state = str(item.get("state") or rollup.get("area_id") or "").strip().upper()
                topic = str(item.get("topic") or "").strip()
                summary = str(item.get("summary") or "Regional evidence").strip()
                pt = station_lookup.get(call) if call else None
                if (
                    pt is not None
                    and state
                    and str(getattr(pt, "state", "") or "").strip().upper() == state
                ):
                    lat, lon = pt.lat, pt.lon
                    grid = pt.grid
                elif state in STATE_CENTERS:
                    lat, lon = STATE_CENTERS.get(state, (0.0, 0.0))
                    grid = ""
                elif pt is not None:
                    lat, lon = pt.lat, pt.lon
                    grid = pt.grid
                else:
                    continue
                if not lat or not lon:
                    continue
                icon = self._map_topic_icon(topic) or "warning"
                severity = "severe" if hint == "severe" else "caution" if hint == "degraded" else "unknown"
                key = (call or state, state, topic, severity)
                event = grouped.get(key)
                if event is None:
                    event = {
                        "callsign": call,
                        "from_call": call,
                        "form_id": "Regional Intel",
                        "utc_ts": 0.0,
                        "utc_str": str(item.get("event_time_utc") or "").strip(),
                        "summary": summary,
                        "title": summary,
                        "icon": icon,
                        "severity": severity,
                        "lat": lat,
                        "lon": lon,
                        "grid": grid,
                        "source_family": item.get("source_family") or "regional_intelligence",
                        "source_label": "Regional Intel",
                        "source_kind": "regional",
                        "topics": [topic] if topic else [],
                        "state": state,
                        "search_text": " ".join(part for part in (call, state, topic, summary) if part),
                        "count": 0,
                    }
                    grouped[key] = event
                event["count"] = int(event.get("count") or 0) + 1
        return sorted(
            grouped.values(),
            key=lambda event: (
                0 if str(event.get("severity") or "") == "severe" else 1,
                str(event.get("state") or ""),
                str(event.get("callsign") or ""),
            ),
        )[:150]

    def _selected_map_search_text(self) -> str:
        references = getattr(self,"_license_reference_presenter",None)
        if references is not None and references.scope.currentData() == "references":
            return references._operational_search
        edit = getattr(self, "_map_search_edit", None)
        if edit is None:
            return ""
        try:
            return str(edit.text() or "").strip()
        except Exception:
            return ""

    def _path_target_from_search_text(self, search_text: object) -> str:
        if self._current_map_mode_key() != "paths":
            return ""
        if not bool(getattr(self, "show_link_paths", False)):
            return ""
        mode, value = self._current_link_selection()
        if str(mode or "").strip().lower() == "relay_target" and str(value or "").strip():
            return ""
        text = str(search_text or "").strip().upper().lstrip("@")
        if not text or any(ch.isspace() for ch in text):
            return ""
        callsign = self._map_callsign_from_value(text)
        if not callsign or not looks_like_callsign_text(callsign):
            return ""
        if self._map_selected_station_is_self(callsign):
            return ""
        return callsign

    def _selected_map_group_filter(self) -> str:
        combo = getattr(self, "group_filter_combo", None)
        if combo is None:
            return ""
        try:
            data = combo.currentData()
        except Exception:
            data = None
        try:
            text = str(combo.currentText() or "").strip()
        except Exception:
            text = ""
        value = data if data not in (None, "") else text
        return self._normalize_map_group_value(value)

    def _selected_map_region_filter(self) -> str:
        combo = getattr(self, "region_filter_combo", None)
        if combo is None:
            return ""
        try:
            data = combo.currentData()
        except Exception:
            data = None
        try:
            text = str(combo.currentText() or "").strip()
        except Exception:
            text = ""
        value = str(data if data not in (None, "") else text or "").strip().upper()
        if value in {"", "ALL", "ANY", "ALL REGIONS", "REGION ALL"}:
            return ""
        if value.startswith("REGION "):
            value = value[7:].strip()
        return value

    def _map_filter_combo_signature(self, attr: str) -> object:
        combo = getattr(self, attr, None)
        if combo is None:
            return ""
        try:
            data = combo.currentData()
        except Exception:
            data = None
        try:
            text = str(combo.currentText() or "").strip()
        except Exception:
            text = ""
        if isinstance(data, dict):
            return {str(key): data[key] for key in sorted(data)}
        if isinstance(data, (list, tuple, set)):
            return [str(value) for value in data]
        if data not in (None, ""):
            return str(data).strip()
        return text

    def _map_report_cache_signature(self, layer_name: str) -> Dict[str, object]:
        if layer_name not in {"report_focus", "alert", "infrastructure", "message_metadata_infrastructure"}:
            return {}
        return {
            "focus": self._effective_map_report_focus_mode(),
            "topic": self._selected_map_topic_filter(),
            "search": self._selected_map_search_text(),
            "group": self._selected_map_group_filter(),
            "region": self._selected_map_region_filter(),
            "band": self._map_filter_combo_signature("band_combo"),
            "source": self._map_filter_combo_signature("_map_source_filter_combo"),
            "state": self._map_filter_combo_signature("_map_state_filter_combo"),
            "status": self._map_filter_combo_signature("_map_status_filter_combo"),
            "scope": self._map_filter_combo_signature("_map_scope_filter_combo"),
            "trust": self._map_filter_combo_signature("_map_trust_filter_combo"),
            "recency_seconds": int(getattr(self, "recency_seconds", 0) or 0),
        }

    @staticmethod
    def _normalize_map_search_text(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).lower()

    @classmethod
    def _map_text_matches_query(cls, query: str, *values: object) -> bool:
        needle = cls._normalize_map_search_text(query)
        if not needle:
            return True
        haystack = cls._normalize_map_search_text(
            " ".join(text for value in values for text in searchable_text_values(value))
        )
        if needle in haystack:
            return True
        tokens = [token for token in re.split(r"[\s,;/|]+", needle) if token]
        if bool(tokens) and all(token in haystack for token in tokens):
            return True
        haystack_tokens = set(token for token in re.split(r"[\s,;/|]+", haystack) if token)
        alias_sets = (
            {"fire", "fires", "wildfire", "wildfires"},
            {"comm", "comms", "communications"},
            {"road", "roads", "travel", "transportation"},
        )
        for aliases in alias_sets:
            if any(token in aliases for token in tokens) and haystack_tokens.intersection(aliases):
                return True
        return False

    @staticmethod
    def _normalize_map_group_value(value: object) -> str:
        group = str(value or "").strip().upper().lstrip("@").rstrip(">")
        if group in {
            "",
            "ALL",
            "ANY",
            "ALL GROUPS",
            "GROUPS: ALL",
            "OPERATING GROUP: ALL",
            "OPERATING GROUPS: ALL",
        }:
            return ""
        return group

    @classmethod
    def _map_group_matches_filter(cls, candidate: object, group_filter: object) -> bool:
        group = cls._normalize_map_group_value(candidate)
        wanted = cls._normalize_map_group_value(group_filter)
        if not wanted:
            return True
        if not group:
            return False
        if group == wanted:
            return True
        # MagNet rosters commonly model MR01..MR10/MRHUB as child groups.
        # Operators expect a MAGNET map filter to include those child reports.
        if wanted == "MAGNET":
            return bool(re.fullmatch(r"MR\d{1,2}[A-Z]*", group) or group == "MRHUB")
        return False

    @classmethod
    def _map_values_match_group_filter(cls, values: List[str], group_filter: object) -> bool:
        wanted = cls._normalize_map_group_value(group_filter)
        if not wanted:
            return True
        return any(cls._map_group_matches_filter(value, wanted) for value in values)

    @staticmethod
    def _map_grid_looks_usable(value: object) -> bool:
        grid = str(value or "").strip().upper()
        if not grid:
            return False
        # MagNet child groups such as MR08 can look like Maidenhead grid4
        # locators. Treat them as groups, never as map coordinates.
        if re.fullmatch(r"MR\d{1,2}[A-Z]*", grid):
            return False
        return bool(re.fullmatch(r"[A-R]{2}\d{2}(?:[A-X]{2})?", grid))

    def _message_file_metadata_lookup(self, db_path: Path, *, paths=None) -> Dict[str, Dict[str, object]]:
        selected_paths = tuple(sorted({str(path) for path in paths if path})) if paths is not None else None
        if selected_paths == ():
            return {}
        cache_key = ("message_file_metadata_lookup", str(db_path), self._nets_db_fingerprint(), selected_paths)
        cached = self._query_cache_get(cache_key, ttl_sec=8.0)
        if isinstance(cached, dict):
            return {
                str(path): dict(meta)
                for path, meta in cached.items()
                if isinstance(meta, dict)
            }
        lookup: Dict[str, Dict[str, object]] = {}
        try:
            with connect_sqlite_readonly(str(db_path)) as conn:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='message_file_metadata'"
                ).fetchone()
                if not exists:
                    self._query_cache_set(cache_key, lookup)
                    return lookup
                query = """
                    SELECT path, source_family, msg_type, display_type, status, from_call,
                           to_call, title, topics_json, search_text, report_ts, source_label
                    FROM message_file_metadata
                """
                rows = []
                batches = (selected_paths[index:index + 500] for index in range(0, len(selected_paths), 500)) if selected_paths is not None else (None,)
                for batch in batches:
                    condition = " WHERE path IN (" + ",".join("?" for _ in batch) + ")" if batch is not None else " ORDER BY report_ts DESC, path LIMIT 1500"
                    rows.extend(conn.execute(query + condition, batch or ()).fetchall())
        except Exception as exc:
            log.debug("StationsMap: failed to load message file metadata: %s", exc)
            self._query_cache_set(cache_key, lookup)
            return lookup
        if selected_paths is None:
            note_map_read_cap("message metadata", len(rows), 1500)
        for row in rows:
            path = str(row[0] or "").strip()
            if not path:
                continue
            try:
                topics = json.loads(row[8] or "[]")
                if not isinstance(topics, list):
                    topics = []
            except Exception:
                topics = []
            search_text = str(row[9] or "").strip()
            grid, state = self._extract_state_grid_from_map_text(search_text)
            if not self._map_grid_looks_usable(grid):
                grid, state = self._map_message_file_location(path)
            lookup[path] = {
                "path": path,
                "source_family": row[1] or "",
                "msg_type": row[2] or "",
                "display_type": row[3] or "",
                "status": row[4] or "",
                "from_call": row[5] or "",
                "to_call": row[6] or "",
                "title": row[7] or "",
                "topics": [str(topic).strip() for topic in topics if str(topic).strip()],
                "search_text": search_text,
                "report_ts": float(row[10] or 0.0),
                "source_label": row[11] or "",
                "grid": grid,
                "state": state,
            }
        self._query_cache_set(cache_key, dict(lookup))
        return lookup

    def _commstat_artifact_metadata_lookup(self, db_path: Path, *, source_refs=None) -> Dict[str, Dict[str, object]]:
        artifact_ids = None
        if source_refs is not None:
            artifact_ids = tuple(sorted({int(ref.split(":", 1)[1]) for ref in source_refs
                                         if ref.startswith("commstat_artifacts:") and ref.split(":", 1)[1].isdigit()}))
            if not artifact_ids:
                return {}
        cache_key = ("commstat_artifact_metadata_lookup", str(db_path), self._nets_db_fingerprint(), artifact_ids)
        cached = self._query_cache_get(cache_key, ttl_sec=8.0)
        if isinstance(cached, dict):
            return {str(ref): dict(meta) for ref, meta in cached.items() if isinstance(meta, dict)}
        lookup: Dict[str, Dict[str, object]] = {}
        try:
            with connect_sqlite_readonly(str(db_path)) as conn:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='commstat_artifacts'"
                ).fetchone()
                if not exists:
                    self._query_cache_set(cache_key, lookup)
                    return lookup
                cols = {
                    str(row[1] or "").strip()
                    for row in conn.execute("PRAGMA table_info(commstat_artifacts)").fetchall()
                    if len(row) > 1 and str(row[1] or "").strip()
                }
                event_ts_expr = "event_ts_utc" if "event_ts_utc" in cols else "event_ts" if "event_ts" in cols else "''"
                query = f"""
                    SELECT id, from_call, target, report_group, grid, state_code, scope,
                           status_label, alert_color, title, body_text, remarks_text,
                           transport_mode, reach_mode, {event_ts_expr}
                    FROM commstat_artifacts
                """
                rows = []
                batches = (artifact_ids[index:index + 500] for index in range(0, len(artifact_ids), 500)) if artifact_ids is not None else (None,)
                for batch in batches:
                    condition = " WHERE id IN (" + ",".join("?" for _ in batch) + ")" if batch is not None else " ORDER BY id DESC LIMIT 1500"
                    rows.extend(conn.execute(query + condition, batch or ()).fetchall())
        except Exception as exc:
            log.debug("StationsMap: failed to load CommStat artifact metadata: %s", exc)
            self._query_cache_set(cache_key, lookup)
            return lookup
        for row in rows:
            ref = f"commstat_artifacts:{int(row[0] or 0)}"
            if ref.endswith(":0"):
                continue
            grid = str(row[4] or "").strip().upper()
            state, state_confidence, geo_confidence = resolve_commstat_reported_for_state(
                state_code=row[5],
                grid=grid,
                scope=row[6],
                remarks=" ".join(str(value or "") for value in (row[10], row[11], row[9]) if str(value or "").strip()),
            )
            state = str(state or "").strip().upper()
            topics = sorted(
                normalize_topic_terms(
                    " ".join(str(value or "") for value in (row[9], row[10], row[11], row[7], row[8]))
                )
            )
            lookup[ref] = {
                "source_ref": ref,
                "from_call": str(row[1] or "").strip().upper(),
                "to_call": str(row[2] or "").strip(),
                "report_group": str(row[3] or row[2] or "").strip(),
                "grid": grid,
                "state": state,
                "scope": str(row[6] or "").strip(),
                "status": str(row[7] or "").strip(),
                "alert_color": str(row[8] or "").strip(),
                "state_confidence": state_confidence,
                "geo_confidence": geo_confidence,
                "title": str(row[9] or "").strip(),
                "body_text": str(row[10] or "").strip(),
                "remarks_text": str(row[11] or "").strip(),
                "transport": str(row[12] or "").strip(),
                "reach": str(row[13] or "").strip(),
                "search_text": " ".join(str(value or "") for value in row[1:14] if str(value or "").strip()),
                "topics": topics,
                "report_ts": self._observation_ts(row[14]),
                "source_label": "CommStat",
            }
        self._query_cache_set(cache_key, dict(lookup))
        return lookup

    def _map_message_file_location(self, path: object) -> Tuple[str, str]:
        """Extract a map-safe state/grid from a message file when the index lacks it.

        Older metadata rows can have enough message summary to be useful but no
        decoded location. Read only the individual file, cap the read size, and
        cache by file fingerprint so map refresh stays cheap.
        """
        path_text = str(path or "").strip()
        if not path_text:
            return "", ""
        try:
            file_path = Path(path_text).expanduser()
            stat = file_path.stat()
        except Exception:
            return "", ""
        cache_key = (
            "map_message_file_location",
            str(file_path),
            float(getattr(stat, "st_mtime", 0.0) or 0.0),
            int(getattr(stat, "st_size", 0) or 0),
        )
        cached = self._query_cache_get(cache_key, ttl_sec=120.0)
        if isinstance(cached, (tuple, list)) and len(cached) == 2:
            grid = str(cached[0] or "").strip().upper()
            state = str(cached[1] or "").strip().upper()
            return grid, state
        text = ""
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read(256 * 1024)
        except Exception as exc:
            log.debug("StationsMap: failed to read message location from %s: %s", file_path, exc)
        grid, state = self._extract_state_grid_from_map_text(text)
        if not self._map_grid_looks_usable(grid):
            grid = ""
        self._query_cache_set(cache_key, (grid, state))
        return grid, state

    @staticmethod
    def _extract_state_grid_from_map_text(text: object) -> Tuple[str, str]:
        haystack = str(text or "").upper()
        state_values = set(STATE_TO_FEMA_REGION) | set(US_STATE_ABBR_FROM_NAME.values())

        def usable_grid(candidate: object) -> str:
            value = str(candidate or "").strip().upper()
            if re.fullmatch(r"MR\d{1,2}[A-Z]*", value):
                return ""
            if re.fullmatch(r"[A-R]{2}\d{2}(?:[A-X]{2})?", value):
                return value
            return ""

        def usable_state(candidate: object) -> str:
            value = str(candidate or "").strip().upper()
            return value if value in state_values else ""

        grid = ""
        state = ""
        explicit_patterns = [
            r"\b(?:AREA|QTH|LOCATION|LOC)\s*[:=\[]?\s*([A-Z]{2})\]?\s*/\s*([A-R]{2}\d{2}(?:[A-X]{2})?)\b",
            r"\b(?:STATE|ST)\s*[:=\[]?\s*([A-Z]{2})\]?\s*(?:/|\s+)\s*(?:GRID|GR)?\s*[:=\[]?\s*([A-R]{2}\d{2}(?:[A-X]{2})?)\b",
            r"\b([A-Z]{2})\s*/\s*([A-R]{2}\d{2}(?:[A-X]{2})?)\b",
        ]
        for pattern in explicit_patterns:
            match = re.search(pattern, haystack)
            if not match:
                continue
            candidate_state = usable_state(match.group(1))
            candidate_grid = usable_grid(match.group(2))
            if candidate_grid:
                return candidate_grid, candidate_state
        grid_match = re.search(r"\b(?:GRID|GR)\s*[:=\[]?\s*([A-R]{2}\d{2}(?:[A-X]{2})?)\]?\b", haystack)
        if grid_match:
            grid = usable_grid(grid_match.group(1))
        if not grid:
            grid_candidates = [
                candidate
                for candidate in (
                    usable_grid(match.group(1))
                    for match in re.finditer(r"\b([A-R]{2}\d{2}(?:[A-X]{2})?)\b", haystack)
                )
                if candidate
            ]
            grid = grid_candidates[-1] if grid_candidates else ""
        state_match = re.search(r"\b(?:STATE|ST)\s*[:=\[]?\s*([A-Z]{2})\]?\b", haystack)
        if state_match:
            state = usable_state(state_match.group(1))
        if grid:
            state_match = re.search(rf"\b([A-Z]{{2}})\s+{re.escape(grid)}\b", haystack)
            if state_match:
                state = usable_state(state_match.group(1)) or state
        return grid, state

    @staticmethod
    def _observation_file_path(obs) -> str:
        source_ref = str(getattr(obs, "source_ref", "") or "").strip()
        if source_ref.startswith("file:"):
            return source_ref[5:]
        provenance = getattr(obs, "provenance", {}) or {}
        if isinstance(provenance, dict):
            return str(provenance.get("file_path") or provenance.get("path") or "").strip()
        return ""

    def _metadata_for_observation(self, obs, metadata_lookup: Optional[Dict[str, Dict[str, object]]] = None) -> Dict[str, object]:
        path = self._observation_file_path(obs)
        if not path:
            return {}
        if metadata_lookup is None:
            try:
                metadata_lookup = self._message_file_metadata_lookup(get_config_dir() / "config" / "freqinout_nets.db", paths=(path,))
            except Exception:
                metadata_lookup = {}
        meta = metadata_lookup.get(path) if isinstance(metadata_lookup, dict) else None
        return dict(meta) if isinstance(meta, dict) else {}

    def _station_matches_map_search(self, pt: StationPoint, search_text: str) -> bool:
        if not str(search_text or "").strip():
            return True
        meta = {}
        try:
            meta = self.operator_index.get(str(pt.callsign or "").strip().upper(), {}) or {}
        except Exception:
            meta = {}
        return self._map_text_matches_query(
            search_text,
            pt.callsign,
            pt.name,
            pt.group,
            pt.state,
            pt.grid,
            meta.get("region") if isinstance(meta, dict) else "",
            meta.get("role") if isinstance(meta, dict) else "",
        )

    def _observation_matches_map_search(
        self,
        obs,
        search_text: str,
        metadata: Optional[Dict[str, object]] = None,
    ) -> bool:
        if not str(search_text or "").strip():
            return True
        provenance = getattr(obs, "provenance", {}) or {}
        if not isinstance(provenance, dict):
            provenance = {}
        meta = metadata or {}
        return self._map_text_matches_query(
            search_text,
            getattr(obs, "from_call", ""),
            getattr(obs, "to_target", ""),
            " ".join(str(g or "") for g in (getattr(obs, "groups", ()) or ())),
            getattr(obs, "state", ""),
            getattr(obs, "grid", ""),
            getattr(obs, "subject", ""),
            getattr(obs, "summary", ""),
            provenance.get("form_name", ""),
            provenance.get("form_id", ""),
            provenance.get("message_type", ""),
            meta.get("from_call"),
            meta.get("to_call"),
            meta.get("title"),
            meta.get("display_type"),
            meta.get("msg_type"),
            meta.get("search_text"),
        )

    def _map_event_matches_primary_filters(
        self,
        event: Dict[str, object],
        *,
        group_filter: str = "",
        topic_filter: str = "",
        search_text: str = "",
    ) -> bool:
        if not isinstance(event, dict):
            return False
        group_key = self._normalize_map_group_value(group_filter)
        if group_key:
            if not self._map_values_match_group_filter(self._map_report_group_values(event), group_key):
                return False
        topic_key = str(topic_filter or "").strip()
        if topic_key:
            evidence_values = [
                str(event.get(key) or "")
                for key in (
                    "summary",
                    "form_id",
                    "form_name",
                    "title",
                    "message",
                    "details",
                    "tooltip",
                    "search_text",
                    "source_label",
                )
            ]
            topics = {
                str(value or "").strip().lower()
                for value in normalize_topic_terms(" ".join(str(value or "") for value in evidence_values))
            }
            if topic_key.lower() not in topics and not self._map_text_matches_query(topic_key, *evidence_values):
                return False
        if str(search_text or "").strip():
            if not self._map_text_matches_query(
                search_text,
                event.get("callsign"),
                event.get("from_call"),
                event.get("to_target"),
                event.get("state"),
                event.get("grid"),
                event.get("summary"),
                event.get("form_id"),
                event.get("form_name"),
                event.get("title"),
                event.get("message"),
                event.get("details"),
                event.get("tooltip"),
                event.get("search_text"),
                event.get("source_label"),
                " ".join(self._map_report_group_values(event)),
                " ".join(self._map_report_topic_values(event)),
            ):
                return False
        return True

    def _map_filters_active(self) -> bool:
        if getattr(self, "_current_map_mode_key", lambda: "all")() == "mesh_nodes":
            recent = getattr(self, "_mesh_recent_chk", None)
            return bool((recent and recent.isChecked()) or getattr(self,"_map_geographic_country",""))
        group = self._selected_map_group_filter()
        region = self._selected_map_region_filter()
        band_data = self.band_combo.currentData() if hasattr(self, "band_combo") else {"type": "all"}
        band_active = False
        if isinstance(band_data, dict):
            band_active = str(band_data.get("type") or "all").lower() != "all"
        topic = self._selected_map_topic_filter()
        advanced = self._map_advanced_filters_signature()
        return bool(
            group
            or region
            or getattr(self,"_map_geographic_country","")
            or band_active
            or int(self.recency_seconds or 0) != MAP_DEFAULT_RECENCY_SECONDS
            or str(topic or "").strip()
            or self._selected_map_search_text()
            or advanced != ("all", "", "", "", "")
        )

    def _map_active_filter_summary(self) -> str:
        if getattr(self, "_current_map_mode_key", lambda: "all")() == "mesh_nodes":
            return "Recently heard (24h)" if self._map_filters_active() else "Known contacts"
        labels: List[str] = []
        group = self._selected_map_group_filter()
        if group:
            labels.append(f"Group {group}")
        region = self._selected_map_region_filter()
        if region:
            labels.append(f"Region {region}")
        band_data = self.band_combo.currentData() if hasattr(self, "band_combo") else {"type": "all"}
        if isinstance(band_data, dict) and str(band_data.get("type") or "all").lower() != "all":
            band_label = ""
            try:
                band_label = str(self.band_combo.currentText() or "").strip()
            except Exception:
                band_label = ""
            labels.append(f"Band {band_label or band_data.get('value') or band_data.get('type')}")
        if int(getattr(self, "recency_seconds", 0) or 0) != MAP_DEFAULT_RECENCY_SECONDS:
            labels.append(f"Age {self._map_recency_menu_label(getattr(self, '_map_recency_label', ''))}")
        topic = self._selected_map_topic_filter()
        if topic:
            labels.append(f"Topic {topic}")
        search = self._selected_map_search_text()
        if search:
            labels.append(f"Search {search}")
        scope, state, source, status, trust = self._map_advanced_filters_signature()
        if scope != "all":
            labels.append(f"Show {scope}")
        if state:
            labels.append(f"State {state}")
        if source:
            labels.append(f"Source {source}")
        if status:
            labels.append(f"Status {status}")
        if trust:
            labels.append(f"Trust {trust}")
        return "; ".join(labels)

    def _map_layers_active(self) -> bool:
        overlay_modes = {"paths", "propagation", "pins", "peer"}
        link_mode, _ = self._current_link_selection()
        return bool(
            self._current_map_mode_key() in overlay_modes
            or bool(getattr(self, "prop_overlay_enabled", False))
            or (
                bool(getattr(self, "show_link_paths", False))
                and str(link_mode or "").strip().lower() != "off"
            )
            or bool(getattr(self, "show_weather_reports", False))
            or bool(getattr(self, "show_alert_reports", False))
            or bool(getattr(self, "show_infrastructure_reports", False))
            or bool(getattr(self, "show_rf_pins", False))
        )

    def _update_clear_filter_buttons_visual(self, theme: Optional[Dict[str, str]] = None) -> None:
        if theme is None:
            theme = self._theme_snapshot()
        clear_filters = getattr(self, "_map_clear_filters_button", None)
        if clear_filters is not None:
            filters_active = self._map_filters_active()
            clear_filters.setStyleSheet(button_style("warning" if filters_active else "muted", theme))
            summary = self._map_active_filter_summary()
            clear_filters.setToolTip(
                f"Clear active filters: {summary}."
                if filters_active and summary
                else "Clear Group, Age, Topic, search, and advanced filters."
            )
        clear_layers = getattr(self, "_map_clear_layers_button", None)
        if clear_layers is not None:
            clear_layers.setStyleSheet(button_style("warning" if self._map_layers_active() else "muted", theme))
        controls = getattr(self, "_controls_button", None)
        if controls is not None:
            _scope, state, source, status, trust = self._map_advanced_filters_signature()
            advanced_summary = "; ".join(
                part
                for part in (
                    f"State {state}" if state else "",
                    f"Source {source}" if source else "",
                    f"Status {status}" if status else "",
                    f"Trust {trust}" if trust else "",
                )
                if part
            )
            controls.setToolTip(
                f"Advanced Map Tools has active filters: {advanced_summary}. Use Clear Filters to reset them."
                if advanced_summary
                else "Refine evidence, inspect paths, adjust basemap detail, and manage planning pins."
            )

    def _on_map_search_text_changed(self, _text: str) -> None:
        references = getattr(self,"_license_reference_presenter",None)
        if references is not None:
            if references.scope.currentData() == "references":
                return
            references._operational_search = str(_text or "").strip()
        # Fence a previous search before the debounce interval elapses.
        self._map_payload_generation = getattr(self, "_map_payload_generation", 0) + 1
        self._map_inspector_generation = getattr(self, "_map_inspector_generation", 0) + 1
        timer = getattr(self, "_map_search_timer", None)
        if timer is not None:
            timer.start()

    def _on_map_search_timeout(self) -> None:
        self._clear_report_query_caches()
        self._update_clear_filter_buttons_visual()
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._request_map_refresh(level="medium", reason="map_search")

    def _set_combo_to_first_matching_data_or_text(self, combo: object, value: str) -> None:
        if combo is None:
            return
        target = str(value or "").strip().lower()
        try:
            combo.blockSignals(True)
            if hasattr(combo, "count"):
                for idx in range(combo.count()):
                    data = str(combo.itemData(idx) or "").strip().lower()
                    text = str(combo.itemText(idx) or "").strip().lower()
                    if target in {data, text}:
                        combo.setCurrentIndex(idx)
                        return
            combo.setCurrentIndex(0)
        except Exception:
            pass
        finally:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def clear_map_filters(self) -> None:
        """Clear map filtering inputs without changing the active map layer/view."""
        recent = getattr(self, "_mesh_recent_chk", None)
        if recent is not None:
            recent.blockSignals(True)
            recent.setChecked(False)
            recent.blockSignals(False)
        for combo_attr, value in (
            ("group_filter_combo", ""),
            ("_map_country_combo", ""),
            ("region_filter_combo", ""),
            ("recency_combo", MAP_DEFAULT_RECENCY_LABEL),
            ("_map_traffic_subtype_combo", "all"),
            ("_map_topic_filter_combo", "All Topics"),
            ("_map_intel_sensitivity_combo", "active"),
            ("_map_scope_filter_combo", "all"),
            ("_map_state_filter_combo", ""),
            ("_map_source_filter_combo", ""),
            ("_map_status_filter_combo", ""),
            ("_map_trust_filter_combo", ""),
        ):
            self._set_combo_to_first_matching_data_or_text(getattr(self, combo_attr, None), value)
        if hasattr(self, "band_combo"):
            self._set_combo_to_first_matching_data_or_text(self.band_combo, "All")
        self._map_geographic_country = ""
        self.recency_seconds = MAP_DEFAULT_RECENCY_SECONDS
        self._map_recency_label = MAP_DEFAULT_RECENCY_LABEL
        self._update_map_since_button_text(MAP_DEFAULT_RECENCY_LABEL)
        edit = getattr(self, "_map_search_edit", None)
        if edit is not None:
            try:
                edit.blockSignals(True)
                edit.clear()
                edit.blockSignals(False)
            except Exception:
                pass
        self._clear_report_query_caches()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason="clear_map_filters")

    def clear_map_layers(self, *, reason: str = "clear_map_layers") -> None:
        """Clear visual overlays while preserving report/data filters."""
        references = getattr(self,"_license_reference_panel",None)
        if references is not None:
            references.overlay_checkbox.setChecked(False)
        self._remember_map_path_scope()
        current_focus_mode = str(getattr(self, "_observation_focus_mode", "") or "").strip().lower()
        preserve_report_focus = self._map_report_refinement_active()
        preserve_regional_focus = current_focus_mode == "regional_intelligence"
        preserve_focus = preserve_report_focus or preserve_regional_focus
        self._sitrep_status_only_enabled = False
        if not preserve_focus:
            self._observation_focus_enabled = False
            self._observation_focus_mode = ""
        else:
            self._observation_focus_enabled = True
            if preserve_regional_focus:
                self._observation_focus_mode = "regional_intelligence"
            elif current_focus_mode not in {"hf_reports", "local_reports", "all_reports"}:
                self._observation_focus_mode = "all_reports"
        self._now_reachable_enabled = False
        self._now_reachable_meta = {}
        self._now_reachable_callsigns = set()
        self.prop_overlay_enabled = False
        self.show_station_markers = not preserve_focus
        self.show_link_paths = False
        self.show_weather_reports = False
        self.show_alert_reports = preserve_report_focus
        self.show_infrastructure_reports = preserve_report_focus
        self.show_rf_pins = False
        self.link_mode = "off"
        self.link_value = ""
        self.relay_target = ""
        self._paths_focus_station = ""
        self._paths_previous_observation_focus = None
        for widget, value in (
            (getattr(self, "_now_reachable_button", None), False),
            (getattr(self, "map_stations_chk", None), not preserve_focus),
            (getattr(self, "map_links_chk", None), False),
            (getattr(self, "map_weather_chk", None), False),
            (getattr(self, "map_alerts_chk", None), preserve_report_focus),
            (getattr(self, "map_infrastructure_chk", None), preserve_report_focus),
            (getattr(self, "prop_overlay_chk", None), False),
        ):
            if widget is None:
                continue
            try:
                widget.blockSignals(True)
                widget.setChecked(value)
                widget.blockSignals(False)
            except Exception:
                pass
        self._sync_link_mode_combo_to_off()
        relay_combo = getattr(self, "relay_target_combo", None)
        if relay_combo is not None:
            try:
                relay_combo.blockSignals(True)
                if relay_combo.isEditable():
                    relay_combo.setEditText("")
                else:
                    relay_combo.setCurrentIndex(0)
                relay_combo.blockSignals(False)
            except Exception:
                pass
        self._clear_report_query_caches()
        self._update_now_reachable_button_visual(False)
        self._update_selected_paths_button_visual()
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_now_reachable_summary()
        self._refresh_relay_targets()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason=reason)

    def _map_combo_data_text(self, attr_name: str, default: str = "") -> str:
        combo = getattr(self, attr_name, None)
        if combo is None:
            return default
        try:
            data = combo.currentData()
            if data is not None:
                return str(data or "").strip()
        except Exception:
            pass
        try:
            return str(combo.currentText() or "").strip()
        except Exception:
            return default

    def _map_advanced_scope_filter(self) -> str:
        return self._map_combo_data_text("_map_scope_filter_combo", "all").lower() or "all"

    def _map_advanced_state_filter(self) -> str:
        value = self._map_combo_data_text("_map_state_filter_combo", "")
        if not value:
            return ""
        if value.lower() in {"all", "all states"}:
            return ""
        return value.strip().upper()

    def _map_advanced_source_filter(self) -> str:
        value = self._map_combo_data_text("_map_source_filter_combo", "")
        return "" if value.lower() in {"all", "all sources"} else value.strip().lower()

    def _map_advanced_status_filter(self) -> str:
        value = self._map_combo_data_text("_map_status_filter_combo", "")
        return "" if value.lower() in {"all", "all statuses"} else value.strip().lower()

    def _map_advanced_trust_filter(self) -> str:
        value = self._map_combo_data_text("_map_trust_filter_combo", "")
        return "" if value.lower() in {"all", "all auth/trust"} else value.strip().lower()

    def _map_advanced_filters_signature(self) -> Tuple[str, str, str, str, str]:
        return (
            self._map_advanced_scope_filter(),
            self._map_advanced_state_filter(),
            self._map_advanced_source_filter(),
            self._map_advanced_status_filter(),
            self._map_advanced_trust_filter(),
        )

    def _advanced_filters_allow_stations(self) -> bool:
        return self._map_advanced_scope_filter() != "reports"

    def _advanced_filters_allow_reports(self) -> bool:
        return self._map_advanced_scope_filter() != "stations"

    def _map_reports_allowed_for_current_view(self) -> bool:
        """The main map view chips are authoritative for traffic/report views."""
        if self._map_report_refinement_active():
            return True
        return self._advanced_filters_allow_reports()

    def _station_matches_advanced_filters(self, pt: StationPoint) -> bool:
        state_filter = self._map_advanced_state_filter()
        if state_filter:
            state = str(getattr(pt, "state", "") or "").strip().upper()
            if state != state_filter:
                return False
        return True

    @staticmethod
    def _map_state_filter_values(*sources: object) -> Set[str]:
        values: Set[str] = set()
        state_keys = (
            "state",
            "reported_for_state",
            "impacted_state",
            "report_state",
            "area_state",
            "target_state",
        )
        for source in sources:
            if isinstance(source, Mapping):
                iterable = (source.get(key) for key in state_keys)
            else:
                iterable = (getattr(source, key, "") for key in state_keys)
            for value in iterable:
                text = str(value or "").strip().upper()
                if not text:
                    continue
                token = re.split(r"[\s/|,]+", text)[0].strip().upper()
                if len(token) == 2 and token.isalpha():
                    values.add(token)
        return values

    @classmethod
    def _map_source_family_matches_filter(cls, source_family: str, source_filter: str) -> bool:
        source = cls._canonical_map_source_family(source_family)
        wanted = str(source_filter or "").strip().lower()
        if not wanted:
            return True
        if wanted == "fastlight":
            return source in {"flmsg", "flamp"}
        if wanted == "hf_apps":
            return source in {"spotter", "commstat", "js8call", "varac", "flmsg", "flamp", "condition_alert"}
        if wanted == "mesh":
            return source in {"mesh", "meshcore", "meshtastic", "mesh_client", "local_mesh"}
        return source == wanted

    @staticmethod
    def _map_status_matches_filter(*values: object, status_filter: str = "") -> bool:
        wanted = str(status_filter or "").strip().lower()
        if not wanted:
            return True
        normalized = {
            str(value or "").strip().lower()
            for value in values
            if str(value or "").strip()
        }
        if wanted == "needs_review":
            return bool(normalized.intersection({"red", "yellow", "watch", "priority", "emergency", "warning", "caution", "severe"}))
        if wanted == "normal":
            return bool(normalized.intersection({"green", "functioning", "info", "normal", "ok"}))
        if wanted == "unconfirmed":
            return bool(normalized.intersection({"unconfirmed", "unknown", "unverified"}))
        return wanted in normalized

    @staticmethod
    def _map_trust_matches_filter(*values: object, trust_filter: str = "") -> bool:
        wanted = str(trust_filter or "").strip().lower()
        if not wanted:
            return True
        normalized = {
            str(value or "").strip().lower()
            for value in values
            if str(value or "").strip()
        }
        if wanted == "verified":
            return bool(normalized.intersection({"verified", "trusted", "confirmed", "valid", "signed"}))
        if wanted == "unverified":
            return not bool(normalized.intersection({"verified", "trusted", "confirmed", "valid", "signed"}))
        if wanted == "confirmed":
            return bool(normalized.intersection({"confirmed", "trusted"}))
        if wanted == "unconfirmed":
            return not bool(normalized.intersection({"confirmed", "trusted"}))
        return wanted in normalized

    def _observation_matches_advanced_filters(self, obs, metadata: Optional[Mapping[str, object]] = None) -> bool:
        metadata = metadata if isinstance(metadata, Mapping) else {}
        state_filter = self._map_advanced_state_filter()
        if state_filter:
            provenance = getattr(obs, "provenance", {}) or {}
            states = self._map_state_filter_values(metadata, provenance, obs)
            if state_filter not in states:
                return False
        source_filter = self._map_advanced_source_filter()
        if not self._map_source_family_matches_filter(getattr(obs, "source_family", ""), source_filter):
            return False
        status_filter = self._map_advanced_status_filter()
        if not self._map_status_matches_filter(
            metadata.get("status"),
            metadata.get("alert_color"),
            getattr(obs, "status", ""),
            getattr(obs, "urgency", ""),
            status_filter=status_filter,
        ):
            return False
        trust_filter = self._map_advanced_trust_filter()
        if not self._map_trust_matches_filter(
            getattr(obs, "auth_state", ""),
            getattr(obs, "trusted_state", ""),
            getattr(obs, "confirmed_state", ""),
            trust_filter=trust_filter,
        ):
            return False
        return True

    def _map_event_matches_advanced_filters(self, event: Dict[str, object]) -> bool:
        if not isinstance(event, dict):
            return False
        state_filter = self._map_advanced_state_filter()
        if state_filter:
            states = self._map_state_filter_values(event)
            if state_filter not in states:
                return False
        source_filter = self._map_advanced_source_filter()
        source = str(event.get("source_family") or event.get("primary_source_family") or "").strip().lower()
        if not self._map_source_family_matches_filter(source, source_filter):
            return False
        status_filter = self._map_advanced_status_filter()
        if not self._map_status_matches_filter(
            event.get("status"),
            event.get("severity"),
            status_filter=status_filter,
        ):
            return False
        trust_filter = self._map_advanced_trust_filter()
        if not self._map_trust_matches_filter(
            event.get("auth_state"),
            event.get("trusted_state"),
            event.get("confirmed_state"),
            trust_filter=trust_filter,
        ):
            return False
        return True

    @staticmethod
    def _map_report_age_text(ts_value: object, *, now: Optional[float] = None) -> str:
        try:
            ts = float(ts_value or 0.0)
        except Exception:
            ts = 0.0
        if ts <= 0:
            return "unknown age"
        now_ts = time.time() if now is None else float(now)
        seconds = max(0, int(now_ts - ts))
        if seconds < 90:
            return "now" if seconds < 15 else f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} min ago"
        if minutes < 24 * 60:
            hours = minutes // 60
            rem = minutes % 60
            return f"{hours}:{rem:02d} h ago"
        days = minutes // (24 * 60)
        return f"{days} day{'s' if days != 1 else ''} ago"

    @staticmethod
    def _map_report_source_label(source_family: object, source_app: object = "") -> str:
        source = str(source_family or "").strip().lower()
        app = str(source_app or "").strip()
        if source == "spotter":
            return "HF JS8Spotter"
        if source == "rf_pin":
            return "Planning Pin"
        if source in {"fused", "mixed", "multiple_sources", "multiple sources"}:
            return "Multiple Sources"
        if not source:
            return "HF Report"
        if source == "local_report":
            return "Local Report"
        label = source_family_label(source or app or "report")
        return str(label or "Report").strip()

    @staticmethod
    def _map_report_source_kind(source_family: object) -> str:
        source = str(source_family or "").strip().lower()
        if source == "local_report":
            return "local"
        if source == "commstat":
            return "commstat"
        if source in {"rf_pin", "pin"}:
            return "pin"
        if source == "mixed":
            return "mixed"
        return "hf"

    @staticmethod
    def _map_report_status_key(report: Mapping[str, object]) -> str:
        from freqinout.core.commstat_artifacts import normalize_status_label

        for value in (report.get("alert_color"), report.get("status")):
            key = normalize_status_label(value).lower()
            if key in {"red", "yellow", "green", "unknown"}:
                return key
        return ""

    @staticmethod
    def _compact_report_status_line(report: Dict[str, object]) -> str:
        source_family = str(report.get("source_family") or "").strip().lower()
        auth = str(report.get("auth_state") or "").strip()
        trusted = str(report.get("trusted_state") or "").strip()
        confirmed = str(report.get("confirmed_state") or "").strip().upper()
        parts: List[str] = []
        if source_family == "local_report" and confirmed:
            parts.append(f"Local: {confirmed.replace('_', ' ').title()}")
        if auth:
            auth_text = auth.replace("_", " ").title()
            if trusted:
                auth_text = f"{auth_text}, {trusted.replace('_', ' ').title()}"
            parts.append(f"Auth: {auth_text}")
        elif trusted:
            parts.append(f"Trust: {trusted.replace('_', ' ').title()}")
        return " | ".join(parts)

    @staticmethod
    def _map_report_location_line(report: Dict[str, object]) -> str:
        state = str(report.get("state") or "").strip().upper()
        grid = str(report.get("grid") or "").strip().upper()
        confidence = str(report.get("location_confidence") or "").strip().replace("_", " ").title()
        area = " / ".join(part for part in (state, grid) if part)
        if not area:
            area = "Mapped location"
        label = "Reported For" if str(report.get("source_family") or "").strip().lower() == "commstat" else "Area"
        return f"{label}: {area}" + (f" ({confidence})" if confidence else "")

    def _map_report_detail_lines(self, report: Dict[str, object], *, now: Optional[float] = None) -> List[str]:
        source = str(report.get("source_label") or self._map_report_source_label(report.get("source_family"))).strip()
        call = str(report.get("callsign") or "").strip().upper()
        to_target = str(report.get("to_target") or "").strip().lstrip("@")
        form = str(report.get("form_id") or "").strip()
        summary = str(report.get("summary") or "Report received").strip()
        raw_topics = report.get("topics", [])
        topics = (
            [str(t).strip() for t in raw_topics if str(t).strip()]
            if isinstance(raw_topics, (list, tuple, set))
            else []
        )
        age = self._map_report_age_text(report.get("utc_ts"), now=now)
        route = " -> ".join(part for part in (call, to_target) if part)
        heading_parts = [source]
        if form:
            heading_parts.append(form)
        if route:
            heading_parts.append(route)
        lines = [f"{' | '.join(heading_parts)} | {age}"]
        if topics:
            lines.append(f"Topics: {', '.join(topics[:4])}" + ("..." if len(topics) > 4 else ""))
        lines.append(self._map_report_location_line(report))
        status_line = self._compact_report_status_line(report)
        if status_line:
            lines.append(status_line)
        if summary:
            lines.append(f"Summary: {summary}")
        return lines

    def _map_event_within_recency(
        self,
        event: Dict[str, object],
        max_age_sec: Optional[int] = None,
        *,
        now: Optional[float] = None,
    ) -> bool:
        """Return whether a map event belongs in the selected traffic age window."""
        window = int(max_age_sec or 0)
        if window <= 0:
            return True
        ts = self._safe_float(event.get("utc_ts"), 0.0) or self._safe_float(event.get("latest_ts"), 0.0)
        if ts <= 0:
            return False
        now_ts = float(now if now is not None else time.time())
        return (now_ts - ts) <= window

    @staticmethod
    def _map_report_unique_key(report: Dict[str, object]) -> str:
        metadata_path = str(report.get("metadata_path") or "").strip()
        source_ref = str(report.get("source_ref") or "").strip()
        if source_ref.startswith("file:"):
            source_ref_path = source_ref[5:].strip()
            if source_ref_path:
                return f"file:{source_ref_path}"
        if metadata_path:
            return f"file:{metadata_path}"
        for key in ("source_ref", "metadata_path", "raw_reference"):
            value = str(report.get(key) or "").strip()
            if value:
                return f"{key}:{value}"
        topics = ",".join(sorted(StationsMapTab._map_report_topic_values(report))).lower()
        groups = ",".join(sorted(StationsMapTab._map_report_group_values(report))).lower()
        parts = [
            str(report.get("source_family") or "").strip().lower(),
            str(report.get("callsign") or report.get("from_call") or "").strip().upper(),
            str(report.get("to_target") or "").strip().upper().lstrip("@").rstrip(">"),
            str(report.get("state") or "").strip().upper(),
            str(report.get("grid") or "").strip().upper(),
            str(report.get("form_id") or "").strip().upper(),
            str(report.get("utc_ts") or report.get("latest_ts") or "").strip(),
            topics,
            groups,
            re.sub(r"\s+", " ", str(report.get("summary") or report.get("title") or "").strip().lower()),
        ]
        return "|".join(parts)

    @staticmethod
    def _map_report_group_values(report: Dict[str, object]) -> List[str]:
        values: List[str] = []
        raw_values = [
            report.get("to_target"),
            report.get("group"),
            report.get("report_group"),
            report.get("operating_group"),
        ]
        raw_groups = report.get("groups")
        if isinstance(raw_groups, (list, tuple, set)):
            raw_values.extend(raw_groups)
        elif raw_groups:
            raw_values.append(raw_groups)
        for raw in raw_values:
            group = str(raw or "").strip().upper().lstrip("@").rstrip(">")
            if group and group not in values:
                values.append(group)
        return values

    @staticmethod
    def _map_report_topic_values(report: Dict[str, object]) -> List[str]:
        values: List[str] = []
        raw_values: List[object] = []
        for key in ("topics", "observed_topics", "topic"):
            raw = report.get(key)
            if isinstance(raw, (list, tuple, set)):
                raw_values.extend(raw)
            elif raw:
                raw_values.append(raw)
        for raw in raw_values:
            topic = str(raw or "").strip()
            if topic and topic not in values:
                values.append(topic)
        return values

    @staticmethod
    def _observation_ts(value: object) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return float(parsed.timestamp())
        except Exception:
            return 0.0

    @staticmethod
    def _report_position(
        report: Dict[str, object],
        station_lookup: Dict[str, StationPoint],
    ) -> tuple[Optional[float], Optional[float]]:
        report_for_state = str(report.get("reported_for_state") or "").strip().upper()
        report_for_grid = str(report.get("reported_for_grid") or "").strip().upper()
        position_report = report
        if report_for_state or report_for_grid:
            position_report = dict(report)
            position_report["state"] = report_for_state or str(report.get("state") or "").strip().upper()
            position_report["grid"] = report_for_grid or str(report.get("grid") or "").strip().upper()
        conflict_state_center = StationsMapTab._report_state_center_if_grid_conflicts(position_report)
        if conflict_state_center:
            return conflict_state_center
        lat = position_report.get("lat")
        lon = position_report.get("lon")
        try:
            if lat is not None and lon is not None:
                return float(lat), float(lon)
        except Exception:
            pass
        state = str(position_report.get("state") or "").strip().upper()
        grid = str(position_report.get("grid") or "").strip().upper()
        if StationsMapTab._map_grid_looks_usable(grid):
            ll = maidenhead_to_latlon(grid)
            if ll:
                state_center = STATE_CENTERS.get(state)
                if state_center:
                    try:
                        km = PropagationService.haversine_km(
                            float(ll[0]),
                            float(ll[1]),
                            float(state_center[0]),
                            float(state_center[1]),
                        )
                    except Exception:
                        km = 0.0
                    if km > 850:
                        return float(state_center[0]), float(state_center[1])
                return float(ll[0]), float(ll[1])
        if state in STATE_CENTERS:
            state_center = STATE_CENTERS[state]
            return float(state_center[0]), float(state_center[1])
        call = str(report.get("callsign") or "").strip().upper()
        pt = station_lookup.get(call)
        if pt is None:
            base = JS8LogLinkIndexer._base_callsign(call)
            pt = station_lookup.get(base) if base else None
        if pt is not None:
            return float(pt.lat or 0.0), float(pt.lon or 0.0)
        return None, None

    @staticmethod
    def _report_state_center_if_grid_conflicts(report: Dict[str, object]) -> Optional[tuple[float, float]]:
        state = str(report.get("state") or "").strip().upper()
        grid = str(report.get("grid") or "").strip().upper()
        state_center = STATE_CENTERS.get(state)
        if not state_center or not StationsMapTab._map_grid_looks_usable(grid):
            return None
        ll = maidenhead_to_latlon(grid)
        if not ll:
            return None
        try:
            km = PropagationService.haversine_km(
                float(ll[0]),
                float(ll[1]),
                float(state_center[0]),
                float(state_center[1]),
            )
        except Exception:
            return None
        if km > 850:
            return float(state_center[0]), float(state_center[1])
        return None

    def _build_weather_map_events(
        self,
        station_lookup: Dict[str, StationPoint],
        *,
        max_age_sec: int = 0,
    ) -> List[Dict[str, object]]:
        reports = self._cached_map_value(
            "spotter_weather_reports",
            {},
            self._load_spotter_weather_reports,
            ttl_sec=6.0,
        )
        if not reports:
            return []
        buckets: Dict[tuple[int, int], Dict[str, object]] = {}
        now = time.time()
        for report in reports:
            if not isinstance(report, dict):
                continue
            if not self._map_event_within_recency(report, max_age_sec, now=now):
                continue
            call = str(report.get("callsign") or "").strip().upper()
            lat, lon = self._report_position(report, station_lookup)
            if lat is None or lon is None:
                continue
            key = (
                int(round(lat / WEATHER_CLUSTER_DEGREES)),
                int(round(lon / WEATHER_CLUSTER_DEGREES)),
            )
            severity = str(report.get("severity") or "unknown").strip().lower()
            icon = str(report.get("icon") or "general").strip().lower()
            rank = WEATHER_SEVERITY_RANK.get(severity, 0)
            ts = self._safe_float(report.get("utc_ts"), 0.0)
            bucket = buckets.setdefault(
                key,
                {
                    "lat_sum": 0.0,
                    "lon_sum": 0.0,
                    "count": 0,
                    "latest_ts": 0.0,
                    "max_rank": 0,
                    "severity": "unknown",
                    "icon": "general",
                    "reports": [],
                    "callsigns": set(),
                    "groups": set(),
                    "topics": set(),
                    "states": set(),
                    "grids": set(),
                    "source_families": set(),
                },
            )
            bucket["lat_sum"] = float(bucket.get("lat_sum", 0.0)) + lat
            bucket["lon_sum"] = float(bucket.get("lon_sum", 0.0)) + lon
            bucket["count"] = int(bucket.get("count", 0) or 0) + 1
            bucket["callsigns"].add(call)
            bucket["groups"].update(self._map_report_group_values(report))
            bucket["topics"].update(self._map_report_topic_values(report))
            state = str(report.get("state") or "").strip().upper()
            grid = str(report.get("grid") or "").strip().upper()
            if state:
                bucket["states"].add(state)
            if grid:
                bucket["grids"].add(grid)
            if rank > int(bucket.get("max_rank", 0) or 0) or ts >= self._safe_float(bucket.get("latest_ts"), 0.0):
                if rank >= int(bucket.get("max_rank", 0) or 0):
                    bucket["icon"] = icon
                    bucket["severity"] = severity
                    bucket["max_rank"] = rank
            if ts > self._safe_float(bucket.get("latest_ts"), 0.0):
                bucket["latest_ts"] = ts
            bucket["reports"].append(report)

        events: List[Dict[str, object]] = []
        for bucket in buckets.values():
            count = max(1, int(bucket.get("count", 0) or 0))
            reports_sorted = sorted(
                [r for r in bucket.get("reports", []) if isinstance(r, dict)],
                key=lambda r: self._safe_float(r.get("utc_ts"), 0.0),
                reverse=True,
            )
            latest_ts = self._safe_float(bucket.get("latest_ts"), 0.0)
            age_minutes = int(max(0.0, now - latest_ts) // 60) if latest_ts else 0
            age_label = f"{age_minutes}m ago" if age_minutes < 120 else f"{age_minutes // 60}h ago"
            calls = sorted(str(c) for c in bucket.get("callsigns", set()) if str(c))
            groups = sorted(str(g) for g in bucket.get("groups", set()) if str(g))
            topics = sorted(str(t) for t in bucket.get("topics", set()) if str(t))
            states = sorted(str(s) for s in bucket.get("states", set()) if str(s))
            grids = sorted(str(g) for g in bucket.get("grids", set()) if str(g))
            primary_topic, event_icon = self._map_event_topic_and_icon(
                topics,
                bucket.get("icon") or "general",
                preferred_topic=self._selected_map_topic_filter(),
            )
            detail_lines = [
                f"Weather Reports: {count}",
                f"Newest: {age_label}",
                f"Status: {str(bucket.get('severity') or 'unknown').title()}",
                f"Groups: {', '.join(groups[:5])}" + ("..." if len(groups) > 5 else "") if groups else "",
                f"Topics: {', '.join(topics[:5])}" + ("..." if len(topics) > 5 else "") if topics else "",
                f"Sources: {', '.join(calls[:6])}" + ("..." if len(calls) > 6 else ""),
            ]
            for report in reports_sorted[:4]:
                summary = str(report.get("summary") or "Weather report received").strip()
                source = str(report.get("callsign") or "").strip().upper()
                form = str(report.get("form_id") or "").strip()
                detail_lines.append(f"{source} {form}: {summary}".strip())
            events.append(
                {
                    "lat": float(bucket.get("lat_sum", 0.0)) / count,
                    "lon": float(bucket.get("lon_sum", 0.0)) / count,
                    "count": count,
                    "icon": event_icon,
                    "severity": str(bucket.get("severity") or "unknown"),
                    "latest_ts": latest_ts,
                    "age": age_label,
                    "callsigns": calls,
                    "groups": groups,
                    "primary_group": groups[0] if groups else "",
                    "topics": topics,
                    "primary_topic": primary_topic,
                    "topic": primary_topic,
                    "title": f"Weather Reports: {count}",
                    "tooltip": self._map_compact_tooltip_html(detail_lines),
                }
            )
        return sorted(
            events,
            key=lambda row: (
                WEATHER_SEVERITY_RANK.get(str(row.get("severity") or "unknown"), 0),
                self._safe_float(row.get("latest_ts"), 0.0),
            ),
            reverse=True,
        )

    def _build_spotter_operational_events(
        self,
        station_lookup: Dict[str, StationPoint],
        *,
        layer_name: str,
        display_label: str,
        reports_loader,
        max_age_sec: int = 0,
    ) -> List[Dict[str, object]]:
        reports = self._cached_map_value(
            f"spotter_{layer_name}_reports",
            self._map_report_cache_signature(layer_name),
            reports_loader,
            ttl_sec=6.0,
        )
        if not reports:
            return []
        buckets: Dict[tuple[int, int], Dict[str, object]] = {}
        now = time.time()
        for report in reports:
            if not isinstance(report, dict):
                continue
            if not self._map_event_within_recency(report, max_age_sec, now=now):
                continue
            call = str(report.get("callsign") or "").strip().upper()
            lat, lon = self._report_position(report, station_lookup)
            if lat is None or lon is None:
                continue
            key = (
                int(round(lat / WEATHER_CLUSTER_DEGREES)),
                int(round(lon / WEATHER_CLUSTER_DEGREES)),
            )
            severity = str(report.get("severity") or "unknown").strip().lower()
            icon = str(report.get("icon") or "general").strip().lower()
            rank = WEATHER_SEVERITY_RANK.get(severity, 0)
            ts = self._safe_float(report.get("utc_ts"), 0.0)
            bucket = buckets.setdefault(
                key,
                {
                    "lat_sum": 0.0,
                    "lon_sum": 0.0,
                    "count": 0,
                    "latest_ts": 0.0,
                    "max_rank": 0,
                    "severity": "unknown",
                    "icon": icon,
                    "reports": [],
                    "callsigns": set(),
                    "groups": set(),
                    "topics": set(),
                    "states": set(),
                    "grids": set(),
                    "reported_for_states": set(),
                    "reported_for_grids": set(),
                    "reported_by": set(),
                    "scopes": set(),
                    "state_confidences": set(),
                    "geo_confidences": set(),
                    "source_refs": set(),
                    "report_keys": set(),
                    "search_terms": [],
                },
            )
            unique_key = self._map_report_unique_key(report)
            report_keys = bucket.setdefault("report_keys", set())
            if isinstance(report_keys, set) and unique_key in report_keys:
                continue
            if isinstance(report_keys, set) and unique_key:
                report_keys.add(unique_key)
            bucket["lat_sum"] = float(bucket.get("lat_sum", 0.0)) + lat
            bucket["lon_sum"] = float(bucket.get("lon_sum", 0.0)) + lon
            bucket["count"] = int(bucket.get("count", 0) or 0) + 1
            bucket["callsigns"].add(call)
            bucket["groups"].update(self._map_report_group_values(report))
            bucket["topics"].update(self._map_report_topic_values(report))
            state = str(report.get("state") or "").strip().upper()
            grid = str(report.get("grid") or "").strip().upper()
            reported_for_state = str(report.get("reported_for_state") or state).strip().upper()
            reported_for_grid = str(report.get("reported_for_grid") or grid).strip().upper()
            reported_by = str(report.get("reported_by") or report.get("from_call") or call).strip().upper()
            if state:
                bucket["states"].add(state)
            if grid:
                bucket["grids"].add(grid)
            if reported_for_state:
                bucket["reported_for_states"].add(reported_for_state)
            if reported_for_grid:
                bucket["reported_for_grids"].add(reported_for_grid)
            if reported_by:
                bucket["reported_by"].add(reported_by)
            scope = str(report.get("scope") or "").strip()
            if scope:
                bucket["scopes"].add(scope)
            state_confidence = str(report.get("state_confidence") or "").strip()
            if state_confidence:
                bucket["state_confidences"].add(state_confidence)
            geo_confidence = str(report.get("geo_confidence") or "").strip()
            if geo_confidence:
                bucket["geo_confidences"].add(geo_confidence)
            source_ref = str(report.get("source_ref") or report.get("metadata_path") or report.get("raw_reference") or "").strip()
            if source_ref:
                bucket["source_refs"].add(source_ref)
            search_terms = bucket.setdefault("search_terms", [])
            if isinstance(search_terms, list):
                search_terms.extend(
                    str(part or "")
                    for part in (
                        report.get("search_text"),
                        report.get("summary"),
                        report.get("title"),
                        report.get("form_id"),
                        report.get("form_name"),
                        report.get("message"),
                        report.get("details"),
                        report.get("tooltip"),
                        report.get("source_label"),
                        report.get("source_family"),
                        report.get("callsign"),
                        report.get("from_call"),
                        report.get("to_target"),
                        report.get("state"),
                        report.get("grid"),
                        report.get("reported_for_state"),
                        report.get("reported_for_grid"),
                        report.get("reported_by"),
                        " ".join(self._map_report_group_values(report)),
                        " ".join(self._map_report_topic_values(report)),
                    )
                    if str(part or "").strip()
                )
            if rank > int(bucket.get("max_rank", 0) or 0) or ts >= self._safe_float(bucket.get("latest_ts"), 0.0):
                if rank >= int(bucket.get("max_rank", 0) or 0):
                    bucket["icon"] = icon
                    bucket["severity"] = severity
                    bucket["max_rank"] = rank
            if ts > self._safe_float(bucket.get("latest_ts"), 0.0):
                bucket["latest_ts"] = ts
            bucket["reports"].append(report)

        events: List[Dict[str, object]] = []
        for bucket in buckets.values():
            count = max(1, int(bucket.get("count", 0) or 0))
            reports_sorted = sorted(
                [r for r in bucket.get("reports", []) if isinstance(r, dict)],
                key=lambda r: self._safe_float(r.get("utc_ts"), 0.0),
                reverse=True,
            )
            latest_ts = self._safe_float(bucket.get("latest_ts"), 0.0)
            age_minutes = int(max(0.0, now - latest_ts) // 60) if latest_ts else 0
            age_label = self._map_report_age_text(latest_ts, now=now)
            calls = sorted(str(c) for c in bucket.get("callsigns", set()) if str(c))
            groups = sorted(str(g) for g in bucket.get("groups", set()) if str(g))
            topics = sorted(str(t) for t in bucket.get("topics", set()) if str(t))
            states = sorted(str(s) for s in bucket.get("states", set()) if str(s))
            grids = sorted(str(g) for g in bucket.get("grids", set()) if str(g))
            reported_for_states = sorted(str(s) for s in bucket.get("reported_for_states", set()) if str(s))
            reported_for_grids = sorted(str(g) for g in bucket.get("reported_for_grids", set()) if str(g))
            reported_by_values = sorted(str(c) for c in bucket.get("reported_by", set()) if str(c))
            scopes = sorted(str(s) for s in bucket.get("scopes", set()) if str(s))
            state_confidences = sorted(str(s) for s in bucket.get("state_confidences", set()) if str(s))
            geo_confidences = sorted(str(s) for s in bucket.get("geo_confidences", set()) if str(s))
            source_refs = sorted(str(ref) for ref in bucket.get("source_refs", set()) if str(ref))
            primary_topic, event_icon = self._map_event_topic_and_icon(
                topics,
                bucket.get("icon") or "general",
                preferred_topic=self._selected_map_topic_filter(),
            )
            source_counts: Dict[str, int] = {}
            source_kinds: Set[str] = set()
            source_families: Set[str] = set()
            for report in reports_sorted:
                source_family = str(report.get("source_family") or "").strip().lower()
                source = str(report.get("source_label") or self._map_report_source_label(source_family)).strip()
                source_counts[source] = source_counts.get(source, 0) + 1
                source_kinds.add(self._map_report_source_kind(source_family))
                if source_family:
                    source_families.add(source_family)
            source_text = ", ".join(f"{label} {count}" for label, count in sorted(source_counts.items()))
            if len(source_kinds) > 1:
                source_kind = "mixed"
            else:
                source_kind = next(iter(source_kinds), "hf")
            if len(source_families) == 1:
                primary_source_family = next(iter(source_families))
            elif "condition_alert" in source_families:
                primary_source_family = "condition_alert"
            else:
                primary_source_family = ""
            source_label = source_text or self._map_report_source_label(primary_source_family)
            mesh_node_cluster = bool(
                primary_source_family in {"mesh", "meshcore", "meshtastic", "mesh_client", "local_mesh"}
                and any(str(ref or "").startswith("mesh-node:") for ref in source_refs)
            )
            list_limit = 14 if mesh_node_cluster else 6
            summary_limit = 14 if mesh_node_cluster else 4
            detail_report_limit = 14 if mesh_node_cluster else 4
            if len(calls) == 1:
                call_label = calls[0]
            elif calls:
                call_label = f"{calls[0]} +{len(calls) - 1}"
            else:
                call_label = ""
            area_parts = []
            if states:
                area_parts.append(", ".join(states[:3]) + ("..." if len(states) > 3 else ""))
            if grids:
                area_parts.append(", ".join(grids[:3]) + ("..." if len(grids) > 3 else ""))
            detail_lines = [
                f"{display_label}: {count}",
                f"Newest: {age_label}",
                f"Status: {str(bucket.get('severity') or 'unknown').title()}",
                f"Source: {source_label}" if source_label else "",
                f"Groups: {', '.join(groups[:5])}" + ("..." if len(groups) > 5 else "") if groups else "",
                f"Topics: {', '.join(topics[:5])}" + ("..." if len(topics) > 5 else "") if topics else "",
                f"From: {', '.join(calls[:list_limit])}" + ("..." if len(calls) > list_limit else ""),
            ]
            row_items = [
                {"label": "Reports", "value": str(count)},
                {"label": "Newest", "value": age_label},
                {"label": "Status", "value": str(bucket.get("severity") or "unknown").title()},
                {"label": "Source", "value": source_label},
            ]
            if call_label:
                row_items.append({"label": "Reporter", "value": call_label})
            if groups:
                row_items.append(
                    {
                        "label": "Groups",
                        "value": ", ".join(groups[:5]) + ("..." if len(groups) > 5 else ""),
                    }
                )
            if topics:
                row_items.append(
                    {
                        "label": "Topics",
                        "value": ", ".join(topics[:5]) + ("..." if len(topics) > 5 else ""),
                    }
                )
            if calls:
                row_items.append(
                    {
                        "label": "From",
                        "value": ", ".join(calls[:list_limit]) + ("..." if len(calls) > list_limit else ""),
                    }
                )
            if scopes:
                row_items.append(
                    {
                        "label": "Report Scope",
                        "value": ", ".join(scopes[:3]) + ("..." if len(scopes) > 3 else ""),
                    }
                )
            if area_parts:
                area_text = " / ".join(area_parts)
                row_items.append({"label": "Area", "value": area_text})
                if primary_source_family == "commstat":
                    reported_for_parts = []
                    if reported_for_states:
                        reported_for_parts.append(", ".join(reported_for_states[:3]) + ("..." if len(reported_for_states) > 3 else ""))
                    if reported_for_grids:
                        reported_for_parts.append(", ".join(reported_for_grids[:3]) + ("..." if len(reported_for_grids) > 3 else ""))
                    row_items.append({"label": "Reported For", "value": " / ".join(reported_for_parts) or area_text})
                    if reported_by_values:
                        row_items.append(
                            {
                                "label": "Reported By",
                                "value": ", ".join(reported_by_values[:6]) + ("..." if len(reported_by_values) > 6 else ""),
                            }
                        )
            summary_lines: List[str] = []
            for idx, report in enumerate(reports_sorted[:detail_report_limit]):
                if idx:
                    detail_lines.append("")
                report_lines = self._map_report_detail_lines(report, now=now)
                detail_lines.extend(report_lines)
                summary = self._map_detail_clean_text(report.get("summary") or "", multiline=True)
                if summary and not self._map_station_summary_is_noise(summary):
                    summary_lines.append(summary)
                elif report_lines:
                    clean_line = self._map_detail_clean_text(report_lines[0])
                    if clean_line and not self._map_station_summary_is_noise(clean_line):
                        summary_lines.append(clean_line)
            clean_detail_lines = [self._map_detail_clean_text(line, multiline=True) for line in detail_lines if line]
            plain_summary = "\n".join(dict.fromkeys(summary_lines[:summary_limit]))
            if not plain_summary:
                plain_summary = "\n".join(clean_detail_lines[: max(8, summary_limit)])
            cluster_search_text = "\n".join(
                dict.fromkeys(
                    str(term or "").strip()
                    for term in bucket.get("search_terms", [])
                    if str(term or "").strip()
                )
            )
            event_title = f"{primary_topic} Reports: {count}" if primary_topic else f"{display_label}: {count}"
            route_parts = []
            if groups:
                route_parts.append(groups[0])
            if primary_topic:
                route_parts.append(primary_topic)
            if call_label:
                route_parts.append(f"from {call_label}")
            events.append(
                {
                    "lat": float(bucket.get("lat_sum", 0.0)) / count,
                    "lon": float(bucket.get("lon_sum", 0.0)) / count,
                    "count": count,
                    "icon": event_icon,
                    "severity": str(bucket.get("severity") or "unknown"),
                    "latest_ts": latest_ts,
                    "age": age_label,
                    "callsigns": calls,
                    "callsign": calls[0] if len(calls) == 1 else "",
                    "call_label": call_label,
                    "groups": groups,
                    "primary_group": groups[0] if groups else "",
                    "topics": topics,
                    "primary_topic": primary_topic,
                    "topic": primary_topic,
                    "group": groups[0] if groups else "",
                    "title": event_title,
                    "source_label": source_label,
                    "source_mix": source_label,
                    "source_kind": source_kind,
                    "source_family": primary_source_family,
                    "source_ref": source_refs[0] if len(source_refs) == 1 else "",
                    "source_refs": source_refs,
                    "state": ", ".join(states[:3]) + ("..." if len(states) > 3 else ""),
                    "grid": ", ".join(grids[:3]) + ("..." if len(grids) > 3 else ""),
                    "reported_for_state": ", ".join(reported_for_states[:3]) + ("..." if len(reported_for_states) > 3 else ""),
                    "reported_for_grid": ", ".join(reported_for_grids[:3]) + ("..." if len(reported_for_grids) > 3 else ""),
                    "reported_by": ", ".join(reported_by_values[:6]) + ("..." if len(reported_by_values) > 6 else ""),
                    "scope": ", ".join(scopes[:3]) + ("..." if len(scopes) > 3 else ""),
                    "state_confidence": ", ".join(state_confidences[:3]) + ("..." if len(state_confidences) > 3 else ""),
                    "geo_confidence": ", ".join(geo_confidences[:3]) + ("..." if len(geo_confidences) > 3 else ""),
                    "route": " | ".join(route_parts),
                    "rows": row_items,
                    "summary": plain_summary,
                    "details": plain_summary or cluster_search_text,
                    "search_text": cluster_search_text,
                    "tooltip": self._map_compact_tooltip_html(detail_lines, limit=20),
                }
            )
        return sorted(
            events,
            key=lambda row: (
                WEATHER_SEVERITY_RANK.get(str(row.get("severity") or "unknown"), 0),
                self._safe_float(row.get("latest_ts"), 0.0),
            ),
            reverse=True,
        )

    def _build_map_report_focus_events(
        self,
        station_lookup: Dict[str, StationPoint],
        *,
        max_age_sec: int,
    ) -> List[Dict[str, object]]:
        """Build the unified operator-facing traffic layer for map report views.

        Traffic subtypes and implicit topic/search focus
        should act like one refinement model. Do not make those views depend on
        legacy alert/infrastructure layer toggles.
        """
        if not self._effective_map_observation_focus_enabled():
            return []
        focus_mode = self._effective_map_report_focus_mode()
        if focus_mode not in {"hf_reports", "local_reports", "all_reports", "mesh_nodes"}:
            return []

        def load_rows() -> List[Dict[str, object]]:
            rows = self._load_observation_operational_reports(
                layer_name="report_focus",
                max_age_sec=max_age_sec,
            )
            rows.extend(
                self._load_message_metadata_operational_reports(
                    layer_name="report_focus",
                    max_age_sec=max_age_sec,
                )
            )
            return rows

        return self._build_spotter_operational_events(
            station_lookup,
            layer_name="report_focus",
            display_label="Traffic Reports",
            reports_loader=load_rows,
            max_age_sec=max_age_sec,
        )

    def _load_sitrep_state_rollup(self, report_group: str = "") -> List[Dict[str, object]]:
        report_group_key = str(report_group or "").strip().upper() or "__ALL__"
        cache_key = ("sitrep_state_rollup", report_group_key)
        cached = self._query_cache_get(cache_key)
        if isinstance(cached, list):
            return [dict(row) for row in cached if isinstance(row, dict)]
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return []
        if not db_path.exists():
            return []
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            cols = {
                str(row[1] or "").strip()
                for row in conn.execute("PRAGMA table_info(sitrep_state_rollup)").fetchall()
                if len(row) > 1 and str(row[1] or "").strip()
            }
            js8_expr = "js8_count" if "js8_count" in cols else "0 AS js8_count"
            internet_expr = "internet_count" if "internet_count" in cols else "0 AS internet_count"
            mixed_expr = "mixed_transport_count" if "mixed_transport_count" in cols else "0 AS mixed_transport_count"
            latest_expr = "latest_event_ts" if "latest_event_ts" in cols else "0 AS latest_event_ts"
            cur.execute(
                f"""
                SELECT state_code, callsign_count, red_count, yellow_count, green_count, unknown_count,
                       {js8_expr}, {internet_expr}, {mixed_expr}, {latest_expr}
                FROM sitrep_state_rollup
                WHERE report_group=?
                ORDER BY callsign_count DESC, latest_event_ts DESC, state_code
                """,
                (report_group_key,),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log.debug("StationsMap: failed to load sitrep_state_rollup for %s: %s", report_group_key, e)
            return []
        out = [
            {
                "state_code": str(row[0] or "").strip().upper(),
                "callsign_count": int(row[1] or 0),
                "red_count": int(row[2] or 0),
                "yellow_count": int(row[3] or 0),
                "green_count": int(row[4] or 0),
                "unknown_count": int(row[5] or 0),
                "js8_count": int(row[6] or 0),
                "internet_count": int(row[7] or 0),
                "mixed_transport_count": int(row[8] or 0),
                "latest_event_ts": float(row[9] or 0.0),
            }
            for row in rows
            if str(row[0] or "").strip()
        ]
        self._query_cache_set(cache_key, list(out))
        return out

    def _load_recent_calls(self, max_age_sec: Optional[int], band_filter=None) -> Set[str]:
        if not max_age_sec or max_age_sec <= 0:
            return set()
        band_sig = ""
        try:
            band_sig = json.dumps(band_filter or {"type": "all"}, sort_keys=True, default=str)
        except Exception:
            band_sig = str(band_filter or {"type": "all"})
        cache_key = ("recent_calls", int(max_age_sec), band_sig)
        cached = self._query_cache_get(cache_key, ttl_sec=15.0)
        if isinstance(cached, (set, list, tuple)):
            return {str(c) for c in cached}
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception as e:
            log.error("StationsMap: failed to resolve DB path for recent calls: %s", e)
            return set()
        if not db_path.exists():
            return set()
        ts_cut = time.time() - max_age_sec
        calls: Set[str] = set()
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT ts, origin, destination, band, freq_hz FROM js8_links WHERE ts >= ?",
                (ts_cut,),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT callsign, last_seen_ts, last_band, last_freq_hz FROM varac_callsign_stats WHERE last_seen_ts >= ?",
                (ts_cut,),
            )
            varac_rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log.error("StationsMap: failed to load recent js8_links: %s", e)
            return set()

        bf = band_filter or {"type": "all"}
        for ts, o, d, band, freq_hz in rows:
            band_val = (band or "").upper()
            if bf.get("type") == "band":
                if band_val != str(bf.get("value")).upper():
                    continue
            elif bf.get("type") == "freq":
                target_f = bf.get("value")
                try:
                    freq_mhz = float(freq_hz) / 1_000_000.0 if freq_hz is not None else None
                except Exception:
                    freq_mhz = None
                if freq_mhz is None or target_f is None or abs(freq_mhz - target_f) > 0.001:
                    continue
            if o:
                calls.add((o or "").strip().upper())
            if d:
                calls.add((d or "").strip().upper())
        for cs, last_seen_ts, last_band, last_freq_hz in varac_rows:
            band_val = (last_band or "").upper()
            if bf.get("type") == "band":
                if band_val != str(bf.get("value")).upper():
                    continue
            elif bf.get("type") == "freq":
                target_f = bf.get("value")
                try:
                    freq_mhz = float(last_freq_hz) / 1_000_000.0 if last_freq_hz is not None else None
                except Exception:
                    freq_mhz = None
                if freq_mhz is None or target_f is None or abs(freq_mhz - target_f) > 0.001:
                    continue
            if cs:
                calls.add((cs or "").strip().upper())
        self._query_cache_set(cache_key, set(calls))
        return calls

    def _load_recent_calls_by_band(self, max_age_sec: Optional[int]) -> Dict[str, Set[str]]:
        out: Dict[str, Set[str]] = {band: set() for band in PROP_BANDS}
        if not max_age_sec or max_age_sec <= 0:
            return out
        cache_key = ("recent_calls_by_band", int(max_age_sec))
        cached = self._query_cache_get(cache_key, ttl_sec=15.0)
        if isinstance(cached, dict):
            normalized: Dict[str, Set[str]] = {band: set() for band in PROP_BANDS}
            for band, calls in cached.items():
                band_key = str(band or "").strip().upper()
                if band_key not in normalized:
                    continue
                if isinstance(calls, (set, list, tuple)):
                    normalized[band_key] = {str(c).strip().upper() for c in calls if str(c).strip()}
            return normalized
        try:
            from freqinout.core.config_paths import get_config_dir

            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception as e:
            log.error("StationsMap: failed to resolve DB path for recent calls by band: %s", e)
            return out
        if not db_path.exists():
            return out
        ts_cut = time.time() - max_age_sec
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT origin, destination, band FROM js8_links WHERE ts >= ?",
                (ts_cut,),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT callsign, last_band FROM varac_callsign_stats WHERE last_seen_ts >= ?",
                (ts_cut,),
            )
            varac_rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log.error("StationsMap: failed to load recent calls by band: %s", e)
            return out
        for o, d, band in rows:
            band_key = (band or "").strip().upper()
            if band_key not in out:
                continue
            if o:
                out[band_key].add((o or "").strip().upper())
            if d:
                out[band_key].add((d or "").strip().upper())
        for cs, last_band in varac_rows:
            band_key = (last_band or "").strip().upper()
            if band_key not in out:
                continue
            if cs:
                out[band_key].add((cs or "").strip().upper())
        self._query_cache_set(cache_key, {k: set(v) for k, v in out.items()})
        return out

    def _is_usa_canada(self, lat: float, lon: float) -> bool:
        return 7.0 <= lat <= 83.0 and -172.0 <= lon <= -50.0

    def _links_active(self) -> bool:
        if not bool(getattr(self, "show_link_paths", False)):
            return False
        combo_mode, _ = self._current_link_selection()
        return bool(combo_mode and combo_mode.lower() != "off")

    def _current_link_selection(self) -> tuple[str, str]:
        if not bool(getattr(self, "show_link_paths", False)):
            return "off", ""
        mode = str(getattr(self, "link_mode", "") or "").strip().lower()
        value = str(getattr(self, "link_value", "") or "").strip().upper()
        if mode == "relay_target" and not value:
            value = str(getattr(self, "relay_target", "") or "").strip().upper()
        if mode and mode != "off":
            return mode, value
        return "off", ""

    def _map_link_status_text(
        self,
        *,
        links_active: bool,
        show_link_paths: bool,
        loaded_link_count: int,
        display_link_count: int,
        link_selection: object = None,
        all_time_link_count: int = 0,
        recency_seconds: Optional[int] = None,
    ) -> str:
        if not show_link_paths:
            return "Path layer hidden."
        if not links_active:
            return "Path scope is Off."
        if display_link_count > 0:
            limited_total = int(getattr(self, "_map_link_display_limited_count", 0) or 0)
            if limited_total and limited_total > display_link_count:
                return (
                    f"Showing strongest {display_link_count} of {limited_total} directional network path link(s) "
                    "in the selected time window. Narrow by Paths To, group, band, or age for full detail."
                )
            if int(recency_seconds or 0) > 0:
                return f"{display_link_count} directional path link(s) shown in the selected time window."
            return f"{display_link_count} directional path link(s) shown."

        mode = ""
        value = ""
        if isinstance(link_selection, dict):
            mode = str(link_selection.get("mode") or "").strip().lower()
            value = str(link_selection.get("value") or "").strip().upper()
        elif isinstance(link_selection, (list, tuple)) and len(link_selection) >= 2:
            mode = str(link_selection[0] or "").strip().lower()
            value = str(link_selection[1] or "").strip().upper()
        if loaded_link_count > 0:
            return "Path links are loaded but filtered out by the current view."
        if int(recency_seconds or 0) > 0 and int(all_time_link_count or 0) > 0:
            return (
                f"No path links in the selected time window; "
                f"{int(all_time_link_count or 0)} older path link(s) match with Since: Any."
            )
        source_rows = int(getattr(self, "_map_last_link_source_rows", 0) or 0)
        missing_positions = int(getattr(self, "_map_last_link_missing_position_rows", 0) or 0)
        if source_rows > 0 and missing_positions >= source_rows:
            return f"{source_rows} path record(s) found; station locations are needed to draw them."
        if source_rows > 0:
            return f"{source_rows} path record(s) found but none match the current view."
        if mode == "my_station":
            return "No path links found for my station and current filters."
        if mode == "station" and value:
            return f"No path links found for {value} and current filters."
        if mode == "relay_target" and value:
            return f"No direct or shared path found from my station to {value} in the selected time window."
        if mode == "group" and value:
            return f"No path links found for group {value}."
        if mode == "region" and value:
            return f"No path links found for region {value}."
        if mode == "all":
            return "No path links found for current filters."
        return "No path links found."

    @staticmethod
    def _path_endpoint_markers(markers, links, my_call, statuses):
        """Keep every drawn path anchored to its station identities, without I/O."""
        by_call = {str(marker.get("callsign") or "").strip().upper(): dict(marker) for marker in markers}
        endpoints = set()
        for link in links:
            for call_key, lat_key, lon_key in (("origin", "lat1", "lon1"), ("destination", "lat2", "lon2")):
                call = str(link.get(call_key) or "").strip().upper()
                if not call:
                    continue
                try:
                    lat, lon = float(link[lat_key]), float(link[lon_key])
                except (KeyError, TypeError, ValueError):
                    continue
                if not math.isfinite(lat) or not math.isfinite(lon):
                    continue
                endpoints.add(call)
                marker = by_call.setdefault(call, {"callsign": call, "label": call, "title": call})
                marker.update(lat=lat, lon=lon, is_own_station=call == my_call)
                status = statuses.get(call, {})
                key = str(status.get("status_key") or marker.get("spotter_status_key") or "unknown")
                marker["spotter_status_key"] = key
                marker["spotter_status_label"] = status.get("status_label") or StationsMapTab._sitrep_status_label(key)
                for target, source in (("spotter_status_source", "status_source"),
                                       ("spotter_status_age", "status_age"),
                                       ("spotter_status_source_detail", "status_source_detail")):
                    if source in status:
                        marker[target] = status[source]
                if call == my_call:
                    marker["tooltip"] = "My station · " + call
        return sorted(by_call.values(), key=lambda marker: (
            not bool(marker.get("is_own_station")), marker.get("callsign") not in endpoints
        ))

    def _display_links_for_mode(self, links: List[Dict], sitrep_mode: bool) -> List[Dict]:
        self._map_link_display_limited_count = 0
        self._map_link_loaded_count = len(links or [])
        if sitrep_mode:
            return []
        display = [dict(link) for link in (links or []) if isinstance(link, dict)]
        current_link_selection = getattr(self, "_current_link_selection", None)
        mode, _value = current_link_selection() if callable(current_link_selection) else ("all", "")
        if str(mode or "").strip().lower() != "all":
            return display
        limit = int(MAP_NETWORK_PATH_DISPLAY_LIMIT)
        if limit <= 0 or len(display) <= limit:
            return display
        self._map_link_display_limited_count = len(display)

        def sort_key(link: Mapping[str, object]) -> tuple[float, float]:
            try:
                snr_score = float(link.get("snr"))
            except Exception:
                snr_score = -999.0
            try:
                ts_score = float(link.get("ts") or link.get("latest_ts") or 0.0)
            except Exception:
                ts_score = 0.0
            return (snr_score, ts_score)

        return sorted(display, key=sort_key, reverse=True)[:limit]

    def _load_prop_target_operator_callsigns(self) -> list[str]:
        out: list[str] = []
        try:
            for cs in sorted(self.operator_index.keys()):
                call = (cs or "").strip().upper()
                if call:
                    out.append(call)
        except Exception:
            out = []
        if out:
            return out
        # Roster options are published by the map worker; display never falls back to SQL.
        return out

    def _prop_target_options(self, target_type: str) -> list[str]:
        target_type = (target_type or "REGION").strip().upper()
        if target_type == "STATE":
            return [s for s in LOWER48_STATES if s in STATE_CENTERS]
        if target_type == "OPERATOR":
            return self._load_prop_target_operator_callsigns()
        return ["ALL"] + sorted(FEMA_REGIONS.keys())

    def _set_prop_target_value_options(self, target_type: str, selected_value: str) -> None:
        if self.prop_target_value_combo is None:
            return
        target_type = (target_type or "REGION").strip().upper()
        selected_value = (selected_value or "").strip().upper()
        if target_type == "REGION" and selected_value == "NATIONAL":
            selected_value = "ALL"
        options = self._prop_target_options(target_type)
        self.prop_target_value_combo.blockSignals(True)
        self.prop_target_value_combo.clear()
        for value in options:
            self.prop_target_value_combo.addItem(value)
        if selected_value:
            idx = self.prop_target_value_combo.findText(selected_value, Qt.MatchFixedString)
            if idx >= 0:
                self.prop_target_value_combo.setCurrentIndex(idx)
            else:
                self.prop_target_value_combo.setEditText(selected_value)
        elif self.prop_target_value_combo.count() > 0:
            self.prop_target_value_combo.setCurrentIndex(0)
        else:
            self.prop_target_value_combo.setEditText("")
        self.prop_target_value_combo.setEditable(target_type == "OPERATOR")
        self.prop_target_value_combo.blockSignals(False)

    def _refresh_prop_target_controls(self, *, cached_only: bool = True) -> None:
        if not self.settings or self.prop_target_type_combo is None or self.prop_target_value_combo is None:
            return
        self._prop_target_syncing = True
        try:
            if not cached_only and hasattr(self.settings, "reload"):
                self.settings.reload()
            target_type = (self.settings.get("prop_target_type", "REGION") or "REGION").strip().upper()
            if target_type not in {"REGION", "STATE", "OPERATOR"}:
                target_type = "REGION"
            target_value = (self.settings.get("prop_target_value", "") or "").strip().upper()
            idx = self.prop_target_type_combo.findData(target_type)
            if idx < 0:
                idx = 0
            self.prop_target_type_combo.blockSignals(True)
            self.prop_target_type_combo.setCurrentIndex(idx)
            self.prop_target_type_combo.blockSignals(False)
            self._set_prop_target_value_options(target_type, target_value)
            current_value = (self.prop_target_value_combo.currentText() or "").strip().upper()
            existing_type = (self.settings.get("prop_target_type", "") or "").strip().upper()
            existing_value = (self.settings.get("prop_target_value", "") or "").strip().upper()
            if not cached_only and (existing_type != target_type or existing_value != current_value):
                self.settings.set_many(
                    {
                        "prop_target_type": target_type,
                        "prop_target_value": current_value,
                    }
                )
        except Exception as e:
            log.debug("StationsMap: failed to refresh propagation target controls: %s", e)
        finally:
            self._prop_target_syncing = False

    def attach_prop_controls(
        self,
        overlay_chk: QCheckBox | None,
        badge: QLabel | None,
        mode_combo: QComboBox | None = None,
        window_combo: QComboBox | None = None,
    ) -> None:
        # Backward compatibility path for legacy external controls.
        if overlay_chk is not None:
            self.prop_overlay_chk = overlay_chk
        if badge is not None:
            self.prop_badge = badge
        if mode_combo is not None:
            self.prop_mode_combo = mode_combo
        if window_combo is not None:
            self.prop_window_combo = window_combo
        # Sync checkbox state from settings
        if self.prop_overlay_chk is not None:
            self.prop_overlay_chk.blockSignals(True)
            self.prop_overlay_chk.setChecked(bool(self._bool_setting("map_prop_overlay", False)))
            self.prop_overlay_chk.blockSignals(False)
            self.prop_overlay_enabled = self.prop_overlay_chk.isChecked()
        try:
            self.prop_adaptive_enabled = self._bool_setting("map_prop_adaptive", True)
        except Exception:
            pass
        if self.prop_mode_combo is not None:
            try:
                mode = (self.settings.get("map_prop_mode", "blended") or "blended").strip().lower()
            except Exception:
                mode = "blended"
            if mode == "adaptive":
                mode = "actual"
            if mode not in ("model", "actual", "blended"):
                mode = "blended"
            self.prop_mode = mode
            idx = self.prop_mode_combo.findData(mode)
            if idx >= 0:
                self.prop_mode_combo.blockSignals(True)
                self.prop_mode_combo.setCurrentIndex(idx)
                self.prop_mode_combo.blockSignals(False)
        if self.prop_window_combo is not None:
            try:
                hours = int(self.settings.get("map_prop_window_hours", 6) or 6)
            except Exception:
                hours = 6
            self.prop_window_hours = hours
            idx = self.prop_window_combo.findData(hours)
            if idx >= 0:
                self.prop_window_combo.blockSignals(True)
                self.prop_window_combo.setCurrentIndex(idx)
                self.prop_window_combo.blockSignals(False)
        # Update badge immediately with current target.
        target_ctx = self._prop_target_context()
        target_label = str(target_ctx.get("label") or "National")
        if self.prop_overlay_enabled:
            region_scores = self._compute_region_scores("")
            state_scores = self._compute_state_scores()
            best_band, best_score = self._best_band_for_target(target_ctx, region_scores, state_scores)
            self._update_prop_badge(target_label, best_band, best_score)
        else:
            self._update_prop_badge(target_label, "", 0.0)

    def _effective_prop_mode(self) -> str:
        mode = (self.prop_mode or "blended").strip().lower()
        if mode == "adaptive":
            mode = "actual"
        if mode not in ("model", "actual", "blended"):
            mode = "blended"
        return mode

    def _prop_window_seconds(self) -> int:
        try:
            hours = int(self.prop_window_hours or 6)
        except Exception:
            hours = 6
        hours = max(1, min(hours, 168))
        return hours * 3600

    # ------------- Propagation overlay ------------- #
    def _load_prop_profiles(self) -> Dict[str, Dict]:
        return self._prop_service.load_profiles()

    def _load_prop_db_cache(self) -> None:
        self._prop_service.load_climatology_cache()

    def _lookup_db_score(self, band: str, lat: float, lon: float, month: int) -> Optional[float]:
        return self._prop_service.lookup_db_score(band, lat, lon, month)

    def _get_user_latlon(self) -> Optional[tuple[float, float]]:
        if not self.settings:
            return None
        grid = (self.settings.get("operator_grid6", "") or self.settings.get("operator_grid", "") or "").strip().upper()
        if grid:
            ll = maidenhead_to_latlon(grid)
            if ll:
                return ll
        return None

    def _get_origin_grid6(self) -> str:
        if not self.settings:
            return ""
        grid = (self.settings.get("operator_grid6", "") or self.settings.get("operator_grid", "") or "").strip().upper()
        return grid[:6] if len(grid) >= 4 else ""

    def _blend_settings_snapshot(self) -> Dict[str, object]:
        if not self.settings:
            return {}
        return {
            "prop_blend_enabled": self.settings.get("prop_blend_enabled", 1),
            "prop_empirical_alpha": self.settings.get("prop_empirical_alpha", 2.0),
            "prop_empirical_beta": self.settings.get("prop_empirical_beta", 3.0),
            "prop_decay_half_life_days": self.settings.get("prop_decay_half_life_days", 75),
            "prop_blend_gate_attempt_min": self.settings.get("prop_blend_gate_attempt_min", 8.0),
            "prop_blend_gate_unique_days_min": self.settings.get("prop_blend_gate_unique_days_min", 3),
            "prop_blend_max_weight": self.settings.get("prop_blend_max_weight", 0.85),
            "prop_blend_recent_window_days": self.settings.get("prop_blend_recent_window_days", 30),
            "prop_blend_history_cap_days": self.settings.get("prop_blend_history_cap_days", 365),
        }

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        return PropagationService.haversine_km(lat1, lon1, lat2, lon2)

    def _band_score(self, band: str, distance_km: float, hour_utc: int) -> float:
        return self._prop_service.band_score(band, distance_km, hour_utc)

    def _local_hour_from_lon(self, utc_dt: datetime.datetime, lon: float) -> int:
        return self._prop_service.local_hour_from_lon(utc_dt, lon)

    def _path_band_weight(self, band: str, distance_km: float, hour_local: int) -> float:
        return self._prop_service.path_band_weight(band, distance_km, hour_local)

    def _modeled_band_score(
        self,
        band: str,
        dest_lat: float,
        dest_lon: float,
        now_utc: datetime.datetime,
        distance_km: float,
    ) -> float:
        user_ll = self._get_user_latlon()
        if not user_ll:
            return 0.0
        return self._prop_service.modeled_band_score(
            band=band,
            user_ll=user_ll,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            now_utc=now_utc,
            distance_km=distance_km,
        )

    def _band_score_db(self, band: str, lat: float, lon: float, month: int) -> Optional[float]:
        return self._prop_service.band_score_db(band, lat, lon, month)

    def _diurnal_weight(self, band: str, hour_local: int) -> float:
        return self._prop_service.diurnal_weight(band, hour_local)

    def _stations_for_region(self, region_id: str) -> List[StationPoint]:
        region_id = (region_id or "").strip().upper()
        if not region_id:
            return []
        out = []
        for pt in self.stations:
            region = self.operator_index.get(pt.callsign, {}).get("region")
            if region == region_id:
                out.append(pt)
        return out

    def _stations_for_state(self, state_abbr: str) -> List[StationPoint]:
        state_abbr = (state_abbr or "").strip().upper()
        if not state_abbr:
            return []
        out = []
        for pt in self.stations:
            state = self.operator_index.get(pt.callsign, {}).get("state")
            if state == state_abbr:
                out.append(pt)
        return out

    def _region_centroid(self, region_id: str) -> Optional[tuple[float, float]]:
        region_id = (region_id or "").strip().upper()
        if not region_id:
            return None
        states = FEMA_REGIONS.get(region_id, [])
        if not states:
            return None
        lat_sum = 0.0
        lon_sum = 0.0
        count = 0
        for st in states:
            center = STATE_CENTERS.get(st)
            if center:
                lat_sum += center[0]
                lon_sum += center[1]
                count += 1
        if count == 0:
            return None
        return (lat_sum / count, lon_sum / count)

    def _state_centroid(self, state_abbr: str) -> Optional[tuple[float, float]]:
        state_abbr = (state_abbr or "").strip().upper()
        if not state_abbr:
            return None
        return STATE_CENTERS.get(state_abbr)

    def _normalize_state_abbr(self, value: str) -> str:
        state = (value or "").strip().upper()
        if not state:
            return ""
        if len(state) <= 2:
            return state
        if state in US_STATE_ABBR_FROM_NAME:
            return US_STATE_ABBR_FROM_NAME[state]
        if state in CANADA_PROV_ABBR_FROM_NAME:
            return CANADA_PROV_ABBR_FROM_NAME[state]
        return ""

    def _points_for_region_lower48(self, region_id: str) -> List[tuple[float, float]]:
        region_id = (region_id or "").strip().upper()
        if not region_id:
            return []
        states = FEMA_REGIONS.get(region_id, [])
        return [STATE_CENTERS[s] for s in states if s in LOWER48_STATES and s in STATE_CENTERS]

    def _operator_target_point(self, callsign: str) -> tuple[Optional[tuple[float, float]], str]:
        callsign = (callsign or "").strip().upper()
        if not callsign:
            return None, ""
        for row in self.operator_rows:
            cs = (row.get("callsign") or "").strip().upper()
            if cs != callsign:
                continue
            grid = (row.get("grid") or "").strip().upper()
            state = self._normalize_state_abbr(row.get("state") or "")
            ll = maidenhead_to_latlon(grid) if grid else None
            if ll:
                return ll, state
            if state and state in STATE_CENTERS and state in LOWER48_STATES:
                return STATE_CENTERS[state], state
            return None, state
        return None, ""

    def _prop_target_context(self) -> Dict[str, object]:
        target_type = "REGION"
        target_value = ""
        if self.settings:
            try:
                target_type = (self.settings.get("prop_target_type", "REGION") or "REGION").strip().upper()
            except Exception:
                target_type = "REGION"
            try:
                target_value = (self.settings.get("prop_target_value", "") or "").strip().upper()
            except Exception:
                target_value = ""
        if target_type not in {"REGION", "STATE", "OPERATOR"}:
            target_type = "REGION"
        if target_type == "STATE":
            target_value = self._normalize_state_abbr(target_value)
        context: Dict[str, object] = {
            "type": target_type,
            "value": target_value,
            "label": "National",
            "region_id": "",
            "state_abbr": "",
            "point": None,
        }

        # Default/fallback target: operator state -> FEMA region.
        operator_state = ""
        if self.settings:
            try:
                operator_state = self._normalize_state_abbr(self.settings.get("operator_state", "") or "")
            except Exception:
                operator_state = ""
        fallback_region = STATE_TO_FEMA_REGION.get(operator_state, "")

        if target_type == "REGION":
            if target_value in {"ALL", "NATIONAL"}:
                context["label"] = "National"
                context["value"] = "ALL"
                return context
            region_id = target_value if target_value in FEMA_REGIONS else fallback_region
            if region_id:
                context["label"] = f"Region {region_id}"
                context["region_id"] = region_id
            return context

        if target_type == "STATE":
            state_abbr = target_value if target_value in STATE_CENTERS and target_value in LOWER48_STATES else ""
            if state_abbr:
                context["label"] = state_abbr
                context["state_abbr"] = state_abbr
                context["region_id"] = STATE_TO_FEMA_REGION.get(state_abbr, "")
            elif fallback_region:
                context["label"] = f"Region {fallback_region}"
                context["region_id"] = fallback_region
            return context

        callsign = target_value
        if callsign:
            context["label"] = callsign
            ll, state_abbr = self._operator_target_point(callsign)
            if ll:
                context["point"] = ll
            if state_abbr:
                context["state_abbr"] = state_abbr
                context["region_id"] = STATE_TO_FEMA_REGION.get(state_abbr, "")
            return context
        if fallback_region:
            context["label"] = f"Region {fallback_region}"
            context["region_id"] = fallback_region
        return context

    def _best_band_for_state(self, state_scores: Dict[str, Dict], state_abbr: str) -> tuple[str, float]:
        state_abbr = (state_abbr or "").strip().upper()
        bands = state_scores.get(state_abbr, {}).get("bands", {})
        if not bands:
            return ("", 0.0)
        best_band = max(bands.items(), key=lambda kv: kv[1])
        return best_band[0], float(best_band[1])

    def _best_band_overall_states(self, state_scores: Dict[str, Dict], states: Optional[List[str]] = None) -> tuple[str, float]:
        if not state_scores:
            return ("", 0.0)
        selected_states = states if states else list(state_scores.keys())
        totals: Dict[str, List[float]] = {b: [] for b in PROP_BANDS}
        for state in selected_states:
            entry = state_scores.get(state, {})
            for band, score in (entry.get("bands") or {}).items():
                if band in totals:
                    totals[band].append(float(score))
        best_band = ""
        best_score = 0.0
        for band, vals in totals.items():
            if not vals:
                continue
            avg = sum(vals) / len(vals)
            if avg > best_score:
                best_score = avg
                best_band = band
        return best_band, best_score

    def _best_band_for_target(
        self,
        target_ctx: Dict[str, object],
        region_scores: Dict[str, Dict],
        state_scores: Dict[str, Dict],
    ) -> tuple[str, float]:
        mode = self._effective_prop_mode()
        user_ll = self._get_user_latlon()
        origin_grid6 = self._get_origin_grid6()
        blend_settings = self._blend_settings_snapshot() if mode == "blended" else None
        target_type = str(target_ctx.get("type") or "REGION").upper()
        target_value = str(target_ctx.get("value") or "").upper()
        region_id = str(target_ctx.get("region_id") or "").upper()
        state_abbr = str(target_ctx.get("state_abbr") or "").upper()
        point = target_ctx.get("point")

        # Modeled/blended parity path: use the same point-set model used by ControlFreq.
        if mode in {"model", "blended"} and user_ll:
            points: List[tuple[float, float]] = []
            target_id = ""
            if target_type == "REGION" and region_id:
                points = self._points_for_region_lower48(region_id)
                target_id = region_id
            elif target_type == "STATE" and state_abbr in STATE_CENTERS and state_abbr in LOWER48_STATES:
                points = [STATE_CENTERS[state_abbr]]
                target_id = state_abbr
            elif target_type == "OPERATOR" and isinstance(point, tuple):
                points = [point]
                target_id = target_value
            if not points:
                points = [STATE_CENTERS[s] for s in LOWER48_STATES if s in STATE_CENTERS]
                target_id = "NATIONAL"
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            top = self._prop_service.top_bands_modeled(
                bands=PROP_BANDS,
                mid_utc=now_utc,
                user_ll=user_ll,
                points=points,
                origin_grid6=origin_grid6,
                target_type=target_type,
                target_id=target_id,
                blend_settings=blend_settings,
                limit=1,
            )
            if top:
                return top[0][0], float(top[0][1])

        if target_type == "REGION" and region_id:
            return self._best_band_for_region(region_scores, region_id)
        if target_type == "STATE" and state_abbr:
            return self._best_band_for_state(state_scores, state_abbr)
        if target_type == "OPERATOR":
            if state_abbr:
                return self._best_band_for_state(state_scores, state_abbr)
            if region_id:
                return self._best_band_for_region(region_scores, region_id)
        return self._best_band_overall_states(state_scores, states=LOWER48_STATES)

    def _adaptive_adjustment(self, band: str, region_id: str) -> float:
        if not self.prop_adaptive_enabled:
            return 0.0
        max_age = self.recency_seconds or 24 * 60 * 60
        recent = self._load_recent_calls_by_band(max_age).get((band or "").strip().upper(), set())
        if not recent:
            return 0.0
        stations = self._stations_for_region(region_id)
        if not stations:
            return 0.0
        hit = sum(1 for s in stations if s.callsign.upper() in recent)
        ratio = hit / max(1, len(stations))
        adj = (ratio - 0.5) * 20.0
        return max(-12.0, min(12.0, adj))

    def _adaptive_adjustment_state(self, band: str, state_abbr: str) -> float:
        if not self.prop_adaptive_enabled:
            return 0.0
        max_age = self.recency_seconds or 24 * 60 * 60
        recent = self._load_recent_calls_by_band(max_age).get((band or "").strip().upper(), set())
        if not recent:
            return 0.0
        stations = self._stations_for_state(state_abbr)
        if not stations:
            return 0.0
        hit = sum(1 for s in stations if s.callsign.upper() in recent)
        ratio = hit / max(1, len(stations))
        adj = (ratio - 0.5) * 20.0
        return max(-12.0, min(12.0, adj))

    def _presence_band_weights(self) -> Dict[str, Dict]:
        now_ts = time.time()
        if self._presence_weights_cache and (now_ts - self._presence_weights_ts) < 10:
            return self._presence_weights_cache

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        schedule_presence = self._load_peer_schedule_presence(now_utc)
        observed_by_band = self._load_observed_band_presence(self._prop_window_seconds())

        sched_region: Dict[str, Dict[str, Set[str]]] = {}
        sched_state: Dict[str, Dict[str, Set[str]]] = {}
        obs_region: Dict[str, Dict[str, Set[str]]] = {}
        obs_state: Dict[str, Dict[str, Set[str]]] = {}

        for entry in schedule_presence:
            cs = (entry.get("callsign") or "").strip().upper()
            band = (entry.get("band") or "").strip().upper()
            if not cs or not band:
                continue
            meta = self.operator_index.get(cs, {})
            region = meta.get("region") or ""
            state = meta.get("state") or ""
            if region:
                sched_region.setdefault(region, {}).setdefault(band, set()).add(cs)
            if state:
                sched_state.setdefault(state, {}).setdefault(band, set()).add(cs)

        for band, calls in observed_by_band.items():
            for cs in calls:
                meta = self.operator_index.get(cs, {})
                region = meta.get("region") or ""
                state = meta.get("state") or ""
                if region:
                    obs_region.setdefault(region, {}).setdefault(band, set()).add(cs)
                if state:
                    obs_state.setdefault(state, {}).setdefault(band, set()).add(cs)

        def _weights(
            obs_map: Dict[str, Dict[str, Set[str]]],
            sched_map: Dict[str, Dict[str, Set[str]]],
        ) -> tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]], Set[str]]:
            out: Dict[str, Dict[str, float]] = {}
            ratio_out: Dict[str, Dict[str, float]] = {}
            has_data: Set[str] = set()
            keys = set(obs_map.keys()) | set(sched_map.keys())
            for key in keys:
                max_weighted = 0
                band_weighted: Dict[str, int] = {}
                for band in PROP_BANDS:
                    obs_count = len(obs_map.get(key, {}).get(band, set()))
                    sched_count = len(sched_map.get(key, {}).get(band, set()))
                    weighted = obs_count * 2 + sched_count
                    band_weighted[band] = weighted
                    if weighted > max_weighted:
                        max_weighted = weighted
                if max_weighted > 0:
                    has_data.add(key)
                if max_weighted <= 0:
                    out[key] = {band: 1.0 for band in PROP_BANDS}
                    ratio_out[key] = {band: 0.0 for band in PROP_BANDS}
                else:
                    ratio_out[key] = {
                        band: (band_weighted[band] / max_weighted) if max_weighted else 0.0
                        for band in PROP_BANDS
                    }
                    out[key] = {
                        band: max(0.6, min(1.4, 0.7 + 0.6 * ratio_out[key][band]))
                        for band in PROP_BANDS
                    }
            return out, ratio_out, has_data

        region_weights, region_ratio, region_has = _weights(obs_region, sched_region)
        state_weights, state_ratio, state_has = _weights(obs_state, sched_state)
        weights = {
            "region": region_weights,
            "state": state_weights,
            "region_ratio": region_ratio,
            "state_ratio": state_ratio,
            "region_has_data": region_has,
            "state_has_data": state_has,
        }
        self._presence_weights_cache = weights
        self._presence_weights_ts = now_ts
        return weights

    def _load_observed_band_presence(self, max_age_sec: int) -> Dict[str, Set[str]]:
        observed: Dict[str, Set[str]] = {band: set() for band in PROP_BANDS}
        recent_by_band = self._load_recent_calls_by_band(max_age_sec)
        for band in PROP_BANDS:
            calls = recent_by_band.get(band, set())
            if calls:
                observed[band] = {c.strip().upper() for c in calls if c}
        return observed

    def _load_peer_schedule_presence(self, now_utc: datetime.datetime) -> List[Dict]:
        rows: List[Dict] = []
        try:
            db_path = get_config_dir() / "config" / "freqinout_nets.db"
        except Exception:
            return rows
        if not db_path.exists():
            return rows
        try:
            conn = connect_sqlite_readonly(db_path)
            cur = conn.cursor()
            has_effective_view = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name='peer_hf_schedule_effective'"
            ).fetchone()
            if has_effective_view:
                cur.execute(
                    """
                    SELECT owner_callsign, day_utc, start_utc, end_utc, band, mode, frequency
                    FROM peer_hf_schedule_effective
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT owner_callsign, day_utc, start_utc, end_utc, band, mode, frequency
                    FROM peer_hf_schedule
                    """
                )
            raw = cur.fetchall()
            conn.close()
        except Exception:
            return rows

        active_by_callsign: Dict[str, Dict] = {}
        for cs, day_utc, start_utc, end_utc, band, mode, freq in raw:
            callsign = self._normalize_peer_callsign(cs)
            if not callsign:
                continue
            start_min = self._parse_hhmm_minutes(start_utc)
            end_min = self._parse_hhmm_minutes(end_utc)
            if start_min is None or end_min is None:
                continue
            minutes_to_end = self._schedule_minutes_to_end(
                str(day_utc or "ALL"),
                now_utc,
                start_min,
                end_min,
            )
            if minutes_to_end is None:
                continue
            existing = active_by_callsign.get(callsign)
            if existing and minutes_to_end >= existing.get("minutes_to_end", 0):
                continue
            active_by_callsign[callsign] = {
                "callsign": callsign,
                "band": (band or "").strip().upper(),
                "mode": (mode or "").strip().upper(),
                "frequency": str(freq or "").strip(),
                "minutes_to_end": minutes_to_end,
            }
        return list(active_by_callsign.values())

    @staticmethod
    def _normalize_peer_callsign(value: object) -> str:
        raw = str(value or "").strip().upper()
        if not raw:
            return ""
        return re.sub(r"/(P|M|MM|QRP|SOTA|ROVER|[A-Z0-9]{1,4})$", "", raw)

    def _parse_hhmm_minutes(self, value: Optional[str]) -> Optional[int]:
        txt = (value or "").strip()
        if not txt:
            return None
        match = re.match(r"^(\d{1,2}):?(\d{2})$", txt)
        if not match:
            return None
        h = int(match.group(1))
        m = int(match.group(2))
        if h < 0 or h > 23 or m < 0 or m > 59:
            return None
        return h * 60 + m

    def _compute_region_scores(self, region_filter: str = "") -> Dict[str, Dict]:
        region_filter = (region_filter or "").strip().upper()
        regions = [region_filter] if region_filter else sorted(FEMA_REGIONS.keys())
        user_ll = self._get_user_latlon()
        if not user_ll:
            return {}
        mode = self._effective_prop_mode()
        presence_stats = self._presence_band_weights()
        presence_ratio = presence_stats.get("region_ratio", {})
        presence_has_data = presence_stats.get("region_has_data", set())
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        month = now_utc.month
        hour_utc = now_utc.hour
        hour_local = hour_utc
        try:
            if self.settings:
                tz_name = self.settings.get("timezone", "UTC") or "UTC"
                tz = get_timezone(tz_name)
                hour_local = now_utc.astimezone(tz).hour
        except Exception:
            hour_local = hour_utc
        scores: Dict[str, Dict] = {}
        for region_id in regions:
            stations = self._stations_for_region(region_id)
            use_station_weight = len(stations) >= 5
            centroid = self._region_centroid(region_id)
            for band in PROP_BANDS:
                if mode == "actual":
                    if region_id not in presence_has_data:
                        continue
                    ratio = presence_ratio.get(region_id, {}).get(band, 0.0)
                    entry = scores.setdefault(region_id, {"bands": {}})
                    entry["bands"][band] = max(0.0, min(100.0, ratio * 100.0))
                    continue
                base_scores = []
                if use_station_weight and stations:
                    for s in stations:
                        dist = self._haversine_km(user_ll[0], user_ll[1], s.lat, s.lon)
                        base_scores.append(self._modeled_band_score(band, s.lat, s.lon, now_utc, dist))
                elif centroid:
                    dist = self._haversine_km(user_ll[0], user_ll[1], centroid[0], centroid[1])
                    base_scores.append(self._modeled_band_score(band, centroid[0], centroid[1], now_utc, dist))
                base = sum(base_scores) / max(1, len(base_scores))
                adj = 0.0 if mode == "model" else self._adaptive_adjustment(band, region_id)
                total = max(0.0, min(100.0, base + adj))
                entry = scores.setdefault(region_id, {"bands": {}})
                entry["bands"][band] = total
        return scores

    def _compute_state_scores(self) -> Dict[str, Dict]:
        user_ll = self._get_user_latlon()
        if not user_ll:
            return {}
        mode = self._effective_prop_mode()
        presence_stats = self._presence_band_weights()
        presence_weights = presence_stats.get("state", {})
        presence_ratio = presence_stats.get("state_ratio", {})
        presence_has_data = presence_stats.get("state_has_data", set())
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        month = now_utc.month
        hour_utc = now_utc.hour
        hour_local = hour_utc
        try:
            if self.settings:
                tz_name = self.settings.get("timezone", "UTC") or "UTC"
                tz = get_timezone(tz_name)
                hour_local = now_utc.astimezone(tz).hour
        except Exception:
            hour_local = hour_utc
        scores: Dict[str, Dict] = {}
        for state_abbr in USA_STATES:
            stations = self._stations_for_state(state_abbr)
            use_station_weight = len(stations) >= 5
            centroid = self._state_centroid(state_abbr)
            for band in PROP_BANDS:
                if mode == "actual":
                    if state_abbr not in presence_has_data:
                        continue
                    ratio = presence_ratio.get(state_abbr, {}).get(band, 0.0)
                    entry = scores.setdefault(state_abbr, {"bands": {}})
                    entry["bands"][band] = max(0.0, min(100.0, ratio * 100.0))
                    continue
                base_scores = []
                if use_station_weight and stations:
                    for s in stations:
                        dist = self._haversine_km(user_ll[0], user_ll[1], s.lat, s.lon)
                        base_scores.append(self._modeled_band_score(band, s.lat, s.lon, now_utc, dist))
                elif centroid:
                    dist = self._haversine_km(user_ll[0], user_ll[1], centroid[0], centroid[1])
                    base_scores.append(self._modeled_band_score(band, centroid[0], centroid[1], now_utc, dist))
                if not base_scores:
                    continue
                base = sum(base_scores) / max(1, len(base_scores))
                adj = 0.0 if mode == "model" else self._adaptive_adjustment_state(band, state_abbr)
                total = max(0.0, min(100.0, base + adj))
                if mode in ("adaptive", "blended") and state_abbr in presence_has_data:
                    band_weight = presence_weights.get(state_abbr, {}).get(band, 1.0)
                    if mode == "adaptive":
                        band_weight = max(0.4, min(1.8, band_weight * band_weight))
                    total = max(0.0, min(100.0, total * band_weight))
                entry = scores.setdefault(state_abbr, {"bands": {}})
                entry["bands"][band] = total
        return scores

    def _best_band_for_region(self, region_scores: Dict[str, Dict], region_id: str) -> tuple[str, float]:
        region_id = (region_id or "").strip().upper()
        bands = region_scores.get(region_id, {}).get("bands", {})
        if not bands:
            return ("", 0.0)
        best_band = max(bands.items(), key=lambda kv: kv[1])
        return best_band[0], float(best_band[1])

    def _best_band_overall(self, region_scores: Dict[str, Dict]) -> tuple[str, float]:
        if not region_scores:
            return ("", 0.0)
        totals: Dict[str, List[float]] = {b: [] for b in PROP_BANDS}
        for entry in region_scores.values():
            for band, score in (entry.get("bands") or {}).items():
                if band in totals:
                    totals[band].append(float(score))
        best_band = ""
        best_score = 0.0
        for band, scores in totals.items():
            if not scores:
                continue
            avg = sum(scores) / len(scores)
            if avg > best_score:
                best_score = avg
                best_band = band
        return best_band, best_score

    def _score_level(self, score: float) -> str:
        if score >= 70:
            return "high"
        if score >= 45:
            return "med"
        return "low"

    def _resolve_prop_band_colors(self) -> Dict[str, str]:
        theme = self._theme_snapshot()
        is_dark = theme.get("bg") == "#0F1216"
        palette = BAND_COLORS_DARK if is_dark else BAND_COLORS_LIGHT
        colors: Dict[str, str] = {k.upper(): v for k, v in palette.items()}
        try:
            raw = self.settings.get("band_colors", {}) or {}
        except Exception:
            raw = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if not k or not v:
                    continue
                key = str(k).strip().lower()
                band_key = key.upper()
                if band_key.endswith("M"):
                    colors[band_key] = str(v).strip()
                else:
                    colors[f"{band_key}M"] = str(v).strip()
        return {b: colors.get(b, PROP_BAND_COLORS[b]) for b in PROP_BANDS if b in PROP_BAND_COLORS}

    def _freq_to_band(self, freq: Optional[float]) -> str:
        if not freq:
            return ""
        try:
            mhz = float(freq)
        except Exception:
            return ""
        bands = [
            ("80M", 3.5, 4.0),
            ("40M", 7.0, 7.3),
            ("30M", 10.1, 10.15),
            ("20M", 14.0, 14.35),
            ("15M", 21.0, 21.45),
            ("10M", 28.0, 29.7),
        ]
        for name, lo, hi in bands:
            if lo <= mhz <= hi:
                return name
        return ""

    def _update_prop_badge(
        self,
        target_label: str,
        best_band: str,
        best_score: float,
        theme: Optional[Dict[str, str]] = None,
    ) -> None:
        if self.prop_badge is None:
            return
        if theme is None:
            theme = self._theme_snapshot()
        self._last_prop_badge_values = (target_label, best_band, best_score)
        scheduled = self._freq_to_band(current_scheduler_freq(self._application_window()))
        level = self._score_level(best_score)
        display_label = (target_label or "National").strip()
        if not best_band:
            self.prop_badge.setText("Best Band: --")
            self.prop_badge.setStyleSheet(f"font-weight: bold; color: {theme.get('text_muted', '#666666')};")
            return
        text_lines = [
            f"Target: {display_label}",
            f"Best Now: {best_band} ({level.upper()})",
        ]
        if scheduled and scheduled != best_band:
            text_lines.append(f"Schedule: {scheduled}")
        text = "<br/>".join(text_lines)
        if scheduled and scheduled != best_band:
            self.prop_badge.setStyleSheet(f"font-weight: bold; color: {theme.get('warning', '#FB8C00')};")
        else:
            self.prop_badge.setStyleSheet(
                f"font-weight: bold; color: {theme.get('info', theme.get('accent', '#1E88E5'))};"
            )
        self.prop_badge.setText(text)

    def _ensure_native_map_renderer(self) -> NativeMapRenderer | None:
        """Return the singleton native renderer in its final viewport parent."""
        if getattr(self, "_is_shutting_down", False):
            return None
        existing = getattr(self, "_native_map_renderer", None)
        if existing is not None:
            return existing
        parent = getattr(self, "_map_stack", None) or getattr(self, "_map_canvas_splitter", None) or self
        try:
            renderer = NativeMapRenderer(parent)
            renderer.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
            renderer.setMinimumSize(0, 0)
            ready_signal = getattr(renderer, "ready", None)
            if ready_signal is not None:
                ready_signal.connect(self._on_native_map_ready)
            unavailable_signal = getattr(renderer, "unavailable", None)
            if unavailable_signal is not None:
                unavailable_signal.connect(self._on_native_map_unavailable)
            action_signal = getattr(renderer, "action_requested", None)
            if action_signal is not None:
                action_signal.connect(self._on_native_map_action)
            viewport_signal = getattr(renderer,"viewport_changed",None)
            if viewport_signal is not None:
                viewport_signal.connect(lambda value: self._on_reference_viewport_changed(value))
            view_signal = getattr(renderer, "view_state_changed", None)
            if view_signal is not None:
                view_signal.connect(self._on_native_map_view_state_changed)
            stack = getattr(self, "_map_stack", None)
            if stack is not None:
                stack.addWidget(renderer)
                stack.setCurrentWidget(renderer)
            else:
                splitter = getattr(self, "_map_canvas_splitter", None)
                if splitter is not None:
                    splitter.addWidget(renderer)
            self._native_map_renderer = renderer
            renderer.set_map_visible(bool(getattr(self, "_map_visible", False)))
            available = getattr(renderer, "is_available", None)
            if callable(available) and not available():
                status_text = getattr(renderer, "status_text", None)
                self._on_native_map_unavailable(status_text() if callable(status_text) else "")
            return renderer
        except Exception as exc:
            log.exception("StationsMap: failed creating native Qt Location renderer")
            enter_degraded = getattr(self, "_enter_map_degraded", None)
            if callable(enter_degraded):
                enter_degraded(
                    "Native Map is unavailable. Review the Qt Location installation and retry without restarting CommsPlan.",
                    reason="native_renderer_create",
                    exc=exc,
                )
            return None

    def _on_native_map_ready(self) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        self._map_page_loading = False
        self._map_load_ok = True
        StationsMapTab._apply_pending_native_map_projection(self)
        if getattr(self, "_map_visible", False) and getattr(self, "_app_active", True):
            maybe_start = getattr(self, "_maybe_start_map_ingest", None)
            if callable(maybe_start):
                maybe_start()

    def _on_native_map_unavailable(self, detail: str = "") -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        self._map_load_ok = False
        enter_degraded = getattr(self, "_enter_map_degraded", None)
        if callable(enter_degraded):
            enter_degraded(
                "Native Map is unavailable. Configuration and other workspaces remain usable.",
                reason="native_renderer_unavailable",
                exc=RuntimeError(str(detail or "Qt Location renderer unavailable")),
            )

    def _on_reference_viewport_changed(self, value) -> None:
        presenter = getattr(self,"_license_reference_presenter",None)
        if presenter is not None:
            presenter.viewport_changed(value)

    def _publish_map_country_catalog(self, catalog) -> None:
        combo = getattr(self, "_map_country_combo", None)
        if combo is None or tuple(catalog) == tuple(getattr(self,"_map_country_catalog",())):
            return
        self._map_country_catalog = tuple(catalog)
        selected = combo.currentData() or ""
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("All countries", "")
        combo.addItem("Unknown location", "unknown")
        for item in catalog:
            combo.addItem(str(item['name']),str(item['code']))
        combo.setCurrentIndex(max(0,combo.findData(selected)))
        combo.blockSignals(False)

    def _on_map_country_changed(self, *_args) -> None:
        self._map_geographic_country = self._map_country_combo.currentData() or ""
        references = getattr(self,"_license_reference_presenter",None)
        if references is not None:
            references.scope_changed()
        selected_region = self._map_state_filter_combo.currentData() or ""
        country = self._map_geographic_country
        self._map_region_filter_label.setText("State" if country == "USA" else "Province / territory" if country == "CAN" else "Region")
        combo = self._map_state_filter_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("All", "")
        scopes = (("US",US_STATE_NAMES),) if country == "USA" else (("CA",CANADA_PROVINCE_NAMES),) if country == "CAN" else (("US",US_STATE_NAMES),("CA",CANADA_PROVINCE_NAMES)) if not country else ()
        for prefix, regions in scopes:
            for code in sorted(regions):
                combo.addItem(code if country else f"{prefix} · {code}",code)
        combo.setCurrentIndex(max(0,combo.findData(selected_region)))
        combo.setEnabled(bool(scopes))
        combo.blockSignals(False)
        self._map_focus_country_button.setEnabled(bool(self._map_geographic_country not in ("", "unknown")))
        self._request_map_refresh(level="light", reason="geographic_country")

    def _focus_map_country(self) -> None:
        from freqinout.gui.map_camera_preferences import country_focus_view
        renderer = getattr(self,"_native_map_renderer",None)
        country = getattr(self,"_map_geographic_country","")
        item = next((item for item in getattr(self,"_map_country_catalog",()) if item['code'] == country), None)
        if item and renderer:
            view = country_focus_view(item['bounds'],renderer.width(),renderer.height())
            if view:
                renderer.set_view_center(view['lat'],view['lon'],view['zoom'])
                self._on_native_map_view_state_changed(view)

    def _save_map_starting_view(self) -> None:
        from freqinout.gui.map_camera_preferences import validated_view
        renderer = getattr(self,"_native_map_renderer",None)
        view = validated_view(renderer.view_state()) if renderer else None
        if view and self.settings:
            self.settings.set("map_starting_view", view)
            renderer.set_home_view(view)
            self._map_starting_view_status.setText("Starting area saved for the next session.")
            self._map_save_starting_view_button.setToolTip("Starting area saved. Country filters are unchanged.")

    def _restore_map_starting_view(self) -> None:
        from freqinout.gui.map_camera_preferences import validated_view
        view = validated_view(self.settings.get("map_starting_view",None)) if self.settings else None
        renderer = getattr(self,"_native_map_renderer",None)
        if view and renderer:
            renderer.set_view_center(view['lat'],view['lon'],view['zoom'])
            self._on_native_map_view_state_changed(view)

    def _on_native_map_view_state_changed(self, view: object) -> None:
        if isinstance(view, Mapping):
            parsed = StationsMapTab._parse_view_state(self, dict(view))
            self._last_map_view = parsed
            self._map_user_camera_owned = True
            StationsMapTab._request_world_geography(self,parsed)

    def _request_world_geography(self, view):
        config = getattr(self,"_world_geography_config",None)
        if not config or not getattr(self,"_map_visible",False) or not getattr(self,"_app_active",True) or getattr(self,"_is_shutting_down",False):
            return
        renderer = getattr(self,"_native_map_renderer",None)
        if renderer is None:
            return
        lane = getattr(self,"_world_geography_lane",None)
        if lane is None:
            from freqinout.gui.world_geography import GeographyLane
            lane = GeographyLane(self)
            lane.ready.connect(self._apply_world_geography)
            self._world_geography_lane = lane
        lane.request(view,renderer.width(),renderer.height(),**config)

    def _apply_world_geography(self, payload):
        if getattr(self,"_is_shutting_down",False) or not getattr(self,"_map_visible",False) or not getattr(self,"_app_active",True):
            return
        renderer = getattr(self,"_native_map_renderer",None)
        if renderer is not None:
            renderer.apply_geography(payload)

    def _on_native_map_action(self, action_payload: object) -> None:
        if not isinstance(action_payload, Mapping):
            return
        action = str(action_payload.get("action") or "").strip().lower()
        if action == "select_marker":
            payload = action_payload.get("payload", action_payload.get("marker"))
        elif action == "select_path":
            payload = action_payload.get("payload", action_payload.get("path"))
        elif action in {"select_polygon", "select_label"}:
            item_key = "polygon" if action == "select_polygon" else "label"
            payload = action_payload.get("payload", action_payload.get(item_key))
        else:
            handler = getattr(self, "_handle_map_detail_action", None)
            if callable(handler):
                handler(dict(action_payload))
            return
        if isinstance(payload, Mapping):
            if str(payload.get("type") or "") == "license_reference":
                references = getattr(self,"_license_reference_presenter",None)
                if references is not None:
                    references.select(payload)
                return
            show_detail = getattr(self, "_show_map_selected_detail", None)
            if callable(show_detail):
                payload = dict(payload)
                rendered = action_payload.get("marker") if action == "select_marker" else None
                if isinstance(rendered, Mapping) and rendered.get("kind"):
                    payload["_map_item_kind"] = str(rendered["kind"])
                show_detail(dict(payload))

    def _apply_native_map_projection(self, payload: Mapping[str, object] | None) -> bool:
        projection = dict(payload or {})
        if getattr(self, "_is_shutting_down", False):
            return False
        if not getattr(self, "_map_visible", False) or not getattr(self, "_app_active", True):
            self._pending_map_payload = projection
            return False
        renderer = getattr(self, "_native_map_renderer", None)
        if renderer is None:
            renderer = StationsMapTab._ensure_native_map_renderer(self)
        available = getattr(renderer, "is_available", None) if renderer is not None else None
        if renderer is None or (callable(available) and not available()):
            self._pending_map_payload = projection
            return False
        if bool(getattr(self, "_map_initialized", False) or getattr(self,"_map_user_camera_owned",False)):
            projection.pop("auto_fit", None)
            current_view = getattr(renderer, "view_state", None)
            if callable(current_view):
                projection["view"] = current_view()
        StationsMapTab._publish_map_country_catalog(self, projection.get("_country_catalog", ()))
        renderer.apply_projection(projection)
        references = getattr(self,"_license_reference_presenter",None)
        if references is not None:
            references.publish()
        if projection.get("_world_geography"):
            self._world_geography_config = {"show_cities":bool(projection.get("show_cities")),
                                             "min_population":float(projection.get("city_min_pop") or 0)}
            StationsMapTab._request_world_geography(self,renderer.view_state())
        self._pending_map_payload = None
        self._map_initialized = True
        self._map_load_ok = True
        return True

    def _apply_pending_native_map_projection(self) -> bool:
        payload = getattr(self, "_pending_map_payload", None)
        if not isinstance(payload, Mapping):
            return False
        self._pending_map_payload = None
        if (
            not bool(payload.get("_native_projection_enriched"))
            and getattr(self, "_map_visible", False)
            and getattr(self, "_app_active", True)
        ):
            # Hidden work retains the raw value snapshot without reading static
            # geometry. Re-entry resumes that bounded worker stage before QML.
            self._queue_native_map_projection(dict(payload))
            return False
        if StationsMapTab._apply_native_map_projection(self, payload):
            return True
        self._pending_map_payload = dict(payload)
        return False

    def _handle_map_detail_action(self, payload: Dict[str, object]) -> None:
        action = str(payload.get("action") or "").strip().lower()
        if action == "regional_summary_collapsed":
            self._regional_summary_collapsed = bool(payload.get("collapsed"))
            return
        if action == "select_detail":
            self._show_map_selected_detail(payload)
            return
        if action == "open_messages":
            self._map_selected_payload = dict(payload or {})
            self._open_map_selected_messages()
            return
        if action == "review_sop":
            self._map_selected_payload = dict(payload or {})
            self._open_map_selected_sop()
            return
        if action == "filter_group":
            group = str(payload.get("group") or "").strip().upper().lstrip("@").rstrip(">")
            if not group or not hasattr(self, "group_filter_combo"):
                return
            self._set_combo_by_text_or_data(self.group_filter_combo, group)
            self._request_map_refresh(level="medium", reason="selected_detail_group")
            return
        if action == "filter_topic":
            topic = str(payload.get("topic") or "").strip()
            combo = getattr(self, "_map_topic_filter_combo", None)
            if not topic or combo is None:
                return
            self._set_combo_by_text_or_data(combo, topic)
            if not bool(getattr(self, "_observation_focus_enabled", False)):
                group = str(payload.get("group") or "").strip().upper().lstrip("@").rstrip(">")
                self._set_report_focus_mode("all_reports", group_filter=group, topic_filter=topic)
                return
            self._clear_report_query_caches()
            self._request_map_refresh(level="medium", reason="selected_detail_topic")

    # ------------- Map rendering ------------- #
    def _on_mesh_recent_changed(self, _checked: bool) -> None:
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="light", reason="mesh_recent_filter")

    def on_mesh_data_changed(self, event) -> None:
        if getattr(event, "node", None) is None and getattr(event, "message", None) is None:
            return
        self._map_dirty = True
        if self._current_map_mode_key() != "mesh_nodes":
            return
        timer = getattr(self, "_mesh_map_update_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(500)
            timer.timeout.connect(lambda: self._request_map_refresh(level="light", reason="mesh_data_changed"))
            self._mesh_map_update_timer = timer
        if not timer.isActive():
            timer.start()

    def _capture_map_read_request(self, *, kind: str, generation: int, preserve_view=True):
        """Snapshot cached values on the GUI thread; never pass Qt/settings owners."""
        from copy import deepcopy
        from freqinout.gui.map_read_snapshot import MapReadRequest, INPUT_FIELDS, SELECTORS, COMBO_FIELDS
        state = {name: deepcopy(getattr(self, name)) for name in INPUT_FIELDS if hasattr(self, name)}
        settings = self.settings.all() if hasattr(self.settings, "all") else dict(self.settings or {})
        selectors = {name: deepcopy(getattr(self, name)()) for name in SELECTORS}
        combos = {name: deepcopy(self._map_filter_combo_signature(name)) for name in COMBO_FIELDS}
        band = self.band_combo.currentData() if hasattr(self, "band_combo") else {"type": "all"}
        recent = getattr(self, "_mesh_recent_chk", None)
        return MapReadRequest(generation, kind, state, deepcopy(settings), selectors, combos,
                              dict(self._theme_snapshot()), deepcopy(band), bool(recent and recent.isChecked()),
                              deepcopy(preserve_view))

    def _render_map(self, preserve_view: bool = True):
        """Queue the entire read/build pipeline; presentation performs no input I/O."""
        if self._is_shutting_down:
            return
        if not self._map_visible or not self._app_active or self._map_page_loading:
            self._map_dirty = True
            return
        from freqinout.gui.map_read_snapshot import MapReadLane
        self._map_dirty = False
        self._map_payload_generation += 1
        generation = self._map_payload_generation
        if self._map_read_lane is None:
            self._map_read_lane = MapReadLane(self)
            self._map_read_lane.ready.connect(self._on_map_read_ready)
        self._map_read_inflight = True
        self._map_read_lane.request(self._capture_map_read_request(
            kind="map", generation=generation, preserve_view=preserve_view))

    def _on_map_read_ready(self, result):
        self._map_read_inflight = bool(self._map_read_lane and self._map_read_lane._running)
        if self._is_shutting_down or result.generation != self._map_payload_generation:
            self._emit_map_event("data_snapshot_stale", generation=result.generation)
            return
        emit_span("map.data_snapshot", result.elapsed_ms, settings=self.settings,
                  meta={"status": "failed" if result.error else "ready"}, min_ms=5.0)
        if result.error:
            self._enter_map_degraded("Map data unavailable; showing the last valid map. Retry to update.",
                                     reason="data_snapshot_failed", exc=RuntimeError(result.error))
            return
        initial = not self._initial_data_loaded
        for name, value in result.state.items():
            setattr(self, name, value)
        self._initial_data_loaded = True
        self._refresh_band_options()
        self._update_now_reachable_summary()
        if initial:
            self._refresh_link_mode_options()
            self._refresh_group_filter_options()
            self._refresh_region_filter_options()
            self._refresh_relay_targets()
            self._refresh_prop_target_controls(cached_only=True)
        if result.badge:
            self._update_prop_badge(*result.badge)
        self._on_map_payload_snapshot_ready(result.projection)
        self._emit_map_event("render_completed", generation=result.generation)
        if self._map_selected_payload:
            self._queue_map_inspector_snapshot()

    def _queue_map_inspector_snapshot(self):
        if self._is_shutting_down or not self._map_visible or not self._app_active:
            return
        from freqinout.gui.map_read_snapshot import MapReadLane
        self._map_inspector_generation += 1
        if self._map_inspector_lane is None:
            self._map_inspector_lane = MapReadLane(self)
            self._map_inspector_lane.ready.connect(self._on_map_inspector_ready)
        self._map_inspector_lane.request(self._capture_map_read_request(
            kind="inspector", generation=self._map_inspector_generation))

    def _on_map_inspector_ready(self, result):
        if (self._is_shutting_down or result.generation != self._map_inspector_generation
                or not self._map_visible or not self._app_active):
            return
        bodies = (self._map_selected_body, self._map_selected_status_body,
                  self._map_selected_paths_body, self._map_selected_messages_body)
        if result.error:
            # Keep coherent prior detail for this selection and mark it stale.
            for body in bodies:
                if body is not None:
                    body.setToolTip("Details unavailable; retained information may be stale. Retry the map refresh.")
                    if not getattr(self, "_map_inspector_has_content", False):
                        body.setHtml("<p>Selection details unavailable. Retry the map refresh to update.</p>")
            return
        self._map_inspector_has_content = True
        for body, text in zip(bodies, result.inspector):
            if body is not None:
                body.setHtml(text)
                body.setToolTip("")

    def _build_map_payload(self, preserve_view: bool = True):
        if not self._map_visible or not self._app_active:
            self._map_dirty = True
            return
        if self._map_page_loading:
            self._map_dirty = True
            self._render_requested_during_load = True
            return
        self._map_dirty = False
        theme_key = ""
        try:
            theme_key = str(self.settings.get("ui_theme", "") or "").strip().lower()
        except Exception:
            theme_key = ""
        config_sig = (
            bool(self.show_callsigns),
            bool(self.show_states),
            bool(self.show_grids),
            bool(self.show_grid_labels),
            bool(self.show_regions),
            bool(self.prop_overlay_enabled),
            bool(self.prop_adaptive_enabled),
            str(self._effective_prop_mode()),
            theme_key,
            int(self.prop_window_hours or 6),
        )
        force_reload = self._map_initialized and self._last_map_config and config_sig != self._last_map_config

        view_state = None
        if isinstance(preserve_view, dict):
            view_state = preserve_view or self._last_map_view
            if view_state:
                self._last_map_view = view_state
        elif preserve_view:
            view_state = self._last_map_view
        if view_state is None and self._last_map_view:
            view_state = self._last_map_view

        if self._current_map_mode_key() == "mesh_nodes":
            # Known contacts are a topology source, independent of traffic age,
            # topic and group filters. Read their bounded snapshot on the worker.
            own_call = str(self.settings.get("operator_callsign", "") or "").strip().upper()
            own_grid = str(self.settings.get("operator_grid6", "") or self.settings.get("operator_grid", "") or "").strip().upper()
            own_location = maidenhead_to_latlon(own_grid) if own_grid else None
            own_marker = ({"id": "fio-own-station", "kind": "station", "callsign": own_call,
                           "label": own_call, "is_own_station": True,
                           "lat": own_location[0], "lon": own_location[1],
                           "tooltip": "My station · Saved operator grid " + own_grid}
                          if own_call and own_location else None)
            self._queue_native_map_projection({
                "_mesh_operator_marker": own_marker,
                "map_mode": "mesh_nodes", "markers": [], "links": [],
                "_mesh_db_path": str(get_config_dir() / "config" / "freqinout_nets.db"),
                "_mesh_show_paths": bool(self.show_link_paths),
                "_mesh_recent_only": bool(getattr(self, "_mesh_recent_chk", None) and self._mesh_recent_chk.isChecked()),
                "link_direction_markers": bool(self.show_link_paths),
                "show_states": bool(self.show_states), "show_regions": bool(self.show_regions),
                "show_grids": bool(self.show_grids), "show_grid_labels": bool(self.show_grid_labels),
                "show_cities": bool(self.show_city_labels),
                "view": view_state or {"lat": 45, "lon": -97, "zoom": 3},
            })
            return

        report_view_without_roster = bool(
            self._effective_map_observation_focus_enabled()
            and self._effective_map_report_focus_mode() in {"hf_reports", "local_reports", "all_reports", "mesh_nodes"}
        )
        regional_view_without_roster = bool(
            self._effective_map_observation_focus_enabled()
            and self._effective_map_observation_focus_mode() == "regional_intelligence"
        )
        propagation_view_without_roster = bool(self.prop_overlay_enabled)
        if (
            not self._station_points_for_map()
            and not report_view_without_roster
            and not regional_view_without_roster
            and not propagation_view_without_roster
        ):
            self._map_marker_count = 0
            self._map_link_count = 0
            self._map_link_status_detail = "No station data available for paths."
            effective_view = view_state or self._last_map_view or {"lat": 45, "lon": -97, "zoom": 3}
            self._queue_native_map_projection(
                {
                    "map_mode": self._current_map_mode_key(),
                    "markers": [],
                    "links": [],
                    "view": effective_view,
                    "show_states": bool(self.show_states),
                    "show_regions": bool(self.show_regions),
                    "show_grids": bool(self.show_grids),
                    "show_grid_labels": bool(self.show_grid_labels),
                    "city_labels": [],
                }
            )
            self._last_map_view = effective_view
            return

        def _fmt_ts(ts_val):
            try:
                if ts_val:
                    return datetime.datetime.utcfromtimestamp(ts_val).strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:
                pass
            return ""

        def _timed_map_call(name: str, fn, *, meta: Optional[Dict[str, object]] = None):
            start = time.perf_counter()
            try:
                return fn()
            finally:
                emit_span(
                    name,
                    (time.perf_counter() - start) * 1000.0,
                    settings=self.settings,
                    meta=meta,
                    min_ms=5.0,
                )

        selection = self._current_link_selection()
        group_filter = self._selected_map_group_filter()
        region_filter = self._selected_map_region_filter()
        topic_filter = self._selected_map_topic_filter()
        search_text = self._selected_map_search_text()
        observation_focus_enabled = self._effective_map_observation_focus_enabled()
        observation_focus_mode = self._effective_map_observation_focus_mode()
        report_focus_mode = self._effective_map_report_focus_mode()
        effective_show_city_labels, effective_city_pop_min = self._effective_city_label_config(
            observation_focus_enabled=observation_focus_enabled,
            observation_focus_mode=observation_focus_mode,
        )
        regional_intelligence_mode = self._current_map_mode_key() == "regional"
        regional_intelligence_sensitivity = self._selected_map_intel_sensitivity()
        config_sig = (
            *config_sig,
            bool(regional_intelligence_mode),
            str(regional_intelligence_sensitivity or "") if regional_intelligence_mode else "",
            str(topic_filter or "").strip() if regional_intelligence_mode else "",
        )
        force_reload = self._map_initialized and self._last_map_config and config_sig != self._last_map_config
        implicit_observation_focus = self._implicit_map_observation_focus_enabled()
        planning_pins_mode = observation_focus_enabled and observation_focus_mode == "rf_pins"
        effective_show_weather_reports = bool(self.show_weather_reports)
        effective_show_alert_reports = bool(self.show_alert_reports or implicit_observation_focus)
        effective_show_infrastructure_reports = bool(
            self.show_infrastructure_reports
            or implicit_observation_focus
            or planning_pins_mode
            or observation_focus_mode == "mesh_nodes"
        )
        observation_scope_applies = self._observation_focus_scopes_station_markers(
            observation_focus_enabled,
            report_focus_mode,
        )
        target_ctx = self._prop_target_context()
        target_label = str(target_ctx.get("label") or "National")
        prop_region_scores: Dict[str, Dict] = {}
        prop_state_scores: Dict[str, Dict] = {}
        if self.prop_overlay_enabled:
            # Keep overlay data broad; link filters are independent from propagation target.
            prop_region_scores = self._cached_map_value(
                "prop_region_scores",
                {"region_filter": "", "window_hours": int(self.prop_window_hours or 6), "mode": str(self._effective_prop_mode())},
                lambda: _timed_map_call(
                    "map.compute_region_scores",
                    lambda: self._compute_region_scores(""),
                ),
                ttl_sec=20.0,
                force=bool(force_reload),
            )
            prop_state_scores = self._cached_map_value(
                "prop_state_scores",
                {"window_hours": int(self.prop_window_hours or 6), "mode": str(self._effective_prop_mode())},
                lambda: _timed_map_call(
                    "map.compute_state_scores",
                    self._compute_state_scores,
                ),
                ttl_sec=20.0,
                force=bool(force_reload),
            )
            best_band, best_score = self._best_band_for_target(target_ctx, prop_region_scores, prop_state_scores)
            self._update_prop_badge(target_label, best_band, best_score)
            target_sig = f"{target_ctx.get('type','')}:{target_ctx.get('value','')}"
            if target_sig != self._last_prop_region_filter:
                force_reload = True
        else:
            self._update_prop_badge(target_label, "", 0.0)
            target_sig = f"{target_ctx.get('type','')}:{target_ctx.get('value','')}"
        self._last_prop_region_filter = target_sig
        self._sitrep_status_only_enabled = False
        sitrep_mode = False
        band_filter = self.band_combo.currentData() if hasattr(self, "band_combo") else {"type": "all"}
        my_call = ""
        try:
            my_call = (self.settings.get("operator_callsign", "") or "").upper()
        except Exception:
            my_call = ""
        path_search_target = self._path_target_from_search_text(search_text)
        effective_relay_target = (
            str(getattr(self, "relay_target", "") or "").strip().upper()
            or path_search_target
        )
        effective_link_selection = (
            ("relay_target", effective_relay_target)
            if path_search_target and effective_relay_target
            else selection
        )

        map_input_sig = (
            config_sig,
            bool(self._links_active()),
            bool(sitrep_mode),
            tuple(sorted(selection.items())) if isinstance(selection, dict) else str(selection),
            str(group_filter or "").strip().upper(),
            str(region_filter or "").strip().upper(),
            self._map_band_filter_signature(band_filter),
            int(self.recency_seconds or 0),
            str(my_call or "").strip().upper(),
            self._get_user_latlon(),
            effective_relay_target,
            bool(self._now_reachable_enabled),
            bool(self.show_station_markers),
            bool(self.show_link_paths),
            bool(effective_show_weather_reports),
            bool(effective_show_alert_reports),
            bool(effective_show_infrastructure_reports),
            observation_focus_enabled,
            observation_focus_mode,
            report_focus_mode,
            bool(regional_intelligence_mode),
            str(regional_intelligence_sensitivity or ""),
            str(topic_filter or "").strip(),
            self._normalize_map_search_text(search_text),
            len(self._now_reachable_callsigns),
            hash("|".join(sorted(self._now_reachable_callsigns))) if self._now_reachable_callsigns else 0,
            str(target_sig or ""),
            self._nets_db_fingerprint(),
            int(self._stations_revision or 0),
            self._map_advanced_filters_signature(),
        )
        if (
            regional_intelligence_mode
            and self._last_map_render_input_sig
            and map_input_sig != self._last_map_render_input_sig
        ):
            force_reload = True
        elif (
            not force_reload
            and self._native_map_renderer is not None
            and self._map_initialized
            and bool(self._last_map_payload_sig)
            and map_input_sig == self._last_map_render_input_sig
        ):
            self._emit_map_event("render_skipped_unchanged", reason="input_signature_match")
            self._last_map_view = view_state or self._last_map_view or {"lat": 45, "lon": -97, "zoom": 3}
            return

        # init stats and links
        stats_lookup: Dict[str, Dict] = {}
        links: List[Dict] = []
        relay_target = ""
        reachable_filter = None
        if self._links_active() and not sitrep_mode:
            relay_target = effective_relay_target
            reachable_filter = self._now_reachable_callsigns if self._now_reachable_enabled else None

            links, stats_lookup = _timed_map_call(
                "map.load_js8_links",
                lambda: self._load_js8_links(
                    band_filter=band_filter,
                    my_call=my_call,
                    link_selection=selection,
                    relay_target=relay_target or None,
                    group_filter=group_filter,
                    region_filter=region_filter,
                    reachable_callsigns=reachable_filter,
                    max_age_sec=self._effective_path_recency_seconds(),
                ),
                meta={"sitrep_mode": sitrep_mode},
            )
            links.extend(
                _timed_map_call(
                    "map.load_varac_links",
                    lambda: self._load_varac_links(
                        band_filter=band_filter,
                        my_call=my_call,
                        link_selection=effective_link_selection,
                        group_filter=group_filter,
                        region_filter=region_filter,
                        reachable_callsigns=reachable_filter,
                        max_age_sec=self._effective_path_recency_seconds(),
                    ),
                    meta={"sitrep_mode": sitrep_mode},
                )
            )
            if view_state:
                self._last_map_view = view_state

        path_view_active = bool(self._links_active())
        station_enrichment_needed = bool(
            not regional_intelligence_mode
            and not sitrep_mode
            and not path_view_active
            and (
                bool(getattr(self, "show_station_markers", False))
                or bool(getattr(self, "_now_reachable_enabled", False))
            )
        )
        if sitrep_mode or not station_enrichment_needed:
            varac_stats = {}
            varac_all = {}
            activity_lookup = {}
            direct_contact_lookup = {}
            js8_all = set()
            fldigi_calls = set()
            spotter_map_activity = {}
            commstat_reporter_activity = {}
        else:
            varac_stats = self._cached_map_value(
                "varac_stats_recent",
                {"max_age_sec": self.recency_seconds},
                lambda: _timed_map_call(
                    "map.load_varac_stats_recent",
                    lambda: self._load_varac_stats(max_age_sec=self.recency_seconds),
                ),
                ttl_sec=8.0,
            )
            varac_all = self._cached_map_value(
                "varac_stats_all",
                {"max_age_sec": None},
                lambda: _timed_map_call("map.load_varac_stats_all", lambda: self._load_varac_stats(max_age_sec=None)),
                ttl_sec=12.0,
            )
            activity_lookup = self._cached_map_value(
                "operator_activity_summary",
                {"recency_seconds": self.recency_seconds},
                lambda: _timed_map_call("map.load_operator_activity_summary", self._load_operator_activity_summary),
                ttl_sec=8.0,
            )
            direct_contact_lookup = self._cached_map_value(
                "js8_direct_contact_summary",
                {"my_call": my_call},
                lambda: _timed_map_call(
                    "map.load_js8_direct_contact_summary",
                    lambda: self._load_js8_direct_contact_summary(my_call),
                ),
                ttl_sec=8.0,
            )
            js8_all = self._cached_map_value(
                "js8_presence",
                {"recency_seconds": self.recency_seconds},
                lambda: _timed_map_call("map.load_js8_presence", self._load_js8_presence),
                ttl_sec=8.0,
            )
            fldigi_calls = self._cached_map_value(
                "fldigi_presence",
                {"recency_seconds": self.recency_seconds},
                lambda: _timed_map_call("map.load_fldigi_presence", self._load_fldigi_presence),
                ttl_sec=8.0,
            )
            spotter_map_activity = self._cached_map_value(
                "spotter_map_activity",
                {},
                lambda: _timed_map_call("map.load_spotter_map_activity", self._load_spotter_map_activity),
                ttl_sec=6.0,
            )
            commstat_reporter_activity = self._cached_map_value(
                "commstat_reporter_activity",
                {"recency_seconds": self.recency_seconds},
                lambda: _timed_map_call(
                    "map.load_commstat_reporter_activity",
                    lambda: self._load_commstat_reporter_activity(max_age_sec=self.recency_seconds),
                ),
                ttl_sec=6.0,
            )
        spotter_status_lookup = (
            self._cached_map_value(
                "spotter_station_status",
                {"group_filter": str(group_filter or "").strip().upper(), "region_filter": str(region_filter or "").strip().upper()},
                lambda: _timed_map_call("map.load_spotter_station_status", self._load_spotter_station_status),
                ttl_sec=6.0,
            )
            if sitrep_mode or station_enrichment_needed or path_view_active
            else {}
        )
        sitrep_state_summary: List[Dict[str, object]] = []
        sitrep_summary_group = ""
        regional_intelligence_payload: Dict[str, object] = {
            "enabled": False,
            "states": {},
            "regions": {},
            "summary": "",
        }
        if sitrep_mode:
            sitrep_summary_group = str(group_filter or "").strip().upper()
            sitrep_state_summary = self._cached_map_value(
                "sitrep_state_rollup",
                {"group": sitrep_summary_group},
                lambda: _timed_map_call(
                    "map.load_sitrep_state_rollup",
                    lambda: self._load_sitrep_state_rollup(sitrep_summary_group),
                ),
                ttl_sec=6.0,
            )
        if regional_intelligence_mode:
            regional_intelligence_payload = self._cached_map_value(
                "regional_intelligence",
                {
                    "sensitivity": regional_intelligence_sensitivity,
                    "topic": str(topic_filter or "").strip(),
                    "group": str(group_filter or "").strip(),
                    "region": str(region_filter or "").strip(),
                    "search": self._normalize_map_search_text(search_text),
                    "state": self._map_advanced_state_filter(),
                    "recency_seconds": int(self.recency_seconds or 0),
                    "db": self._nets_db_fingerprint(),
                },
                lambda: _timed_map_call(
                    "map.build_regional_intelligence",
                    lambda: self._regional_intelligence_payload(
                        topic_filter=topic_filter,
                        group_filter=group_filter,
                        region_filter=region_filter,
                        search_text=search_text,
                        state_filter=self._map_advanced_state_filter(),
                        sensitivity=regional_intelligence_sensitivity,
                        max_age_sec=int(self.recency_seconds or 0),
                    ),
                ),
                ttl_sec=6.0,
                force=bool(force_reload),
            )
        links = self._display_links_for_mode(links, sitrep_mode)
        reports_allowed = self._map_reports_allowed_for_current_view()
        stations_allowed = self._advanced_filters_allow_stations()
        if self._map_advanced_scope_filter() == "reports":
            links = []
        loaded_link_count = len(links)
        self._map_last_link_all_time_count = 0

        # Spread overlapping stations with the same base lat/lon
        markers = []
        weather_station_lookup: Dict[str, StationPoint] = {}
        base_map: Dict[tuple[float, float], List[StationPoint]] = {}
        my_call = (self.settings.get("operator_callsign", "") or "").strip().upper()
        observation_scope_calls = (
            self._observation_station_scope_calls(max_age_sec=self.recency_seconds or 0)
            if observation_focus_enabled
            else set()
        )
        traffic_calls = {cs.upper() for cs in stats_lookup.keys()}
        for link in links:
            origin = (link.get("origin") or "").strip().upper()
            dest = (link.get("destination") or "").strip().upper()
            if origin:
                traffic_calls.add(origin)
                base_origin = JS8LogLinkIndexer._base_callsign(origin)
                if base_origin:
                    traffic_calls.add(base_origin)
            if dest:
                traffic_calls.add(dest)
                base_dest = JS8LogLinkIndexer._base_callsign(dest)
                if base_dest:
                    traffic_calls.add(base_dest)
        for cs in varac_stats.keys():
            if cs:
                traffic_calls.add(cs)
        for cs in spotter_map_activity.keys():
            if cs:
                traffic_calls.add(str(cs).upper())
        if self._now_reachable_enabled:
            traffic_calls.update({c for c in self._now_reachable_callsigns if c})
        show_all_stations = (not self._links_active()) or sitrep_mode
        recent_calls: Set[str] = set()
        if show_all_stations and self.recency_seconds and not sitrep_mode:
            band_filter = self.band_combo.currentData() if hasattr(self, "band_combo") else {"type": "all"}
            recent_calls = self._cached_map_value(
                "recent_calls",
                {"recency_seconds": self.recency_seconds, "band_filter": band_filter},
                lambda: _timed_map_call(
                    "map.load_recent_calls",
                    lambda: self._load_recent_calls(self.recency_seconds, band_filter=band_filter),
                ),
                ttl_sec=6.0,
            )
        for pt in self._station_points_for_map():
            cs_upper = pt.callsign.upper()
            if self._marker_station_matches_filters(
                cs_upper,
                group_filter="" if sitrep_mode else group_filter,
                region_filter=region_filter,
                my_call=my_call,
                allow_self=True,
            ):
                weather_station_lookup[cs_upper] = pt
                base_cs = JS8LogLinkIndexer._base_callsign(cs_upper)
                if base_cs:
                    weather_station_lookup.setdefault(base_cs, pt)
            if not self._marker_station_matches_filters(
                cs_upper,
                group_filter="" if sitrep_mode else group_filter,
                region_filter=region_filter,
                my_call=my_call,
                allow_self=bool(self._links_active() or self._now_reachable_enabled),
            ):
                continue
            if self._now_reachable_enabled:
                # Peer Sched Now is a strict station filter: only peers whose
                # schedule alignment is in the computed reachable set (plus me).
                if cs_upper != my_call and cs_upper not in self._now_reachable_callsigns:
                    continue
            if sitrep_mode:
                status_data = spotter_status_lookup.get(cs_upper, {})
                if (status_data.get("status_key") or "").strip().lower() not in {"red", "yellow", "green"}:
                    continue
                if group_filter:
                    status_group = str(status_data.get("report_group") or "").strip().upper()
                    if status_group != str(group_filter or "").strip().upper():
                        continue
            if not show_all_stations:
                if cs_upper not in traffic_calls and cs_upper != my_call:
                    continue
            elif recent_calls:
                if cs_upper not in recent_calls and cs_upper != my_call:
                    continue
            if observation_scope_applies:
                base_cs = JS8LogLinkIndexer._base_callsign(cs_upper)
                if cs_upper not in observation_scope_calls and base_cs not in observation_scope_calls:
                    continue
            if not stations_allowed:
                continue
            if not self._station_matches_advanced_filters(pt):
                continue
            observation_filter_already_scoped = bool(
                observation_scope_applies
                and (str(topic_filter or "").strip() or str(search_text or "").strip())
            )
            if not observation_filter_already_scoped and not self._station_matches_map_search(pt, search_text):
                continue
            key = (round(pt.lat, 4), round(pt.lon, 4))
            base_map.setdefault(key, []).append(pt)

        observation_report_max_age_sec = int(self.recency_seconds or 0)
        report_focus_active = bool(
            observation_focus_enabled and report_focus_mode in {"hf_reports", "local_reports", "all_reports", "mesh_nodes"}
        )
        report_focus_events = (
            self._build_map_report_focus_events(
                weather_station_lookup,
                max_age_sec=observation_report_max_age_sec,
            )
            if report_focus_active and reports_allowed
            else []
        )
        weather_events = (
            []
            if report_focus_active
            else self._build_weather_map_events(
                weather_station_lookup,
                max_age_sec=observation_report_max_age_sec,
            )
            if effective_show_weather_reports and reports_allowed
            else []
        )
        include_legacy_spotter_reports = self._include_legacy_spotter_report_layers()
        alert_events = (
            self._build_spotter_operational_events(
                weather_station_lookup,
                layer_name="alert",
                display_label="Alerts",
                reports_loader=self._load_spotter_alert_reports,
                max_age_sec=observation_report_max_age_sec,
            )
            if effective_show_alert_reports and include_legacy_spotter_reports and reports_allowed
            else []
        )
        if effective_show_alert_reports and reports_allowed and observation_focus_enabled and not report_focus_active:
            alert_events.extend(
                self._build_spotter_operational_events(
                    weather_station_lookup,
                    layer_name="alert",
                    display_label="Observation Alerts",
                    reports_loader=lambda: self._load_observation_operational_reports(
                        layer_name="alert",
                        max_age_sec=observation_report_max_age_sec,
                    ),
                    max_age_sec=observation_report_max_age_sec,
                )
            )
        infrastructure_events = (
            self._build_spotter_operational_events(
                weather_station_lookup,
                layer_name="infrastructure",
                display_label="Infrastructure Reports",
                reports_loader=self._load_spotter_infrastructure_reports,
                max_age_sec=observation_report_max_age_sec,
            )
            if effective_show_infrastructure_reports and include_legacy_spotter_reports and reports_allowed
            else []
        )
        if report_focus_active:
            alert_events = []
            infrastructure_events = report_focus_events
        elif effective_show_infrastructure_reports and reports_allowed and observation_focus_enabled:
            infrastructure_events.extend(
                self._build_spotter_operational_events(
                    weather_station_lookup,
                    layer_name="infrastructure",
                    display_label="Observation Infrastructure",
                    reports_loader=lambda: self._load_observation_operational_reports(
                        layer_name="infrastructure",
                        max_age_sec=observation_report_max_age_sec,
                    ),
                    max_age_sec=observation_report_max_age_sec,
                )
            )
            infrastructure_events.extend(
                self._build_spotter_operational_events(
                    weather_station_lookup,
                    layer_name="message_metadata_infrastructure",
                    display_label="Message Reports",
                    reports_loader=lambda: self._load_message_metadata_operational_reports(
                        layer_name="infrastructure",
                        max_age_sec=observation_report_max_age_sec,
                    ),
                    max_age_sec=observation_report_max_age_sec,
                )
            )
        if reports_allowed:
            weather_events = [
                event
                for event in weather_events
                if self._map_event_matches_primary_filters(
                    event,
                    group_filter=group_filter,
                    topic_filter=topic_filter,
                    search_text=search_text,
                )
                and self._map_event_matches_advanced_filters(event)
            ]
            alert_events = [
                event
                for event in alert_events
                if self._map_event_matches_primary_filters(
                    event,
                    group_filter=group_filter,
                    topic_filter=topic_filter,
                    search_text=search_text,
                )
                and self._map_event_matches_advanced_filters(event)
            ]
            infrastructure_events = [
                event
                for event in infrastructure_events
                if self._map_event_matches_primary_filters(
                    event,
                    group_filter=group_filter,
                    topic_filter=topic_filter,
                    search_text=search_text,
                )
                and self._map_event_matches_advanced_filters(event)
            ]
        if regional_intelligence_mode and reports_allowed:
            infrastructure_events = self._regional_intelligence_density_events(
                regional_intelligence_payload,
                weather_station_lookup,
            )

        def offset_positions(base_lat: float, base_lon: float, items: List[StationPoint]):
            if len(items) == 1:
                return [(base_lat, base_lon)]
            coords = []
            radius = 0.25  # degrees, modest spread
            for idx, _ in enumerate(items):
                angle = (idx / len(items)) * 6.28318530718  # 2*pi
                lat_off = base_lat + radius * math.cos(angle)
                lon_off = base_lon + (radius * math.sin(angle) / max(0.1, math.cos(math.radians(base_lat))))
                coords.append((lat_off, lon_off))
            return coords

        for (base_lat, base_lon), items in base_map.items():
            positions = offset_positions(base_lat, base_lon, items)
            for pt, (lat_off, lon_off) in zip(items, positions):
                cs_upper = pt.callsign.upper()
                if cs_upper == my_call:
                    lat_off, lon_off = pt.lat, pt.lon
                stats = stats_lookup.get(cs_upper, {})
                vstats = varac_stats.get(cs_upper, {})
                activity = activity_lookup.get(cs_upper, {})
                direct_contact = direct_contact_lookup.get(cs_upper, {})
                modes: List[str] = []
                if cs_upper in js8_all:
                    modes.append("JS8Call")
                if cs_upper in varac_all:
                    modes.append("VarAC")
                if cs_upper in fldigi_calls:
                    modes.append("FLDigi")
                reach_meta = self._now_reachable_meta.get(cs_upper, {}) if self._now_reachable_enabled else {}
                qsy_text = (reach_meta.get("qsy_text") or "").strip() if isinstance(reach_meta, dict) else ""
                qsy_soon = bool(reach_meta.get("qsy_soon")) if isinstance(reach_meta, dict) else False
                spotter_data = spotter_status_lookup.get(cs_upper, {})
                spotter_status_key = str(spotter_data.get("status_key") or "unknown").strip().lower()
                if spotter_status_key not in {"red", "yellow", "green", "unknown"}:
                    spotter_status_key = "unknown"
                spotter_status_label = str(spotter_data.get("status_label") or "").strip() or self._sitrep_status_label(spotter_status_key)
                spotter_status_ts = _fmt_ts(spotter_data.get("updated_utc_ts", 0))
                spotter_status_source = str(spotter_data.get("status_source") or "").strip()
                spotter_status_source_detail = str(spotter_data.get("status_source_detail") or "").strip()
                spotter_status_source_chips = str(spotter_data.get("status_source_chips") or "").strip()
                spotter_status_conflict = bool(spotter_data.get("status_conflict"))
                spotter_status_age = str(spotter_data.get("status_age") or "").strip()
                spotter_status_group = str(spotter_data.get("report_group") or "").strip()
                spotter_status_transport = str(spotter_data.get("transport_label") or "").strip()
                spotter_status_state = str(spotter_data.get("state_code") or "").strip()
                spotter_status_state_conf = str(spotter_data.get("state_confidence") or "").strip()
                spotter_status_geo_conf = str(spotter_data.get("geo_confidence") or "").strip()
                spotter_status_brevity = str(spotter_data.get("brevity_summary") or "").strip()
                spotter_map_data = spotter_map_activity.get(cs_upper, {})
                spotter_map_form = str(spotter_map_data.get("form_id") or "").strip()
                spotter_map_ts = _fmt_ts(spotter_map_data.get("utc_ts", 0))
                spotter_map_summary = str(spotter_map_data.get("summary") or "").strip()
                commstat_data = commstat_reporter_activity.get(cs_upper, {})
                uses: List[str] = []
                if (
                    spotter_map_form
                    or self._safe_float(activity.get("spotter_last_seen_ts"), 0.0) > 0.0
                    or "spotter" in str(spotter_status_source or spotter_status_source_chips).lower()
                ):
                    uses.append("Spotter")
                if commstat_data:
                    uses.append("CommStat")
                detected_capabilities = self._station_detected_capability_text(modes, uses)

                detail_lines = [
                    f"{pt.callsign}",
                    f"Name: {pt.name}" if pt.name else "",
                    f"State: {pt.state}" if pt.state else "",
                    f"Grid: {pt.grid}" if pt.grid else "",
                    f"Group: {pt.group}" if pt.group else "",
                    f"Detected: {detected_capabilities}" if detected_capabilities else "",
                ]
                if qsy_text:
                    detail_lines.append(f"Schedule: {qsy_text}")
                if spotter_map_form:
                    detail_lines.append(f"Spotter Form: {spotter_map_form}" + (f" at {spotter_map_ts}" if spotter_map_ts else ""))
                    if spotter_map_summary:
                        detail_lines.append(f"Spotter Summary: {spotter_map_summary}")
                # Filter empty lines
                detail_lines = [d for d in detail_lines if d]
                title = "\n".join(detail_lines[:4])
                tooltip_html = self._map_compact_tooltip_html(detail_lines)

                markers.append(
                    {
                        "lat": lat_off,
                        "lon": lon_off,
                        "callsign": pt.callsign,
                        "is_own_station": cs_upper == my_call,
                        "name": pt.name,
                        "state": pt.state,
                        "grid": pt.grid,
                        "group": pt.group,
                        "groups": list(pt.groups or ([pt.group] if pt.group else [])),
                        "trusted": bool(pt.trusted),
                        "fema_region": STATE_TO_FEMA_REGION.get(str(pt.state or "").strip().upper(), ""),
                        "modes": modes,
                        "app_uses": uses,
                        "detected": detected_capabilities,
                        "spotter_map_form": spotter_map_form,
                        "spotter_map_summary": spotter_map_summary,
                        "spotter_map_ts": spotter_map_ts,
                        "title": title,
                        "tooltip": tooltip_html,
                        "label": pt.callsign if self.show_callsigns else "",
                        "last_seen": _fmt_ts(activity.get("overall_last_seen_ts", 0)),
                        "last_spotter": _fmt_ts(stats.get("last_spotter", 0)),
                        "direct_snr": stats.get("direct_snr"),
                        "avg_snr_excl_my": stats.get("avg_snr_excl_my"),
                        "direct_count": stats.get("direct_count", 0),
                        "avg_snr_count": stats.get("avg_snr_count", 0),
                        "last_band": activity.get("overall_last_band", "") or "",
                        "last_contact": _fmt_ts(direct_contact.get("last_contact_ts", 0)),
                        "last_contact_band": direct_contact.get("last_contact_band", "") or "",
                        "last_contact_snr": direct_contact.get("last_contact_snr"),
                        "varac_last_seen": _fmt_ts(vstats.get("last_seen_ts", 0)),
                        "varac_last_band": vstats.get("last_band", ""),
                        "varac_avg_snr": vstats.get("avg_snr"),
                        "qsy_soon": qsy_soon,
                        "qsy_text": qsy_text,
                        "spotter_status_key": spotter_status_key,
                        "spotter_status_label": spotter_status_label,
                        "spotter_status_ts": spotter_status_ts,
                        "spotter_status_source": spotter_status_source,
                        "spotter_status_source_detail": spotter_status_source_detail,
                        "spotter_status_source_chips": spotter_status_source_chips,
                        "spotter_status_conflict": spotter_status_conflict,
                        "spotter_status_age": spotter_status_age,
                        "spotter_status_group": spotter_status_group,
                        "spotter_status_transport": spotter_status_transport,
                        "spotter_status_state": spotter_status_state,
                        "spotter_status_state_conf": spotter_status_state_conf,
                        "spotter_status_geo_conf": spotter_status_geo_conf,
                        "spotter_status_brevity": spotter_status_brevity,
                    }
                )

        if sitrep_mode:
            markers = [
                marker
                for marker in markers
                if str(marker.get("spotter_status_key") or "").strip().lower() in {"red", "yellow", "green"}
            ]

        if planning_pins_mode:
            weather_events = []
            alert_events = []
            infrastructure_events = [
                event
                for event in infrastructure_events
                if self._canonical_map_source_family(
                    event.get("source_family") or event.get("primary_source_family") or event.get("source_kind")
                )
                == "rf_pin"
            ]
            display_markers = []
            display_links = []
        elif regional_intelligence_mode:
            display_markers = []
            display_links = []
            weather_events = []
            alert_events = []
        else:
            display_markers = markers if self.show_station_markers and stations_allowed else []
            display_links = links if self.show_link_paths else []
            if (self.show_station_markers and stations_allowed) or display_links:
                own = next((point for point in self._station_points_for_map() if point.callsign.upper() == my_call), None)
                if own is not None and not any(marker.get("callsign") == my_call for marker in display_markers):
                    status = spotter_status_lookup.get(my_call, {})
                    display_markers.append({
                        "callsign": my_call, "label": my_call if self.show_callsigns else "",
                        "lat": own.lat, "lon": own.lon, "grid": own.grid,
                        "is_own_station": True, "title": "My station · " + my_call,
                        "spotter_status_key": status.get("status_key", "unknown"),
                        "spotter_status_label": status.get("status_label", "Unknown"),
                    })
            if display_links:
                display_markers = self._path_endpoint_markers(
                    display_markers, display_links, my_call, spotter_status_lookup
                )
        link_direction_markers = bool(self._map_link_direction_markers_enabled())
        self._map_link_status_detail = self._map_link_status_text(
            links_active=bool(self._links_active() and not sitrep_mode),
            show_link_paths=bool(self.show_link_paths and not sitrep_mode),
            loaded_link_count=loaded_link_count,
            display_link_count=len(display_links),
            link_selection=effective_link_selection,
            all_time_link_count=int(getattr(self, "_map_last_link_all_time_count", 0) or 0),
            recency_seconds=self._effective_path_recency_seconds() if self.show_link_paths else int(self.recency_seconds or 0),
        )
        report_event_count = len(weather_events) + len(alert_events) + len(infrastructure_events)
        if planning_pins_mode:
            self._map_marker_count = len(infrastructure_events)
        elif regional_intelligence_mode:
            states = regional_intelligence_payload.get("states", {}) if isinstance(regional_intelligence_payload, dict) else {}
            self._map_marker_count = len(states) if isinstance(states, dict) else 0
        elif observation_focus_enabled and report_focus_mode in {"hf_reports", "local_reports", "all_reports"}:
            self._map_marker_count = report_event_count
        else:
            self._map_marker_count = len(display_markers)
        self._map_link_count = len(display_links)
        self._last_map_render_input_sig = map_input_sig
        auto_fit = self._map_auto_fit_requested(
            map_input_sig,
            map_mode=self._current_map_mode_key(),
            markers=display_markers,
            links=display_links,
            weather_events=weather_events,
            alert_events=alert_events,
            infrastructure_events=infrastructure_events,
        )

        native_payload: Dict[str, object] = {
            "map_mode": self._current_map_mode_key(),
            "markers": display_markers,
            "links": display_links,
            "weather_events": weather_events,
            "alert_events": alert_events,
            "infrastructure_events": infrastructure_events,
            "link_direction_markers": link_direction_markers,
            "now_reachable_enabled": bool(self._now_reachable_enabled),
            "sitrep_state_summary": sitrep_state_summary,
            "sitrep_summary_group": sitrep_summary_group,
            "regional_intelligence": regional_intelligence_payload,
            "prop_region_scores": prop_region_scores,
            "prop_state_scores": prop_state_scores,
            "prop_overlay_enabled": bool(self.prop_overlay_enabled),
            "prop_band_colors": self._resolve_prop_band_colors() if self.prop_overlay_enabled else {},
            "show_states": bool(self.show_states),
            "show_regions": bool(self.show_regions),
            "show_grids": bool(self.show_grids),
            "show_grid_labels": bool(self.show_grid_labels),
            "show_cities": bool(effective_show_city_labels),
            "show_city_labels": bool(effective_show_city_labels),
            "city_min_pop": int(effective_city_pop_min),
            "city_labels": [
                {
                    "id": f"city:{name}:{lat:.4f}:{lon:.4f}",
                    "kind": "city",
                    "label": name,
                    "lat": lat,
                    "lon": lon,
                    "population": population,
                    "min_zoom": 4.0 if population >= 500_000 else 5.0,
                }
                for name, lat, lon, population in CITIES
                if effective_show_city_labels and population >= int(effective_city_pop_min)
            ][:250],
            "auto_fit": bool(auto_fit),
        }
        if not auto_fit:
            native_payload["view"] = view_state or self._last_map_view or {"lat": 45, "lon": -97, "zoom": 3}
        self._last_map_config = config_sig
        self._queue_native_map_projection(native_payload)
        renderer = getattr(self, "_native_map_renderer", None)
        if renderer is not None:
            try:
                self._last_map_view = renderer.view_state()
            except Exception:
                self._last_map_view = view_state or self._last_map_view or {"lat": 45, "lon": -97, "zoom": 3}
        self._maybe_start_map_ingest()
        return

    def _on_map_visible_deferred(self) -> None:
        if not self._map_visible or self._is_shutting_down:
            return
        if not self._app_active:
            self._map_dirty = True
            self._set_map_runtime_state("warming", "Preparing the map view.")
            return
        self._ensure_initial_data_loaded()
        if self._ensure_native_map_renderer() is None:
            self._enter_map_degraded(
                "Qt Location is not available for the native Map.",
                reason="native_renderer_missing",
            )
            return
        if getattr(self, "_map_page_loading", False):
            return
        if not self._map_initialized:
            # The first visible render builds one retained native projection.
            # Clear dirty first so a source update during construction can
            # schedule exactly one newer generation.
            self._map_dirty = False
            self._request_map_refresh(level="full", reason="visible_init", preserve_view=True)
            return
        if self._pending_map_payload and not self._map_dirty and not self._pending_refresh_level:
            if self._apply_pending_native_map_projection():
                self._set_map_runtime_state("ready", self._map_ready_detail_text())
            return
        if self._map_dirty:
            # A completed in-flight payload predates source changes recorded
            # while hidden. Discard it and build one current projection.
            self._pending_map_payload = None
            self._map_dirty = False
            self._request_map_refresh(level="medium", reason="visible_dirty", preserve_view=True)
            return
        if getattr(self, "_map_read_inflight", False):
            self._set_map_runtime_state("loading", "Updating map data; the last valid map remains available.")
            return
        self._set_map_runtime_state("ready", self._map_ready_detail_text())

    def _map_auto_fit_requested(
        self,
        map_input_sig: object,
        *,
        map_mode: str,
        markers: List[Dict],
        links: List[Dict],
        weather_events: Optional[List[Dict[str, object]]] = None,
        alert_events: Optional[List[Dict[str, object]]] = None,
        infrastructure_events: Optional[List[Dict[str, object]]] = None,
    ) -> bool:
        """Fit an initial focused view; retain navigation after first publication."""
        if bool(getattr(self, "_map_initialized", False) or getattr(self,"_map_user_camera_owned",False)):
            return False
        mode = str(map_mode or "").strip().lower() or "all"
        point_count = (
            len(markers or [])
            + len(links or [])
            + len(weather_events or [])
            + len(alert_events or [])
            + len(infrastructure_events or [])
        )
        if point_count <= 0:
            return False
        if mode == "mesh_nodes":
            if not bool(getattr(self, "_mesh_nodes_auto_fit_pending", False)):
                return False
            self._mesh_nodes_auto_fit_pending = False
            focused = True
        else:
            focused_modes = {"paths", "peer", "hf", "local", "reports", "regional", "propagation", "pins"}
            focused = mode in focused_modes or bool(self._map_filters_active())
        if not focused:
            return False
        sig = (
            mode,
            map_input_sig,
            point_count,
            len(markers or []),
            len(links or []),
            len(weather_events or []),
            len(alert_events or []),
            len(infrastructure_events or []),
        )
        if sig == getattr(self, "_last_map_auto_fit_sig", None):
            return False
        self._last_map_auto_fit_sig = sig
        return True

    def _push_map_payload(
        self,
        markers: List[Dict],
        links: List[Dict],
        weather_events: Optional[List[Dict[str, object]]] = None,
        alert_events: Optional[List[Dict[str, object]]] = None,
        infrastructure_events: Optional[List[Dict[str, object]]] = None,
        link_direction_markers: Optional[bool] = None,
        now_reachable_enabled: Optional[bool] = None,
        sitrep_state_summary: Optional[List[Dict[str, object]]] = None,
        sitrep_summary_group: str = "",
        regional_intelligence: Optional[Dict[str, object]] = None,
        show_cities: Optional[bool] = None,
        show_city_labels: Optional[bool] = None,
        city_min_pop: Optional[int] = None,
        auto_fit: bool = False,
    ) -> None:
        link_direction_flag = (
            bool(self._map_link_direction_markers_enabled())
            if link_direction_markers is None
            else bool(link_direction_markers)
        )
        map_mode = self._current_map_mode_key()
        show_cities_flag = bool(self.show_cities) if show_cities is None else bool(show_cities)
        show_city_labels_flag = bool(show_city_labels) if show_city_labels is not None else show_cities_flag
        city_min_pop_value = int(city_min_pop or 0)
        now_reachable_flag = (
            bool(self._now_reachable_enabled)
            if now_reachable_enabled is None
            else bool(now_reachable_enabled)
        )
        pending_payload: Dict[str, object] = {
            "map_mode": map_mode,
            "markers": list(markers),
            "links": list(links),
            "weather_events": list(weather_events or []),
            "alert_events": list(alert_events or []),
            "infrastructure_events": list(infrastructure_events or []),
            "link_direction_markers": link_direction_flag,
            "now_reachable_enabled": now_reachable_flag,
            "sitrep_state_summary": list(sitrep_state_summary or []),
            "sitrep_summary_group": str(sitrep_summary_group or ""),
            "regional_intelligence": dict(regional_intelligence or {}),
            "show_cities": show_cities_flag,
            "show_city_labels": show_city_labels_flag,
            "city_min_pop": city_min_pop_value,
            "auto_fit": bool(auto_fit),
        }
        self._queue_native_map_projection(pending_payload)

    def _queue_native_map_projection(self, pending_payload: Dict[str, object]) -> None:
        """Serialize one newest-wins projection away from the GUI thread."""
        if getattr(self, "_is_shutting_down", False):
            return
        renderer = getattr(self,"_native_map_renderer",None)
        if renderer is not None:
            pending_payload = {**pending_payload,"geography_viewport":{"width":renderer.width(),"height":renderer.height()}}
        if not getattr(self, "_map_visible", False) or not getattr(self, "_app_active", True):
            self._pending_map_payload = dict(pending_payload)
            return
        self._map_payload_generation += 1
        payload_generation = int(self._map_payload_generation)
        previous = getattr(self, "_map_projection_future", None)
        if previous is not None:
            previous.cancel()  # Drop queued obsolete work; one running + one newest snapshot.
        future = self._map_projection_worker().submit(
            self._serialize_map_projection_snapshot,
            payload_generation,
            pending_payload,
        )
        self._map_projection_future = future
        future.add_done_callback(lambda done: self._on_map_payload_future_done(payload_generation, pending_payload, done))

    def _map_projection_worker(self) -> ThreadPoolExecutor:
        if self._map_payload_executor is None:
            self._map_payload_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fio-map-projection")
        return self._map_payload_executor

    @staticmethod
    def _serialize_map_projection_snapshot(
        generation: int,
        pending_payload: Dict[str, object],
    ) -> _MapProjectionSnapshotResult:
        try:
            # Contract boundary: retained markers, observations, nodes, and
            # routes are serialized away from the UI thread. Live connection
            # health should update labels/chips and not rebuild this payload.
            from freqinout.gui.mesh_map_snapshot import enrich_mesh_map_snapshot
            enriched_payload = build_native_overlay_projection(enrich_mesh_map_snapshot(pending_payload))
            payload = json.dumps(enriched_payload)
            return _MapProjectionSnapshotResult(
                generation=generation,
                payload=payload,
                signature=str(hash(payload)),
                pending_payload=enriched_payload,
            )
        except Exception as exc:
            payload = (
                '{"map_mode": "all", "markers": [], "links": [], "weather_events": [], "alert_events": [], "infrastructure_events": [], "link_direction_markers": false, "sitrep_state_summary": [], "sitrep_summary_group": "", '
                f'"now_reachable_enabled": {str(bool(pending_payload.get("now_reachable_enabled"))).lower()}}}'
            )
            return _MapProjectionSnapshotResult(
                generation=generation,
                payload=payload,
                signature=str(hash(payload)),
                pending_payload=dict(pending_payload),
                error=str(exc),
            )

    def _on_map_payload_future_done(
        self,
        generation: int,
        pending_payload: Dict[str, object],
        future: Future,
    ) -> None:
        if future.cancelled():
            return
        try:
            result = future.result()
        except Exception as exc:
            payload = (
                '{"map_mode": "all", "markers": [], "links": [], "weather_events": [], "alert_events": [], "infrastructure_events": [], "link_direction_markers": false, "sitrep_state_summary": [], "sitrep_summary_group": "", '
                f'"now_reachable_enabled": {str(bool(pending_payload.get("now_reachable_enabled"))).lower()}}}'
            )
            result = _MapProjectionSnapshotResult(
                generation=generation,
                payload=payload,
                signature=str(hash(payload)),
                pending_payload=dict(pending_payload),
                error=str(exc),
            )
        self._map_payload_emitter.finished.emit(result)

    def _on_map_payload_snapshot_ready(self, result: object) -> None:
        if not isinstance(result, _MapProjectionSnapshotResult):
            return
        if getattr(self, "_is_shutting_down", False):
            return
        payload_generation = int(result.generation)
        emit_event = getattr(self, "_emit_map_event", None)
        if payload_generation != getattr(self, "_map_payload_generation", 0):
            if callable(emit_event):
                # Keep the established telemetry call explicit at the stale
                # generation fence so responsiveness audits can verify that a
                # discarded worker result remains observable.
                self._emit_map_event("payload_update_stale", generation=payload_generation)
            return
        if result.error:
            self._enter_map_degraded("Map data unavailable; showing the last valid map. Retry to update.",
                                     reason="snapshot_failed", exc=RuntimeError(result.error))
            return
        payload = result.payload
        sig = result.signature or str(hash(payload))
        if sig == getattr(self, "_last_map_payload_sig", None):
            if self._map_visible and self._app_active:
                self._set_map_runtime_state("ready", self._map_ready_detail_text())
            return
        pending_payload = dict(result.pending_payload)
        if pending_payload.get("map_mode") == "mesh_nodes":
            self._map_marker_count = len(pending_payload.get("markers") or [])
            self._map_link_count = len(pending_payload.get("links") or [])
            summary = pending_payload.get("mesh_summary") or {}
            self._mesh_map_summary = dict(summary)
            self._map_link_status_detail = (
                f"Known nodes: {summary.get('known_nodes', 0)} · Located: {summary.get('located_nodes', 0)}"
                f" · No location: {summary.get('unlocated_nodes', 0)} · Excluded: {summary.get('excluded_nodes', 0)} · Capped: {summary.get('omitted_nodes', 0)}"
                f" · Path segments: {self._map_link_count} ({summary.get('observed_links', 0)} observed)."
                f" Unresolved hops: {summary.get('unresolved_hops', 0)} · Ambiguous: {summary.get('ambiguous_hops', 0)}"
                f" · Omitted paths: {summary.get('omitted_links', 0) + summary.get('omitted_candidates', 0)}."
                " Advertised position age may be unknown; cached routes do not prove current reachability."
            )
        if not getattr(self, "_map_visible", False) or not getattr(self, "_app_active", True):
            # Keep only the newest completed projection. A later source update
            # marks the map dirty and wins over this snapshot on reopen.
            self._pending_map_payload = pending_payload
            if callable(emit_event):
                emit_event("payload_update_deferred_hidden", generation=payload_generation)
            return
        if StationsMapTab._apply_native_map_projection(self, pending_payload):
            self._last_map_payload_sig = sig
            set_state = getattr(self, "_set_map_runtime_state", None)
            ready_detail = getattr(self, "_map_ready_detail_text", None)
            if callable(set_state):
                set_state("ready", ready_detail() if callable(ready_detail) else "Map is ready.")
            maybe_start = getattr(self, "_maybe_start_map_ingest", None)
            if callable(maybe_start):
                maybe_start()

    def _parse_view_state(self, js_result) -> Dict[str, float]:
        """
        Normalize renderer view state from a mapping or serialized mapping.
        """
        if isinstance(js_result, dict):
            lat = js_result.get("lat")
            lon = js_result.get("lon")
            zoom = js_result.get("zoom")
        else:
            try:
                data = json.loads(js_result) if js_result else {}
            except Exception:
                data = {}
            lat = data.get("lat")
            lon = data.get("lon")
            zoom = data.get("zoom")
        if lat is None or lon is None or zoom is None:
            return self._last_map_view or {"lat": 45, "lon": -97, "zoom": 3}
        return {"lat": float(lat), "lon": float(lon), "zoom": float(zoom)}

    def _on_show_calls_changed(self, state):
        self.show_callsigns = bool(state)
        self._save_display_preferences()
        self._request_map_refresh(level="light", reason="toggle_callsigns")

    def _on_show_states_changed(self, state):
        self.show_states = bool(state)
        self._sync_city_pop_enabled()
        self._save_display_preferences()
        self._request_map_refresh(level="light", reason="toggle_states")

    def _on_show_cities_changed(self, state):
        self.show_cities = bool(state)
        self._sync_city_pop_enabled()
        self.show_city_labels = self.show_cities
        self._save_display_preferences()
        self._request_map_refresh(level="light", reason="toggle_cities")

    def _on_show_grid_labels_changed(self, state):
        # Single toggle now controls both grid lines and labels
        enabled = bool(state)
        self.show_grids = enabled
        self.show_grid_labels = enabled
        self._save_display_preferences()
        self._request_map_refresh(level="light", reason="toggle_grids")

    def _on_map_stations_changed(self, state):
        if getattr(self, "_map_layer_controls", None) is not None:
            self._on_map_layer_toggled("stations", bool(state))
            return
        self.show_station_markers = bool(state)
        self._save_display_preferences()
        self._request_map_refresh(level="medium", reason="toggle_station_markers")

    def _on_map_links_changed(self, state):
        if getattr(self, "_map_layer_controls", None) is not None:
            self._on_map_layer_toggled("paths", bool(state))
            return
        self.show_link_paths = bool(state)
        if not self.show_link_paths:
            self._set_path_layer_off()
        elif not self._links_active():
            self.link_mode = "my_station"
            self.link_value = ""
            self._sync_path_scope_combo(("my_station", ""))
            combo = getattr(self, "link_mode_combo", None)
            if combo is not None:
                try:
                    idx = combo.findData(("my_station", ""))
                    if idx < 0:
                        idx = combo.findText("My Station")
                    if idx >= 0:
                        combo.blockSignals(True)
                        combo.setCurrentIndex(idx)
                        combo.blockSignals(False)
                except Exception:
                    pass
        self._save_display_preferences()
        self._update_selected_paths_button_visual()
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason="toggle_link_paths")

    def _on_map_weather_changed(self, state):
        self.show_weather_reports = bool(state)
        self._save_display_preferences()
        self._request_map_refresh(level="medium", reason="toggle_weather")

    def _on_map_alerts_changed(self, state):
        self.show_alert_reports = bool(state)
        self._save_display_preferences()
        self._request_map_refresh(level="medium", reason="toggle_alerts")

    def _on_map_infrastructure_changed(self, state):
        self.show_infrastructure_reports = bool(state)
        self._save_display_preferences()
        self._request_map_refresh(level="medium", reason="toggle_infrastructure")

    def _on_show_regions_changed(self, state):
        self.show_regions = bool(state)
        self._save_display_preferences()
        self._request_map_refresh(level="light", reason="toggle_regions")

    def _on_city_pop_changed(self, idx: int):
        try:
            val = int(self.city_pop_combo.itemData(idx))
        except Exception:
            val = 100000
        self.city_pop_min = val
        if self.show_cities or self.show_states:
            self._request_map_refresh(level="light", reason="city_population")
        self._save_display_preferences()

    def _on_link_mode_changed(self, idx: int):
        data = self.link_mode_combo.itemData(idx) if hasattr(self, "link_mode_combo") else ("off", "")
        self.link_mode, self.link_value = self._parse_link_selection(data)
        if self._now_reachable_enabled and (self.link_mode or "").lower() == "off":
            try:
                self.link_mode_combo.blockSignals(True)
                self.link_mode_combo.setCurrentText("My Station")
                self.link_mode_combo.blockSignals(False)
                self.link_mode, self.link_value = self._parse_link_selection(self.link_mode_combo.currentData())
            except Exception:
                pass
        if (self.link_mode or "").lower() == "off":
            self.relay_target = ""
            try:
                self.relay_target_combo.blockSignals(True)
                self.relay_target_combo.setCurrentIndex(0)
                self.relay_target_combo.setEditText("")
            except Exception:
                pass
            finally:
                try:
                    self.relay_target_combo.blockSignals(False)
                except Exception:
                    pass
        self.show_link_paths = (self.link_mode or "").lower() != "off"
        links_chk = getattr(self, "map_links_chk", None)
        if links_chk is not None:
            try:
                links_chk.blockSignals(True)
                links_chk.setChecked(self.show_link_paths)
                links_chk.blockSignals(False)
            except Exception:
                pass
        self._sync_path_scope_combo((self.link_mode, self.link_value))
        self._update_map_mode_buttons()
        self._request_map_refresh(level="medium", reason="link_mode")

    def _on_map_path_scope_changed(self, idx: int) -> None:
        combo = getattr(self, "_map_path_scope_combo", None)
        data = combo.itemData(idx) if combo is not None else ("off", "")
        mode, value = self._parse_link_selection(data)
        if (mode or "off").lower() == "off":
            self._set_path_layer_off(remember=False)
            self._update_map_mode_buttons()
            self._update_map_view_status_label()
            self._update_clear_filter_buttons_visual()
            self._request_map_refresh(level="medium", reason="path_scope")
            return
        hidden_combo = getattr(self, "link_mode_combo", None)
        if hidden_combo is not None and mode in {"off", "my_station", "all"}:
            target = (mode, "")
            try:
                hidden_idx = hidden_combo.findData(target)
                if hidden_idx >= 0:
                    hidden_combo.blockSignals(True)
                    hidden_combo.setCurrentIndex(hidden_idx)
                    hidden_combo.blockSignals(False)
            except Exception:
                pass
        self.link_mode = mode or "off"
        self.link_value = value if self.link_mode in {"station", "relay_target", "group", "region"} else ""
        if self.link_mode == "relay_target":
            self.relay_target = value
            self._paths_focus_station = value
            relay_combo = getattr(self, "relay_target_combo", None)
            if relay_combo is not None:
                try:
                    relay_idx = relay_combo.findData(value)
                    relay_combo.blockSignals(True)
                    if relay_idx >= 0:
                        relay_combo.setCurrentIndex(relay_idx)
                    elif value:
                        relay_combo.setEditText(value)
                    relay_combo.blockSignals(False)
                except Exception:
                    pass
        elif self.link_mode != "station":
            self._paths_focus_station = ""
            self.relay_target = ""
        self.show_link_paths = self.link_mode != "off"
        if self.show_link_paths and getattr(self, "_map_layer_controls", None) is not None:
            self._on_map_layer_toggled("paths", True)
            return
        self._clamp_path_recency_if_needed()
        links_chk = getattr(self, "map_links_chk", None)
        if links_chk is not None:
            try:
                links_chk.blockSignals(True)
                links_chk.setChecked(self.show_link_paths)
                links_chk.blockSignals(False)
            except Exception:
                pass
        if not self.show_link_paths:
            self._restore_path_focus_if_needed()
        self._update_selected_paths_button_visual()
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason="path_scope")

    def _on_group_filter_changed(self, idx: int):
        self._clear_report_query_caches()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason="group_filter")

    def _on_region_filter_changed(self, idx: int):
        self._clear_report_query_caches()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason="region_filter")

    def _on_band_changed(self, idx: int):
        self.selected_band = self.band_combo.itemText(idx)
        self._clear_report_query_caches()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason="band_filter")

    def _on_recency_changed(self, idx: int):
        val = self.recency_combo.itemText(idx)
        mapping = dict(self._current_recency_options())
        self.recency_seconds = mapping.get(val, None)
        self._map_recency_label = val
        self._clamp_path_recency_if_needed()
        self._update_map_since_button_text(val)
        self._clear_report_query_caches()
        self._refresh_selected_paths_panel()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason="recency_filter")

    def _on_map_topic_filter_changed(self, _idx: int):
        self._clear_report_query_caches()
        self._update_clear_filter_buttons_visual()
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._request_map_refresh(level="medium", reason="topic_filter")

    def _on_map_intel_sensitivity_changed(self, _idx: int) -> None:
        self._clear_report_query_caches()
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._request_map_refresh(level="full", reason="regional_intel_sensitivity")

    def _on_advanced_map_filter_changed(self, *_args):
        if self._map_advanced_scope_filter() == "reports":
            self.show_station_markers = False
            self._set_path_layer_off()
        self._update_map_mode_buttons()
        self._clear_report_query_caches()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason="advanced_filter")

    def _on_relay_target_changed(self, text: str):
        normalized = self._relay_target_callsign_from_text(text)
        if normalized == self.relay_target:
            return
        self.relay_target = normalized
        if normalized:
            self.show_link_paths = True
            self.link_mode = "relay_target"
            self.link_value = normalized
            self._paths_focus_station = normalized
            self._sync_path_scope_combo(("relay_target", normalized))
        elif str(getattr(self, "link_mode", "") or "").strip().lower() == "relay_target":
            self._set_path_layer_off()
        self._update_selected_paths_button_visual()
        self._update_map_mode_buttons()
        self._update_map_view_status_label()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="medium", reason="relay_target")

    def _on_prop_overlay_changed(self, state):
        self.prop_overlay_enabled = bool(state)
        self._save_display_preferences()
        self._update_clear_filter_buttons_visual()
        self._request_map_refresh(level="full", reason="prop_overlay")

    def _on_prop_mode_changed(self, _idx: int) -> None:
        if self.prop_mode_combo is None:
            return
        mode = self.prop_mode_combo.currentData() or self.prop_mode_combo.currentText()
        mode = str(mode or "blended").strip().lower()
        if mode == "adaptive":
            mode = "actual"
        if mode not in {"model", "actual", "blended"}:
            mode = "blended"
        self.prop_mode = mode
        self._save_display_preferences()
        self._request_map_refresh(level="full", reason="prop_mode")

    def _on_prop_window_changed(self, _idx: int) -> None:
        if self.prop_window_combo is None:
            return
        try:
            hours = int(self.prop_window_combo.currentData())
        except Exception:
            hours = 6
        self.prop_window_hours = hours
        self._save_display_preferences()
        self._request_map_refresh(level="full", reason="prop_window")

    def _on_prop_target_type_changed(self, _idx: int) -> None:
        if self._prop_target_syncing or self.prop_target_type_combo is None or not self.settings:
            return
        target_type = (self.prop_target_type_combo.currentData() or "REGION").strip().upper()
        self._prop_target_syncing = True
        try:
            self._set_prop_target_value_options(target_type, "")
            value = (self.prop_target_value_combo.currentText() if self.prop_target_value_combo is not None else "").strip().upper()
            self.settings.set_many(
                {
                    "prop_target_type": target_type,
                    "prop_target_value": value,
                }
            )
        except Exception as e:
            log.debug("StationsMap: propagation target type change failed: %s", e)
        finally:
            self._prop_target_syncing = False
        self._request_map_refresh(level="full", reason="prop_target_type")

    def _on_prop_target_value_changed(self, text: str) -> None:
        if self._prop_target_syncing or self.prop_target_type_combo is None or not self.settings:
            return
        target_type = (self.prop_target_type_combo.currentData() or "REGION").strip().upper()
        value = (text or "").strip().upper()
        if target_type == "REGION" and value == "NATIONAL":
            value = "ALL"
        try:
            self.settings.set_many(
                {
                    "prop_target_type": target_type,
                    "prop_target_value": value,
                }
            )
        except Exception as e:
            log.debug("StationsMap: propagation target value change failed: %s", e)
        self._request_map_refresh(level="full", reason="prop_target_value")

    def _on_prop_adaptive_changed(self, state):
        self.prop_adaptive_enabled = bool(state)
        self._save_display_preferences()
        self._request_map_refresh(level="full", reason="prop_adaptive")
