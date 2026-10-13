from __future__ import annotations

"""Station-owned administration surface for the shared Managed BBS catalog.

This tab owns operator-facing BBS administration. It uses bounded catalog
interfaces and may update a configured radio's BBS adapter fields, but it does
not scan folders, reconcile sources, or publish a live directory on the UI
thread. Those operations remain owned by their background services.
"""

from collections import defaultdict
from dataclasses import dataclass
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Iterable, Optional

from PySide6.QtCore import QEvent, QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.settings_manager import SettingsManager
from freqinout.core.bbs_access_policy import effective_callsigns
from freqinout.gui.bbs_access_editor import BbsAccessEditor
from freqinout.core import varac_bbs_library_store as bbs_library_store
from freqinout.core.message_file_scanner import is_fio_bbs_helper_file_name
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.sqlite_utils import connect_sqlite
from freqinout.core.varac_bbs_library_store import (
    BbsArtifactAdminRow,
    BbsLocationRecord,
    bbs_library_db_path_from_settings,
    list_bbs_admin_rows,
    list_bbs_artifact_location_ids,
    list_bbs_locations,
    load_station_bbs_sweeper_rules,
    save_station_bbs_sweeper_rules,
    set_bbs_artifact_locations,
    upsert_bbs_location,
)
from freqinout.core.varac_bbs_sweeper import (
    BbsSweeperRule,
    bbs_sweeper_rules_to_data,
    load_bbs_sweeper_rules,
)
from freqinout.core.varac_bbs_initialization import (
    BbsInitializationResult,
    initialize_station_bbs_library,
)
from freqinout.core.varac_bbs_vault import DEFAULT_LOCATION_ID, hash_access_code
from freqinout.gui.help_registry import resolve_help_host
from freqinout.gui.theme import (
    button_height_for_font,
    button_style,
    contrast_text_for_background,
    label_style,
    resolve_theme,
    style_splitter_handles,
)


MAX_ARTIFACT_ROWS = 200
_LOCATION_ID_ROLE = Qt.UserRole
_ARTIFACT_ID_ROLE = Qt.UserRole + 1


@dataclass(frozen=True)
class _ArtifactDisplay:
    row: BbsArtifactAdminRow
    rows: tuple[BbsArtifactAdminRow, ...]
    location_names: tuple[str, ...]
    published_location_ids: tuple[str, ...]


@dataclass(frozen=True)
class _BbsCatalogSnapshot:
    profiles: tuple[dict[str, object], ...]
    locations: tuple[BbsLocationRecord, ...]
    artifact_rows: tuple[BbsArtifactAdminRow, ...]
    default_location_id: str
    allowed_callsigns: frozenset[str]
    limit_access_enabled: bool
    sweeper_rules: tuple[dict[str, object], ...]
    managed_root: str = ""


class _BbsCatalogWorker(QObject):
    """Load one bounded immutable BBS snapshot away from the GUI thread."""

    finished = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, generation: int, db_path: Path) -> None:
        super().__init__()
        self.generation = int(generation)
        self.db_path = Path(db_path)

    def run(self) -> None:
        try:
            try:
                profiles = tuple(dict(row) for row in MultiRadioStore(self.db_path).list_device_profiles())
            except Exception:
                # A catalog-only/upgrade database may not have radio tables
                # yet. That must not suppress otherwise valid BBS content.
                profiles = tuple()
            with connect_sqlite(self.db_path) as conn:
                locations = tuple(list_bbs_locations(conn, include_disabled=True))
                default_row = conn.execute(
                    "SELECT value FROM bbs_library_meta WHERE key='station_default_location_id' LIMIT 1"
                ).fetchone()
                root_row = conn.execute("SELECT value FROM bbs_library_meta WHERE key='station_managed_root' LIMIT 1").fetchone()
                permission_rows = dict(
                    conn.execute(
                        "SELECT key, value FROM bbs_library_meta "
                        "WHERE key IN ('station_allowed_callsigns', 'station_limit_access_enabled')"
                    ).fetchall()
                )
                artifact_rows = tuple(
                    list_bbs_admin_rows(conn, location_id="", limit=MAX_ARTIFACT_ROWS)
                )
                station_rules = load_station_bbs_sweeper_rules(conn)
                if station_rules is None:
                    legacy_rules: object = []
                    for profile in profiles:
                        candidate = profile.get("varac_bbs_sweeper_rules_v1", [])
                        if isinstance(candidate, str):
                            try:
                                candidate = json.loads(candidate)
                            except (TypeError, ValueError, json.JSONDecodeError):
                                candidate = []
                        if candidate:
                            legacy_rules = candidate
                            break
                    station_rules = bbs_sweeper_rules_to_data(load_bbs_sweeper_rules(legacy_rules))
                sweeper_rules = tuple(station_rules)
            allowed = frozenset(
                value.strip().upper()
                for value in re.split(
                    r"[,;\s]+",
                    str(permission_rows.get("station_allowed_callsigns", "") or ""),
                )
                if value.strip()
            )
            self.finished.emit(
                self.generation,
                _BbsCatalogSnapshot(
                    profiles=profiles,
                    locations=locations,
                    artifact_rows=artifact_rows,
                    default_location_id=str(default_row[0] or "").strip() if default_row else "",
                    allowed_callsigns=allowed,
                    limit_access_enabled=(
                        str(permission_rows.get("station_limit_access_enabled", "0") or "0") == "1"
                    ),
                    sweeper_rules=sweeper_rules,
                    managed_root=str(root_row[0] or "") if root_row else "",
                ),
            )
        except Exception as exc:
            self.failed.emit(self.generation, str(exc))


class _BbsInitializationWorker(QObject):
    """Prepare/import one Managed BBS library away from the GUI thread."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path: Path, live_dir: Path, import_existing: bool) -> None:
        super().__init__()
        self.db_path = Path(db_path)
        self.live_dir = Path(live_dir)
        self.import_existing = bool(import_existing)

    def run(self) -> None:
        try:
            self.finished.emit(
                initialize_station_bbs_library(
                    self.db_path,
                    self.live_dir,
                    import_existing_files=self.import_existing,
                )
            )
        except Exception as exc:
            self.failed.emit(str(exc))


_DETACHED_BBS_CATALOG_JOBS: dict[int, tuple[QThread, QObject]] = {}


def _release_detached_bbs_catalog_job(job_id: int) -> None:
    _DETACHED_BBS_CATALOG_JOBS.pop(int(job_id), None)


def _access_text(value: object) -> str:
    text = str(value or "public").strip().replace("_", " ")
    return text.title() if text else "Public"


def _retention_text(mode: object, days: object) -> str:
    normalized = str(mode or "global_default").strip().lower()
    try:
        day_count = int(days or 0)
    except (TypeError, ValueError):
        day_count = 0
    if normalized == "expire_after_days":
        return f"Expires after {day_count} day{'s' if day_count != 1 else ''}"
    if normalized == "manual":
        return "Keep until manually removed"
    return "Station default retention"


def _health_text(row: BbsArtifactAdminRow) -> str:
    state = str(row.publication_state or "").strip().lower()
    if state == "published":
        return "Published"
    if state == "source_missing":
        return "Missing source"
    if state == "deleted":
        return "Deleted source"
    if state == "retention_expired":
        return "Retention expired"
    if state == "operator_disabled":
        return "Operator disabled"
    return state.replace("_", " ").title() or "Not published"


def _byte_text(value: object) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:g} {unit}" if unit == "B" else f"{size / 1:g} {unit}"
        size /= 1024
    return f"{size:g} GB"


def _modified_detail(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "modified time unknown"
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        utc_value = parsed.astimezone(dt.timezone.utc)
        local_value = parsed.astimezone()
        return f"Local {local_value.strftime('%Y-%m-%d %H:%M:%S %Z')}; UTC {utc_value.strftime('%Y-%m-%d %H:%M:%S Z')}"
    except ValueError:
        return f"modified {raw}"


def _expires_text(row: BbsArtifactAdminRow, *, now_utc: dt.datetime | None = None) -> str:
    """Return a short operator-facing expiry without changing persisted state."""

    state = str(row.publication_state or "").strip().lower()
    if state == "operator_disabled":
        return "Removed"
    if state == "retention_expired":
        return "Expired"
    if str(getattr(row, "retention_class", "") or "").strip().lower() == "keep":
        return "Never"
    if str(row.retention_mode or "").strip().lower() == "manual":
        return "Never"
    raw = str(row.expires_utc or "").strip()
    if not raw:
        return "Not set"
    try:
        expires = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "Unknown"
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    now = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    remaining = (expires.astimezone(dt.timezone.utc) - now).total_seconds()
    if remaining <= 0:
        return "Expired"
    if remaining <= 86400:
        return "Today"
    return f"{int(math.ceil(remaining / 86400.0))}d"


class StationBbsTab(QWidget):
    """A bounded, database-backed catalog view for station Managed BBS work."""

    def __init__(self, parent: Optional[QWidget] = None, *, settings: object | None = None) -> None:
        super().__init__(parent)
        self.settings = settings if settings is not None else SettingsManager()
        self._selected_location_id = ""
        self._publishing_location_id = ""
        self._visitor_location_id = ""
        self._locations_by_id: dict[str, BbsLocationRecord] = {}
        self._display_rows: dict[str, _ArtifactDisplay] = {}
        self._loading = False
        self._pending_publication: dict[tuple[str, str], bool] = {}
        self._artifact_filter = "in_bbs"
        self._compact_layout = False
        self._responsive_signature: tuple[int, int, int] | None = None
        self._editing_location_id = ""
        self._location_editor_loading = False
        self._station_allowed_callsigns: set[str] = set()
        self._station_limit_access_enabled = False
        self._catalog_rows: tuple[BbsArtifactAdminRow, ...] = ()
        self._catalog_generation = 0
        self._catalog_refresh_pending = False
        self._catalog_thread: QThread | None = None
        self._catalog_worker: _BbsCatalogWorker | None = None
        self._initialization_thread: QThread | None = None
        self._initialization_worker: _BbsInitializationWorker | None = None
        self._initialization_profile_id = 0
        self._initialization_live_dir = ""
        self._catalog_shutdown = False
        self._pending_location_status = ""
        self._pending_publication_status = ""
        self._pending_radio_status = ""
        self._automation_rules: list[dict[str, object]] = []
        self._automation_saved_rules: list[dict[str, object]] = []
        self._automation_selected_rule_id = ""
        self._automation_dirty = False
        self._automation_loading = False
        self._build_ui()
        self.refresh_catalog()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        self.bbs_title = QLabel("CommsPlan BBS")
        self.bbs_title.setObjectName("stationBbsTitle")
        self.bbs_title.setAccessibleName("CommsPlan BBS")
        title_row.addWidget(self.bbs_title)
        title_row.addStretch(1)
        self.help_btn = QPushButton("Help")
        self.help_btn.setToolTip("Open the Managed BBS workflow help.")
        self.help_btn.setAccessibleName("Managed BBS help")
        self.help_btn.clicked.connect(self._open_context_help)
        title_row.addWidget(self.help_btn)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Refresh the bounded Managed BBS catalog view.")
        self.refresh_btn.setAccessibleName("Refresh Managed BBS catalog")
        self.refresh_btn.clicked.connect(self.refresh_catalog)
        title_row.addWidget(self.refresh_btn)
        layout.addLayout(title_row)

        self.why_label = QLabel(
            "One station BBS: configure the radios that serve it, define locations and access, then publish files. "
            "CommsPlan projects the same catalog to each enabled VarAC radio without deleting source files."
        )
        self.why_label.setObjectName("stationBbsWhy")
        self.why_label.setWordWrap(True)
        self.why_label.setAccessibleName("Managed BBS explanation")
        layout.addWidget(self.why_label)

        self.summary_label = QLabel("Loading Managed BBS catalog…")
        self.summary_label.setWordWrap(True)
        self.summary_label.setObjectName("stationBbsSummary")
        layout.addWidget(self.summary_label)

        self.service_tabs = QTabWidget(self)
        self.service_tabs.setObjectName("stationBbsServiceTabs")
        self.service_tabs.setAccessibleName("BBS service workspace")
        self.service_tabs.setUsesScrollButtons(True)
        self.service_tabs.setDocumentMode(True)
        layout.addWidget(self.service_tabs, 1)

        self.radio_service_page = QWidget(self.service_tabs)
        radio_layout = QVBoxLayout(self.radio_service_page)
        radio_layout.setContentsMargins(10, 10, 10, 10)
        radio_layout.setSpacing(8)
        self.radio_title = QLabel("Radio Service")
        radio_layout.addWidget(self.radio_title)
        radio_copy = QLabel(
            "Choose the VarAC radios that serve this BBS. Each radio has its own live folder but publishes the same "
            "station catalog. VarAC launcher, inbox, and outbox paths remain in Radio Settings."
        )
        radio_copy.setWordWrap(True)
        radio_copy.setAccessibleName("Radio Service summary")
        radio_layout.addWidget(radio_copy)
        radio_selection_row = QHBoxLayout()
        radio_selection_row.setContentsMargins(0, 0, 0, 0)
        radio_selection_row.addWidget(QLabel("Serving radio"))
        self.radio_service_selector = QComboBox(self.radio_service_page)
        self.radio_service_selector.setAccessibleName("BBS radio selector")
        self.radio_service_selector.setToolTip("Choose a configured radio to review its BBS service state and settings.")
        self.radio_service_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.radio_service_selector.setMinimumContentsLength(28)
        self.radio_service_selector.currentIndexChanged.connect(self._on_radio_selector_changed)
        radio_selection_row.addWidget(self.radio_service_selector, 1)
        radio_layout.addLayout(radio_selection_row)
        self.radio_selector_summary = QLabel("Select a configured radio to review its service state.")
        self.radio_selector_summary.setWordWrap(True)
        self.radio_selector_summary.setAccessibleName("Selected radio service summary")
        radio_layout.addWidget(self.radio_selector_summary)
        radio_editor = QGroupBox("Selected radio BBS service", self.radio_service_page)
        radio_editor_layout = QGridLayout(radio_editor)
        radio_editor_layout.setContentsMargins(8, 8, 8, 8)
        radio_editor_layout.setHorizontalSpacing(8)
        radio_editor_layout.setVerticalSpacing(6)
        service_options = QGroupBox("Service options", radio_editor)
        service_options.setAccessibleName("Selected radio BBS service options")
        service_options_layout = QVBoxLayout(service_options)
        service_options_layout.setContentsMargins(8, 6, 8, 6)
        service_options_layout.setSpacing(4)
        self.radio_service_enabled_chk = QCheckBox("Enable VarAC BBS")
        self.radio_publish_enabled_chk = QCheckBox("Publish the CommsPlan catalog")
        self.radio_announce_enabled_chk = QCheckBox("Announce BBS")
        service_options_layout.addWidget(self.radio_service_enabled_chk)
        service_options_layout.addWidget(self.radio_publish_enabled_chk)
        service_options_layout.addWidget(self.radio_announce_enabled_chk)
        radio_editor_layout.addWidget(service_options, 0, 0, 1, 3)
        radio_editor_layout.addWidget(QLabel("Live BBS folder"), 1, 0)
        self.radio_live_dir_edit = QLineEdit()
        self.radio_live_dir_edit.setPlaceholderText("VarAC live BBS folder for the selected radio")
        self.radio_live_dir_edit.setAccessibleName("Selected radio live BBS folder")
        radio_editor_layout.addWidget(self.radio_live_dir_edit, 1, 1)
        self.radio_live_dir_browse_btn = QPushButton("Browse")
        self.radio_live_dir_browse_btn.clicked.connect(self._browse_radio_live_dir)
        radio_editor_layout.addWidget(self.radio_live_dir_browse_btn, 1, 2)
        self.radio_native_paths_toggle = QToolButton(radio_editor)
        self.radio_native_paths_toggle.setText("Managed in Radio Settings")
        self.radio_native_paths_toggle.setCheckable(True)
        self.radio_native_paths_toggle.setToolTip("Show the selected radio's managed VarAC install and outbox paths.")
        self.radio_native_paths_toggle.setAccessibleName("Show managed VarAC paths")
        radio_editor_layout.addWidget(self.radio_native_paths_toggle, 2, 0, 1, 3)
        self.radio_native_paths_label = QLabel("Select a configured radio.")
        self.radio_native_paths_label.setWordWrap(True)
        self.radio_native_paths_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.radio_native_paths_label.setAccessibleName("Selected radio native VarAC path summary")
        self.radio_native_paths_label.setVisible(False)
        self.radio_native_paths_toggle.toggled.connect(self.radio_native_paths_label.setVisible)
        radio_editor_layout.addWidget(self.radio_native_paths_label, 3, 0, 1, 3)
        self.radio_service_save_btn = QPushButton("Save Radio Service")
        self.radio_service_save_btn.clicked.connect(self._save_selected_radio_service)
        self.radio_initialize_btn = QPushButton("Initialize BBS…")
        self.radio_initialize_btn.setToolTip(
            "Create the station Managed BBS library beside this radio's live BBS folder "
            "and optionally import its current files."
        )
        self.radio_initialize_btn.setAccessibleName("Initialize station Managed BBS library")
        self.radio_initialize_btn.clicked.connect(self._initialize_selected_radio_bbs)
        self.radio_settings_btn = QPushButton("Open Radio Settings")
        self.radio_settings_btn.clicked.connect(self._open_radio_settings)
        radio_action_row = QHBoxLayout()
        radio_action_row.setContentsMargins(0, 0, 0, 0)
        radio_action_row.addWidget(self.radio_service_save_btn)
        radio_action_row.addWidget(self.radio_initialize_btn)
        radio_action_row.addWidget(self.radio_settings_btn)
        radio_action_row.addStretch(1)
        radio_editor_layout.addLayout(radio_action_row, 4, 0, 1, 3)
        self.radio_service_status = QLabel("Select a configured VarAC radio to review its BBS service.")
        self.radio_service_status.setWordWrap(True)
        self.radio_service_status.setAccessibleName("Radio Service status")
        radio_editor_layout.addWidget(self.radio_service_status, 5, 0, 1, 3)
        from freqinout.gui.bbs_varac_sync_panel import BbsVaracSyncPanel
        self.varac_sync_panel = BbsVaracSyncPanel(
            self, db_path=bbs_library_db_path_from_settings(self.settings),
            radio_id=self._selected_radio_service_id, radio_dirty=self._radio_service_is_dirty,
        )
        self.varac_sync_panel.saved.connect(self.refresh_catalog)
        radio_editor_layout.addWidget(self.varac_sync_panel, 6, 0, 1, 3)
        radio_editor_layout.setColumnStretch(1, 1)
        self.radio_editor_scroll = QScrollArea(self.radio_service_page)
        self.radio_editor_scroll.setWidgetResizable(True)
        self.radio_editor_scroll.setFrameShape(QFrame.NoFrame)
        self.radio_editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.radio_editor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.radio_editor_scroll.setWidget(radio_editor)
        radio_layout.addWidget(self.radio_editor_scroll, 1)
        self._radio_profiles_by_id: dict[int, dict[str, object]] = {}
        self._radio_service_loading = False

        self.locations_page = QWidget(self.service_tabs)
        locations_layout = QVBoxLayout(self.locations_page)
        locations_layout.setContentsMargins(0, 0, 0, 0)
        self.publishing_page = QWidget(self.service_tabs)
        publishing_layout = QVBoxLayout(self.publishing_page)
        publishing_layout.setContentsMargins(0, 0, 0, 0)
        publishing_scope_row = QHBoxLayout()
        publishing_scope_row.setContentsMargins(8, 8, 8, 0)
        publishing_scope_row.addWidget(QLabel("Publish in"))
        self.publishing_chips_layout = self._add_chip_strip(publishing_scope_row, self.publishing_page, "Publishing location")
        self.manage_locations_btn = QPushButton("Manage Locations")
        self.manage_locations_btn.clicked.connect(
            lambda: self.service_tabs.setCurrentWidget(self.locations_page)
        )
        publishing_scope_row.addWidget(self.manage_locations_btn)
        publishing_layout.addLayout(publishing_scope_row)
        publishing_filter_row = QHBoxLayout()
        publishing_filter_row.setContentsMargins(8, 0, 8, 0)
        publishing_filter_row.addWidget(QLabel("Show"))
        self.artifact_filter_combo = QComboBox(self.publishing_page)
        self.artifact_filter_combo.setAccessibleName("Publishing file filter")
        self.artifact_filter_combo.addItem("In BBS", "in_bbs")
        self.artifact_filter_combo.addItem("Expired", "expired")
        self.artifact_filter_combo.addItem("Removed", "removed")
        self.artifact_filter_combo.addItem("All", "all")
        self.artifact_filter_combo.currentIndexChanged.connect(self._on_artifact_filter_changed)
        publishing_filter_row.addWidget(self.artifact_filter_combo)
        publishing_filter_row.addStretch(1)
        self.publication_status_label = QLabel("No pending changes.")
        self.publication_status_label.setAccessibleName("Publishing change status")
        publishing_filter_row.addWidget(self.publication_status_label)
        publishing_layout.addLayout(publishing_filter_row)

        self.visitor_preview_page = QWidget(self.service_tabs)
        visitor_layout = QVBoxLayout(self.visitor_preview_page)
        visitor_layout.setContentsMargins(10, 10, 10, 10)
        visitor_layout.setSpacing(7)
        self.visitor_title = QLabel("Visitor Preview")
        visitor_layout.addWidget(self.visitor_title)
        visitor_copy = QLabel(
            "Read-only caller-facing view. It applies enabled locations and visibility rules, but does not validate or reveal access codes."
        )
        visitor_copy.setWordWrap(True)
        visitor_copy.setAccessibleName("Visitor Preview explanation")
        visitor_layout.addWidget(visitor_copy)
        visitor_row = QHBoxLayout()
        visitor_row.setContentsMargins(0, 0, 0, 0)
        visitor_row.addWidget(QLabel("Visitor callsign"))
        self.visitor_callsign_edit = QLineEdit()
        self.visitor_callsign_edit.setPlaceholderText("Optional visitor callsign")
        self.visitor_callsign_edit.setAccessibleName("Visitor preview callsign")
        self.visitor_callsign_edit.setMaximumWidth(240)
        self.visitor_callsign_edit.textChanged.connect(self._refresh_visitor_preview)
        visitor_row.addWidget(self.visitor_callsign_edit)
        visitor_row.addStretch(1)
        visitor_layout.addLayout(visitor_row)
        self.visitor_preview_status = QLabel("Loading visitor preview…")
        self.visitor_preview_status.setWordWrap(True)
        self.visitor_preview_status.setAccessibleName("Visitor Preview status")
        visitor_layout.addWidget(self.visitor_preview_status)
        visitor_chip_row = QHBoxLayout()
        visitor_chip_row.addWidget(QLabel("Location"))
        self.visitor_chips_layout = self._add_chip_strip(visitor_chip_row, self.visitor_preview_page, "Visitor preview location")
        visitor_layout.addLayout(visitor_chip_row)
        self.visitor_policy_label = QLabel("Select a visible location to review its access policy.")
        self.visitor_policy_label.setWordWrap(True)
        self.visitor_policy_label.setAccessibleName("Visitor selected location policy")
        visitor_layout.addWidget(self.visitor_policy_label)
        self.visitor_artifact_table = QTableWidget(0, 4, self.visitor_preview_page)
        self.visitor_artifact_table.setHorizontalHeaderLabels(["File", "Location", "Access", "Health"])
        self.visitor_artifact_table.setAccessibleName("Visitor visible BBS artifacts")
        self.visitor_artifact_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.visitor_artifact_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.visitor_artifact_table.verticalHeader().setVisible(False)
        visitor_header = self.visitor_artifact_table.horizontalHeader()
        visitor_header.setStretchLastSection(False)
        visitor_header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            visitor_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        visitor_layout.addWidget(self.visitor_artifact_table, 1)

        self.helpers_page = QWidget(self.service_tabs)
        helpers_layout = QVBoxLayout(self.helpers_page)
        helpers_layout.setContentsMargins(10, 10, 10, 10)
        helpers_layout.setSpacing(7)
        self.helpers_title = QLabel("Visitor Helpers")
        helpers_layout.addWidget(self.helpers_title)
        helpers_copy = QLabel(
            "Generated visitor helper files are system-owned support material. They are intentionally excluded from Publishing, "
            "so changing catalog membership can never replace or delete them."
        )
        helpers_copy.setWordWrap(True)
        helpers_copy.setAccessibleName("Visitor Helpers explanation")
        helpers_layout.addWidget(helpers_copy)
        self.helpers_status = QLabel("No generated helper files are present in the bounded catalog view.")
        self.helpers_status.setWordWrap(True)
        self.helpers_status.setAccessibleName("Visitor Helpers status")
        helpers_layout.addWidget(self.helpers_status)
        self.helpers_table = QTableWidget(0, 5, self.helpers_page)
        self.helpers_table.setHorizontalHeaderLabels(
            ["Helper", "Purpose", "Locations", "Age", "Health"]
        )
        self.helpers_table.setAccessibleName("Visitor BBS helpers")
        self.helpers_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.helpers_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.helpers_table.verticalHeader().setVisible(False)
        helper_header = self.helpers_table.horizontalHeader()
        helper_header.setStretchLastSection(False)
        helper_header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3, 4):
            helper_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        helpers_layout.addWidget(self.helpers_table, 1)

        self.automation_page = QWidget(self.service_tabs)
        automation_layout = QVBoxLayout(self.automation_page)
        automation_layout.setContentsMargins(0, 0, 0, 0)
        self.automation_scroll = QScrollArea(self.automation_page)
        self.automation_scroll.setWidgetResizable(True)
        self.automation_scroll.setFrameShape(QFrame.NoFrame)
        self.automation_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        automation_layout.addWidget(self.automation_scroll, 1)
        automation_body = QWidget(self.automation_scroll)
        automation_body_layout = QVBoxLayout(automation_body)
        automation_body_layout.setContentsMargins(10, 10, 10, 10)
        automation_body_layout.setSpacing(8)
        self.automation_title = QLabel("BBS Automation Rules")
        automation_body_layout.addWidget(self.automation_title)
        automation_copy = QLabel(
            "Automatically copy newly received VarAC, FLMsg, or FLAmp files into selected Managed BBS locations. "
            "A rule runs only when it is enabled, has a destination, and includes a sender or text match."
        )
        automation_copy.setWordWrap(True)
        automation_copy.setAccessibleName("BBS automation explanation")
        automation_body_layout.addWidget(automation_copy)
        self.automation_status_label = QLabel("Loading BBS automation rules…")
        self.automation_status_label.setWordWrap(True)
        self.automation_status_label.setAccessibleName("BBS automation status")
        automation_body_layout.addWidget(self.automation_status_label)

        automation_editor = QGroupBox("Rule editor", automation_body)
        editor_grid = QGridLayout(automation_editor)
        editor_grid.setContentsMargins(8, 8, 8, 8)
        editor_grid.setHorizontalSpacing(8)
        editor_grid.setVerticalSpacing(6)
        self.automation_enabled_chk = QCheckBox("Use this rule")
        editor_grid.addWidget(self.automation_enabled_chk, 0, 0)
        self.automation_name_edit = QLineEdit()
        self.automation_name_edit.setPlaceholderText("Rule name")
        self.automation_name_edit.setAccessibleName("BBS automation rule name")
        editor_grid.addWidget(self.automation_name_edit, 0, 1, 1, 3)
        editor_grid.addWidget(QLabel("Sources"), 1, 0)
        source_row = QHBoxLayout()
        self.automation_source_varac_chk = QCheckBox("VarAC incoming")
        self.automation_source_flmsg_chk = QCheckBox("FLMsg")
        self.automation_source_flamp_chk = QCheckBox("FLAmp")
        for checkbox in (
            self.automation_source_varac_chk,
            self.automation_source_flmsg_chk,
            self.automation_source_flamp_chk,
        ):
            source_row.addWidget(checkbox)
        source_row.addStretch(1)
        editor_grid.addLayout(source_row, 1, 1, 1, 3)
        editor_grid.addWidget(QLabel("From"), 2, 0)
        self.automation_from_calls_edit = QLineEdit()
        self.automation_from_calls_edit.setPlaceholderText("Optional callsigns, comma-separated")
        self.automation_from_calls_edit.setAccessibleName("BBS automation sender callsigns")
        editor_grid.addWidget(self.automation_from_calls_edit, 2, 1, 1, 3)
        editor_grid.addWidget(QLabel("Text contains"), 3, 0)
        self.automation_subject_contains_edit = QLineEdit()
        self.automation_subject_contains_edit.setPlaceholderText("Optional filename, subject, or message terms")
        self.automation_subject_contains_edit.setAccessibleName("BBS automation text terms")
        editor_grid.addWidget(self.automation_subject_contains_edit, 3, 1, 1, 3)
        editor_grid.addWidget(QLabel("Copy to"), 4, 0, Qt.AlignTop)
        self.automation_targets_list = QListWidget(automation_editor)
        self.automation_targets_list.setAccessibleName("BBS automation destination locations")
        self.automation_targets_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.automation_targets_list.setMaximumHeight(
            max(90, self.fontMetrics().lineSpacing() * 5 + 12)
        )
        editor_grid.addWidget(self.automation_targets_list, 4, 1, 1, 2)
        self.automation_copy_mode_combo = QComboBox()
        self.automation_copy_mode_combo.addItem("Copy each new arrival", "copy")
        self.automation_copy_mode_combo.addItem("Copy once per filename", "copy_once")
        self.automation_copy_mode_combo.setAccessibleName("BBS automation copy behavior")
        editor_grid.addWidget(self.automation_copy_mode_combo, 4, 3, Qt.AlignTop)
        editor_grid.setColumnStretch(1, 1)
        editor_grid.setColumnStretch(2, 1)
        automation_body_layout.addWidget(automation_editor)

        edit_actions = QHBoxLayout()
        self.automation_new_btn = QPushButton("New Rule")
        self.automation_new_btn.clicked.connect(self._new_automation_rule)
        edit_actions.addWidget(self.automation_new_btn)
        self.automation_add_update_btn = QPushButton("Add Rule")
        self.automation_add_update_btn.clicked.connect(self._add_or_update_automation_rule)
        edit_actions.addWidget(self.automation_add_update_btn)
        self.automation_delete_btn = QPushButton("Delete Rule")
        self.automation_delete_btn.clicked.connect(self._delete_automation_rule)
        edit_actions.addWidget(self.automation_delete_btn)
        edit_actions.addStretch(1)
        automation_body_layout.addLayout(edit_actions)

        self.automation_rules_table = QTableWidget(0, 6, automation_body)
        self.automation_rules_table.setHorizontalHeaderLabels(
            ["Use", "Rule", "Sources", "Match", "Copy To", "Behavior"]
        )
        self.automation_rules_table.setAccessibleName("BBS automation rules")
        self.automation_rules_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.automation_rules_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.automation_rules_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.automation_rules_table.verticalHeader().setVisible(False)
        automation_header = self.automation_rules_table.horizontalHeader()
        automation_header.setStretchLastSection(False)
        automation_header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (0, 2, 3, 4, 5):
            automation_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.automation_rules_table.setMinimumHeight(
            max(120, self.fontMetrics().lineSpacing() * 5 + 28)
        )
        self.automation_rules_table.itemSelectionChanged.connect(self._load_selected_automation_rule)
        automation_body_layout.addWidget(self.automation_rules_table, 1)
        automation_save_row = QHBoxLayout()
        self.automation_save_btn = QPushButton("Save Automation Rules")
        self.automation_save_btn.clicked.connect(self._save_automation_rules)
        automation_save_row.addWidget(self.automation_save_btn)
        self.automation_revert_btn = QPushButton("Revert")
        self.automation_revert_btn.clicked.connect(self._revert_automation_rules)
        automation_save_row.addWidget(self.automation_revert_btn)
        automation_save_row.addStretch(1)
        automation_body_layout.addLayout(automation_save_row)
        self.automation_scroll.setWidget(automation_body)
        self._clear_automation_editor()

        self.splitter = QSplitter(Qt.Horizontal, self.locations_page)
        self.splitter.setObjectName("stationBbsCatalogSplitter")
        self.splitter.setChildrenCollapsible(False)
        style_splitter_handles(self.splitter, resolve_theme(self.settings), width=12)
        locations_layout.addWidget(self.splitter, 1)

        tree_panel = QWidget(self.splitter)
        tree_layout = QVBoxLayout(tree_panel)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(5)
        tree_title_row = QHBoxLayout()
        tree_title = QLabel("Locations")
        tree_title.setStyleSheet(label_style("text", resolve_theme(self.settings), weight=700))
        tree_title_row.addWidget(tree_title)
        tree_title_row.addStretch(1)
        self.location_add_btn = QPushButton("Add")
        self.location_add_btn.setToolTip("Add a station-owned Managed BBS location. This never creates a folder.")
        self.location_add_btn.setAccessibleName("Add Managed BBS location")
        self.location_add_btn.clicked.connect(self._begin_new_location)
        tree_title_row.addWidget(self.location_add_btn)
        self.location_edit_btn = QToolButton(tree_panel)
        self.location_edit_btn.setText("Edit")
        self.location_edit_btn.setCheckable(True)
        self.location_edit_btn.setToolTip("Show or hide the selected location editor.")
        self.location_edit_btn.setAccessibleName("Show location editor")
        self.location_edit_btn.toggled.connect(self._set_location_editor_visible)
        tree_title_row.addWidget(self.location_edit_btn)
        tree_layout.addLayout(tree_title_row)
        self.location_compact_selector = QComboBox(tree_panel)
        self.location_compact_selector.setAccessibleName("Managed BBS location selector")
        self.location_compact_selector.setToolTip("Choose a location to review its policy.")
        self.location_compact_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.location_compact_selector.setMinimumContentsLength(24)
        self.location_compact_selector.currentIndexChanged.connect(self._on_location_compact_selector_changed)
        self.location_compact_selector.setVisible(False)
        tree_layout.addWidget(self.location_compact_selector)
        self.location_tree = QTreeWidget(tree_panel)
        self.location_tree.setObjectName("stationBbsLocationTree")
        self.location_tree.setHeaderHidden(True)
        self.location_tree.setAccessibleName("Managed BBS locations")
        self.location_tree.setToolTip("Select a location to review and change its artifact memberships.")
        self.location_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.location_tree.setTextElideMode(Qt.ElideRight)
        self.location_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.location_tree.itemSelectionChanged.connect(self._on_location_selection_changed)
        tree_layout.addWidget(self.location_tree, 1)

        self.location_editor = QFrame(tree_panel)
        self.location_editor.setObjectName("stationBbsLocationEditor")
        self.location_editor.setVisible(False)
        editor_layout = QVBoxLayout(self.location_editor)
        editor_layout.setContentsMargins(8, 8, 8, 8)
        editor_layout.setSpacing(5)
        editor_title = QLabel("Location editor")
        editor_title.setStyleSheet(label_style("text", resolve_theme(self.settings), weight=700))
        editor_layout.addWidget(editor_title)
        editor_grid = QGridLayout()
        editor_grid.setContentsMargins(0, 0, 0, 0)
        editor_grid.setHorizontalSpacing(6)
        editor_grid.setVerticalSpacing(4)
        editor_grid.addWidget(QLabel("Name"), 0, 0)
        self.location_name_edit = QLineEdit()
        self.location_name_edit.setAccessibleName("Location name")
        self.location_name_edit.setPlaceholderText("Example: Logistics")
        editor_grid.addWidget(self.location_name_edit, 0, 1)
        editor_grid.addWidget(QLabel("Source folder"), 1, 0)
        self.location_source_edit = QLineEdit()
        self.location_source_edit.setAccessibleName("Location source folder")
        self.location_source_edit.setPlaceholderText("Existing folder path; CommsPlan will not create it here")
        self.location_source_edit.setToolTip("Records a source folder path only. Saving never creates, scans, or changes that folder.")
        editor_grid.addWidget(self.location_source_edit, 1, 1)
        editor_grid.addWidget(QLabel("Parent"), 2, 0)
        self.location_parent_combo = QComboBox()
        self.location_parent_combo.setAccessibleName("Optional parent location")
        editor_grid.addWidget(self.location_parent_combo, 2, 1)
        editor_grid.addWidget(QLabel("Access"), 3, 0)
        self.location_access_combo = QComboBox()
        self.location_access_combo.setAccessibleName("Location access rule")
        self.location_access_combo.addItem("Public", "Public")
        self.location_access_combo.addItem("Allowed callsigns", "Allowed callsigns only")
        self.location_access_combo.addItem("Access code", "Access code required")
        self.location_access_combo.addItem("Allowed callsigns + code", "Allowed callsigns + access code")
        self.location_access_combo.currentIndexChanged.connect(self._update_location_access_state)
        editor_grid.addWidget(self.location_access_combo, 3, 1)
        self.location_callsigns_label = QLabel("Allowed callsigns")
        editor_grid.addWidget(self.location_callsigns_label, 4, 0)
        settings_db_path = getattr(self.settings, "db_path", None)
        self.location_access_editor = BbsAccessEditor(
            self,
            operator_db_path=Path(settings_db_path).with_name("freqinout_nets.db") if settings_db_path else None,
        )
        self.location_callsigns_edit = self.location_access_editor.manual_edit
        self.location_callsigns_edit.setAccessibleName("Location allowed callsigns")
        self.location_callsigns_edit.setPlaceholderText("Callsign; add more with commas")
        editor_grid.addWidget(self.location_access_editor, 4, 1)
        self.location_code_label = QLabel("Access code")
        editor_grid.addWidget(self.location_code_label, 5, 0)
        code_row = QHBoxLayout()
        code_row.setContentsMargins(0, 0, 0, 0)
        self.location_code_edit = QLineEdit()
        self.location_code_edit.setEchoMode(QLineEdit.Password)
        self.location_code_edit.setAccessibleName("New location access code")
        self.location_code_edit.setPlaceholderText("Leave blank to keep the saved code")
        code_row.addWidget(self.location_code_edit, 1)
        self.location_code_confirm_edit = QLineEdit()
        self.location_code_confirm_edit.setEchoMode(QLineEdit.Password)
        self.location_code_confirm_edit.setAccessibleName("Confirm new location access code")
        self.location_code_confirm_edit.setPlaceholderText("Confirm new code")
        code_row.addWidget(self.location_code_confirm_edit, 1)
        editor_grid.addLayout(code_row, 5, 1)
        editor_grid.addWidget(QLabel("Retention"), 6, 0)
        retention_row = QHBoxLayout()
        retention_row.setContentsMargins(0, 0, 0, 0)
        self.location_retention_combo = QComboBox()
        self.location_retention_combo.setAccessibleName("Location retention mode")
        self.location_retention_combo.addItem("Station default", "global_default")
        self.location_retention_combo.addItem("Keep until removed", "manual")
        self.location_retention_combo.addItem("Expire after days", "expire_after_days")
        self.location_retention_combo.currentIndexChanged.connect(self._update_location_retention_days_state)
        retention_row.addWidget(self.location_retention_combo, 1)
        self.location_retention_days_spin = QSpinBox()
        self.location_retention_days_spin.setRange(1, 3650)
        self.location_retention_days_spin.setValue(14)
        self.location_retention_days_spin.setSuffix(" days")
        self.location_retention_days_spin.setAccessibleName("Retention days")
        retention_row.addWidget(self.location_retention_days_spin)
        editor_grid.addLayout(retention_row, 6, 1)
        self.location_enabled_chk = QCheckBox("Enabled")
        self.location_enabled_chk.setAccessibleName("Location enabled")
        editor_grid.addWidget(self.location_enabled_chk, 7, 1)
        editor_layout.addLayout(editor_grid)
        editor_actions = QHBoxLayout()
        editor_actions.setContentsMargins(0, 0, 0, 0)
        self.location_save_btn = QPushButton("Save Location")
        self.location_save_btn.setToolTip("Save this station catalog location. No folder is created or scanned.")
        self.location_save_btn.clicked.connect(self._save_location)
        editor_actions.addWidget(self.location_save_btn)
        self.location_disable_btn = QPushButton("Disable")
        self.location_disable_btn.setToolTip("Safely disable this location without deleting its catalog history or source files.")
        self.location_disable_btn.clicked.connect(self._disable_location)
        editor_actions.addWidget(self.location_disable_btn)
        self.location_cancel_btn = QPushButton("Cancel")
        self.location_cancel_btn.setToolTip("Close the location editor without saving changes.")
        self.location_cancel_btn.setAccessibleName("Cancel location editing")
        self.location_cancel_btn.clicked.connect(self._cancel_location_edit)
        editor_actions.addWidget(self.location_cancel_btn)
        editor_actions.addStretch(1)
        editor_layout.addLayout(editor_actions)
        self.location_editor_status = QLabel("")
        self.location_editor_status.setWordWrap(True)
        self.location_editor_status.setAccessibleName("Location editor status")
        editor_layout.addWidget(self.location_editor_status)
        self.splitter.addWidget(tree_panel)

        self.location_context_scroll = QScrollArea(self.splitter)
        self.location_context_scroll.setWidgetResizable(True)
        self.location_context_scroll.setFrameShape(QFrame.NoFrame)
        location_context_panel = QWidget(self.location_context_scroll)
        location_context_layout = QVBoxLayout(location_context_panel)
        location_context_layout.setContentsMargins(10, 10, 10, 10)
        location_context_layout.setSpacing(8)
        location_context_title = QLabel("Location policy")
        location_context_title.setStyleSheet(label_style("text", resolve_theme(self.settings), weight=700))
        location_context_layout.addWidget(location_context_title)
        self.location_context_label = QLabel(
            "Select a location to review its access and retention labels. Use Edit to reveal the progressive location editor."
        )
        self.location_context_label.setWordWrap(True)
        self.location_context_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.location_context_label.setAccessibleName("Selected location policy summary")
        location_context_layout.addWidget(self.location_context_label)
        self.location_copy_path_btn = QToolButton(location_context_panel)
        self.location_copy_path_btn.setText("Copy source path")
        self.location_copy_path_btn.setToolTip("Copy the selected location's full source folder path.")
        self.location_copy_path_btn.setAccessibleName("Copy selected location source path")
        self.location_copy_path_btn.clicked.connect(self._copy_selected_location_source_path)
        self.location_copy_path_btn.setVisible(False)
        location_context_layout.addWidget(self.location_copy_path_btn, 0, Qt.AlignLeft)
        location_context_layout.addWidget(self.location_editor, 1)
        location_context_layout.addStretch(0)
        self.location_context_scroll.setWidget(location_context_panel)
        self.splitter.addWidget(self.location_context_scroll)

        content_panel = QWidget(self.publishing_page)
        content_layout = QVBoxLayout(content_panel)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)
        artifact_title_row = QHBoxLayout()
        artifact_title_row.setContentsMargins(0, 0, 0, 0)
        self.artifact_heading = QLabel("Artifacts")
        self.artifact_heading.setStyleSheet(label_style("text", resolve_theme(self.settings), weight=700))
        artifact_title_row.addWidget(self.artifact_heading)
        artifact_title_row.addStretch(1)
        self.detail_toggle_btn = QToolButton(content_panel)
        self.detail_toggle_btn.setText("Details")
        self.detail_toggle_btn.setCheckable(True)
        self.detail_toggle_btn.setChecked(False)
        self.detail_toggle_btn.setToolTip("Show or hide details for the selected artifact.")
        self.detail_toggle_btn.setAccessibleName("Show artifact details")
        self.detail_toggle_btn.toggled.connect(self._update_detail_visibility)
        self.detail_toggle_btn.setVisible(False)
        artifact_title_row.addWidget(self.detail_toggle_btn)
        content_layout.addLayout(artifact_title_row)
        self.artifact_table = QTableWidget(0, 5, content_panel)
        self.artifact_table.setObjectName("stationBbsArtifactTable")
        self.artifact_table.setAccessibleName("Managed BBS artifacts")
        self.artifact_table.setToolTip("Published is a managed catalog membership, not a live-folder copy.")
        self.artifact_table.setHorizontalHeaderLabels(["Published", "File", "Age", "Expires", "Health"])
        self.artifact_table.verticalHeader().setVisible(False)
        self.artifact_table.setAlternatingRowColors(True)
        self.artifact_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.artifact_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.artifact_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.artifact_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.artifact_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        header = self.artifact_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (0, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.artifact_table.setColumnWidth(0, 84)
        self.artifact_table.setColumnWidth(1, 230)
        self.artifact_table.setColumnWidth(2, 64)
        self.artifact_table.setColumnWidth(3, 130)
        self.artifact_table.itemSelectionChanged.connect(self._refresh_detail_panel)
        self.artifact_table.itemChanged.connect(self._on_publication_item_changed)
        content_layout.addWidget(self.artifact_table, 1)

        self.publication_actions = QGridLayout()
        publication_actions = self.publication_actions
        publication_actions.setContentsMargins(0, 0, 0, 0)
        self.apply_changes_btn = QPushButton("Apply Changes")
        self.apply_changes_btn.setAccessibleName("Apply staged BBS membership changes")
        self.apply_changes_btn.clicked.connect(self._apply_publication_changes)
        publication_actions.addWidget(self.apply_changes_btn, 0, 0)
        self.revert_changes_btn = QPushButton("Revert")
        self.revert_changes_btn.setAccessibleName("Revert staged BBS membership changes")
        self.revert_changes_btn.clicked.connect(self._revert_publication_changes)
        publication_actions.addWidget(self.revert_changes_btn, 0, 1)
        publication_actions.setColumnStretch(2, 1)
        self.remove_from_bbs_btn = QPushButton("Remove from BBS")
        self.remove_from_bbs_btn.setToolTip("Remove the selected file from every BBS location. The catalog and source are preserved.")
        self.remove_from_bbs_btn.clicked.connect(self._remove_selected_from_bbs)
        publication_actions.addWidget(self.remove_from_bbs_btn, 0, 3)
        self.keep_in_bbs_btn = QPushButton("Keep in BBS")
        self.keep_in_bbs_btn.setToolTip("Keep the selected file in the selected BBS location.")
        self.keep_in_bbs_btn.clicked.connect(self._keep_selected_in_bbs)
        publication_actions.addWidget(self.keep_in_bbs_btn, 0, 4)
        self.republish_btn = QPushButton("Republish")
        self.republish_btn.setToolTip("Request republication of the selected file in the selected BBS location.")
        self.republish_btn.clicked.connect(self._republish_selected_in_bbs)
        publication_actions.addWidget(self.republish_btn, 0, 5)
        content_layout.addLayout(publication_actions)

        self.detail_group = QGroupBox("Artifact details", content_panel)
        self.detail_group.setObjectName("stationBbsArtifactDetails")
        self.detail_group.setAccessibleName("Selected Managed BBS artifact details")
        details = QGridLayout(self.detail_group)
        details.setContentsMargins(8, 8, 8, 8)
        details.setHorizontalSpacing(10)
        details.setVerticalSpacing(4)
        self.detail_labels: dict[str, QLabel] = {}
        for index, (key, label) in enumerate(
            (
                ("origin", "Origin"),
                ("path", "Path"),
                ("size", "Size"),
                ("age", "Age / modified"),
                ("access", "Access"),
                ("retention", "Retention / expiry"),
                ("health", "Publication health"),
            )
        ):
            left = QLabel(f"{label}:")
            left.setStyleSheet(label_style("text", resolve_theme(self.settings), weight=600))
            value = QLabel("Select an artifact.")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setAccessibleName(label)
            details.addWidget(left, index, 0, Qt.AlignTop)
            details.addWidget(value, index, 1)
            self.detail_labels[key] = value
        content_layout.addWidget(self.detail_group, 0)
        publishing_layout.addWidget(content_panel, 1)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([280, 760])

        self.service_tabs.addTab(self.radio_service_page, "Radio Service")
        self.service_tabs.addTab(self.locations_page, "Locations && Access")
        self.service_tabs.setTabToolTip(1, "Locations & Access")
        self.service_tabs.addTab(self.publishing_page, "Publishing")
        self.service_tabs.addTab(self.automation_page, "Automation")
        self.service_tabs.addTab(self.visitor_preview_page, "Visitor Preview")
        self.service_tabs.addTab(self.helpers_page, "Visitor Helpers")
        self._apply_responsive_layout(force=True)
        self.apply_theme()
        self._set_publication_status()

    def apply_theme(self) -> None:
        theme = resolve_theme(self.settings)
        for title in (
            self.bbs_title,
            self.radio_title,
            self.automation_title,
            self.visitor_title,
            self.helpers_title,
        ):
            title.setStyleSheet(label_style("text", theme, weight=700))
        self.help_btn.setStyleSheet(button_style("muted", theme))
        self.refresh_btn.setStyleSheet(button_style("muted", theme))
        self.radio_service_save_btn.setStyleSheet(button_style("primary", theme))
        self.radio_initialize_btn.setStyleSheet(button_style("muted", theme))
        self.radio_live_dir_browse_btn.setStyleSheet(button_style("muted", theme))
        self.radio_settings_btn.setStyleSheet(button_style("muted", theme))
        self.location_add_btn.setStyleSheet(button_style("muted", theme))
        self.location_save_btn.setStyleSheet(button_style("primary", theme))
        self.location_disable_btn.setStyleSheet(button_style("warning", theme))
        self.location_cancel_btn.setStyleSheet(button_style("muted", theme))
        self.manage_locations_btn.setStyleSheet(button_style("muted", theme))
        self.apply_changes_btn.setStyleSheet(button_style("primary", theme))
        self.revert_changes_btn.setStyleSheet(button_style("muted", theme))
        self.remove_from_bbs_btn.setStyleSheet(button_style("warning", theme))
        self.keep_in_bbs_btn.setStyleSheet(button_style("muted", theme))
        self.republish_btn.setStyleSheet(button_style("muted", theme))
        self.automation_new_btn.setStyleSheet(button_style("muted", theme))
        self.automation_add_update_btn.setStyleSheet(button_style("primary", theme))
        self.automation_delete_btn.setStyleSheet(button_style("warning", theme))
        self.automation_save_btn.setStyleSheet(button_style("primary", theme))
        self.automation_revert_btn.setStyleSheet(button_style("muted", theme))
        self.detail_group.setStyleSheet(
            f"QGroupBox {{ border: 1px solid {theme.get('border', '#d0d7de')}; border-radius: 4px; margin-top: 8px; }} "
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }"
        )
        self.location_editor.setStyleSheet(
            f"QFrame#stationBbsLocationEditor {{ border: 1px solid {theme.get('border', '#d0d7de')}; border-radius: 4px; }}"
        )
        style_splitter_handles(self.splitter, theme, width=12)
        for button in (
            self.help_btn,
            self.refresh_btn,
            self.radio_live_dir_browse_btn,
            self.radio_service_save_btn,
            self.radio_initialize_btn,
            self.radio_settings_btn,
            self.location_add_btn,
            self.location_save_btn,
            self.location_disable_btn,
            self.location_cancel_btn,
            self.automation_new_btn,
            self.automation_add_update_btn,
            self.automation_delete_btn,
            self.automation_save_btn,
            self.automation_revert_btn,
        ):
            button.setMinimumHeight(button_height_for_font(button))
        for chip_layout in (
            getattr(self, "publishing_chips_layout", None),
            getattr(self, "visitor_chips_layout", None),
        ):
            if chip_layout is not None:
                self._fit_chip_strip(chip_layout)
        self._apply_responsive_layout(force=True)

    def _open_context_help(self) -> None:
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_context_help"):
            try:
                host.open_context_help("tab.bbs")
            except Exception:
                pass

    @staticmethod
    def _clear_chip_layout(layout: QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _add_chip_strip(self, row: QHBoxLayout, parent: QWidget, accessible_name: str) -> QHBoxLayout:
        scroll = QScrollArea(parent)
        scroll.setWidgetResizable(False)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(scroll)
        chips = QHBoxLayout(body)
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(5)
        chips.setAlignment(Qt.AlignLeft)
        scroll.setWidget(body)
        scroll.setAccessibleName(accessible_name)
        row.addWidget(scroll, 1)
        return chips

    @staticmethod
    def _fit_chip_strip(layout: QHBoxLayout) -> None:
        """Size a horizontal chip strip from the active chip font/content."""

        body = layout.parentWidget()
        scroll = body.parentWidget() if body is not None else None
        while scroll is not None and not isinstance(scroll, QScrollArea):
            scroll = scroll.parentWidget()
        if body is None or not isinstance(scroll, QScrollArea):
            return
        layout.activate()
        body.adjustSize()
        chip_heights = [
            widget.sizeHint().height()
            for widget in body.findChildren(QToolButton, options=Qt.FindDirectChildrenOnly)
            if widget.isVisible()
        ]
        body_height = max(chip_heights or [body.sizeHint().height(), 1])
        # Keep the viewport exactly tall enough for one text-bearing chip row;
        # horizontal overflow remains local to this bounded strip.
        scroll.setMinimumHeight(body_height)
        scroll.setMaximumHeight(body_height)

    def _add_location_chips(
        self,
        layout: QHBoxLayout,
        locations: Iterable[BbsLocationRecord],
        *,
        selected_id: str,
        on_select,
        include_all: bool,
        accessible_prefix: str,
    ) -> None:
        self._clear_chip_layout(layout)
        group = QButtonGroup(self)
        group.setExclusive(True)
        if include_all:
            rows: list[tuple[str, str]] = [("", "All visible")]
        else:
            rows = []
        rows.extend((location.location_id, location.name) for location in locations if location.enabled)
        theme = resolve_theme(self.settings)
        accent = theme.get("accent", "#2E6F9E")
        accent_text = contrast_text_for_background(accent, theme)
        for location_id, label in rows:
            chip = QToolButton()
            chip.setText(label)
            chip.setCheckable(True)
            chip.setToolButtonStyle(Qt.ToolButtonTextOnly)
            chip.setAccessibleName(f"{accessible_prefix}: {label}")
            chip.setToolTip(label)
            chip.setStyleSheet(
                "QToolButton {"
                f"background: {theme.get('surface_alt', '#DDE1E6')}; color: {theme.get('text', '#1C1F21')}; "
                f"border: 1px solid {theme.get('border', '#D3D7DD')}; border-radius: 10px; padding: 3px 10px;"
                "} QToolButton:checked {"
                f"background: {accent}; color: {accent_text}; "
                f"border-color: {theme.get('accent_active', '#1F5A83')};"
                "}"
            )
            chip_height = button_height_for_font(chip, vertical_padding=8, floor=30)
            chip.setMinimumHeight(chip_height)
            chip.setMaximumHeight(chip_height)
            chip.setChecked(location_id == selected_id or (include_all and not selected_id and not location_id))
            chip.clicked.connect(lambda _checked=False, value=location_id: on_select(value))
            group.addButton(chip)
            layout.addWidget(chip)
        layout.addStretch(1)
        # Non-resizable scroll-area contents do not automatically adopt a new
        # layout size after the chip set is rebuilt. Without this hint Qt can
        # leave the strip at zero width until the window is resized.
        chip_body = layout.parentWidget()
        if chip_body is not None:
            layout.activate()
            chip_body.setMinimumWidth(max(1, layout.sizeHint().width()))
            chip_body.adjustSize()
        self._fit_chip_strip(layout)
        # Keep ownership alive with the widgets; QButtonGroup otherwise has no parent relationship to the strip.
        if accessible_prefix.startswith("Publishing"):
            self._publishing_chip_group = group
        else:
            self._visitor_chip_group = group

    def _radio_store(self) -> MultiRadioStore:
        host_store = getattr(self.window(), "multi_radio_store", None)
        if callable(getattr(host_store, "list_device_profiles", None)) and callable(
            getattr(host_store, "save_device_profile", None)
        ):
            return host_store
        db_path = str(getattr(self.settings, "db_path", "") or "").strip()
        return MultiRadioStore(Path(db_path)) if db_path else MultiRadioStore()

    @staticmethod
    def _radio_service_state(profile: dict[str, object]) -> tuple[str, str, str]:
        """Return concise, non-colour-only state for the serving-radio selector."""

        use_varac = bool(profile.get("use_varac", False))
        enabled = bool(profile.get("varac_bbs_enabled", False))
        published = bool(profile.get("varac_bbs_vault_enabled", False))
        live_dir = str(profile.get("varac_bbs_dir", "") or "").strip()
        serving = "Serving" if use_varac and enabled else "Not serving"
        publication = "Published" if published else "Paused"
        if not use_varac:
            health = "VarAC unavailable"
        elif not live_dir:
            health = "Live folder needed"
        elif published and not enabled:
            health = "Enable VarAC BBS"
        elif enabled:
            health = "Ready"
        else:
            health = "Service paused"
        return serving, publication, health

    @classmethod
    def _radio_selector_text(cls, profile: dict[str, object]) -> str:
        profile_id = int(profile.get("id", 0) or 0)
        name = str(profile.get("name", "") or f"Radio {profile_id}")
        serving, publication, health = cls._radio_service_state(profile)
        return f"{name} · {serving} · {publication} · {health}"

    def _refresh_radio_services(self, profiles: Iterable[dict[str, object]] | None = None) -> None:
        selected_id = self._selected_radio_service_id()
        if profiles is None:
            profiles = self._radio_profiles_by_id.values()
        profiles = [dict(profile) for profile in profiles]
        self._radio_profiles_by_id = {
            int(profile.get("id", 0) or 0): dict(profile)
            for profile in profiles
            if int(profile.get("id", 0) or 0) > 0
        }
        self._radio_service_loading = True
        try:
            self.radio_service_selector.blockSignals(True)
            self.radio_service_selector.clear()
            selected_index = -1
            for profile_index, profile in enumerate(profiles):
                profile_id = int(profile.get("id", 0) or 0)
                selector_text = self._radio_selector_text(dict(profile))
                self.radio_service_selector.addItem(selector_text, profile_id)
                self.radio_service_selector.setItemData(
                    self.radio_service_selector.count() - 1,
                    selector_text,
                    Qt.ToolTipRole,
                )
                if profile_id == selected_id:
                    selected_index = profile_index
            if profiles:
                self.radio_service_selector.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        finally:
            self.radio_service_selector.blockSignals(False)
            self._radio_service_loading = False
        self._load_selected_radio_service()

    def _on_radio_selector_changed(self, index: int) -> None:
        if self._radio_service_loading:
            return
        self._load_selected_radio_service()
        self.varac_sync_panel.radio_changed()

    def _radio_service_is_dirty(self) -> bool:
        profile = self._radio_profiles_by_id.get(self._selected_radio_service_id(), {})
        return any((
            self.radio_service_enabled_chk.isChecked() != bool(profile.get("varac_bbs_enabled")),
            self.radio_announce_enabled_chk.isChecked() != bool(profile.get("varac_bbs_announce_enabled")),
            self.radio_live_dir_edit.text().strip() != str(profile.get("varac_bbs_dir") or ""),
        ))

    def _selected_radio_service_id(self) -> int:
        if hasattr(self, "radio_service_selector"):
            try:
                selected = int(self.radio_service_selector.currentData() or 0)
            except (TypeError, ValueError):
                selected = 0
            if selected:
                return selected
        return 0

    def _set_radio_service_editor_enabled(self, enabled: bool) -> None:
        for widget in (
            self.radio_service_enabled_chk,
            self.radio_publish_enabled_chk,
            self.radio_announce_enabled_chk,
            self.radio_live_dir_edit,
            self.radio_live_dir_browse_btn,
            self.radio_service_save_btn,
            self.radio_initialize_btn,
        ):
            widget.setEnabled(bool(enabled))

    def _load_selected_radio_service(self) -> None:
        if self._radio_service_loading:
            return
        profile = self._radio_profiles_by_id.get(self._selected_radio_service_id())
        self._radio_service_loading = True
        try:
            if profile is None:
                self.radio_service_enabled_chk.setChecked(False)
                self.radio_publish_enabled_chk.setChecked(False)
                self.radio_announce_enabled_chk.setChecked(False)
                self.radio_live_dir_edit.clear()
                self.radio_native_paths_label.setText("Select a configured radio.")
                self.radio_native_paths_label.setToolTip("")
                self.radio_selector_summary.setText("No configured radio is selected.")
                self.radio_service_status.setText("No configured radio is selected.")
                self._set_radio_service_editor_enabled(False)
                return
            selector_index = self.radio_service_selector.findData(int(profile.get("id", 0) or 0))
            if selector_index >= 0 and self.radio_service_selector.currentIndex() != selector_index:
                self.radio_service_selector.blockSignals(True)
                self.radio_service_selector.setCurrentIndex(selector_index)
                self.radio_service_selector.blockSignals(False)
            use_varac = bool(profile.get("use_varac", False))
            self.radio_service_enabled_chk.setChecked(bool(profile.get("varac_bbs_enabled", False)))
            self.radio_publish_enabled_chk.setChecked(bool(profile.get("varac_bbs_vault_enabled", False)))
            self.radio_announce_enabled_chk.setChecked(bool(profile.get("varac_bbs_announce_enabled", False)))
            self.radio_live_dir_edit.setText(str(profile.get("varac_bbs_dir", "") or ""))
            self.radio_live_dir_edit.setCursorPosition(0)
            install = str(profile.get("varac_install_path", "") or "Not configured")
            outbox = str(profile.get("varac_outbox_dir", "") or "Not configured")
            self.radio_native_paths_label.setText(
                f"Native VarAC paths (managed in Radio Settings) · Install: {install} · Outbox: {outbox}"
            )
            self.radio_native_paths_label.setToolTip(
                f"Install path: {install}\nOutbox path: {outbox}\nSelect this text to copy it."
            )
            serving, publication, health = self._radio_service_state(profile)
            radio_name = str(profile.get("name", "") or f"Radio {profile.get('id', '')}")
            self.radio_selector_summary.setText(
                f"{radio_name}: {serving}; {publication}; {health}."
            )
            self._set_radio_service_editor_enabled(use_varac)
            self.radio_initialize_btn.setText("Serve station BBS" if getattr(self, "_station_managed_root", "") else "Initialize BBS…")
            self.radio_settings_btn.setEnabled(True)
            self.radio_service_status.setText(
                "Configure the live BBS service here."
                if use_varac
                else "VarAC is not enabled for this radio. Open Radio Settings first."
            )
        finally:
            self._radio_service_loading = False

    def _browse_radio_live_dir(self) -> None:
        start = self.radio_live_dir_edit.text().strip()
        selected = QFileDialog.getExistingDirectory(self, "Select VarAC Live BBS Folder", start)
        if selected:
            self.radio_live_dir_edit.setText(selected)

    def _save_selected_radio_service(self) -> None:
        profile_id = self._selected_radio_service_id()
        profile = self._radio_profiles_by_id.get(profile_id)
        if profile is None:
            self.radio_service_status.setText("Select a configured VarAC radio before saving.")
            return
        if not bool(profile.get("use_varac", False)):
            self.radio_service_status.setText("Enable VarAC for this radio in Radio Settings before configuring its BBS service.")
            return
        live_dir = self.radio_live_dir_edit.text().strip()
        if self.radio_publish_enabled_chk.isChecked() and not self.radio_service_enabled_chk.isChecked():
            self.radio_service_status.setText("Enable VarAC BBS before publishing the CommsPlan catalog on this radio.")
            return
        if (self.radio_service_enabled_chk.isChecked() or self.radio_publish_enabled_chk.isChecked()) and not live_dir:
            self.radio_service_status.setText("Choose the selected radio's live BBS folder before enabling service or publication.")
            self.radio_live_dir_edit.setFocus(Qt.OtherFocusReason)
            return
        payload = dict(profile)
        payload.update(
            {
                "varac_bbs_dir": live_dir,
                "varac_bbs_enabled": bool(self.radio_service_enabled_chk.isChecked()),
                "varac_bbs_vault_enabled": bool(self.radio_publish_enabled_chk.isChecked()),
                "varac_bbs_announce_enabled": bool(self.radio_announce_enabled_chk.isChecked()),
            }
        )
        try:
            saved = self._radio_store().save_device_profile(payload)
        except Exception as exc:
            self.radio_service_status.setText(f"Radio BBS service was not saved: {exc}")
            return
        self._radio_profiles_by_id[profile_id] = dict(saved)
        radio_name = str(saved.get("name", "") or f"Radio {profile_id}")
        self._refresh_radio_services(self._radio_profiles_by_id.values())
        self.radio_service_status.setText(f"Saved the BBS service for {radio_name}. No source files were copied or deleted.")

    def _open_radio_settings(self) -> None:
        host = self.window()
        opener = getattr(host, "open_settings_section", None)
        if callable(opener):
            opener(
                "radio_profiles",
                radio_id=self._selected_radio_service_id() or None,
                settings_nav_context="radios",
            )
            return
        self.radio_service_status.setText(
            "Open Configuration → Radios to configure the selected radio's native VarAC paths."
        )

    def _initialize_selected_radio_bbs(self) -> None:
        if isinstance(self._initialization_thread, QThread) and self._initialization_thread.isRunning():
            self.radio_service_status.setText("Managed BBS initialization is already running.")
            return
        profile = self._radio_profiles_by_id.get(self._selected_radio_service_id())
        if profile is None or not bool(profile.get("use_varac", False)):
            self.radio_service_status.setText("Select a configured VarAC radio before initializing its BBS.")
            return
        live_dir = Path(self.radio_live_dir_edit.text().strip()).expanduser()
        if not live_dir.exists() or not live_dir.is_dir():
            self.radio_service_status.setText("Choose an existing VarAC live BBS folder before initializing.")
            self.radio_live_dir_edit.setFocus(Qt.OtherFocusReason)
            return
        if getattr(self, "_station_managed_root", ""):
            default = self._locations_by_id.get(getattr(self, "_station_default_location_id", ""))
            if default is None or not str(default.source_dir or "").strip():
                self.radio_service_status.setText("Repair the station BBS Default location before attaching another radio.")
                return
            self.radio_service_enabled_chk.setChecked(True)
            self.radio_publish_enabled_chk.setChecked(True)
            self._save_selected_radio_service()
            return
        answer = QMessageBox.question(
            self,
            "Initialize Managed BBS",
            "Create or reuse the station Managed BBS library beside this live BBS folder?\n\n"
            f"Live BBS: {live_dir}\n\n"
            "The live folder will not be replaced or deleted. Catalog publication will be enabled for this radio.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self.radio_service_status.setText("BBS initialization cancelled. No files or settings were changed.")
            return
        import_answer = QMessageBox.question(
            self,
            "Import Existing BBS Files",
            "Copy the files currently in this live BBS folder into the managed Default location?\n\n"
            "Choose No to initialize an empty managed location. Existing live files remain untouched either way.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if import_answer == QMessageBox.Cancel:
            self.radio_service_status.setText("BBS initialization cancelled. No files or settings were changed.")
            return
        self._initialization_profile_id = int(profile.get("id", 0) or 0)
        self._initialization_live_dir = str(live_dir)
        self.radio_initialize_btn.setEnabled(False)
        self.radio_service_status.setText("Initializing the Managed BBS in the background…")
        thread = QThread(self)
        worker = _BbsInitializationWorker(
            bbs_library_db_path_from_settings(self.settings),
            live_dir,
            import_answer == QMessageBox.Yes,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_bbs_initialization_finished)
        worker.failed.connect(self._on_bbs_initialization_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_bbs_initialization_thread_finished)
        self._initialization_thread = thread
        self._initialization_worker = worker
        thread.start()

    def _on_bbs_initialization_finished(self, result: object) -> None:
        if self._catalog_shutdown or not isinstance(result, BbsInitializationResult):
            return
        profile_id = self._initialization_profile_id
        profile = self._radio_profiles_by_id.get(profile_id)
        if profile is None:
            self.radio_service_status.setText(
                "Managed BBS folders were initialized, but the selected radio is no longer available. "
                "Refresh and save its Radio Service."
            )
            return
        payload = dict(profile)
        payload.update(
            {
                "varac_bbs_dir": self._initialization_live_dir,
                "varac_bbs_enabled": True,
                "varac_bbs_vault_enabled": True,
            }
        )
        try:
            saved = self._radio_store().save_device_profile(payload)
        except Exception as exc:
            self.radio_service_status.setText(
                f"Managed BBS folders were initialized, but the radio service was not enabled: {exc}"
            )
            return
        self._radio_profiles_by_id[profile_id] = dict(saved)
        self._refresh_radio_services(self._radio_profiles_by_id.values())
        self._pending_radio_status = (
            f"Station BBS reused; this radio now serves the existing locations and access rules. "
            if result.reused else
            f"Managed BBS initialized. {result.imported_files} existing file(s) copied; "
            f"{result.cataloged_files} file(s) cataloged in {result.default_location_dir}."
        )
        self.radio_service_status.setText(self._pending_radio_status)
        self.refresh_catalog()

    def _on_bbs_initialization_failed(self, detail: str) -> None:
        if not self._catalog_shutdown:
            self.radio_service_status.setText(f"Managed BBS initialization failed: {detail}")

    def _on_bbs_initialization_thread_finished(self) -> None:
        self._initialization_thread = None
        self._initialization_worker = None
        self._initialization_profile_id = 0
        self._initialization_live_dir = ""
        if not self._catalog_shutdown:
            profile = self._radio_profiles_by_id.get(self._selected_radio_service_id())
            self.radio_initialize_btn.setEnabled(bool(profile and profile.get("use_varac", False)))

    @staticmethod
    def _automation_rule_id(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-") or "rule"

    def _refresh_automation_targets(self, selected_ids: Iterable[str] = ()) -> None:
        selected = {str(value or "").strip() for value in selected_ids}
        self.automation_targets_list.blockSignals(True)
        try:
            self.automation_targets_list.clear()
            for location in sorted(
                self._locations_by_id.values(),
                key=lambda row: (row.name.lower(), row.location_id),
            ):
                item = QListWidgetItem(location.name)
                item.setData(Qt.UserRole, location.location_id)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if location.location_id in selected else Qt.Unchecked)
                if not location.enabled:
                    item.setText(f"{location.name} · Disabled")
                self.automation_targets_list.addItem(item)
        finally:
            self.automation_targets_list.blockSignals(False)

    def _clear_automation_editor(self) -> None:
        self._automation_loading = True
        try:
            self._automation_selected_rule_id = ""
            if hasattr(self, "automation_rules_table"):
                self.automation_rules_table.clearSelection()
            self.automation_enabled_chk.setChecked(True)
            self.automation_name_edit.clear()
            self.automation_source_varac_chk.setChecked(True)
            self.automation_source_flmsg_chk.setChecked(True)
            self.automation_source_flamp_chk.setChecked(True)
            self.automation_from_calls_edit.clear()
            self.automation_subject_contains_edit.clear()
            self._refresh_automation_targets()
            self.automation_copy_mode_combo.setCurrentIndex(0)
            self.automation_add_update_btn.setText("Add Rule")
            self.automation_delete_btn.setEnabled(False)
        finally:
            self._automation_loading = False

    def _new_automation_rule(self) -> None:
        self._clear_automation_editor()
        self.automation_status_label.setText("Enter the match and destination, then choose Add Rule.")
        self.automation_name_edit.setFocus(Qt.OtherFocusReason)

    def _automation_editor_rule(self) -> BbsSweeperRule | None:
        name = self.automation_name_edit.text().strip()
        if not name:
            self.automation_status_label.setText("Enter a rule name.")
            self.automation_name_edit.setFocus(Qt.OtherFocusReason)
            return None
        sources = []
        if self.automation_source_varac_chk.isChecked():
            sources.append("varac_bbs")
        if self.automation_source_flmsg_chk.isChecked():
            sources.append("flmsg")
        if self.automation_source_flamp_chk.isChecked():
            sources.append("flamp")
        if not sources:
            self.automation_status_label.setText("Choose at least one incoming message source.")
            return None
        targets = [
            str(self.automation_targets_list.item(index).data(Qt.UserRole) or "")
            for index in range(self.automation_targets_list.count())
            if self.automation_targets_list.item(index).checkState() == Qt.Checked
        ]
        from_calls = [value for value in re.split(r"[,;\s]+", self.automation_from_calls_edit.text()) if value]
        terms = [
            value.strip()
            for value in re.split(r"[,;\n]+", self.automation_subject_contains_edit.text())
            if value.strip()
        ]
        raw = {
            "id": self._automation_selected_rule_id or self._automation_rule_id(name),
            "name": name,
            "enabled": self.automation_enabled_chk.isChecked(),
            "source_families": sources,
            "from_calls": from_calls,
            "subject_contains": terms,
            "target_location_ids": targets,
            "copy_mode": str(self.automation_copy_mode_combo.currentData() or "copy"),
        }
        normalized = load_bbs_sweeper_rules([raw])
        if not normalized:
            self.automation_status_label.setText("The rule could not be normalized.")
            return None
        rule = normalized[0]
        if rule.enabled and not rule.target_location_ids:
            self.automation_status_label.setText("Choose at least one BBS location for an enabled rule.")
            return None
        if rule.enabled and not (rule.from_calls or rule.subject_contains):
            self.automation_status_label.setText(
                "Add a sender callsign or message/filename term. "
                "Enabled rules never copy all incoming traffic implicitly."
            )
            return None
        return rule

    def _add_or_update_automation_rule(self) -> None:
        rule = self._automation_editor_rule()
        if rule is None:
            return
        existing = load_bbs_sweeper_rules(self._automation_rules)
        selected_id = self._automation_selected_rule_id
        if not selected_id and any(item.id == rule.id for item in existing):
            self.automation_status_label.setText("A rule with this name already exists. Select it to update it.")
            return
        updated = [rule if item.id == selected_id else item for item in existing] if selected_id else [*existing, rule]
        self._automation_rules = bbs_sweeper_rules_to_data(updated)
        self._automation_selected_rule_id = rule.id
        self._automation_dirty = True
        self._populate_automation_rules(select_rule_id=rule.id)
        self.automation_status_label.setText("Automation rule staged. Choose Save Automation Rules to apply it.")

    def _delete_automation_rule(self) -> None:
        selected_id = self._automation_selected_rule_id
        if not selected_id:
            return
        rules = [rule for rule in load_bbs_sweeper_rules(self._automation_rules) if rule.id != selected_id]
        self._automation_rules = bbs_sweeper_rules_to_data(rules)
        self._automation_dirty = True
        self._populate_automation_rules()
        self._clear_automation_editor()
        self.automation_status_label.setText("Rule removal staged. Choose Save Automation Rules to apply it.")

    def _populate_automation_rules(self, *, select_rule_id: str = "") -> None:
        rules = load_bbs_sweeper_rules(self._automation_rules)
        self._automation_loading = True
        try:
            self.automation_rules_table.setRowCount(len(rules))
            selected_row = -1
            for row, rule in enumerate(rules):
                locations = [
                    self._locations_by_id[target].name if target in self._locations_by_id else target
                    for target in rule.target_location_ids
                ]
                match = ", ".join(rule.from_calls) or ", ".join(rule.subject_contains)
                values = (
                    "Yes" if rule.enabled else "No",
                    rule.name,
                    ", ".join(rule.source_families),
                    match or "Not ready",
                    ", ".join(locations) or "None",
                    "Once per filename" if rule.copy_mode == "copy_once" else "Each new arrival",
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    item.setData(Qt.UserRole, rule.id)
                    self.automation_rules_table.setItem(row, column, item)
                if rule.id == select_rule_id:
                    selected_row = row
            if selected_row >= 0:
                self.automation_rules_table.selectRow(selected_row)
        finally:
            self._automation_loading = False

    def _load_selected_automation_rule(self) -> None:
        if self._automation_loading:
            return
        row = self.automation_rules_table.currentRow()
        if row < 0:
            return
        item = self.automation_rules_table.item(row, 0)
        rule_id = str(item.data(Qt.UserRole) or "") if item is not None else ""
        rule = next((value for value in load_bbs_sweeper_rules(self._automation_rules) if value.id == rule_id), None)
        if rule is None:
            return
        self._automation_loading = True
        try:
            self._automation_selected_rule_id = rule.id
            self.automation_enabled_chk.setChecked(rule.enabled)
            self.automation_name_edit.setText(rule.name)
            self.automation_source_varac_chk.setChecked("varac_bbs" in rule.source_families)
            self.automation_source_flmsg_chk.setChecked("flmsg" in rule.source_families)
            self.automation_source_flamp_chk.setChecked("flamp" in rule.source_families)
            self.automation_from_calls_edit.setText(", ".join(rule.from_calls))
            self.automation_subject_contains_edit.setText(", ".join(rule.subject_contains))
            self._refresh_automation_targets(rule.target_location_ids)
            index = self.automation_copy_mode_combo.findData(rule.copy_mode)
            self.automation_copy_mode_combo.setCurrentIndex(index if index >= 0 else 0)
            self.automation_add_update_btn.setText("Update Rule")
            self.automation_delete_btn.setEnabled(True)
        finally:
            self._automation_loading = False

    def _save_automation_rules(self) -> None:
        rules = bbs_sweeper_rules_to_data(load_bbs_sweeper_rules(self._automation_rules))
        try:
            with connect_sqlite(bbs_library_db_path_from_settings(self.settings)) as conn:
                with conn:
                    save_station_bbs_sweeper_rules(conn, rules)
            setter = getattr(self.settings, "set", None)
            if callable(setter):
                setter("varac_bbs_sweeper_rules_v1", rules)
        except Exception as exc:
            self.automation_status_label.setText(f"Automation rules were not saved: {exc}")
            return
        self._automation_rules = [dict(rule) for rule in rules]
        self._automation_saved_rules = [dict(rule) for rule in rules]
        self._automation_dirty = False
        self.automation_status_label.setText(
            f"Saved {len(rules)} station BBS automation rule{'s' if len(rules) != 1 else ''}."
        )

    def _revert_automation_rules(self) -> None:
        self._automation_rules = [dict(rule) for rule in self._automation_saved_rules]
        self._automation_dirty = False
        self._populate_automation_rules()
        self._clear_automation_editor()
        self.automation_status_label.setText("Reverted unsaved automation rule changes.")

    def _set_location_editor_visible(self, visible: bool) -> None:
        if not visible:
            self.location_editor.setVisible(False)
            self.location_edit_btn.setText("Edit")
            self.location_edit_btn.setAccessibleName("Show location editor")
            return
        location = self._locations_by_id.get(self._selected_location_id)
        if location is None:
            self.location_editor.setVisible(False)
            self.location_edit_btn.blockSignals(True)
            self.location_edit_btn.setChecked(False)
            self.location_edit_btn.blockSignals(False)
            self.location_edit_btn.setText("Edit")
            self.location_edit_btn.setAccessibleName("Show location editor")
            self.location_context_label.setText(
                "Select a saved location to edit it, or choose Add to create a location policy."
            )
            return
        self.location_editor.setVisible(True)
        self.location_edit_btn.setText("Hide")
        self.location_edit_btn.setAccessibleName("Hide location editor")
        self._load_location_editor(location)

    def _begin_new_location(self) -> None:
        if not self.location_edit_btn.isChecked():
            self.location_edit_btn.blockSignals(True)
            self.location_edit_btn.setChecked(True)
            self.location_edit_btn.blockSignals(False)
        self.location_editor.setVisible(True)
        self.location_edit_btn.setText("Hide")
        self.location_edit_btn.setAccessibleName("Hide location editor")
        self._load_location_editor(None)
        self.location_name_edit.setFocus(Qt.OtherFocusReason)

    def _cancel_location_edit(self) -> None:
        """Close the progressive editor without changing the selected policy."""

        self._editing_location_id = ""
        self.location_edit_btn.setChecked(False)
        self.location_editor_status.setText("Editing cancelled. No location policy or source folder was changed.")

    def _copy_selected_location_source_path(self) -> None:
        location = self._locations_by_id.get(self._selected_location_id)
        source_dir = str(location.source_dir or "").strip() if location is not None else ""
        if source_dir:
            QApplication.clipboard().setText(source_dir)

    def _populate_parent_choices(self, current_id: str = "") -> None:
        selected_parent = self.location_parent_combo.currentData()
        self.location_parent_combo.blockSignals(True)
        try:
            self.location_parent_combo.clear()
            self.location_parent_combo.addItem("No parent", "")
            for location in sorted(self._locations_by_id.values(), key=lambda item: (item.name.lower(), item.location_id)):
                if location.location_id == current_id:
                    continue
                self.location_parent_combo.addItem(location.name, location.location_id)
            wanted = str(selected_parent or "")
            index = self.location_parent_combo.findData(wanted)
            self.location_parent_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.location_parent_combo.blockSignals(False)

    def _load_location_editor(self, location: BbsLocationRecord | None) -> None:
        self._location_editor_loading = True
        try:
            self._editing_location_id = location.location_id if location is not None else ""
            self._populate_parent_choices(self._editing_location_id)
            self.location_name_edit.setText(location.name if location is not None else "")
            self.location_source_edit.setText(location.source_dir if location is not None else "")
            self.location_enabled_chk.setChecked(bool(location.enabled) if location is not None else True)
            self._set_combo_data(self.location_access_combo, location.access_rule if location is not None else "Public")
            metadata = dict(location.metadata or {}) if location is not None else {}
            allowed = metadata.get("allowed_callsigns", [])
            self.location_access_editor.set_policy(metadata.get("access_policy_v1"), legacy_callsigns=allowed)
            self.location_code_edit.clear()
            self.location_code_confirm_edit.clear()
            self._set_combo_data(self.location_retention_combo, location.retention_mode if location is not None else "global_default")
            if location is not None:
                self.location_retention_days_spin.setValue(max(1, int(location.retention_days or 0) or 14))
                parent_index = self.location_parent_combo.findData(location.parent_location_id)
                self.location_parent_combo.setCurrentIndex(parent_index if parent_index >= 0 else 0)
            else:
                self.location_retention_days_spin.setValue(14)
                self.location_parent_combo.setCurrentIndex(0)
            self.location_disable_btn.setEnabled(location is not None and bool(location.enabled))
            self.location_editor_status.setText(
                "Editing station-owned location. Saving records catalog policy only; it does not create or scan the source folder."
                if location is not None
                else "New station-owned location. Choose an existing source folder path if one is needed; it will not be created here."
            )
            self._update_location_retention_days_state()
            self._update_location_access_state()
        finally:
            self._location_editor_loading = False

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        wanted = str(value or "").strip()
        index = combo.findData(wanted)
        if index < 0 and wanted:
            combo.addItem(_access_text(wanted), wanted)
            index = combo.count() - 1
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _update_location_retention_days_state(self, *_args) -> None:
        expire = str(self.location_retention_combo.currentData() or "") == "expire_after_days"
        self.location_retention_days_spin.setEnabled(expire)
        self.location_retention_days_spin.setToolTip(
            "Number of days before this location's published mappings expire." if expire else "Used only when retention is Expire after days."
        )

    def _update_location_access_state(self, *_args) -> None:
        rule = str(self.location_access_combo.currentData() or "Public")
        callsigns = "Allowed callsigns" in rule
        code = "access code" in rule.lower()
        self.location_callsigns_label.setVisible(callsigns)
        self.location_access_editor.setVisible(callsigns)
        self.location_code_label.setVisible(code)
        self.location_code_edit.setVisible(code)
        self.location_code_confirm_edit.setVisible(code)

    def _new_location_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-") or "location"
        candidate = base
        suffix = 2
        while candidate in self._locations_by_id:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _save_location(self) -> None:
        name = " ".join(self.location_name_edit.text().split())
        if not name:
            self.location_editor_status.setText("Location name is required before saving.")
            self.location_name_edit.setFocus(Qt.OtherFocusReason)
            return
        location_id = self._editing_location_id or self._new_location_id(name)
        existing = self._locations_by_id.get(location_id)
        parent_id = str(self.location_parent_combo.currentData() or "").strip()
        if parent_id == location_id:
            parent_id = ""
        retention_mode = str(self.location_retention_combo.currentData() or "global_default")
        retention_days = self.location_retention_days_spin.value() if retention_mode == "expire_after_days" else 0
        access_rule = str(self.location_access_combo.currentData() or "Public")
        metadata = dict(existing.metadata or {}) if existing is not None else {}
        metadata["open_rule"] = access_rule
        metadata.setdefault("visibility_rule", "Public")
        metadata.setdefault("list_in_root_menu", True)
        metadata["inherit_global_allowed_callsigns"] = False
        metadata["access_policy_v1"] = self.location_access_editor.policy()
        metadata["allowed_callsigns"] = effective_callsigns(metadata["access_policy_v1"])
        code_text = self.location_code_edit.text()
        confirm_text = self.location_code_confirm_edit.text()
        needs_code = "access code" in access_rule.lower()
        has_saved_code = bool(metadata.get("access_code_hash") and metadata.get("access_code_salt"))
        if code_text or confirm_text:
            if code_text != confirm_text:
                self.location_editor_status.setText("The access code and confirmation do not match.")
                return
            metadata.update(hash_access_code(code_text))
            has_saved_code = True
        if needs_code and not has_saved_code:
            self.location_editor_status.setText("Enter and confirm an access code for this location.")
            return
        try:
            with connect_sqlite(bbs_library_db_path_from_settings(self.settings)) as conn:
                with conn:
                    upsert_bbs_location(
                        conn,
                        location_id=location_id,
                        name=name,
                        source_dir=self.location_source_edit.text().strip(),
                        enabled=bool(self.location_enabled_chk.isChecked()),
                        parent_location_id=parent_id,
                        access_rule=access_rule,
                        retention_mode=retention_mode,
                        retention_days=retention_days,
                        metadata=metadata,
                    )
        except Exception as exc:
            self.location_editor_status.setText(f"Location was not saved: {exc}")
            return
        self._selected_location_id = location_id
        if self.location_enabled_chk.isChecked():
            self._publishing_location_id = location_id
        self._editing_location_id = location_id
        state = "enabled" if self.location_enabled_chk.isChecked() else "disabled"
        self._pending_location_status = f"Saved {name} ({state}). No source folders were created or scanned."
        self.location_editor_status.setText(self._pending_location_status)
        self.refresh_catalog()

    def _disable_location(self) -> None:
        location = self._locations_by_id.get(self._editing_location_id or self._selected_location_id)
        if location is None:
            self.location_editor_status.setText("Select a saved location before disabling it.")
            return
        try:
            with connect_sqlite(bbs_library_db_path_from_settings(self.settings)) as conn:
                with conn:
                    upsert_bbs_location(
                        conn,
                        location_id=location.location_id,
                        name=location.name,
                        source_dir=location.source_dir,
                        enabled=False,
                        parent_location_id=location.parent_location_id,
                        access_rule=location.access_rule,
                        retention_mode=location.retention_mode,
                        retention_days=location.retention_days,
                        metadata=location.metadata,
                    )
        except Exception as exc:
            self.location_editor_status.setText(f"Location was not disabled: {exc}")
            return
        self._selected_location_id = location.location_id
        self._pending_location_status = (
            f"Disabled {location.name}. It remains visible for review; no rows, source files, or folders were deleted."
        )
        self.location_editor_status.setText(self._pending_location_status)
        self.refresh_catalog()

    def set_tab_active(self, active: bool) -> None:
        if active:
            self.refresh_catalog()

    def shutdown(self) -> None:
        """Stop accepting refresh results and give the bounded loader time to exit."""
        self._catalog_shutdown = True
        self._catalog_refresh_pending = False
        thread = self._catalog_thread
        if isinstance(thread, QThread) and thread.isRunning():
            thread.requestInterruption()
            thread.quit()
            if not thread.wait(1000):
                thread.setParent(None)
                job_id = id(thread)
                _DETACHED_BBS_CATALOG_JOBS[job_id] = (thread, self._catalog_worker)
                thread.finished.connect(
                    lambda ident=job_id: _release_detached_bbs_catalog_job(ident)
                )
        self._catalog_thread = None
        self._catalog_worker = None
        init_thread = self._initialization_thread
        if isinstance(init_thread, QThread) and init_thread.isRunning():
            init_thread.requestInterruption()
            init_thread.quit()
            if not init_thread.wait(1000):
                init_thread.setParent(None)
                job_id = id(init_thread)
                _DETACHED_BBS_CATALOG_JOBS[job_id] = (init_thread, self._initialization_worker)
                init_thread.finished.connect(
                    lambda ident=job_id: _release_detached_bbs_catalog_job(ident)
                )
        self._initialization_thread = None
        self._initialization_worker = None

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            for chip_layout in (
                getattr(self, "publishing_chips_layout", None),
                getattr(self, "visitor_chips_layout", None),
            ):
                if chip_layout is not None:
                    self._fit_chip_strip(chip_layout)
            self._apply_responsive_layout(force=True)

    def _responsive_viewport(self) -> tuple[int, int, int]:
        """Return the actual tab viewport and current text height, not window size."""

        viewport = self.service_tabs.contentsRect()
        width = int(viewport.width() or self.contentsRect().width() or self.width() or 0)
        height = int(viewport.height() or self.contentsRect().height() or self.height() or 0)
        line_height = max(1, int(self.fontMetrics().lineSpacing() or self.fontMetrics().height() or 1))
        return width, height, line_height

    def _apply_responsive_layout(self, *, force: bool = False) -> None:
        """Arrange controls only; resize must never refresh the catalog or folders."""

        width, height, line_height = self._responsive_viewport()
        compact = (
            width < max(840, line_height * 54)
            # A 720px application window leaves roughly 630px for these tabs
            # after the station shell and BBS header.  It needs the compact,
            # single-detail layout even at ordinary text size.
            or height < max(700, line_height * 40)
        )
        signature = (width, height, line_height)
        if not force and compact == self._compact_layout and signature == self._responsive_signature:
            return
        self._responsive_signature = signature
        self._compact_layout = compact
        self.splitter.setOrientation(Qt.Vertical if compact else Qt.Horizontal)
        self.splitter.setSizes([160, 600] if compact else [320, 760])
        self.location_compact_selector.setVisible(compact)
        self.location_tree.setVisible(not compact)
        for button in (
            self.apply_changes_btn,
            self.revert_changes_btn,
            self.remove_from_bbs_btn,
            self.keep_in_bbs_btn,
            self.republish_btn,
        ):
            self.publication_actions.removeWidget(button)
        if compact:
            self.publication_actions.addWidget(self.apply_changes_btn, 0, 0)
            self.publication_actions.addWidget(self.revert_changes_btn, 0, 1)
            self.publication_actions.addWidget(self.remove_from_bbs_btn, 1, 0)
            self.publication_actions.addWidget(self.keep_in_bbs_btn, 1, 1)
            self.publication_actions.addWidget(self.republish_btn, 1, 2)
        else:
            self.publication_actions.addWidget(self.apply_changes_btn, 0, 0)
            self.publication_actions.addWidget(self.revert_changes_btn, 0, 1)
            self.publication_actions.addWidget(self.remove_from_bbs_btn, 0, 3)
            self.publication_actions.addWidget(self.keep_in_bbs_btn, 0, 4)
            self.publication_actions.addWidget(self.republish_btn, 0, 5)
        self.detail_toggle_btn.setVisible(compact)
        self._update_detail_visibility()

    def _update_detail_visibility(self, *_args) -> None:
        visible = not self._compact_layout or self.detail_toggle_btn.isChecked()
        self.detail_group.setVisible(visible)
        self.detail_toggle_btn.setText("Hide Details" if visible and self._compact_layout else "Details")
        self.detail_toggle_btn.setAccessibleName(
            "Hide artifact details" if visible and self._compact_layout else "Show artifact details"
        )

    def refresh_catalog(self) -> None:
        """Request one bounded snapshot; presentation remains cache-only."""

        if self._catalog_shutdown:
            return
        self._catalog_generation += 1
        if isinstance(self._catalog_thread, QThread) and self._catalog_thread.isRunning():
            self._catalog_refresh_pending = True
            return
        generation = self._catalog_generation
        self._catalog_refresh_pending = False
        if self._catalog_rows or self._locations_by_id:
            self.summary_label.setText("Refreshing Managed BBS catalog… Current results remain available.")
        else:
            self.summary_label.setText("Loading Managed BBS catalog…")
        thread = QThread(self)
        worker = _BbsCatalogWorker(generation, bbs_library_db_path_from_settings(self.settings))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_catalog_snapshot)
        worker.failed.connect(self._on_catalog_failure)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_catalog_thread_finished)
        self._catalog_thread = thread
        self._catalog_worker = worker
        thread.start()

    def _on_catalog_snapshot(self, generation: int, snapshot: object) -> None:
        if self._catalog_shutdown or generation != self._catalog_generation:
            return
        if not isinstance(snapshot, _BbsCatalogSnapshot):
            self._on_catalog_failure(generation, "Catalog loader returned an invalid snapshot.")
            return
        self._catalog_rows = snapshot.artifact_rows
        self._station_managed_root = snapshot.managed_root
        self._station_default_location_id = snapshot.default_location_id or DEFAULT_LOCATION_ID
        self._locations_by_id = {location.location_id: location for location in snapshot.locations}
        self._refresh_radio_services(snapshot.profiles)
        if self._pending_radio_status:
            self.radio_service_status.setText(self._pending_radio_status)
            self._pending_radio_status = ""
        locations = list(snapshot.locations)
        selected = self._selected_location_id
        self._locations_by_id = {location.location_id: location for location in locations}
        self._station_allowed_callsigns = set(snapshot.allowed_callsigns)
        self._station_managed_root = snapshot.managed_root
        self._station_limit_access_enabled = snapshot.limit_access_enabled
        if selected and selected not in self._locations_by_id:
            selected = ""
        if not selected and locations:
            configured_default = snapshot.default_location_id
            selected = configured_default if configured_default in self._locations_by_id else next(
                (location.location_id for location in locations if location.enabled),
                locations[0].location_id,
            )
        self._selected_location_id = selected
        enabled_ids = {location.location_id for location in locations if location.enabled}
        if self._publishing_location_id not in enabled_ids:
            self._publishing_location_id = selected if selected in enabled_ids else next(
                (location.location_id for location in locations if location.enabled),
                "",
            )
        self._populate_locations(locations)
        if not self._automation_dirty:
            normalized_rules = bbs_sweeper_rules_to_data(
                load_bbs_sweeper_rules(list(snapshot.sweeper_rules))
            )
            self._automation_rules = [dict(rule) for rule in normalized_rules]
            self._automation_saved_rules = [dict(rule) for rule in normalized_rules]
            self._populate_automation_rules()
            self._clear_automation_editor()
            self.automation_status_label.setText(
                f"{len(normalized_rules)} saved station automation rule"
                f"{'s' if len(normalized_rules) != 1 else ''}."
            )
        else:
            selected_targets: tuple[str, ...] = ()
            selected_rule = next(
                (
                    rule
                    for rule in load_bbs_sweeper_rules(self._automation_rules)
                    if rule.id == self._automation_selected_rule_id
                ),
                None,
            )
            if selected_rule is not None:
                selected_targets = selected_rule.target_location_ids
            self._refresh_automation_targets(selected_targets)
        self._on_location_selection_changed()
        self._refresh_visitor_preview()
        if self._pending_location_status:
            self.location_editor_status.setText(self._pending_location_status)
            self._pending_location_status = ""
        if self._pending_publication_status:
            self._set_publication_status(self._pending_publication_status)
            self._pending_publication_status = ""

    def _on_catalog_failure(self, generation: int, detail: str) -> None:
        if self._catalog_shutdown or generation != self._catalog_generation:
            return
        has_current = bool(self._locations_by_id or self._catalog_rows or self._radio_profiles_by_id)
        if not has_current:
            self._populate_locations([])
            self._populate_artifacts([])
            self._populate_helpers([])
            self._populate_visitor_preview([])
        suffix = " Current results remain available." if has_current else " You can continue using other BBS controls and retry Refresh."
        self.summary_label.setText(f"Managed BBS catalog is unavailable: {detail}.{suffix}")

    def _on_catalog_thread_finished(self) -> None:
        self._catalog_thread = None
        self._catalog_worker = None
        if self._catalog_shutdown:
            return
        if self._catalog_refresh_pending:
            pending = self._catalog_refresh_pending
            self._catalog_refresh_pending = False
            # A stale generation necessarily needs its newest replacement.
            if pending:
                self.refresh_catalog()

    def _refresh_visitor_preview(self, *_args) -> None:
        locations = [row for row in self._locations_by_id.values() if self._visitor_can_see_location(row)]
        if self._visitor_location_id not in {location.location_id for location in locations}:
            self._visitor_location_id = ""
        self._add_location_chips(
            self.visitor_chips_layout,
            locations,
            selected_id=self._visitor_location_id,
            on_select=self._on_visitor_location_selected,
            include_all=True,
            accessible_prefix="Visitor preview location",
        )
        self._populate_visitor_preview(locations)
        rows = self._catalog_rows
        visible_ids = {location.location_id for location in locations}
        visible_rows = [
            row for row in rows
            if row.published
            and row.location_id in visible_ids
            and (not self._visitor_location_id or row.location_id == self._visitor_location_id)
            and not is_fio_bbs_helper_file_name(row.display_name or row.source_path)
        ][:MAX_ARTIFACT_ROWS]
        self.visitor_artifact_table.setRowCount(len(visible_rows))
        for index, row in enumerate(visible_rows):
            location = self._locations_by_id.get(row.location_id)
            values = (
                row.display_name or row.artifact_id,
                row.location_name or (location.name if location else "Unknown location"),
                _access_text(location.access_rule if location else "public"),
                _health_text(row),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setToolTip("Read-only visitor preview")
                self.visitor_artifact_table.setItem(index, column, item)
        suffix = " (first 200)" if len(visible_rows) >= MAX_ARTIFACT_ROWS else ""
        self.visitor_preview_status.setText(
            f"{len(locations)} visible location{'s' if len(locations) != 1 else ''}; "
            f"{len(visible_rows)} published artifact{'s' if len(visible_rows) != 1 else ''}{suffix}. Read-only preview."
        )

    def _visitor_can_see_location(self, location: BbsLocationRecord) -> bool:
        if not location.enabled:
            return False
        metadata = dict(location.metadata or {})
        visibility = str(metadata.get("visibility_rule", "Public") or "Public").strip()
        if visibility == "Hidden":
            return False
        if visibility == "Public":
            return True
        callsign = self.visitor_callsign_edit.text().strip().upper()
        if not callsign:
            return False
        allowed_value = metadata.get("allowed_callsigns", [])
        if isinstance(allowed_value, str):
            allowed_text = allowed_value
        else:
            allowed_text = " ".join(str(value) for value in allowed_value)
        allowed = {
            value.strip().upper()
            for value in re.split(r"[,;\s]+", allowed_text)
            if value.strip()
        }
        if bool(metadata.get("inherit_global_allowed_callsigns", True)):
            allowed.update(self._station_allowed_callsigns)
        if self._station_limit_access_enabled and callsign not in self._station_allowed_callsigns:
            return False
        return not allowed or callsign in allowed

    def _populate_locations(self, locations: Iterable[BbsLocationRecord]) -> None:
        location_rows = list(locations)
        locations_by_id = {location.location_id: location for location in location_rows}

        def _selector_label(location: BbsLocationRecord) -> str:
            ancestors: list[str] = []
            parent_id = location.parent_location_id
            seen = {location.location_id}
            while parent_id and parent_id not in seen:
                seen.add(parent_id)
                parent = locations_by_id.get(parent_id)
                if parent is None:
                    break
                ancestors.append(parent.name)
                parent_id = parent.parent_location_id
            hierarchy = " / ".join(reversed(ancestors + [location.name]))
            return f"{hierarchy}{' · Disabled' if not location.enabled else ''}"

        self.location_compact_selector.blockSignals(True)
        self.location_tree.blockSignals(True)
        try:
            self.location_compact_selector.clear()
            self.location_compact_selector.addItem("Managed BBS Library", "")
            for location in location_rows:
                self.location_compact_selector.addItem(_selector_label(location), location.location_id)
            self.location_tree.clear()
            root = QTreeWidgetItem(["Managed BBS Library"])
            root.setData(0, _LOCATION_ID_ROLE, "")
            root.setToolTip(0, "All catalog artifacts with an existing Managed BBS membership.")
            self.location_tree.addTopLevelItem(root)
            parents: dict[str, QTreeWidgetItem] = {"": root}
            unresolved = location_rows
            while unresolved:
                deferred: list[BbsLocationRecord] = []
                progressed = False
                for location in unresolved:
                    parent = parents.get(location.parent_location_id) or root
                    if location.parent_location_id and location.parent_location_id not in parents:
                        deferred.append(location)
                        continue
                    label = location.name
                    if not location.enabled:
                        label += " · Disabled"
                    item = QTreeWidgetItem([label])
                    item.setData(0, _LOCATION_ID_ROLE, location.location_id)
                    item.setToolTip(
                        0,
                        f"Location: {location.name}\nAccess: {_access_text(location.access_rule)}\n"
                        f"Retention: {_retention_text(location.retention_mode, location.retention_days)}\n"
                        f"Source: {location.source_dir or 'Not configured'}",
                    )
                    parent.addChild(item)
                    parents[location.location_id] = item
                    progressed = True
                if not deferred or not progressed:
                    for location in deferred:
                        parent = root
                        label = f"{location.name}{' · Disabled' if not location.enabled else ''}"
                        item = QTreeWidgetItem([label])
                        item.setData(0, _LOCATION_ID_ROLE, location.location_id)
                        item.setToolTip(
                            0,
                            f"Location: {location.name}\nAccess: {_access_text(location.access_rule)}\n"
                            f"Retention: {_retention_text(location.retention_mode, location.retention_days)}\n"
                            f"Source: {location.source_dir or 'Not configured'}",
                        )
                        parent.addChild(item)
                        parents[location.location_id] = item
                    break
                unresolved = deferred
            root.setExpanded(True)
            wanted = parents.get(self._selected_location_id, root)
            self.location_tree.setCurrentItem(wanted)
            selector_index = self.location_compact_selector.findData(self._selected_location_id)
            self.location_compact_selector.setCurrentIndex(selector_index if selector_index >= 0 else 0)
        finally:
            self.location_tree.blockSignals(False)
            self.location_compact_selector.blockSignals(False)
        self._sync_location_chips(location_rows)

    def _on_location_compact_selector_changed(self, index: int) -> None:
        location_id = str(self.location_compact_selector.itemData(index) or "") if index >= 0 else ""
        item = self._location_tree_item(location_id)
        if item is not None:
            self.location_tree.setCurrentItem(item)

    def _sync_location_chips(self, locations: Iterable[BbsLocationRecord]) -> None:
        rows = [location for location in locations if location.enabled]
        if self._publishing_location_id not in {location.location_id for location in rows}:
            self._publishing_location_id = rows[0].location_id if rows else ""
        self._add_location_chips(
            self.publishing_chips_layout,
            rows,
            selected_id=self._publishing_location_id,
            on_select=self._on_publishing_location_selected,
            include_all=False,
            accessible_prefix="Publishing location",
        )
        visible_rows = [location for location in rows if self._visitor_can_see_location(location)]
        if self._visitor_location_id not in {location.location_id for location in visible_rows}:
            self._visitor_location_id = ""
        self._add_location_chips(
            self.visitor_chips_layout,
            visible_rows,
            selected_id=self._visitor_location_id,
            on_select=self._on_visitor_location_selected,
            include_all=True,
            accessible_prefix="Visitor preview location",
        )

    def _location_tree_item(self, location_id: str) -> QTreeWidgetItem | None:
        root = self.location_tree.topLevelItem(0)
        pending = [root] if root is not None else []
        while pending:
            item = pending.pop()
            if str(item.data(0, _LOCATION_ID_ROLE) or "") == location_id:
                return item
            pending.extend(item.child(index) for index in range(item.childCount()))
        return None

    def _on_publishing_location_selected(self, location_id: str) -> None:
        if not location_id or location_id == self._publishing_location_id:
            return
        self._publishing_location_id = location_id
        self._load_artifacts()

    def _on_visitor_location_selected(self, location_id: str) -> None:
        self._visitor_location_id = location_id
        self._refresh_visitor_preview()

    def _on_artifact_filter_changed(self, _index: int = -1) -> None:
        self._artifact_filter = str(self.artifact_filter_combo.currentData() or "all")
        self._load_artifacts()

    def _populate_visitor_preview(self, locations: Iterable[BbsLocationRecord]) -> None:
        selected = next((row for row in locations if row.location_id == self._visitor_location_id), None)
        if selected is None:
            self.visitor_policy_label.setText(
                "All visible locations are shown. Choose a location chip to review its access and retention policy."
            )
        else:
            self.visitor_policy_label.setText(
                f"{selected.name}: {_access_text(selected.access_rule)}; "
                f"{_retention_text(selected.retention_mode, selected.retention_days)}. Read-only visitor view."
            )

    def _on_location_selection_changed(self) -> None:
        selected = self.location_tree.currentItem()
        self._selected_location_id = str(selected.data(0, _LOCATION_ID_ROLE) or "") if selected is not None else ""
        selector_index = self.location_compact_selector.findData(self._selected_location_id)
        if selector_index >= 0 and self.location_compact_selector.currentIndex() != selector_index:
            self.location_compact_selector.blockSignals(True)
            self.location_compact_selector.setCurrentIndex(selector_index)
            self.location_compact_selector.blockSignals(False)
        # isVisible() is false while an ancestor tab is hidden; isHidden()
        # reflects the operator's actual Edit/Hide choice.
        if not self.location_editor.isHidden():
            self._load_location_editor(self._locations_by_id.get(self._selected_location_id))
        location = self._locations_by_id.get(self._selected_location_id)
        if location is None:
            self.location_context_label.setText(
                "Select a location to review its policy. Use Edit to change a saved policy or Add to create one."
            )
            self.location_context_label.setToolTip("")
            self.location_copy_path_btn.setVisible(False)
        else:
            status = "Disabled — it remains in the catalog and no source files were removed." if not location.enabled else "Enabled"
            source_dir = str(location.source_dir or "").strip()
            source_summary = self._elided_path(source_dir) if source_dir else "Not configured"
            self.location_context_label.setText(
                f"{location.name} — {status}\n"
                f"Access: {_access_text(location.access_rule)} · "
                f"Retention: {_retention_text(location.retention_mode, location.retention_days)}\n"
                f"Source folder: {source_summary}"
            )
            self.location_context_label.setToolTip(
                f"Full source folder path: {source_dir}" if source_dir else "No source folder is configured."
            )
            self.location_copy_path_btn.setVisible(bool(source_dir))
            if location.enabled:
                self._publishing_location_id = location.location_id
        self._sync_location_chips(self._locations_by_id.values())
        self._load_artifacts()

    @staticmethod
    def _elided_path(path: str, *, limit: int = 84) -> str:
        """Keep policy detail compact while retaining the full path in a tooltip/copy action."""

        if len(path) <= limit:
            return path
        edge = max(12, (limit - 1) // 2)
        return f"{path[:edge]}…{path[-edge:]}"

    def _load_artifacts(self) -> None:
        # Selection and filtering are projections of the immutable worker
        # snapshot. No store or filesystem access is allowed on this path.
        rows = self._catalog_rows
        helper_rows = [row for row in rows if is_fio_bbs_helper_file_name(row.display_name or row.source_path)]
        catalog_displays = self._display_artifacts(
            row for row in rows if not is_fio_bbs_helper_file_name(row.display_name or row.source_path)
        )
        displays = self._filter_artifact_displays(catalog_displays)
        self._populate_artifacts(displays)
        self._populate_helpers(helper_rows)
        location_name = self._locations_by_id.get(self._publishing_location_id)
        scope = location_name.name if location_name is not None else "all locations"
        self.artifact_heading.setText(f"Artifacts — publish in {scope}")
        clipped = " (first 200)" if len(rows) >= MAX_ARTIFACT_ROWS else ""
        guidance = (
            "Select a location to change Published."
            if not self._publishing_location_id
            else "Published changes only catalog membership; source files remain in place."
        )
        self.summary_label.setText(
            f"{len(self._locations_by_id)} location{'s' if len(self._locations_by_id) != 1 else ''}; "
            f"{len(catalog_displays)} artifact{'s' if len(catalog_displays) != 1 else ''}{clipped}. {guidance}"
        )

    def _filter_artifact_displays(self, displays: Iterable[_ArtifactDisplay]) -> list[_ArtifactDisplay]:
        mode = self._artifact_filter
        if mode == "all":
            return list(displays)
        filtered: list[_ArtifactDisplay] = []
        for display in displays:
            selected_row = next(
                (row for row in display.rows if row.location_id == self._publishing_location_id),
                None,
            )
            state = str(selected_row.publication_state or "").lower() if selected_row is not None else ""
            if mode == "in_bbs" and state == "published":
                filtered.append(display)
            elif mode == "expired" and state == "retention_expired":
                filtered.append(display)
            elif mode == "removed" and state == "operator_disabled":
                filtered.append(display)
        return filtered

    @staticmethod
    def _helper_display_name(row: BbsArtifactAdminRow) -> str:
        name = Path(row.display_name or row.source_path or row.artifact_id).name
        clean = name[:-4] if name.lower().endswith(".txt") else name
        if clean.upper().startswith("00 READ FIRST"):
            return "00 HOW TO USE - Type command then refresh BBS"
        return clean

    @staticmethod
    def _helper_purpose(row: BbsArtifactAdminRow) -> str:
        name = Path(row.display_name or row.source_path or row.artifact_id).name.upper()
        if name.startswith("00 HOW TO USE") or name.startswith("00 READ FIRST") or name.startswith("00 NOTICE"):
            return "Visitor start"
        if name.startswith("01 COMMANDS"):
            return "Command index"
        if "TYPE FLAMP" in name:
            return "FLAMP request command"
        if re.match(r"^\d{2} TYPE ", name):
            return "Location request command"
        if name.startswith("BBS_QUEUE_LIST"):
            return "Queue status"
        if name.startswith("BBS_BLOCK_LIST"):
            return "Block status"
        return "Visitor navigation"

    def _populate_helpers(self, rows: Iterable[BbsArtifactAdminRow]) -> None:
        grouped: dict[str, list[BbsArtifactAdminRow]] = defaultdict(list)
        for row in rows:
            grouped[row.artifact_id].append(row)
        helpers = list(grouped.values())[:MAX_ARTIFACT_ROWS]
        self.helpers_table.setRowCount(len(helpers))
        for index, helper_rows in enumerate(helpers):
            row = helper_rows[0]
            locations = ", ".join(dict.fromkeys(item.location_name for item in helper_rows if item.location_name))
            values = (
                self._helper_display_name(row),
                self._helper_purpose(row),
                locations or "System projection",
                f"{row.age_days}d",
                _health_text(row),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setToolTip(
                    "System-generated helper; shown separately from operator files and not editable as catalog membership."
                )
                self.helpers_table.setItem(index, column, item)
        self.helpers_status.setText(
            f"{len(helpers)} generated visitor helper{'s' if len(helpers) != 1 else ''} in the bounded catalog view."
            if helpers
            else "No generated visitor helper files are present in the bounded catalog view. They remain excluded from Publishing."
        )

    def _display_artifacts(self, rows: Iterable[BbsArtifactAdminRow]) -> list[_ArtifactDisplay]:
        grouped: dict[str, list[BbsArtifactAdminRow]] = defaultdict(list)
        for row in rows:
            grouped[row.artifact_id].append(row)
        displays: list[_ArtifactDisplay] = []
        for artifact_id, artifact_rows in grouped.items():
            primary = artifact_rows[0]
            locations = tuple(item.location_name for item in artifact_rows)
            published_ids = tuple(item.location_id for item in artifact_rows if item.published)
            displays.append(_ArtifactDisplay(primary, tuple(artifact_rows), locations, published_ids))
        return displays[:MAX_ARTIFACT_ROWS]

    def _populate_artifacts(self, displays: Iterable[_ArtifactDisplay]) -> None:
        rendered = list(displays)
        self._loading = True
        self._display_rows = {display.row.artifact_id: display for display in rendered}
        self.artifact_table.blockSignals(True)
        try:
            self.artifact_table.clearContents()
            self.artifact_table.setRowCount(len(rendered))
            for index, display in enumerate(rendered):
                row = display.row
                published = QTableWidgetItem("")
                published.setData(_ARTIFACT_ID_ROLE, row.artifact_id)
                published.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                selected_location = self._locations_by_id.get(self._publishing_location_id)
                can_change = bool(selected_location is not None and selected_location.enabled)
                if not can_change:
                    published.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    published.setToolTip("Select one location before changing membership.")
                    published.setText("Choose location")
                else:
                    published.setToolTip("Check to publish this artifact in the selected managed location. Clearing it never deletes the source file.")
                    staged = self._pending_publication.get((row.artifact_id, self._publishing_location_id))
                    checked = self._publishing_location_id in display.published_location_ids if staged is None else staged
                    published.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                self.artifact_table.setItem(index, 0, published)
                name = QTableWidgetItem(row.display_name or row.artifact_id)
                name.setToolTip(row.source_path)
                self.artifact_table.setItem(index, 1, name)
                age = QTableWidgetItem(f"{row.age_days}d")
                age.setToolTip(_modified_detail(row.modified_utc))
                self.artifact_table.setItem(index, 2, age)
                selected_row = next(
                    (candidate for candidate in display.rows if candidate.location_id == self._publishing_location_id),
                    None,
                ) or row
                expires = QTableWidgetItem(_expires_text(selected_row))
                expires.setToolTip(
                    f"Exact expiry: {selected_row.expires_utc or 'none'}. Expiration never deletes the source file."
                )
                self.artifact_table.setItem(index, 3, expires)
                health = QTableWidgetItem(_health_text(selected_row))
                health.setToolTip(
                    f"Source state: {selected_row.source_state or 'unknown'}; "
                    f"publication state: {selected_row.publication_state or 'unknown'}"
                )
                self.artifact_table.setItem(index, 4, health)
        finally:
            self.artifact_table.blockSignals(False)
            self._loading = False
        if rendered:
            self.artifact_table.selectRow(0)
        else:
            self._clear_detail_panel()

    def _selected_display(self) -> _ArtifactDisplay | None:
        row_index = self.artifact_table.currentRow()
        item = self.artifact_table.item(row_index, 0) if row_index >= 0 else None
        artifact_id = str(item.data(_ARTIFACT_ID_ROLE) or "") if item is not None else ""
        return self._display_rows.get(artifact_id)

    def _clear_detail_panel(self) -> None:
        for label in self.detail_labels.values():
            label.setText("Select an artifact.")
            label.setToolTip("")
        self._update_selected_action_buttons(False)

    def _update_selected_action_buttons(self, selected: bool | None = None) -> None:
        has_selected = self._selected_display() is not None if selected is None else bool(selected)
        self.remove_from_bbs_btn.setEnabled(has_selected)
        location = self._locations_by_id.get(self._publishing_location_id)
        can_target = has_selected and location is not None and location.enabled
        self.keep_in_bbs_btn.setEnabled(can_target)
        self.republish_btn.setEnabled(can_target)
        display = self._selected_display() if has_selected else None
        selected_row = next(
            (row for row in display.rows if row.location_id == self._publishing_location_id),
            None,
        ) if display is not None else None
        keep = bool(selected_row and str(getattr(selected_row, "retention_class", "") or "").lower() == "keep")
        self.keep_in_bbs_btn.setText("Use Retention" if keep else "Keep in BBS")
        self.keep_in_bbs_btn.setToolTip(
            "Restore this file to the selected location's normal retention policy."
            if keep
            else "Keep the selected file in this BBS location without expiry."
        )

    def _refresh_detail_panel(self) -> None:
        display = self._selected_display()
        if display is None:
            self._clear_detail_panel()
            return
        self._update_selected_action_buttons(True)
        row = display.row
        location = self._locations_by_id.get(self._publishing_location_id or row.location_id)
        selected_location_id = self._publishing_location_id
        selected_row = next(
            (candidate for candidate in display.rows if candidate.location_id == selected_location_id),
            None,
        )
        if selected_location_id and selected_row is None:
            health_text = "Not published in selected location"
        else:
            health_text = _health_text(selected_row or row)
        retention_row = selected_row or row
        keep_override = str(getattr(retention_row, "retention_class", "") or "").lower() == "keep"
        values = {
            "origin": (row.source_kind or "Managed BBS catalog").replace("_", " ").title(),
            "path": row.source_path or "No source path recorded",
            "size": _byte_text(row.size),
            "age": f"{row.age_days}d; {_modified_detail(row.modified_utc)}",
            "access": _access_text(location.access_rule if location is not None else "public"),
            "retention": ("Keep in BBS override" if keep_override else _retention_text(retention_row.retention_mode, retention_row.retention_days))
            + (f"; expires {retention_row.expires_utc}" if retention_row.expires_utc else "; no expiry recorded"),
            "health": health_text,
        }
        for key, value in values.items():
            self.detail_labels[key].setText(value)
            self.detail_labels[key].setToolTip(value)

    def _on_publication_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 0 or not self._publishing_location_id:
            return
        artifact_id = str(item.data(_ARTIFACT_ID_ROLE) or "").strip()
        if not artifact_id:
            return
        display = self._display_rows.get(artifact_id)
        persisted = bool(display and self._publishing_location_id in display.published_location_ids)
        requested = item.checkState() == Qt.Checked
        key = (artifact_id, self._publishing_location_id)
        if requested == persisted:
            self._pending_publication.pop(key, None)
        else:
            self._pending_publication[key] = requested
        self._set_publication_status()

    def _set_publication_status(self, message: str = "") -> None:
        pending = len(self._pending_publication)
        self.apply_changes_btn.setEnabled(bool(pending))
        self.revert_changes_btn.setEnabled(bool(pending))
        self.publication_status_label.setText(message or (f"{pending} staged change{'s' if pending != 1 else ''}." if pending else "No pending changes."))

    def _apply_publication_changes(self) -> None:
        if not self._pending_publication:
            self._set_publication_status()
            return
        try:
            with connect_sqlite(bbs_library_db_path_from_settings(self.settings)) as conn:
                with conn:
                    by_artifact: dict[str, dict[str, bool]] = defaultdict(dict)
                    for (artifact_id, location_id), enabled in self._pending_publication.items():
                        by_artifact[artifact_id][location_id] = enabled
                    for artifact_id, changes in by_artifact.items():
                        current = set(list_bbs_artifact_location_ids(conn, artifact_id))
                        for location_id, enabled in changes.items():
                            if enabled:
                                current.add(location_id)
                            else:
                                current.discard(location_id)
                        set_bbs_artifact_locations(conn, artifact_id=artifact_id, location_ids=current)
                        for location_id, enabled in changes.items():
                            if enabled:
                                bbs_library_store.republish_bbs_artifact(
                                    conn,
                                    artifact_id=artifact_id,
                                    location_id=location_id,
                                )
        except Exception as exc:
            self._set_publication_status(f"Changes were not applied: {exc}")
            return
        count = len(self._pending_publication)
        self._pending_publication.clear()
        self._pending_publication_status = (
            f"Applied {count} staged change{'s' if count != 1 else ''}. Source files were not deleted."
        )
        self._set_publication_status(self._pending_publication_status)
        self.refresh_catalog()

    def _revert_publication_changes(self) -> None:
        self._pending_publication.clear()
        self._set_publication_status("Staged changes reverted.")
        self._load_artifacts()

    def _selected_artifact_id(self) -> str:
        display = self._selected_display()
        return display.row.artifact_id if display is not None else ""

    def _run_selected_bbs_action(self, function_name: str, action_label: str, *, all_locations: bool = False) -> None:
        artifact_id = self._selected_artifact_id()
        if not artifact_id:
            self._set_publication_status("Select a file before using this action.")
            return
        if not all_locations and not self._publishing_location_id:
            self._set_publication_status("Select a publishing location before using this action.")
            return
        operation = getattr(bbs_library_store, function_name, None)
        if not callable(operation):
            self._set_publication_status(f"{action_label} is unavailable in this build.")
            return
        try:
            with connect_sqlite(bbs_library_db_path_from_settings(self.settings)) as conn:
                with conn:
                    kwargs = {"artifact_id": artifact_id}
                    if not all_locations:
                        kwargs["location_id"] = self._publishing_location_id
                    operation(conn, **kwargs)
        except Exception as exc:
            self._set_publication_status(f"{action_label} failed: {exc}")
            return
        self._pending_publication = {
            key: value for key, value in self._pending_publication.items() if key[0] != artifact_id
        }
        self._pending_publication_status = f"{action_label} completed. Source and catalog records were preserved."
        self._set_publication_status(self._pending_publication_status)
        self.refresh_catalog()

    def _remove_selected_from_bbs(self) -> None:
        self._run_selected_bbs_action("remove_bbs_artifact_from_all_locations", "Remove from BBS", all_locations=True)

    def _keep_selected_in_bbs(self) -> None:
        display = self._selected_display()
        selected_row = next(
            (row for row in display.rows if row.location_id == self._publishing_location_id),
            None,
        ) if display is not None else None
        keep = not bool(
            selected_row
            and str(getattr(selected_row, "retention_class", "") or "").lower() == "keep"
        )
        artifact_id = self._selected_artifact_id()
        if not artifact_id or not self._publishing_location_id:
            self._set_publication_status("Select a file and publishing location before changing retention.")
            return
        try:
            with connect_sqlite(bbs_library_db_path_from_settings(self.settings)) as conn:
                with conn:
                    bbs_library_store.set_bbs_artifact_keep(
                        conn,
                        artifact_id=artifact_id,
                        location_id=self._publishing_location_id,
                        keep=keep,
                    )
        except Exception as exc:
            self._set_publication_status(f"Retention was not changed: {exc}")
            return
        self._pending_publication_status = (
            "File will remain in this BBS location until removed."
            if keep
            else "Normal location retention was restored."
        )
        self._set_publication_status(self._pending_publication_status)
        self.refresh_catalog()

    def _republish_selected_in_bbs(self) -> None:
        self._run_selected_bbs_action("republish_bbs_artifact", "Republish")
