from __future__ import annotations

import datetime as dt
import json
from copy import deepcopy
import math
import re
import sqlite3
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, Signal, QSize, QRectF, QModelIndex
from PySide6.QtGui import (
    QFont,
    QFontMetrics,
    QShortcut,
    QKeySequence,
    QColor,
    QPainter,
    QPen,
    QIcon,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QBoxLayout,
    QGridLayout,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QPushButton,
    QGroupBox,
    QTableWidget,
    QTableWidgetItem,
    QSizePolicy,
    QMessageBox,
    QHeaderView,
    QSplitter,
    QAbstractItemView,
    QInputDialog,
    QMenu,
    QCompleter,
    QScrollArea,
    QFrame,
    QToolButton,
    QProgressBar,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
)

from freqinout.core.config_paths import get_config_dir
from freqinout.core.condition_sop_policy import evaluate_condition_sop_invocations
from freqinout.core.group_utils import normalize_group_name
from freqinout.core.logger import log
from freqinout.core.operating_group_frequency import normalize_frequency_hz, resolve_operating_group_frequency
from freqinout.core.latest_read_cache import LatestReadCache
from freqinout.core.ops_intelligence import SearchRequest, read_search_brief, save_guidance_preference
from freqinout.gui.ops_intelligence_panel import SopAttentionWidget, SearchBriefWidget
from threading import Event
from freqinout.core.multi_radio_store import MultiRadioStore, settings_db_path
from freqinout.core.perf_metrics import span as perf_span
from freqinout.core.observation_queries import (
    ObservationQuery,
    activity_snapshot_from_observations,
    query_observations,
)
from freqinout.core.observation_store import list_observations, upsert_observation
from freqinout.core.ops_focus import (
    OpsFocus,
    OpsFocusSnapshot,
    OpsFocusSuggestion,
    backfill_ops_focus_index,
    backfill_ops_focus_observation_index,
    build_focus_snapshot,
    format_focus_last_known,
    search_focus_suggestions,
)
from freqinout.core.mesh import (
    clear_mesh_message_topic_override,
    default_mesh_db_path,
    set_mesh_message_topic_override,
)
from freqinout.core.controlfreq_awareness import AwarenessAction, build_awareness_snapshot, build_radio_source_lanes
from freqinout.core.operational_view_registry import (
    controlfreq_preset_names,
    controlfreq_view_labels,
    controlfreq_view_presets,
    operational_view_for,
)
from freqinout.core.plan_context_service import PlanContextService
from freqinout.core.propagation_service import PropagationService
from freqinout.core.schedule_targeting import (
    normalize_schedule_target_fields,
    schedule_row_matches_target_context,
)
from freqinout.core.sqlite_utils import connect_sqlite, fetch_all, rows_to_dicts, table_exists
from freqinout.core.dependency_status_service import get_dependency_status_service
from freqinout.core.software_status_service import PROGRAM_PATH_KEYS
from freqinout.core.station_readiness import (
    build_station_readiness_report,
    format_readiness_issue,
    readiness_report_detail_text,
    readiness_report_overall_text,
    readiness_state_label,
    should_show_startup_review,
    visible_status_programs,
)
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.sitrep_metadata import source_family_label
from freqinout.core.sop_manager import SOPManager
from freqinout.core.source_view_contracts import source_contract_for
from freqinout.core.message_projection_store import list_projected_attention_messages
from freqinout.core.traffic_actionability import (
    TrafficGroupVolume,
    TrafficActionSummary,
    build_traffic_action_summary,
    configured_group_names,
    filter_traffic_messages,
    load_projected_traffic_group_volumes,
    load_operator_traffic_context,
)
from freqinout.core.varac_bbs_inventory import build_bbs_inventory, format_bbs_inventory_detail
from freqinout.core.view_contracts import compose_intent_from_map_context, map_context_from_mapping
from freqinout.utils.timezones import get_timezone
from freqinout.gui.qsy_helper import (
    load_operating_groups,
    selected_qsy_meta,
    perform_qsy,
    perform_qsy_with_hold,
    current_scheduler_freq,
    refresh_hold_duration_combo,
    selected_hold_duration,
    set_hold_duration_default,
    notify_hold_duration_default_changed,
    suspend_snapshot,
    resume_schedule_hold,
    active_hold_button_role,
    active_hold_button_text,
    active_hold_status_text,
)
from freqinout.gui.stations_map_tab import (
    FEMA_REGIONS,
    LOWER48_STATES,
    PROP_BANDS,
    PROP_DEFAULT_PROFILES,
    STATE_CENTERS,
    STATE_TO_FEMA_REGION,
    US_STATE_ABBR_FROM_NAME,
    maidenhead_to_latlon,
)
from freqinout.gui.help_registry import resolve_help_host
from freqinout.gui.plan_context_label import PlanContextLabel
from freqinout.gui.traffic_action_summary_widget import TrafficActionSummaryWidget
from freqinout.gui.ops_dashboard import OpsDashboard
from freqinout.gui.theme import (
    apply_text_size_accessibility_guards,
    font_derived_widget_height,
    button_height_for_font,
    button_style,
    control_height_for_font,
    led_style,
    label_style,
    resolve_theme,
    single_line_label_height,
    style_splitter_handles,
)
from freqinout.version import __version__


FLMSG_FLAMP_RECENT_SECONDS = 24 * 60 * 60
FLMSG_FLAMP_SUMMARY_EXTS = {".b2s", ".k2s", ".txt", ".rtf", ".html", ".htm", ".xml", ".ff"}
FLMSG_FLAMP_SUMMARY_MAX_FILES = 750
FLMSG_FLAMP_SUMMARY_MAX_DIRS = 80

TRAFFIC_CHART_CURRENT_ROLE = int(Qt.UserRole) + 1
TRAFFIC_CHART_PREVIOUS_ROLE = int(Qt.UserRole) + 2
TRAFFIC_CHART_SCALE_ROLE = int(Qt.UserRole) + 3
TRAFFIC_CHART_TREND_ROLE = int(Qt.UserRole) + 4
PEER_TIMELINE_WINDOWS_ROLE = int(Qt.UserRole) + 11
PEER_TIMELINE_HORIZON_ROLE = int(Qt.UserRole) + 12


class TrafficVolumeBarDelegate(QStyledItemDelegate):
    """Paint an exact current/prior traffic comparison without losing table accessibility."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme: Dict[str, str] = {
            "surface_alt": "#DDE1E6",
            "border": "#D3D7DD",
            "text_muted": "#5B6570",
            "accent": "#2E6F9E",
            "warning": "#C99700",
            "text": "#1C1F21",
        }

    def apply_theme(self, theme: Dict[str, str]) -> None:
        self._theme = dict(theme or {})
        parent = self.parent()
        if isinstance(parent, QWidget):
            if hasattr(parent, "viewport"):
                parent.viewport().update()
            else:
                parent.update()

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # type: ignore[override]
        painter.save()
        try:
            selected = bool(option.state & QStyle.State_Selected)
            background = (
                option.palette.highlight().color()
                if selected
                else option.palette.base().color()
            )
            painter.fillRect(option.rect, background)
            painter.setRenderHint(QPainter.Antialiasing, True)

            current = max(0, int(index.data(TRAFFIC_CHART_CURRENT_ROLE) or 0))
            previous = max(0, int(index.data(TRAFFIC_CHART_PREVIOUS_ROLE) or 0))
            scale = max(1, int(index.data(TRAFFIC_CHART_SCALE_ROLE) or 1))
            trend = str(index.data(TRAFFIC_CHART_TREND_ROLE) or "")
            label = str(index.data(Qt.DisplayRole) or f"{current} now · {previous} prior")

            metrics = option.fontMetrics
            label_width = max(92, metrics.horizontalAdvance(label) + 8)
            outer = option.rect.adjusted(8, 7, -8, -7)
            bar_width = max(12, outer.width() - label_width - 8)
            bar_rect = QRectF(float(outer.x()), float(outer.center().y() - 6), float(bar_width), 12.0)

            track = QColor(self._theme.get("surface_alt", "#DDE1E6"))
            border = QColor(self._theme.get("border", "#D3D7DD"))
            muted = QColor(self._theme.get("text_muted", "#5B6570"))
            accent = QColor(
                self._theme.get("warning" if trend.startswith("Spike") else "accent", "#2E6F9E")
            )
            text_color = option.palette.highlightedText().color() if selected else QColor(
                self._theme.get("text", "#1C1F21")
            )

            painter.setPen(QPen(border, 1))
            painter.setBrush(track)
            painter.drawRoundedRect(bar_rect, 4.0, 4.0)

            current_width = bar_width * min(1.0, current / scale)
            if current > 0:
                painter.setPen(Qt.NoPen)
                painter.setBrush(accent)
                painter.drawRoundedRect(
                    QRectF(bar_rect.x(), bar_rect.y(), max(3.0, current_width), bar_rect.height()),
                    4.0,
                    4.0,
                )

            if previous > 0:
                marker_x = bar_rect.x() + (bar_width * min(1.0, previous / scale))
                marker_pen = QPen(muted, 2)
                marker_pen.setStyle(Qt.DashLine)
                painter.setPen(marker_pen)
                painter.drawLine(
                    int(marker_x),
                    int(bar_rect.top() - 3),
                    int(marker_x),
                    int(bar_rect.bottom() + 3),
                )

            painter.setPen(text_color)
            painter.drawText(
                outer.adjusted(bar_width + 8, 0, 0, 0),
                Qt.AlignVCenter | Qt.AlignLeft,
                label,
            )
        finally:
            painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # type: ignore[override]
        base = super().sizeHint(option, index)
        return QSize(base.width(), max(base.height(), option.fontMetrics.height() + 18))


class PeerRendezvousDelegate(QStyledItemDelegate):
    """Paint consolidated peer rendezvous windows in one stable viewport row."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme: Dict[str, str] = {
            "surface_alt": "#DDE1E6",
            "border": "#D3D7DD",
            "text_muted": "#5B6570",
            "accent": "#2E6F9E",
            "text": "#1C1F21",
        }

    def apply_theme(self, theme: Dict[str, str]) -> None:
        self._theme = dict(theme or {})
        parent = self.parent()
        if isinstance(parent, QTableWidget):
            parent.viewport().update()

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # type: ignore[override]
        painter.save()
        try:
            selected = bool(option.state & QStyle.State_Selected)
            painter.fillRect(
                option.rect,
                option.palette.highlight().color() if selected else option.palette.base().color(),
            )
            painter.setRenderHint(QPainter.Antialiasing, True)
            windows = tuple(index.data(PEER_TIMELINE_WINDOWS_ROLE) or ())
            horizon = max(1, int(index.data(PEER_TIMELINE_HORIZON_ROLE) or 120))
            label = str(index.data(Qt.DisplayRole) or "No matching window")
            outer = option.rect.adjusted(8, 4, -8, -4)
            text_color = (
                option.palette.highlightedText().color()
                if selected
                else QColor(self._theme.get("text", "#1C1F21"))
            )
            painter.setPen(text_color)
            painter.drawText(
                outer.adjusted(0, 0, 0, -14),
                Qt.AlignLeft | Qt.AlignVCenter,
                option.fontMetrics.elidedText(label, Qt.ElideRight, max(20, outer.width())),
            )

            track_rect = QRectF(float(outer.x()), float(outer.bottom() - 11), float(max(12, outer.width())), 10.0)
            painter.setPen(QPen(QColor(self._theme.get("border", "#D3D7DD")), 1))
            painter.setBrush(QColor(self._theme.get("surface_alt", "#DDE1E6")))
            painter.drawRoundedRect(track_rect, 3.0, 3.0)
            accent = QColor(self._theme.get("accent", "#2E6F9E"))
            painter.setPen(Qt.NoPen)
            visible_windows = windows[:8]
            lane_count = max(1, min(3, len(visible_windows)))
            lane_height = max(2.0, (track_rect.height() - 2.0) / lane_count)
            for window_index, window in enumerate(visible_windows):
                try:
                    start = max(0, min(horizon, int(window.get("start_offset_minutes") or 0)))
                    end = max(start + 1, min(horizon, int(window.get("end_offset_minutes") or start + 1)))
                except (AttributeError, TypeError, ValueError):
                    continue
                x = track_rect.x() + (track_rect.width() * start / horizon)
                width = max(4.0, track_rect.width() * (end - start) / horizon)
                lane = window_index % lane_count
                lane_color = QColor(accent)
                if lane:
                    lane_color = lane_color.lighter(112 + (lane * 10))
                painter.setBrush(lane_color)
                painter.drawRoundedRect(
                    QRectF(
                        x,
                        track_rect.y() + 1.0 + (lane * lane_height),
                        width,
                        max(2.0, lane_height - 1.0),
                    ),
                    2.0,
                    2.0,
                )
        finally:
            painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # type: ignore[override]
        base = super().sizeHint(option, index)
        return QSize(base.width(), max(base.height(), option.fontMetrics.height() + 24))


class _ActivityReadContext:
    """Detached aggregation inputs; never contains widgets or SettingsManager."""
    def __init__(self, db_path, group_freqs, group_bands, operator_groups=None, operating_groups=()):
        self.db_path = db_path
        self.group_freqs = {key: list(values) for key, values in group_freqs.items()}
        self.group_bands = {key: set(values) for key, values in group_bands.items()}
        self.operating_groups = [deepcopy(dict(row)) for row in operating_groups if isinstance(row, Mapping)]
        self.operator_groups = (
            {str(key): set(values) for key, values in operator_groups.items()}
            if operator_groups is not None else None
        )
        self._operator_groups_cache = {}
        self._operator_groups_cache_ts = 0.0
        self._operator_groups_cache_ttl_sec = 0.0
        self._operator_groups_cache_mtime = 0.0

    def _db_path(self):
        return self.db_path

    def _group_freq_map(self):
        return self.group_freqs

    def _group_band_map(self):
        return self.group_bands

    def _load_operator_group_map(self):
        if self.operator_groups is not None:
            return {key: set(values) for key, values in self.operator_groups.items()}
        return ControlFreqTab._load_operator_group_map(self)

    @staticmethod
    def _is_activity_callsign_token(value):
        return ControlFreqTab._is_activity_callsign_token(value)


@dataclass(frozen=True)
class _OperatorRosterSnapshot:
    group_map: Dict[str, Set[str]]
    peer_meta: Dict[str, Dict[str, object]]
    geo: Dict[str, Dict[str, str]]
    peer_schedule_rows: List[Dict[str, Any]]
    my_schedule_rows: List[Dict[str, Any]]


class _OperatorRosterReadWorker:
    """One detached, read-only publication for roster and peer schedule inputs."""

    MAX_OPERATOR_ROWS = 25_000
    MAX_SCHEDULE_ROWS = 50_000
    MAX_PEER_SCHEDULE_ROWS = 50_000

    def __init__(self, generation: int, settings_db_path: Path, nets_db_path: Path):
        self.generation = int(generation)
        self.settings_db_path = Path(settings_db_path)
        self.nets_db_path = Path(nets_db_path)

    @staticmethod
    def _connect_readonly(path: Path):
        if not path.is_file():
            return None
        # URI mode=ro prevents this background read from creating a missing DB.
        from urllib.parse import quote

        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1500")
        return conn

    @staticmethod
    def _table_names(conn: sqlite3.Connection) -> Set[str]:
        return {
            str(row[0] or "")
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }

    @staticmethod
    def _rows(
        conn: sqlite3.Connection,
        table: str,
        *,
        limit: int,
    ) -> List[Dict[str, Any]]:
        if table not in _OperatorRosterReadWorker._table_names(conn):
            return []
        rows = conn.execute(
            f'SELECT * FROM "{table}" LIMIT ?', (int(limit) + 1,)
        ).fetchall()
        if len(rows) > int(limit):
            raise RuntimeError(f"{table} exceeds the {int(limit)} row publication bound")
        return [dict(row) for row in rows]

    def run(self) -> Dict[str, Any]:
        settings_conn = nets_conn = None
        try:
            settings_conn = self._connect_readonly(self.settings_db_path)
            nets_conn = self._connect_readonly(self.nets_db_path)
            if nets_conn is None:
                raise FileNotFoundError("operator roster database is unavailable")
            # Each database is read from one stable read transaction. Settings
            # and Nets are separate files, so this intentionally makes no
            # cross-database atomicity claim.
            nets_conn.execute("BEGIN")
            if settings_conn is not None:
                settings_conn.execute("BEGIN")

            if "operator_checkins" not in self._table_names(nets_conn):
                raise RuntimeError("operator roster table is unavailable")
            checkins = self._rows(
                nets_conn, "operator_checkins", limit=self.MAX_OPERATOR_ROWS
            )
            groups: Dict[str, Set[str]] = {}
            peer_meta: Dict[str, Dict[str, object]] = {}
            geo: Dict[str, Dict[str, str]] = {}
            for row in checkins:
                callsign = str(row.get("callsign") or "").strip().upper()
                if not callsign:
                    continue
                operator_groups: Set[str] = set()
                for key in ("group1", "group2", "group3"):
                    value = normalize_group_name(row.get(key, ""))
                    if value:
                        operator_groups.add(value)
                try:
                    parsed = json.loads(row.get("groups_json") or "[]")
                    for raw in parsed if isinstance(parsed, list) else ():
                        value = normalize_group_name(raw)
                        if value:
                            operator_groups.add(value)
                except Exception:
                    pass
                if operator_groups:
                    groups[callsign] = operator_groups
                peer_meta[callsign] = {
                    "groups": operator_groups,
                    "role": str(row.get("group_role") or "").strip().upper(),
                    "region": str(row.get("roster_region") or row.get("state") or "").strip().upper(),
                }
                geo[callsign] = {
                    "state": ControlFreqTab._normalize_state_abbr(str(row.get("state") or "")),
                    "grid": str(row.get("grid") or "").strip().upper(),
                }

            peer_rows: List[Dict[str, Any]] = []
            names = self._table_names(nets_conn)
            peer_table = (
                "peer_hf_schedule_effective"
                if "peer_hf_schedule_effective" in names
                else "peer_hf_schedule"
            )
            if peer_table not in names:
                raise RuntimeError("peer schedule table is unavailable")
            peer_rows = [
                dict(row)
                for row in nets_conn.execute(
                    f"SELECT owner_callsign, day_utc, start_utc, end_utc, band, frequency "
                    f"FROM \"{peer_table}\" LIMIT ?",
                    (self.MAX_PEER_SCHEDULE_ROWS + 1,),
                ).fetchall()
            ]
            if len(peer_rows) > self.MAX_PEER_SCHEDULE_ROWS:
                raise RuntimeError(
                    f"peer schedule exceeds the {self.MAX_PEER_SCHEDULE_ROWS} row publication bound"
                )

            my_schedule_rows: List[Dict[str, Any]] = []
            for source_conn in (settings_conn, nets_conn):
                if source_conn is None:
                    continue
                for table in ("daily_schedule_tab", "net_schedule_tab"):
                    my_schedule_rows.extend(
                        self._rows(
                            source_conn,
                            table,
                            limit=self.MAX_SCHEDULE_ROWS,
                        )
                    )

            return {
                    "generation": self.generation,
                    "ok": True,
                    "snapshot": _OperatorRosterSnapshot(
                        group_map=groups,
                        peer_meta=peer_meta,
                        geo=geo,
                        peer_schedule_rows=peer_rows,
                        my_schedule_rows=my_schedule_rows,
                    ),
                    "error": "",
                }
        except Exception as exc:
            return {"generation": self.generation, "ok": False, "snapshot": None, "error": str(exc)}
        finally:
            for source_conn in (settings_conn, nets_conn):
                try:
                    if source_conn is not None:
                        source_conn.close()
                except Exception:
                    pass


class ControlFreqTab(QWidget):
    """
    ControlFreq: summary/console view for activity and operational status.
    """

    _message_summary_ready = Signal(int, object, object)
    _focus_suggestions_ready = Signal(int, object, object)
    _focus_snapshot_ready = Signal(int, object, object)
    _focus_backfill_ready = Signal(bool, int)
    _local_nets_outlook_ready = Signal(int, object, object)
    _shortwave_listening_outlook_ready = Signal(int, object, object)
    _shortwave_listening_action_ready = Signal(object)
    _propagation_ready = Signal(int, object, object)
    _operator_roster_ready = Signal(object)
    # Local Nets are reminder-only.  The host owns persistence and typed routing;
    # this presentation seam deliberately carries the immutable projection item
    # back to the host instead of interpreting it as a scheduler row.
    local_net_details_requested = Signal(object)
    local_net_dismiss_requested = Signal(object)
    local_net_open_sop_requested = Signal(object)
    shortwave_listening_details_requested = Signal(object)

    def __init__(
        self,
        parent=None,
        *,
        plan_context_service: Optional[PlanContextService] = None,
        defer_initial_refresh: bool = False,
    ):
        super().__init__(parent)
        # The main shell creates Ops Center before it can paint.  Keep the
        # constructor limited to widget/layout construction there; the first
        # active-tab tick owns DB-backed projections and index maintenance.
        # Standalone/test callers retain the historical eager behaviour.
        self._defer_initial_refresh = bool(defer_initial_refresh)
        self.settings = SettingsManager()
        self.plan_context_service = plan_context_service
        self._sop_manager = SOPManager()
        self._timer: Optional[QTimer] = None
        self._active = False
        self._brief_cache = LatestReadCache("fio-ops-brief")
        self._guidance_save_cache = LatestReadCache("fio-ops-guidance-save")
        self._brief_request = None
        self._brief_cancel = Event()
        self._brief_generation = 0
        self._brief_published = None
        self._guidance_save_request = None
        self._guidance_save_generation = 0
        # Paths are already cached by SettingsManager; no file probing on UI.
        self._brief_db_path = Path(self.settings.db_path).with_name("freqinout_nets.db")
        self._guidance_db_path = Path(self.settings.db_path)
        self._brief_poll = QTimer(self)
        self._brief_poll.setInterval(100)
        self._brief_poll.timeout.connect(self._poll_intelligence_publications)
        self._last_refresh_ts = 0.0
        self._theme_cache: Optional[Dict[str, Any]] = None
        self._settings_reload_mtime_ns = 0
        self._freq_timer: Optional[QTimer] = None
        self._status_timer: Optional[QTimer] = None
        self._clock_timer: Optional[QTimer] = None
        self._show_local = True
        self._prop_target_syncing = False
        self._prop_operator_geo: Dict[str, Dict[str, str]] = {}
        self._focus_mode = False
        self._freq_ctrl_fixed_height = 0
        self._pending_group_filter = ""
        self._saved_top_sizes: List[int] = []
        self._saved_left_sizes: List[int] = []
        self._saved_right_sizes: List[int] = []
        self._schedule_entries_by_row: Dict[int, Dict[str, Any]] = {}
        self._next_schedule_outlook_preview: Optional[Dict[str, Any]] = None
        self._local_nets_outlook_items: Tuple[Any, ...] = ()
        self._local_nets_outlook_rendered_revision = -1
        self._local_nets_outlook_revision = 0
        self._local_nets_outlook_provider: Optional[Callable[[dt.datetime], Any]] = None
        self._local_nets_outlook_executor: Optional[ThreadPoolExecutor] = None
        self._local_nets_outlook_pending = False
        self._local_nets_outlook_followup = False
        self._local_nets_outlook_request_id = 0
        self._shortwave_listening_outlook_items: Tuple[Any, ...] = ()
        self._shortwave_listening_outlook_provider: Optional[Callable[[dt.datetime], Any]] = None
        self._shortwave_listening_outlook_executor: Optional[ThreadPoolExecutor] = None
        self._shortwave_listening_outlook_pending = False
        self._shortwave_listening_outlook_followup = False
        self._shortwave_listening_outlook_request_id = 0
        self._shortwave_listening_dismiss_provider: Optional[Callable[[Any], None]] = None
        self._shortwave_listening_action_pending = False
        self._force_hero_resync = False
        self._message_summary_target_height = 0
        self._freq_meta_full_text = "Scheduled: -- | Active: --"
        self._hero_combo_font_px = 18
        self._primary_freq_action_mode = "none"
        self._freq_action_busy_reason_label: Optional[str] = None
        self._hold_state_snapshot: Optional[Dict[str, object]] = None
        self._freq_combo_cache_key: Tuple[Tuple[str, str, float, str, str, bool], ...] = ()
        self._last_hero_sched_freq_mhz: Optional[float] = None
        self._last_hero_display_freq_mhz: Optional[float] = None
        self._current_time_full_text = "--"
        self._operator_groups_cache: Dict[str, Set[str]] = {}
        self._operator_groups_cache_ts = 0.0
        self._operator_groups_cache_mtime = 0.0
        self._operator_groups_cache_ttl_sec = 20.0
        self._operator_peer_meta_cache: Dict[str, Dict[str, object]] = {}
        self._operator_peer_meta_cache_ts = 0.0
        self._operator_peer_meta_cache_mtime = 0.0
        self._operator_roster_snapshot: Optional[_OperatorRosterSnapshot] = None
        self._operator_roster_published_at = 0.0
        self._operator_roster_ttl_sec = 60.0
        self._operator_roster_generation = 0
        self._operator_roster_inflight_generation: Optional[int] = None
        self._operator_roster_pending = False
        self._operator_roster_retry_after = 0.0
        self._operator_roster_error = ""
        self._operator_roster_executor: Optional[ThreadPoolExecutor] = None
        self._operator_roster_future: Optional[Future] = None
        self._operator_roster_ready.connect(self._on_operator_roster_ready)
        self._activity_cache_key: Tuple[Any, ...] = ()
        self._activity_cache_rows: List[List[str]] = []
        self._activity_cache_ts = 0.0
        self._activity_cache_ttl_sec = 10.0
        self._operational_snapshot_cache_key: Tuple[Any, ...] = ()
        self._operational_snapshot_cache_ts = 0.0
        self._operational_snapshot_cache_ttl_sec = 10.0
        self._operational_activity_context: Dict[str, str] = {}
        self._operational_awareness_context: Dict[str, str] = {}
        self._operational_awareness_full_snapshot: object | None = None
        self._awareness_topic_filter = ""
        self._awareness_row_contexts: List[Dict[str, str]] = []
        self._source_lane_contexts: List[Dict[str, str]] = []
        self._source_family_filter = ""
        self._source_lane_syncing = False
        self._my_schedule_entries_cache: List[Dict[str, object]] = []
        self._my_schedule_entries_cache_ts = 0.0
        self._my_schedule_entries_cache_key: Tuple[float, float] = (0.0, 0.0)
        self._my_schedule_entries_cache_ttl_sec = 20.0
        self._daily_schedule_rows_cache: List[Dict[str, Any]] = []
        self._daily_schedule_rows_cache_ts = 0.0
        self._daily_schedule_rows_cache_mtime = 0.0
        self._daily_schedule_rows_cache_ttl_sec = 10.0
        self._net_schedule_rows_cache: List[Dict[str, Any]] = []
        self._net_schedule_rows_cache_ts = 0.0
        self._net_schedule_rows_cache_mtime = 0.0
        self._net_schedule_rows_cache_ttl_sec = 10.0
        self._peer_schedule_rows_cache: List[Dict[str, Any]] = []
        self._peer_schedule_rows_cache_ts = 0.0
        self._peer_schedule_rows_cache_mtime = 0.0
        self._peer_schedule_rows_cache_ttl_sec = 20.0
        self._view_cards: Dict[str, bool] = dict(controlfreq_view_presets().get("All", {}))
        self._view_preset = "All"
        self._view_syncing = False
        self._card_expanded_heights: Dict[str, int] = {}
        self._card_animations: Dict[str, QPropertyAnimation] = {}
        self._responsive_layout_mode = "wide"
        self._responsive_compact_width = 1200
        self.status_labels: Dict[str, QLabel] = {}
        self._status_text_labels: Dict[str, QLabel] = {}
        self._status_checked_at: Dict[str, str] = {}
        self._status_service = get_dependency_status_service(self.settings)
        try:
            self._status_service.snapshot_changed.connect(self._on_dependency_status_snapshot_changed)
        except Exception:
            pass
        self._multi_radio_store = MultiRadioStore()
        self._readiness_banner_dismissed = False
        self._readiness_banner_digest = ""
        self._readiness_has_device_profiles: bool | None = None
        self._readiness_suppressed_version = str(self.settings.get("readiness_review_suppressed_version", "") or "").strip()
        self._readiness_dismissed_digest = str(self.settings.get("readiness_review_dismissed_digest", "") or "").strip()
        self._sop_window_cache: Dict[Tuple[Any, ...], Tuple[float, List[Dict[str, Any]]]] = {}
        self._sop_today_cache_ttl_sec = 30.0
        self._sop_tomorrow_cache_ttl_sec = 180.0
        self._sop_cache_epoch = 0
        self._sop_outlook_refresh_pending = False
        self._prop_service = PropagationService(
            default_profiles=PROP_DEFAULT_PROFILES,
            profiles_path=None,
            climatology_db_path=get_config_dir() / "config" / "propagation" / "prop_climatology.db",
            outcome_db_path=get_config_dir() / "config" / "freqinout_nets.db",
            db_index_mode="round_halfdeg",
        )
        self._prefs_timer = QTimer(self)
        self._prefs_timer.setSingleShot(True)
        self._prefs_timer.timeout.connect(self._persist_ui_state)
        # One UI-only geometry owner coalesces the many dynamic Ops card
        # mutations. It never performs settings, database, network, or radio
        # work; it only republishes the current layout extent to the outer
        # page scroll area.
        self._ops_geometry_sync_running = False
        self._ops_geometry_sync_count = 0
        self._ops_geometry_timer = QTimer(self)
        self._ops_geometry_timer.setSingleShot(True)
        self._ops_geometry_timer.setInterval(0)
        self._ops_geometry_timer.timeout.connect(self._run_ops_geometry_sync)
        self._filter_refresh_timer = QTimer(self)
        self._filter_refresh_timer.setSingleShot(True)
        self._filter_refresh_timer.timeout.connect(self._run_filter_refresh)
        self._peer_filter_timer = QTimer(self)
        self._peer_filter_timer.setSingleShot(True)
        self._peer_filter_timer.setInterval(150)
        self._peer_filter_timer.timeout.connect(self._on_peer_filters_changed)
        self._activation_refresh_pending = False
        self._activation_refresh_interval_sec = 60.0
        self._secondary_refresh_pending = False
        self._secondary_refresh_interval_sec = 60.0
        self._last_secondary_refresh_ts = 0.0
        self._heavy_refresh_pending = False
        self._heavy_refresh_interval_sec = 120.0
        self._last_heavy_refresh_ts = 0.0
        self._message_summary_executor: Optional[ThreadPoolExecutor] = None
        self._message_summary_pending = False
        self._message_summary_followup = False
        self._message_summary_request_id = 0
        self._message_summary_applied_id = 0
        self._message_summary_cache_rows: List[List[str]] = []
        self._message_summary_ready.connect(self._on_message_summary_ready)
        self._propagation_executor: Optional[ThreadPoolExecutor] = None
        self._propagation_pending = False
        self._propagation_followup = False
        self._propagation_request_id = 0
        self._propagation_ready.connect(self._on_propagation_ready)
        self._focus_executor: Optional[ThreadPoolExecutor] = None
        self._focus_suggestion_request_id = 0
        self._focus_snapshot_request_id = 0
        self._focus_apply_when_ready = False
        self._focus_suggestions: tuple[OpsFocusSuggestion, ...] = ()
        self._active_ops_focus: OpsFocus | None = None
        self._focus_suggestion_cache: OrderedDict[tuple[object, ...], tuple[OpsFocusSuggestion, ...]] = OrderedDict()
        self._focus_snapshot_cache: OrderedDict[tuple[object, ...], OpsFocusSnapshot] = OrderedDict()
        self._focus_suggestion_cache_keys: Dict[int, tuple[object, ...]] = {}
        self._focus_snapshot_cache_keys: Dict[int, tuple[object, ...]] = {}
        self._focus_backfill_pending = False
        self._focus_backfill_complete = False
        self._focus_suggestions_ready.connect(self._on_focus_suggestions_ready)
        self._focus_snapshot_ready.connect(self._on_focus_snapshot_ready)
        self._focus_backfill_ready.connect(self._on_focus_backfill_ready)
        self._local_nets_outlook_ready.connect(self._on_local_nets_outlook_ready)
        self._shortwave_listening_outlook_ready.connect(self._on_shortwave_listening_outlook_ready)
        self._shortwave_listening_action_ready.connect(self._on_shortwave_listening_action_ready)
        self._focus_autocomplete_timer = QTimer(self)
        self._focus_autocomplete_timer.setSingleShot(True)
        self._focus_autocomplete_timer.setInterval(125)
        self._focus_autocomplete_timer.timeout.connect(self._request_focus_suggestions)
        self.destroyed.connect(lambda *_args: self._shutdown_background_executors())
        self._build_ui()
        refresh_hold_duration_combo(self.hold_duration_combo, self.settings, self._runtime_hold_duration_profile())
        self._restore_ui_state()
        self._apply_theme()
        if self._defer_initial_refresh:
            # A clock-only first frame makes the dashboard immediately
            # intelligible without opening SQLite, refreshing schedule views,
            # or competing with startup ingestion for the database lock.
            self._refresh_clock_display()
            self._last_refresh_ts = 0.0
        else:
            self._refresh_all()
            QTimer.singleShot(0, self._schedule_focus_index_backfill)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.controlfreq_scroll = QScrollArea(self)
        self.controlfreq_scroll.setObjectName("controlfreqScrollArea")
        self.controlfreq_scroll.setWidgetResizable(True)
        self.controlfreq_scroll.setFrameShape(QFrame.NoFrame)
        self.controlfreq_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(self.controlfreq_scroll, 1)

        self.controlfreq_content = QWidget(self.controlfreq_scroll)
        root = QVBoxLayout(self.controlfreq_content)
        self.controlfreq_root_layout = root
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        self.controlfreq_scroll.setWidget(self.controlfreq_content)

        self.readiness_review_widget = QWidget()
        readiness_layout = QVBoxLayout(self.readiness_review_widget)
        readiness_layout.setContentsMargins(10, 8, 10, 8)
        readiness_layout.setSpacing(8)
        self.readiness_review_label = QLabel()
        self.readiness_review_label.setWordWrap(True)
        readiness_layout.addWidget(self.readiness_review_label)
        readiness_actions = QGridLayout()
        readiness_actions.setContentsMargins(0, 0, 0, 0)
        readiness_actions.setHorizontalSpacing(8)
        readiness_actions.setVerticalSpacing(8)
        self.readiness_review_now_btn = QPushButton("Review Now")
        self.readiness_review_now_btn.clicked.connect(self._review_readiness_now)
        readiness_actions.addWidget(self.readiness_review_now_btn, 0, 0)
        self.readiness_review_copy_btn = QPushButton("Copy Summary")
        self.readiness_review_copy_btn.clicked.connect(self._copy_readiness_review_summary)
        readiness_actions.addWidget(self.readiness_review_copy_btn, 0, 1)
        self.readiness_review_dismiss_btn = QPushButton("Dismiss")
        self.readiness_review_dismiss_btn.clicked.connect(self._dismiss_readiness_review)
        readiness_actions.addWidget(self.readiness_review_dismiss_btn, 0, 2)
        self.readiness_review_suppress_btn = QPushButton("Do Not Remind Again For This Version")
        self.readiness_review_suppress_btn.clicked.connect(self._suppress_readiness_review_for_version)
        readiness_actions.addWidget(self.readiness_review_suppress_btn, 1, 0, 1, 3)
        readiness_actions.setColumnStretch(3, 1)
        readiness_layout.addLayout(readiness_actions)
        self.readiness_review_widget.setVisible(False)
        root.addWidget(self.readiness_review_widget)

        header = QHBoxLayout()
        title = QLabel("<h3>Ops Center</h3>")
        header.addWidget(title)
        self.help_btn = QPushButton("Help")
        self.help_btn.setToolTip("Open Ops Center help.")
        self.help_btn.clicked.connect(lambda: self._open_context_help("tab.controlfreq"))
        header.addWidget(self.help_btn)

        header.addStretch(1)
        root.addLayout(header)

        self.sop_attention_widget = SopAttentionWidget(self)
        self.sop_attention_widget.set_guidance_enabled(bool(self.settings.get("controlfreq_sop_guidance", True)))
        self.sop_attention_widget.guidanceChanged.connect(self._set_sop_guidance)
        self.sop_attention_widget.contextRequested.connect(self._activate_intelligence_context)
        root.addWidget(self.sop_attention_widget)

        filter_row = QGridLayout()
        self.filter_row = filter_row
        filter_row.setSpacing(8)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Focus on callsign, group, event, topic, place, band, or source…")
        self.search_edit.setAccessibleName("Focus Ops Center")
        self.search_edit.setAccessibleDescription(
            "Type an operational entity. Suggestions do not refresh the dashboard until selected."
        )
        self.search_edit.textChanged.connect(self._clear_intelligence_search)
        self.search_edit.textChanged.connect(self._on_focus_search_text_changed)
        self.search_edit.returnPressed.connect(self._apply_best_focus_suggestion)
        self.search_edit.setMinimumWidth(220)
        self.search_edit.setMaximumWidth(420)

        self._focus_completion_model = QStandardItemModel(self)
        self._focus_completer = QCompleter(self._focus_completion_model, self)
        self._focus_completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self._focus_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._focus_completer.setMaxVisibleItems(14)
        self._focus_completer.activated[QModelIndex].connect(self._activate_focus_completion)
        self.search_edit.setCompleter(self._focus_completer)

        self.app_search_btn = QPushButton("Focus")
        self.app_search_btn.setToolTip("Apply the best matching operational focus.")
        self.app_search_btn.clicked.connect(self._apply_best_focus_suggestion)
        self.command_palette_btn = QToolButton()
        self.command_palette_btn.setText("Go…")
        self.command_palette_btn.setToolTip("Find a screen, setting, radio, schedule, action, or setup issue (Ctrl+K).")
        self.command_palette_btn.setAccessibleName("Open command search")
        self.command_palette_btn.clicked.connect(self._show_command_palette)

        self.intelligence_search_btn = QToolButton()
        self.intelligence_search_btn.setText("Search")
        self.intelligence_search_btn.setAccessibleName("Search known information")
        self.intelligence_search_btn.setToolTip("Search the entered exact callsign, topic, or SOP event. Results use the dashboard filters.")
        self.intelligence_search_btn.setPopupMode(QToolButton.InstantPopup)
        search_menu = QMenu(self.intelligence_search_btn)
        for label, kind in (("Search callsign", "callsign"), ("Search topic", "topic"), ("Search SOP event", "event")):
            action = search_menu.addAction(label)
            action.triggered.connect(lambda _checked=False, k=kind: self._start_intelligence_search(k))
        self.intelligence_search_btn.setMenu(search_menu)

        self.group_combo = QComboBox()
        self.group_combo.setMinimumWidth(130)
        self.group_combo.currentIndexChanged.connect(self._on_filters_changed)

        self.traffic_source_combo = QComboBox()
        self.traffic_source_combo.setToolTip("Limit Ops Center traffic to one message source.")
        self.traffic_source_combo.addItem("Traffic Source: All", "")
        self.traffic_source_combo.addItem("FLMsg/FLAmp", "nbems")
        self.traffic_source_combo.addItem("JS8Call", "js8call")
        self.traffic_source_combo.addItem("CommsPlan Spotter", "spotter")
        self.traffic_source_combo.addItem("CommStat", "commstat")
        self.traffic_source_combo.addItem("Mesh", "meshcore")
        self.traffic_source_combo.addItem("VarAC", "varac")
        self.traffic_source_combo.addItem("BBS", "bbs")
        self.traffic_source_combo.setMinimumWidth(140)
        self.traffic_source_combo.currentIndexChanged.connect(self._on_traffic_source_filter_changed)

        self.traffic_age_combo = QComboBox()
        self.traffic_age_combo.setToolTip(
            "Bound traffic counts and action queues by when CommsPlan received each message."
        )
        for label, seconds in (
            ("Traffic: 1h", 60 * 60),
            ("Traffic: 6h", 6 * 60 * 60),
            ("Traffic: 24h", 24 * 60 * 60),
            ("Traffic: 7d", 7 * 24 * 60 * 60),
            ("Traffic: 30d", 30 * 24 * 60 * 60),
            ("Traffic: All", 0),
        ):
            self.traffic_age_combo.addItem(label, seconds)
        self.traffic_age_combo.setCurrentIndex(2)
        self.traffic_age_combo.setMinimumWidth(112)
        self.traffic_age_combo.currentIndexChanged.connect(self._on_filters_changed)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_all)

        self.clear_filters_btn = QPushButton("Clear Filters")
        self.clear_filters_btn.clicked.connect(self._clear_filters)

        self.time_toggle_btn = QPushButton("Times: Local")
        self.time_toggle_btn.clicked.connect(self._toggle_time_view)

        self.focus_mode_btn = QPushButton("Focus Mode: Off")
        self.focus_mode_btn.clicked.connect(self._toggle_focus_mode)
        self.focus_mode_btn.setVisible(False)

        self._arrange_filter_controls(compact=False)
        root.addLayout(filter_row)

        self.focus_banner = QFrame()
        self.focus_banner.setObjectName("controlfreqFocusBanner")
        self.focus_banner.setVisible(False)
        focus_layout = QHBoxLayout(self.focus_banner)
        focus_layout.setContentsMargins(10, 7, 8, 7)
        focus_layout.setSpacing(8)
        self.focus_icon_label = QLabel()
        self.focus_icon_label.setFixedSize(24, 24)
        focus_layout.addWidget(self.focus_icon_label, alignment=Qt.AlignTop)
        focus_text_layout = QVBoxLayout()
        focus_text_layout.setContentsMargins(0, 0, 0, 0)
        focus_text_layout.setSpacing(2)
        self.focus_title_label = QLabel("Focused")
        self.focus_title_label.setObjectName("controlfreqFocusTitle")
        self.focus_title_label.setStyleSheet("font-weight: 700;")
        self.focus_title_label.setWordWrap(True)
        focus_text_layout.addWidget(self.focus_title_label)
        self.focus_current_label = QLabel("")
        self.focus_current_label.setObjectName("controlfreqFocusCurrent")
        self.focus_current_label.setWordWrap(True)
        focus_text_layout.addWidget(self.focus_current_label)
        self.focus_history_label = QLabel("")
        self.focus_history_label.setObjectName("controlfreqFocusHistory")
        self.focus_history_label.setWordWrap(True)
        focus_text_layout.addWidget(self.focus_history_label)
        focus_layout.addLayout(focus_text_layout, 1)
        self.focus_inbox_btn = QPushButton("Inbox")
        self.focus_inbox_btn.clicked.connect(self._open_focus_inbox)
        self.focus_map_btn = QPushButton("Map")
        self.focus_map_btn.clicked.connect(self._open_focus_map)
        self.focus_pin_btn = QPushButton("Pin")
        self.focus_pin_btn.clicked.connect(self._pin_active_focus)
        self.focus_history_btn = QPushButton("History")
        self.focus_history_btn.clicked.connect(self._open_focus_history)
        self.focus_more_btn = QToolButton()
        self.focus_more_btn.setText("More…")
        self.focus_more_btn.setPopupMode(QToolButton.InstantPopup)
        self.focus_more_btn.setToolTip("Additional focus actions.")
        focus_more_menu = QMenu(self.focus_more_btn)
        focus_more_menu.addAction("Map", self._open_focus_map)
        focus_more_menu.addAction("Pin", self._pin_active_focus)
        focus_more_menu.addAction("History", self._open_focus_history)
        self.focus_more_btn.setMenu(focus_more_menu)
        self.focus_more_btn.setVisible(False)
        self.focus_clear_btn = QPushButton("Clear Focus")
        self.focus_clear_btn.clicked.connect(self._clear_ops_focus)
        for button in (
            self.focus_inbox_btn,
            self.focus_map_btn,
            self.focus_pin_btn,
            self.focus_history_btn,
            self.focus_more_btn,
            self.focus_clear_btn,
        ):
            focus_layout.addWidget(button, alignment=Qt.AlignTop)
        root.addWidget(self.focus_banner)

        self.applied_filters_label = QLabel("")
        self.applied_filters_label.setObjectName("controlfreqAppliedFilters")
        self.applied_filters_label.setWordWrap(True)
        self.applied_filters_label.setStyleSheet(label_style("muted", self._theme(), weight=600))
        root.addWidget(self.applied_filters_label)
        self.search_brief_widget = SearchBriefWidget(self)
        self.search_brief_widget.contextRequested.connect(self._activate_intelligence_context)
        self.search_brief_widget.closed.connect(self._clear_intelligence_search)
        self.search_brief_widget.hide()

        controlfreq_context_text = (
            "Ops Center uses the current radio and Frequency Plan context when reviewing schedule control."
        )
        self.plan_context_label = PlanContextLabel(
            "controlfreq",
            service=self.plan_context_service,
            fallback_text=controlfreq_context_text,
            create_service=self.plan_context_service is not None,
        )
        self.plan_context_label.setToolTip(
            "Use this context to confirm which radio and assigned Frequency Plan Ops Center is displaying."
        )
        self.plan_context_label.setVisible(False)
        root.addWidget(self.plan_context_label)
        if self.plan_context_service is not None:
            self.plan_context_label.refresh_context(refresh=True)

        updated_row = QHBoxLayout()

        self.status_group = QGroupBox("Operating Status")
        self.status_group.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.status_layout = QHBoxLayout()
        self.status_group.setLayout(self.status_layout)
        self._rebuild_status_indicators()
        self.status_group.setVisible(False)
        right_status_col = QVBoxLayout()
        right_status_col.setContentsMargins(0, 0, 0, 0)
        right_status_col.setSpacing(6)
        self.current_time_label = QLabel("--")
        self.current_time_label.setVisible(False)
        self.current_time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.current_time_label.setMinimumWidth(160)
        self.current_time_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.current_time_label.setStyleSheet("font-weight: 600;")
        right_status_col.addWidget(self.current_time_label)
        self.updated_label = QLabel("Last updated: --")
        self.updated_label.setVisible(False)
        updated_row.addLayout(right_status_col, 1)
        root.addLayout(updated_row)

        # Top region: left = Activity/Intersections/Messages, right = Frequency/Schedule Outlook
        self.top_splitter = QSplitter(Qt.Horizontal)
        style_splitter_handles(self.top_splitter, self._theme(), width=14)

        self.left_col = QWidget()
        left_layout = QVBoxLayout(self.left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.setAlignment(Qt.AlignTop)

        self.activity_box = QGroupBox("Operational Awareness")
        act_layout = QVBoxLayout(self.activity_box)
        act_layout.setAlignment(Qt.AlignTop)
        act_header = QHBoxLayout()
        act_header.addWidget(QLabel("Window"))
        self.activity_window_combo = QComboBox()
        self.activity_window_combo.addItem("30m", 30)
        self.activity_window_combo.addItem("1h", 60)
        self.activity_window_combo.addItem("2h", 120)
        self.activity_window_combo.addItem("6h", 360)
        self.activity_window_combo.addItem("12h", 720)
        self.activity_window_combo.addItem("24h", 1440)
        self.activity_window_combo.setCurrentIndex(2)
        self.activity_window_combo.currentIndexChanged.connect(self._refresh_activity)
        self.activity_window_combo.currentIndexChanged.connect(self._schedule_persist_ui_state)
        act_header.addWidget(self.activity_window_combo)
        self.source_lanes_details_btn = QToolButton()
        self.source_lanes_details_btn.setText("Source Details")
        self.source_lanes_details_btn.setCheckable(True)
        self.source_lanes_details_btn.setToolTip("Show the detailed source-lane table.")
        self.source_lanes_details_btn.toggled.connect(
            lambda checked: self.source_lanes_table.setVisible(bool(checked))
        )
        act_header.addWidget(self.source_lanes_details_btn)
        self.awareness_details_btn = QToolButton()
        self.awareness_details_btn.setText("Evidence")
        self.awareness_details_btn.setCheckable(True)
        self.awareness_details_btn.setToolTip("Show the dense awareness and activity evidence tables.")
        self.awareness_details_btn.toggled.connect(self._set_awareness_details_visible)
        act_header.addWidget(self.awareness_details_btn)
        act_header.addStretch(1)
        act_layout.addLayout(act_header)
        self.awareness_now_next_label = QLabel("Now: -- | Next: --")
        self.awareness_now_next_label.setObjectName("controlfreqAwarenessNowNext")
        self.awareness_now_next_label.setWordWrap(True)
        self.awareness_now_next_label.setStyleSheet("font-weight: 600;")
        act_layout.addWidget(self.awareness_now_next_label)
        self.source_lanes_table = QTableWidget(0, 4)
        self.source_lanes_table.setObjectName("controlfreqSourceLanesTable")
        self.source_lanes_table.setHorizontalHeaderLabels(["Source", "Now", "Next", "Attention"])
        self._setup_table_defaults(self.source_lanes_table)
        self.source_lanes_table.setWordWrap(True)
        source_header = self.source_lanes_table.horizontalHeader()
        source_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        source_header.setSectionResizeMode(1, QHeaderView.Stretch)
        source_header.setSectionResizeMode(2, QHeaderView.Stretch)
        source_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.source_lanes_table.itemSelectionChanged.connect(self._set_source_lane_focus_from_selection)
        self.source_lane_cards_container = QWidget()
        self.source_lane_cards_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.source_lane_cards_layout = QVBoxLayout(self.source_lane_cards_container)
        self.source_lane_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.source_lane_cards_layout.setSpacing(5)
        act_layout.addWidget(self.source_lane_cards_container)
        act_layout.addWidget(self.source_lanes_table)
        self.source_lanes_table.setVisible(False)
        self.awareness_sop_label = QLabel("SOP: --")
        self.awareness_sop_label.setObjectName("controlfreqAwarenessSop")
        self.awareness_sop_label.setWordWrap(True)
        self.awareness_sop_label.setStyleSheet(label_style("muted", self._theme()))
        act_layout.addWidget(self.awareness_sop_label)
        self.awareness_recommend_label = QLabel("Recommended: monitor traffic.")
        self.awareness_recommend_label.setObjectName("controlfreqAwarenessRecommended")
        self.awareness_recommend_label.setWordWrap(True)
        self.awareness_recommend_label.setStyleSheet(label_style("muted", self._theme(), weight=600))
        act_layout.addWidget(self.awareness_recommend_label)
        self.situation_summary_label = QLabel("Situation: no traffic needs attention.")
        self.situation_summary_label.setObjectName("controlfreqSituationSummary")
        self.situation_summary_label.setWordWrap(True)
        self.situation_summary_label.setStyleSheet("font-weight: 700; padding: 6px; border-radius: 6px;")
        act_layout.addWidget(self.situation_summary_label)
        self.situation_detail_label = QLabel("")
        self.situation_detail_label.setObjectName("controlfreqSituationDetail")
        self.situation_detail_label.setWordWrap(True)
        self.situation_detail_label.setStyleSheet(label_style("muted", self._theme()))
        self.situation_detail_label.setVisible(False)
        act_layout.addWidget(self.situation_detail_label)
        self.situation_cards_container = QWidget()
        self.situation_cards_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.situation_cards_layout = QVBoxLayout(self.situation_cards_container)
        self.situation_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.situation_cards_layout.setSpacing(6)
        self.situation_cards_container.setVisible(False)
        act_layout.addWidget(self.situation_cards_container)
        self.awareness_lead_label = QLabel("Needs Attention: clear.")
        self.awareness_lead_label.setObjectName("controlfreqAwarenessLead")
        self.awareness_lead_label.setWordWrap(True)
        self.awareness_lead_label.setStyleSheet("font-weight: 700; padding: 6px; border-radius: 6px;")
        act_layout.addWidget(self.awareness_lead_label)
        self.operational_activity_label = QLabel("Recent Traffic: none in the selected window")
        self.operational_activity_label.setWordWrap(True)
        self.operational_activity_label.setStyleSheet("font-weight: 600;")
        act_layout.addWidget(self.operational_activity_label)
        self.operational_topics_label = QLabel("")
        self.operational_topics_label.setWordWrap(True)
        self.operational_topics_label.setStyleSheet(label_style("muted", self._theme()))
        act_layout.addWidget(self.operational_topics_label)
        self.awareness_topic_chip_container = QWidget()
        self.awareness_topic_chip_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.awareness_topic_chip_layout = QHBoxLayout(self.awareness_topic_chip_container)
        self.awareness_topic_chip_layout.setContentsMargins(0, 0, 0, 0)
        self.awareness_topic_chip_layout.setSpacing(6)
        self.awareness_topic_chip_container.setVisible(False)
        act_layout.addWidget(self.awareness_topic_chip_container)
        self.awareness_table = QTableWidget(0, 4)
        self.awareness_table.setObjectName("controlfreqAwarenessTable")
        self.awareness_table.setHorizontalHeaderLabels(["Priority", "From", "Focus", "Actions"])
        self._setup_table_defaults(self.awareness_table)
        self.awareness_table.setWordWrap(True)
        self.awareness_table.itemDoubleClicked.connect(lambda *_args: self._open_operational_activity_messages())
        self.awareness_table.itemSelectionChanged.connect(self._sync_operational_action_buttons)
        awareness_header = self.awareness_table.horizontalHeader()
        awareness_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        awareness_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        awareness_header.setSectionResizeMode(2, QHeaderView.Stretch)
        awareness_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        act_layout.addWidget(self.awareness_table)
        self.awareness_table.setVisible(False)
        self.awareness_pins_label = QLabel("")
        self.awareness_pins_label.setObjectName("controlfreqAwarenessPins")
        self.awareness_pins_label.setWordWrap(True)
        self.awareness_pins_label.setStyleSheet(label_style("muted", self._theme()))
        act_layout.addWidget(self.awareness_pins_label)
        self.awareness_pin_chip_container = QWidget()
        self.awareness_pin_chip_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.awareness_pin_chip_layout = QHBoxLayout(self.awareness_pin_chip_container)
        self.awareness_pin_chip_layout.setContentsMargins(0, 0, 0, 0)
        self.awareness_pin_chip_layout.setSpacing(6)
        self.awareness_pin_chip_container.setVisible(False)
        act_layout.addWidget(self.awareness_pin_chip_container)
        self.more_traffic_label = QLabel("")
        self.more_traffic_label.setObjectName("controlfreqMoreTraffic")
        self.more_traffic_label.setWordWrap(True)
        self.more_traffic_label.setStyleSheet(label_style("muted", self._theme()))
        act_layout.addWidget(self.more_traffic_label)
        self.activity_actions_widget = QWidget()
        self.activity_actions_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        activity_actions = QHBoxLayout(self.activity_actions_widget)
        activity_actions.setContentsMargins(0, 0, 0, 0)
        activity_actions.setSpacing(6)
        self.operational_messages_btn = QPushButton("Inbox")
        self.operational_messages_btn.setToolTip("Open the Messages Inbox with this activity context.")
        self.operational_messages_btn.setEnabled(False)
        self.operational_messages_btn.clicked.connect(self._open_operational_activity_messages)
        activity_actions.addWidget(self.operational_messages_btn)
        self.operational_map_btn = QPushButton("Map")
        self.operational_map_btn.setToolTip("Open the Map filtered to this activity context.")
        self.operational_map_btn.setEnabled(False)
        self.operational_map_btn.clicked.connect(self._open_operational_activity_map)
        activity_actions.addWidget(self.operational_map_btn)
        self.operational_compose_btn = QPushButton("Compose")
        self.operational_compose_btn.setToolTip("Open Compose using the selected traffic context.")
        self.operational_compose_btn.setEnabled(False)
        self.operational_compose_btn.clicked.connect(self._open_operational_activity_compose)
        activity_actions.addWidget(self.operational_compose_btn)
        self.operational_pin_btn = QPushButton("Pin")
        self.operational_pin_btn.setToolTip("Keep this topic, callsign, or group visible in Operational Awareness.")
        self.operational_pin_btn.setEnabled(False)
        self.operational_pin_btn.clicked.connect(self._pin_selected_awareness_focus)
        activity_actions.addWidget(self.operational_pin_btn)
        self.operational_clear_pins_btn = QPushButton("Clear")
        self.operational_clear_pins_btn.setToolTip("Remove all pinned Operational Awareness focuses.")
        self.operational_clear_pins_btn.setEnabled(False)
        self.operational_clear_pins_btn.clicked.connect(self._clear_awareness_pins)
        activity_actions.addWidget(self.operational_clear_pins_btn)
        for action_btn in (
            self.operational_messages_btn,
            self.operational_map_btn,
            self.operational_compose_btn,
            self.operational_pin_btn,
            self.operational_clear_pins_btn,
        ):
            action_btn.setMaximumWidth(104)
            action_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        activity_actions.addStretch(1)
        self.activity_actions_widget.setVisible(False)
        act_layout.addWidget(self.activity_actions_widget)
        self.activity_table = QTableWidget(0, 4)
        self.activity_table.setHorizontalHeaderLabels(
            ["Group", "Band/Freq", "Callsigns Seen", "Traffic"]
        )
        self._setup_table_defaults(self.activity_table)
        self.activity_table.horizontalHeader().setStretchLastSection(True)
        act_layout.addWidget(self.activity_table)
        self.activity_table.setVisible(False)

        self.intersection_box = QGroupBox("Peer Schedule Finder")
        intersection_layout = QVBoxLayout(self.intersection_box)
        intersection_layout.setContentsMargins(8, 6, 8, 6)
        intersection_layout.setSpacing(4)
        inter_header_row = QHBoxLayout()
        inter_header_row.setContentsMargins(0, 0, 0, 0)
        inter_header_row.setSpacing(6)
        self.intersection_label = QLabel("Overlap Window")
        self.intersection_label.setStyleSheet("font-weight: bold;")
        inter_header_row.addWidget(self.intersection_label)
        self.intersection_window_combo = QComboBox()
        self.intersection_window_combo.addItem("30m", 30)
        self.intersection_window_combo.addItem("1h", 60)
        self.intersection_window_combo.addItem("2h", 120)
        self.intersection_window_combo.addItem("6h", 360)
        self.intersection_window_combo.setCurrentIndex(2)
        self.intersection_window_combo.setToolTip(
            "Choose how far ahead to look for exact-frequency overlaps with peer schedules."
        )
        self.intersection_window_combo.currentIndexChanged.connect(self._refresh_intersections)
        self.intersection_window_combo.currentIndexChanged.connect(self._schedule_persist_ui_state)
        inter_header_row.addWidget(self.intersection_window_combo, alignment=Qt.AlignVCenter)
        inter_header_row.addStretch(1)
        intersection_layout.addLayout(inter_header_row)

        peer_filters = QGridLayout()
        peer_filters.setContentsMargins(0, 0, 0, 0)
        peer_filters.setHorizontalSpacing(6)
        peer_filters.setVerticalSpacing(4)
        self.peer_callsign_filter = QLineEdit()
        self.peer_callsign_filter.setPlaceholderText("Filter callsign…")
        self.peer_callsign_filter.setClearButtonEnabled(True)
        self.peer_callsign_filter.setAccessibleName("Filter peer schedules by callsign")
        self.peer_callsign_filter.textChanged.connect(lambda *_args: self._peer_filter_timer.start())
        peer_filters.addWidget(self.peer_callsign_filter, 0, 0, 1, 2)
        self.peer_group_filter = QComboBox()
        self.peer_group_filter.addItem("All groups", "")
        self.peer_group_filter.currentIndexChanged.connect(self._on_peer_filters_changed)
        peer_filters.addWidget(self.peer_group_filter, 0, 2)
        self.peer_region_filter = QComboBox()
        self.peer_region_filter.addItem("All regions", "")
        self.peer_region_filter.currentIndexChanged.connect(self._on_peer_filters_changed)
        peer_filters.addWidget(self.peer_region_filter, 1, 0)
        self.peer_role_filter = QComboBox()
        self.peer_role_filter.addItem("All roles", "")
        self.peer_role_filter.currentIndexChanged.connect(self._on_peer_filters_changed)
        peer_filters.addWidget(self.peer_role_filter, 1, 1)
        self.peer_result_label = QLabel("0 operators")
        self.peer_result_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.peer_result_label.setStyleSheet(label_style("muted", self._theme()))
        peer_filters.addWidget(self.peer_result_label, 1, 2)
        for column in range(3):
            peer_filters.setColumnStretch(column, 1)
        intersection_layout.addLayout(peer_filters)

        self.peer_summary_label = QLabel("Now 0 · Next 2h 0")
        self.peer_summary_label.setStyleSheet("font-weight: 600;")
        self.peer_summary_label.setWordWrap(True)
        intersection_layout.addWidget(self.peer_summary_label)

        self.peer_chart_table = QTableWidget(0, 3)
        self.peer_chart_table.setObjectName("controlfreqPeerRendezvousChart")
        self.peer_chart_table.setAccessibleName("Peer rendezvous chart")
        self.peer_chart_table.setAccessibleDescription(
            "One row per operator. Timeline segments combine all matching band and frequency windows."
        )
        self.peer_chart_table.setHorizontalHeaderLabels(["Operator", "Rendezvous", "Actions"])
        self._setup_table_defaults(self.peer_chart_table)
        self.peer_chart_table.horizontalHeader().setVisible(False)
        self.peer_chart_table.setAlternatingRowColors(False)
        self.peer_chart_table.setShowGrid(False)
        self.peer_chart_table.setWordWrap(True)
        self.peer_chart_table.setVerticalScrollMode(QAbstractItemView.ScrollPerItem)
        self.peer_chart_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.peer_chart_table.customContextMenuRequested.connect(self._show_peer_finder_context_menu)
        self.peer_chart_table.itemActivated.connect(self._activate_peer_chart_item)
        self.peer_chart_table.itemClicked.connect(self._activate_peer_chart_item)
        peer_header = self.peer_chart_table.horizontalHeader()
        peer_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        peer_header.setSectionResizeMode(1, QHeaderView.Stretch)
        peer_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.peer_rendezvous_delegate = PeerRendezvousDelegate(self.peer_chart_table)
        self.peer_chart_table.setItemDelegateForColumn(1, self.peer_rendezvous_delegate)
        intersection_layout.addWidget(self.peer_chart_table)
        self._fit_table_height_to_rows(self.peer_chart_table, min_rows=1, max_rows=6, empty_rows=1)
        self._peer_finder_contexts: List[Dict[str, str]] = []

        self.inbox_box = QGroupBox("Traffic Intelligence")
        inbox_layout = QVBoxLayout(self.inbox_box)
        inbox_layout.setContentsMargins(8, 6, 8, 6)
        inbox_layout.setSpacing(4)
        self.traffic_action_summary = TrafficActionSummaryWidget(self.settings)
        self.traffic_action_summary.bucketActivated.connect(self._open_traffic_action_bucket)
        inbox_layout.addWidget(self.traffic_action_summary)
        traffic_group_header = QHBoxLayout()
        self.traffic_group_header = traffic_group_header
        self.traffic_group_title = QToolButton()
        self.traffic_group_title.setText("Traffic by group")
        self.traffic_group_title.setCheckable(True)
        self.traffic_group_title.setChecked(
            bool(self.settings.get("controlfreq_traffic_group_expanded", True))
        )
        self.traffic_group_title.setAutoRaise(True)
        self.traffic_group_title.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.traffic_group_title.setStyleSheet("font-weight: 700;")
        self.traffic_group_title.setToolTip(
            "Show or hide group/source traffic detail. The header remains visible as an aggregate event signal."
        )
        self.traffic_group_title.toggled.connect(self._toggle_traffic_group_detail)
        traffic_group_header.addWidget(self.traffic_group_title)
        traffic_group_header.addStretch(1)
        self.traffic_group_hint = QLabel("Solid = current · dashed marker = prior window")
        self.traffic_group_hint.setStyleSheet(label_style("muted", self._theme()))
        traffic_group_header.addWidget(self.traffic_group_hint)
        inbox_layout.addLayout(traffic_group_header)
        self.traffic_group_table = QTableWidget(0, 3)
        self.traffic_group_table.setObjectName("controlfreqTrafficByGroupChart")
        self.traffic_group_table.setAccessibleName("Traffic by group comparison chart")
        self.traffic_group_table.setAccessibleDescription(
            "Horizontal bars compare current traffic with the prior equal time window. "
            "Associated operating and membership groups are listed first."
        )
        self.traffic_group_table.setHorizontalHeaderLabels(["Group", "Volume comparison", "Details"])
        self._setup_table_defaults(self.traffic_group_table)
        self.traffic_group_table.horizontalHeader().setVisible(False)
        self.traffic_group_table.setAlternatingRowColors(False)
        self.traffic_group_table.setShowGrid(False)
        self.traffic_group_table.setWordWrap(False)
        self.traffic_group_bar_delegate = TrafficVolumeBarDelegate(self.traffic_group_table)
        self.traffic_group_table.setItemDelegateForColumn(1, self.traffic_group_bar_delegate)
        self.traffic_group_table.setToolTip(
            "Current versus prior-window traffic by group. Associated operating and membership groups are first. "
            "A spike is an awareness signal, not an automatic action. Double-click or press Enter to open Inbox traffic."
        )
        self.traffic_group_table.itemActivated.connect(self._open_traffic_group_row)
        inbox_layout.addWidget(self.traffic_group_table)
        self._fit_table_height_to_rows(self.traffic_group_table, min_rows=1, max_rows=5, empty_rows=1)
        self._toggle_traffic_group_detail(self.traffic_group_title.isChecked(), persist=False)
        detail_row = QHBoxLayout()
        detail_row.setContentsMargins(0, 0, 0, 0)
        self.traffic_source_detail_btn = QToolButton()
        self.traffic_source_detail_btn.setText("Sources")
        self.traffic_source_detail_btn.setCheckable(True)
        self.traffic_source_detail_btn.setToolTip("Show unread/file counts by message source.")
        self.traffic_source_detail_btn.toggled.connect(self._toggle_traffic_source_detail)
        detail_row.addWidget(self.traffic_source_detail_btn)
        detail_row.addStretch(1)
        inbox_layout.addLayout(detail_row)
        self.inbox_table = QTableWidget(0, 3)
        self.inbox_table.setHorizontalHeaderLabels(["Source", "Unread / Files", "What needs attention"])
        self._setup_table_defaults(self.inbox_table)
        inbox_header = self.inbox_table.horizontalHeader()
        inbox_header.setStretchLastSection(True)
        inbox_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        inbox_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        inbox_header.setSectionResizeMode(2, QHeaderView.Stretch)
        inbox_layout.addWidget(self.inbox_table)
        self.inbox_table.setVisible(False)
        self._set_message_summary_visible_rows(6)

        self.left_splitter = QSplitter(Qt.Vertical)
        style_splitter_handles(self.left_splitter, self._theme(), width=12)
        self.left_splitter.addWidget(self.activity_box)
        self.left_splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        left_layout.addWidget(self.left_splitter)
        left_layout.addStretch(1)
        self.top_splitter.addWidget(self.left_col)

        self.right_col = QWidget()
        right_layout = QVBoxLayout(self.right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.setAlignment(Qt.AlignTop)

        self.freq_ctrl_box = QGroupBox("Frequency Control")
        freq_layout = QVBoxLayout(self.freq_ctrl_box)
        freq_layout.setContentsMargins(8, 6, 8, 6)
        freq_layout.setSpacing(2)
        self.freq_state_badge = QLabel("Unknown")
        self.freq_state_badge.setAlignment(Qt.AlignCenter)
        self.freq_state_badge.setMinimumWidth(132)
        freq_badge_h = single_line_label_height(self.freq_state_badge, vertical_padding=8, floor=26)
        self.freq_state_badge.setMinimumHeight(freq_badge_h)
        self.freq_state_badge.setMaximumHeight(freq_badge_h)
        self.freq_state_badge.setStyleSheet(
            "font-weight: 600; border-radius: 6px; padding: 0 8px;"
        )
        self.freq_combo = QComboBox()
        freq_combo_h = control_height_for_font(self.freq_combo, vertical_padding=14, floor=40)
        self.freq_combo.setMinimumHeight(freq_combo_h)
        self.freq_combo.setMaximumHeight(freq_combo_h)
        self.freq_combo.setMinimumWidth(220)
        self.freq_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.freq_combo.currentIndexChanged.connect(self._on_freq_selection_changed)
        freq_layout.addWidget(self.freq_combo)
        self.freq_meta_label = QLabel(self._freq_meta_full_text)
        self.freq_meta_label.setWordWrap(False)
        self.freq_meta_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.freq_meta_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.freq_meta_label.setStyleSheet("")
        self.freq_meta_label.setToolTip(self._freq_meta_full_text)
        freq_layout.addWidget(self.freq_meta_label)
        self.effective_source_label = QLabel("Active Source: --")
        self.effective_source_label.setWordWrap(False)
        self.effective_source_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.effective_source_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.effective_source_label.setStyleSheet(label_style("muted", self._theme()))
        freq_layout.addWidget(self.effective_source_label)
        self.next_change_label = QLabel("Next Change: --")
        self.next_change_label.setWordWrap(False)
        self.next_change_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.next_change_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.next_change_label.setStyleSheet(label_style("muted", self._theme()))
        freq_layout.addWidget(self.next_change_label)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(8)
        btn_row.addWidget(self.freq_state_badge)
        self.hold_duration_combo = QComboBox()
        self.hold_duration_combo.setMinimumWidth(88)
        self.hold_duration_combo.setMaximumWidth(104)
        self.hold_duration_combo.setToolTip("Temporary schedule hold duration after QSY.")
        self.hold_duration_combo.currentIndexChanged.connect(self._on_hold_duration_changed)
        btn_row.addWidget(self.hold_duration_combo)
        self.freq_action_btn = QPushButton("QSY + Hold")
        self.freq_action_btn.clicked.connect(self._on_primary_freq_action_clicked)
        self.freq_action_btn.setMinimumHeight(button_height_for_font(self.freq_action_btn, floor=30))
        self.freq_action_btn.setMinimumWidth(132)
        self.freq_action_btn.setMaximumWidth(170)
        btn_row.addWidget(self.freq_action_btn)
        btn_row.addStretch(1)
        freq_layout.addLayout(btn_row)

        self.top_overview_row = QHBoxLayout()
        self.freq_ctrl_box.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.freq_ctrl_box.setMinimumWidth(380)
        self.freq_ctrl_box.setMaximumWidth(540)
        self.freq_ctrl_box.setVisible(False)
        self.inbox_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.top_overview_row.addWidget(self.inbox_box, 1)
        root.addLayout(self.top_overview_row)
        self._lock_frequency_control_height()

        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("View"))
        self.view_preset_combo = QComboBox()
        for preset in controlfreq_preset_names():
            self.view_preset_combo.addItem(preset, preset)
        self.view_preset_combo.setMinimumWidth(150)
        self.view_preset_combo.currentIndexChanged.connect(self._on_view_preset_changed)
        view_row.addWidget(self.view_preset_combo)
        self.view_chip_buttons: Dict[str, QPushButton] = {}
        for key, label in controlfreq_view_labels():
            btn = QPushButton(label)
            btn.setCheckable(True)
            try:
                view_def = operational_view_for(key)
                btn.setToolTip(view_def.notes or view_def.label)
            except Exception:
                pass
            btn.toggled.connect(lambda checked, k=key: self._on_view_chip_toggled(k, checked))
            self.view_chip_buttons[key] = btn
            view_row.addWidget(btn)
        view_row.addStretch(1)
        root.addLayout(view_row)
        # Keep intelligence and group-spike signals above the optional inspector.
        root.addWidget(self.search_brief_widget)

        self.schedule_box = QGroupBox("Schedule Outlook")
        schedule_layout = QVBoxLayout(self.schedule_box)
        schedule_header = QHBoxLayout()
        self.schedule_now_marker = QLabel("● NOW")
        self.schedule_now_marker.setStyleSheet("font-weight: 700;")
        schedule_header.addWidget(self.schedule_now_marker)
        schedule_header.addStretch(1)
        self.schedule_later_btn = QToolButton()
        self.schedule_later_btn.setText("Later")
        self.schedule_later_btn.setCheckable(True)
        self.schedule_later_btn.setToolTip("Show or hide routine later events.")
        self.schedule_later_btn.toggled.connect(lambda *_args: self._refresh_schedule_outlook())
        schedule_header.addWidget(self.schedule_later_btn)
        self.schedule_details_btn = QToolButton()
        self.schedule_details_btn.setText("Details")
        self.schedule_details_btn.setCheckable(True)
        self.schedule_details_btn.setToolTip("Show the detailed schedule table.")
        self.schedule_details_btn.toggled.connect(self._set_schedule_details_visible)
        schedule_header.addWidget(self.schedule_details_btn)
        schedule_layout.addLayout(schedule_header)
        self.schedule_timeline_container = QWidget()
        self.schedule_timeline_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.schedule_timeline_layout = QVBoxLayout(self.schedule_timeline_container)
        self.schedule_timeline_layout.setContentsMargins(0, 0, 0, 0)
        self.schedule_timeline_layout.setSpacing(5)
        schedule_layout.addWidget(self.schedule_timeline_container)

        # This is intentionally a distinct surface from commandable HF/SOP
        # timeline rows.  It has its own bounded, internally-scrollable list and
        # only exposes reminder actions supplied by the Local Nets projection.
        self.local_nets_outlook_box = QFrame(self.schedule_box)
        self.local_nets_outlook_box.setObjectName("controlfreqLocalNetsOutlook")
        self.local_nets_outlook_box.setFrameShape(QFrame.StyledPanel)
        local_nets_layout = QVBoxLayout(self.local_nets_outlook_box)
        local_nets_layout.setContentsMargins(8, 6, 8, 6)
        local_nets_layout.setSpacing(5)
        local_nets_header = QHBoxLayout()
        self.local_nets_outlook_title = QLabel("Local Nets · reminders")
        self.local_nets_outlook_title.setStyleSheet("font-weight: 700;")
        self.local_nets_outlook_title.setToolTip(
            "Local Net reminders are informational. They do not tune or control a radio."
        )
        local_nets_header.addWidget(self.local_nets_outlook_title)
        local_nets_header.addStretch(1)
        self.local_nets_later_btn = QToolButton()
        self.local_nets_later_btn.setText("Later (0)")
        self.local_nets_later_btn.setCheckable(True)
        self.local_nets_later_btn.setToolTip("Show or hide later Local Net reminders (up to 50).")
        self.local_nets_later_btn.toggled.connect(lambda _checked: self._refresh_local_nets_outlook())
        local_nets_header.addWidget(self.local_nets_later_btn)
        self.local_nets_outlook_toggle = QToolButton()
        self.local_nets_outlook_toggle.setText("Hide")
        self.local_nets_outlook_toggle.setCheckable(True)
        self.local_nets_outlook_toggle.setChecked(True)
        self.local_nets_outlook_toggle.setToolTip("Show or hide Local Net reminders.")
        self.local_nets_outlook_toggle.toggled.connect(self._set_local_nets_outlook_visible)
        local_nets_header.addWidget(self.local_nets_outlook_toggle)
        local_nets_layout.addLayout(local_nets_header)
        self.local_nets_outlook_list = QScrollArea(self.local_nets_outlook_box)
        self.local_nets_outlook_list.setObjectName("controlfreqLocalNetsOutlookList")
        self.local_nets_outlook_list.setWidgetResizable(True)
        self.local_nets_outlook_list.setFrameShape(QFrame.NoFrame)
        self.local_nets_outlook_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.local_nets_outlook_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.local_nets_outlook_list.setMaximumHeight(276)
        self.local_nets_outlook_list_container = QWidget(self.local_nets_outlook_list)
        self.local_nets_outlook_list_layout = QVBoxLayout(self.local_nets_outlook_list_container)
        self.local_nets_outlook_list_layout.setContentsMargins(0, 0, 0, 0)
        self.local_nets_outlook_list_layout.setSpacing(4)
        self.local_nets_outlook_list.setWidget(self.local_nets_outlook_list_container)
        local_nets_layout.addWidget(self.local_nets_outlook_list)
        schedule_layout.addWidget(self.local_nets_outlook_box)

        # Shortwave is deliberately separate from both HF command rows and
        # Local Net reminders.  It begins collapsed, and no provider query is
        # made until an operator expands it.
        self.shortwave_listening_outlook_box = QFrame(self.schedule_box)
        self.shortwave_listening_outlook_box.setObjectName("controlfreqShortwaveListeningOutlook")
        self.shortwave_listening_outlook_box.setFrameShape(QFrame.StyledPanel)
        shortwave_layout = QVBoxLayout(self.shortwave_listening_outlook_box)
        shortwave_layout.setContentsMargins(8, 6, 8, 6)
        shortwave_layout.setSpacing(5)
        shortwave_header = QHBoxLayout()
        self.shortwave_listening_outlook_title = QLabel("Shortwave Listening · reminders")
        self.shortwave_listening_outlook_title.setStyleSheet("font-weight: 700;")
        self.shortwave_listening_outlook_title.setToolTip("Manual listening reminders. They never tune or control a radio or receiver.")
        shortwave_header.addWidget(self.shortwave_listening_outlook_title)
        shortwave_header.addStretch(1)
        self.shortwave_listening_outlook_toggle = QToolButton()
        self.shortwave_listening_outlook_toggle.setText("Show")
        self.shortwave_listening_outlook_toggle.setCheckable(True)
        self.shortwave_listening_outlook_toggle.setChecked(False)
        self.shortwave_listening_outlook_toggle.setAccessibleName("Show Shortwave Listening reminders")
        self.shortwave_listening_outlook_toggle.setToolTip("Show manual Shortwave Listening reminders.")
        self.shortwave_listening_outlook_toggle.toggled.connect(self._set_shortwave_listening_outlook_visible)
        shortwave_header.addWidget(self.shortwave_listening_outlook_toggle)
        shortwave_layout.addLayout(shortwave_header)
        self.shortwave_listening_outlook_list = QScrollArea(self.shortwave_listening_outlook_box)
        self.shortwave_listening_outlook_list.setObjectName("controlfreqShortwaveListeningOutlookList")
        self.shortwave_listening_outlook_list.setWidgetResizable(True)
        self.shortwave_listening_outlook_list.setFrameShape(QFrame.NoFrame)
        self.shortwave_listening_outlook_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.shortwave_listening_outlook_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.shortwave_listening_outlook_list.setMaximumHeight(220)
        self.shortwave_listening_outlook_list_container = QWidget(self.shortwave_listening_outlook_list)
        self.shortwave_listening_outlook_list_layout = QVBoxLayout(self.shortwave_listening_outlook_list_container)
        self.shortwave_listening_outlook_list_layout.setContentsMargins(0, 0, 0, 0)
        self.shortwave_listening_outlook_list_layout.setSpacing(4)
        self.shortwave_listening_outlook_list.setWidget(self.shortwave_listening_outlook_list_container)
        self.shortwave_listening_outlook_list.setVisible(False)
        shortwave_layout.addWidget(self.shortwave_listening_outlook_list)
        schedule_layout.addWidget(self.shortwave_listening_outlook_box)
        self.schedule_table = QTableWidget(0, 5)
        self.schedule_table.setHorizontalHeaderLabels(["When/Day", "Type", "Group/Net", "Band/Freq", "Actions"])
        self._setup_table_defaults(self.schedule_table)
        self.schedule_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.schedule_table.customContextMenuRequested.connect(self._on_schedule_context_menu)
        sched_header = self.schedule_table.horizontalHeader()
        sched_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        sched_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        sched_header.setSectionResizeMode(2, QHeaderView.Stretch)
        sched_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        sched_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        schedule_layout.addWidget(self.schedule_table)
        self.schedule_table.setVisible(False)
        self.schedule_action_hint = QLabel("Tip: use buttons in Actions, or right-click a row for actions.")
        self.schedule_action_hint.setWordWrap(True)
        self.schedule_action_hint.setVisible(False)
        schedule_layout.addWidget(self.schedule_action_hint)

        self.right_splitter = QSplitter(Qt.Vertical)
        style_splitter_handles(self.right_splitter, self._theme(), width=12)
        self.right_splitter.addWidget(self.intersection_box)
        self.right_splitter.addWidget(self.schedule_box)
        right_layout.addWidget(self.right_splitter)
        right_layout.addStretch(1)
        self.top_splitter.addWidget(self.right_col)
        self.top_splitter.setStretchFactor(0, 1)
        self.top_splitter.setStretchFactor(1, 1)
        self.left_splitter.setSizes([1])
        self.right_splitter.setSizes([1, 3])
        self.top_splitter.setSizes([480, 560])
        root.addWidget(self.top_splitter, 5)

        # Bottom region: full-width propagation forecast
        self.bottom_row = QWidget()
        row2 = QHBoxLayout(self.bottom_row)
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(0)

        self.prop_box = QGroupBox("Propagation Forecast")
        prop_layout = QVBoxLayout(self.prop_box)
        prop_layout.setAlignment(Qt.AlignTop)
        prop_summary_actions = QHBoxLayout()
        prop_summary_actions.setContentsMargins(0, 0, 0, 0)
        prop_summary_actions.setSpacing(6)
        self.prop_details_btn = QPushButton("Forecast Details")
        self.prop_details_btn.setCheckable(True)
        self.prop_details_btn.setChecked(False)
        self.prop_details_btn.setToolTip("Show or hide the detailed propagation forecast table.")
        self.prop_details_btn.toggled.connect(self._set_propagation_details_visible)
        prop_summary_actions.addWidget(self.prop_details_btn)
        prop_summary_actions.addStretch(1)
        prop_layout.addLayout(prop_summary_actions)
        self.prop_summary_label = QLabel("RF Readiness: set operator grid to enable forecast.")
        self.prop_summary_label.setObjectName("controlfreqPropagationSummary")
        self.prop_summary_label.setWordWrap(True)
        self.prop_summary_label.setStyleSheet("font-weight: 600;")
        prop_layout.addWidget(self.prop_summary_label)
        self.prop_band_ladder_container = QWidget()
        self.prop_band_ladder_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.prop_band_ladder_layout = QHBoxLayout(self.prop_band_ladder_container)
        self.prop_band_ladder_layout.setContentsMargins(0, 0, 0, 0)
        self.prop_band_ladder_layout.setSpacing(8)
        prop_layout.addWidget(self.prop_band_ladder_container)
        self.prop_detail_widget = QWidget()
        prop_detail_layout = QVBoxLayout(self.prop_detail_widget)
        prop_detail_layout.setContentsMargins(0, 0, 0, 0)
        prop_detail_layout.setSpacing(6)
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Target"))
        self.prop_target_type_combo = QComboBox()
        self.prop_target_type_combo.addItem("Region", "REGION")
        self.prop_target_type_combo.addItem("State", "STATE")
        self.prop_target_type_combo.addItem("Operator", "OPERATOR")
        self.prop_target_type_combo.currentIndexChanged.connect(self._on_prop_target_type_changed)
        target_row.addWidget(self.prop_target_type_combo)
        self.prop_target_value_combo = QComboBox()
        self.prop_target_value_combo.setEditable(True)
        self.prop_target_value_combo.setInsertPolicy(QComboBox.NoInsert)
        self.prop_target_value_combo.setDuplicatesEnabled(False)
        self._prop_target_completer = QCompleter(self.prop_target_value_combo.model(), self)
        self._prop_target_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._prop_target_completer.setFilterMode(Qt.MatchContains)
        self.prop_target_value_combo.setCompleter(self._prop_target_completer)
        if self.prop_target_value_combo.lineEdit():
            self.prop_target_value_combo.lineEdit().setPlaceholderText("Type to search...")
            self.prop_target_value_combo.lineEdit().setClearButtonEnabled(True)
        self.prop_target_value_combo.currentTextChanged.connect(self._on_prop_target_value_changed)
        target_row.addWidget(self.prop_target_value_combo, 1)
        self.prop_hint = QLabel("Model uses today's schedule bands.")
        self.prop_hint.setStyleSheet(label_style("muted", self._theme()))
        self.prop_hint.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        target_row.addWidget(self.prop_hint, 1)
        prop_detail_layout.addLayout(target_row)
        self.prop_table = QTableWidget(0, 4)
        self.prop_table.setHorizontalHeaderLabels(
            ["Zone", "Morning (Dawn-10:00)", "Day (10:00-Sunset)", "Night (Sunset-Dawn)"]
        )
        self._setup_table_defaults(self.prop_table)
        self.prop_table.horizontalHeader().setStretchLastSection(True)
        self.prop_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        prop_detail_layout.addWidget(self.prop_table)
        self.prop_detail_widget.setVisible(False)
        prop_layout.addWidget(self.prop_detail_widget)
        row2.addWidget(self.prop_box, 1)
        root.addWidget(self.bottom_row, 0)

        # Save user-resized layout proportions.
        self.top_splitter.splitterMoved.connect(self._schedule_persist_ui_state)
        self.left_splitter.splitterMoved.connect(self._schedule_persist_ui_state)
        self.right_splitter.splitterMoved.connect(self._schedule_persist_ui_state)

        # Keyboard shortcuts
        self.shortcut_refresh = QShortcut(QKeySequence("Ctrl+R"), self)
        self.shortcut_refresh.activated.connect(self._refresh_all)
        self.shortcut_set_frequency = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.shortcut_set_frequency.activated.connect(self._on_freq_set_clicked)
        self.shortcut_resume_schedule = QShortcut(QKeySequence("Ctrl+Shift+R"), self)
        self.shortcut_resume_schedule.activated.connect(self._on_resume_schedule_clicked)
        self.refresh_btn.setToolTip("Refresh (Ctrl+R)")
        self.freq_action_btn.setToolTip("QSY now and pause schedule control for the selected duration (Ctrl+Enter)")
        self._install_dashboard(root, header, view_row)
        QTimer.singleShot(0, self._update_responsive_layout)

    def _install_dashboard(self, root, header, legacy_view_row) -> None:
        """Rehouse existing panels; do not create another projection or command path."""
        root.setSpacing(max(4, self.fontMetrics().lineSpacing() // 4))
        self.dashboard = OpsDashboard(self, action_role=self._schedule_action_button_role,
                                      action_tooltip=self._schedule_action_tooltip)
        self._dashboard_traffic_volumes = None
        self._dashboard_traffic_expanded = self.traffic_group_title.isChecked()
        for widget in (self.sop_attention_widget, self.search_brief_widget,
                       self.top_splitter, self.bottom_row):
            root.removeWidget(widget)
        self.top_splitter.hide()
        self.bottom_row.hide()
        root.removeItem(self.top_overview_row)
        self.top_overview_row.removeWidget(self.inbox_box)
        root.removeItem(legacy_view_row)
        # Old preference controls remain owned (and keys retained), but the
        # mutually exclusive tab selector now owns presentation selection.
        self._legacy_view_controls = QWidget(self)
        self._legacy_view_controls.setLayout(legacy_view_row)
        self._legacy_view_controls.hide()
        root.removeItem(self.filter_row)
        self.dashboard_filters = QWidget()
        self.dashboard_filters.setLayout(self.filter_row)
        self.dashboard_filters.hide()
        toolbar = QGridLayout()
        self.dashboard_toolbar = toolbar
        for widget in (self.search_edit, self.app_search_btn, self.command_palette_btn,
                       self.intelligence_search_btn, self.refresh_btn):
            self.filter_row.removeWidget(widget)
        self.search_edit.setMinimumWidth(160)
        self.dashboard_filters_btn = QPushButton("Filters")
        self.dashboard_filters_btn.setCheckable(True)
        self.dashboard_filters_btn.setToolTip("Show group, source, age and time filters. Applied scope remains visible.")
        self.dashboard_filters_btn.toggled.connect(self.dashboard_filters.setVisible)
        self._arrange_dashboard_toolbar()
        # Rebuild only the disclosed secondary controls into one compact row.
        self._arrange_filter_controls(compact=False)
        header.addWidget(self.dashboard.attention_button)
        root.insertLayout(2, toolbar)
        root.insertWidget(3, self.dashboard_filters)
        self.inbox_box.layout().removeWidget(self.traffic_action_summary)
        root.insertWidget(4, self.traffic_action_summary)
        self.dashboard.add_panel("overview", self.inbox_box)
        self.traffic_group_header.removeWidget(self.traffic_group_hint)
        self.inbox_box.layout().insertWidget(1, self.traffic_group_hint)
        self.traffic_group_hint.setWordWrap(True)
        self.traffic_group_table.setRowCount(1)
        self.traffic_group_table.setItem(0, 0, QTableWidgetItem("Awaiting traffic update"))
        self.traffic_group_table.setSpan(0, 0, 1, 3)
        self.dashboard_traffic_all_btn = QPushButton("View all →")
        self.dashboard_traffic_all_btn.clicked.connect(lambda: self.dashboard.select_page("traffic"))
        self.traffic_group_title.clicked.connect(
            lambda: self.dashboard.select_page("traffic") if self.dashboard.current_page == "overview" else None
        )
        self.traffic_group_header.addWidget(self.dashboard_traffic_all_btn)
        self.dashboard.add_panel("traffic", self.activity_box)
        self.dashboard.add_panel("sop", self.sop_attention_widget)
        self.sop_attention_widget.builder_button.hide()
        self.dashboard_manage_sop_btn = QPushButton("Open SOP Builder")
        self.dashboard_manage_sop_btn.clicked.connect(lambda: self._activate_intelligence_context("sop", {}))
        self.dashboard.page_layouts["sop"].addWidget(self.dashboard_manage_sop_btn, alignment=Qt.AlignLeft)
        self.dashboard.add_panel("peers", self.intersection_box)
        self.dashboard.add_panel("schedule", self.schedule_box)
        self.dashboard.add_panel("propagation", self.prop_box)
        self.dashboard.add_panel("search", self.search_brief_widget)
        self.search_brief_widget.hide()
        self.dashboard.pageChanged.connect(self._dashboard_page_changed)
        self.dashboard.scheduleAction.connect(self._on_schedule_action_clicked)
        self.dashboard.mapRequested.connect(self._open_operational_activity_map)
        root.addWidget(self.dashboard, 1)
        self._dashboard_page_changed("overview")

    def _dashboard_page_changed(self, key: str) -> None:
        """Presentation only: the same traffic table moves, never duplicates."""
        if key in {"overview", "traffic"}:
            self.dashboard.page_layouts[key].insertWidget(0, self.inbox_box)
            self.inbox_box.show()
            self.traffic_source_detail_btn.setVisible(key == "traffic")
            self.dashboard_traffic_all_btn.setVisible(key == "overview")
            self.inbox_table.setVisible(key == "traffic" and self.traffic_source_detail_btn.isChecked())
            self.traffic_group_title.blockSignals(True)
            self.traffic_group_title.setCheckable(key == "traffic")
            self.traffic_group_title.setChecked(key == "traffic" and self._dashboard_traffic_expanded)
            self.traffic_group_title.blockSignals(False)
            self.traffic_group_table.setVisible(key == "overview" or self._dashboard_traffic_expanded)
            if self._dashboard_traffic_volumes is not None:
                self._render_traffic_group_volumes(self._dashboard_traffic_volumes)
        self._request_ops_geometry_sync()

    def _dashboard_card_enabled(self, key: str) -> bool:
        return hasattr(self, "dashboard") or bool(self._view_cards.get(key, True))

    def _arrange_dashboard_toolbar(self) -> None:
        controls = (self.search_edit, self.app_search_btn, self.command_palette_btn,
                    self.dashboard_filters_btn, self.intelligence_search_btn, self.refresh_btn)
        layout = self.dashboard_toolbar
        natural = sum(max(w.minimumWidth(), w.minimumSizeHint().width()) for w in controls)
        compact = self.width() < natural + max(0, layout.spacing()) * 5 + 16
        for widget in controls:
            layout.removeWidget(widget)
        for column in range(6):
            layout.setColumnStretch(column, 0)
        if compact:
            layout.addWidget(self.search_edit, 0, 0, 1, 4)
            layout.addWidget(self.app_search_btn, 0, 4)
            layout.addWidget(self.command_palette_btn, 0, 5)
            for column, widget in zip((0, 2, 4), controls[3:]):
                layout.addWidget(widget, 1, column, 1, 2)
            for column in range(4):
                layout.setColumnStretch(column, 1)
        else:
            for column, widget in enumerate(controls):
                layout.addWidget(widget, 0, column)
            layout.setColumnStretch(0, 1)

    @staticmethod
    def _setup_table_defaults(table: QTableWidget) -> None:
        table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        vh = table.verticalHeader()
        vh.setVisible(False)
        # Rows and headers must follow the active text metrics. A literal 24px
        # row clipped Large Text and indicator delegates on some platforms.
        row_floor = font_derived_widget_height(
            table, vertical_padding=8, floor=24, include_size_hints=False
        )
        vh.setDefaultSectionSize(row_floor)
        hh = table.horizontalHeader()
        hh.setSectionsMovable(False)
        hh.setHighlightSections(False)
        header_floor = font_derived_widget_height(hh, vertical_padding=10, floor=28)
        hh.setMinimumHeight(header_floor)
        hh.setMaximumHeight(max(hh.maximumHeight(), header_floor))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_freq_meta_text()
        self._update_responsive_layout()
        self._request_ops_geometry_sync()

    def _controlfreq_responsive_mode_for_width(self, width: int) -> str:
        try:
            return "compact" if int(width) < int(self._responsive_compact_width) else "wide"
        except Exception:
            return "wide"

    def _update_responsive_layout(self) -> None:
        if hasattr(self, "dashboard"):
            try:
                compact = self.width() < self.fontMetrics().horizontalAdvance("M") * 85
            except RuntimeError:
                return
            self._responsive_layout_mode = "compact" if compact else "wide"
            self._arrange_dashboard_toolbar()
            self._arrange_filter_controls(compact)
            self._apply_ops_table_column_layout(compact)
            return
        if not hasattr(self, "top_overview_row") or not hasattr(self, "top_splitter"):
            return
        # A zero-delay first-layout callback can remain queued while a short-
        # lived test/page is being destroyed. Treat that lifecycle race as a
        # cancelled presentation update rather than touching a deleted QObject.
        try:
            width = int(self.width() or 0)
        except RuntimeError:
            return
        mode = self._controlfreq_responsive_mode_for_width(width)
        self._arrange_filter_controls(mode == "compact")
        self._apply_ops_table_column_layout(mode == "compact")
        compact = mode == "compact"
        for name in ("focus_map_btn", "focus_pin_btn", "focus_history_btn"):
            button = getattr(self, name, None)
            if button is not None:
                button.setVisible(not compact)
        if hasattr(self, "focus_more_btn"):
            self.focus_more_btn.setVisible(compact)
        if mode == self._responsive_layout_mode:
            return
        self._responsive_layout_mode = mode
        try:
            self.top_overview_row.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)
            self.top_splitter.setOrientation(Qt.Vertical if compact else Qt.Horizontal)
        except Exception:
            pass
        control_visible = bool(getattr(self.freq_ctrl_box, "isVisible", lambda: True)())
        if compact:
            if control_visible:
                self.freq_ctrl_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                self.freq_ctrl_box.setMinimumWidth(0)
                self.freq_ctrl_box.setMaximumWidth(16777215)
            self.inbox_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.top_splitter.setSizes([1, 1])
        else:
            if control_visible:
                self.freq_ctrl_box.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
                self.freq_ctrl_box.setMinimumWidth(380)
                self.freq_ctrl_box.setMaximumWidth(540)
        self._rebalance_main_card_layout()
        if not compact:
            self._apply_saved_splitter_sizes()
        self._sync_top_panel_heights()

    def _arrange_filter_controls(self, compact: bool) -> None:
        layout = getattr(self, "filter_row", None)
        if layout is None:
            return
        if hasattr(self, "dashboard"):
            controls = (self.group_combo, self.traffic_source_combo, self.traffic_age_combo,
                        self.clear_filters_btn, self.time_toggle_btn)
            for control in controls:
                layout.removeWidget(control)
            for column in range(10):
                layout.setColumnStretch(column, 0)
            if compact:
                for column, control in enumerate(controls[:3]):
                    layout.addWidget(control, 0, column)
                    layout.setColumnStretch(column, 1)
                layout.addWidget(self.clear_filters_btn, 1, 0, 1, 2)
                layout.addWidget(self.time_toggle_btn, 1, 2)
            else:
                for column, control in enumerate(controls):
                    layout.addWidget(control, 0, column)
                    layout.setColumnStretch(column, 1 if column < 3 else 0)
            return
        controls = (
            self.search_edit,
            self.app_search_btn,
            self.command_palette_btn,
            self.group_combo,
            self.traffic_source_combo,
            self.traffic_age_combo,
            self.refresh_btn,
            self.clear_filters_btn,
            self.time_toggle_btn,
            self.intelligence_search_btn,
        )
        for control in controls:
            layout.removeWidget(control)
        for column in range(10):
            layout.setColumnStretch(column, 0)
        if compact:
            layout.addWidget(self.search_edit, 0, 0, 1, 2)
            layout.addWidget(self.app_search_btn, 0, 2)
            layout.addWidget(self.command_palette_btn, 0, 3)
            layout.addWidget(self.group_combo, 1, 0)
            layout.addWidget(self.traffic_source_combo, 1, 1)
            layout.addWidget(self.traffic_age_combo, 1, 2)
            layout.addWidget(self.refresh_btn, 1, 3)
            layout.addWidget(self.clear_filters_btn, 2, 0, 1, 2)
            layout.addWidget(self.time_toggle_btn, 2, 2)
            layout.addWidget(self.intelligence_search_btn, 2, 3)
            for column in range(4):
                layout.setColumnStretch(column, 1)
        else:
            for column, control in enumerate(controls):
                layout.addWidget(control, 0, column)
            layout.setColumnStretch(0, 1)

    def _apply_ops_table_column_layout(self, compact: bool) -> None:
        """Keep meaningful center columns elastic as the navigation width changes."""
        layouts = (
            (getattr(self, "source_lanes_table", None), ("contents", "stretch", "stretch", "stretch")),
            (getattr(self, "awareness_table", None), ("contents", "contents", "stretch", "contents")),
            (getattr(self, "activity_table", None), ("contents", "stretch", "stretch", "stretch")),
            (getattr(self, "peer_chart_table", None), ("contents", "stretch", "contents")),
            (getattr(self, "traffic_group_table", None), ("contents", "stretch", "stretch")),
            (getattr(self, "inbox_table", None), ("contents", "contents", "stretch")),
        )
        for table, modes in layouts:
            if table is None:
                continue
            header = table.horizontalHeader()
            header.setStretchLastSection(False)
            for column, column_mode in enumerate(modes):
                mode = QHeaderView.Stretch if column_mode == "stretch" else QHeaderView.ResizeToContents
                header.setSectionResizeMode(column, mode)
        if hasattr(self, "traffic_group_hint"):
            expanded = bool(getattr(self, "traffic_group_title", None) and self.traffic_group_title.isChecked())
            self.traffic_group_hint.setVisible(
                (self.dashboard.current_page == "overview" or expanded) if hasattr(self, "dashboard") else not compact and expanded
            )

    def _lock_frequency_control_height(self) -> None:
        if hasattr(self, "dashboard"):
            return
        try:
            if not bool(getattr(self.freq_ctrl_box, "isVisible", lambda: True)()):
                self.inbox_box.setMinimumHeight(
                    self._content_fit_group_height(self.inbox_box, floor=150)
                )
                self.inbox_box.setMaximumHeight(16777215)
                return
            # Recompute from natural content height, then lock to keep stable size across modes.
            self.freq_ctrl_box.setMinimumHeight(0)
            self.freq_ctrl_box.setMaximumHeight(16777215)
            height = max(140, int(self.freq_ctrl_box.sizeHint().height()))
            self._freq_ctrl_fixed_height = height
            self.freq_ctrl_box.setMinimumHeight(height)
            self.freq_ctrl_box.setMaximumHeight(height)
            self._sync_top_panel_heights()
        except Exception:
            pass

    def _sync_top_panel_heights(self) -> None:
        if hasattr(self, "dashboard"):
            return
        try:
            self._set_left_activity_content_height()
            if not bool(getattr(self.freq_ctrl_box, "isVisible", lambda: True)()):
                self.inbox_box.setMinimumHeight(
                    self._content_fit_group_height(self.inbox_box, floor=150)
                )
                self.inbox_box.setMaximumHeight(16777215)
                if getattr(self, "_responsive_layout_mode", "wide") == "compact":
                    for widget, height in (
                        (self.intersection_box, self._content_fit_group_height(self.intersection_box, floor=96)),
                        (self.schedule_box, self._content_fit_group_height(self.schedule_box, floor=120)),
                    ):
                        widget.setMinimumHeight(height)
                        widget.setMaximumHeight(height)
                return
            if getattr(self, "_responsive_layout_mode", "wide") == "compact":
                freq_h = max(170, int(self.freq_ctrl_box.sizeHint().height()))
                inbox_h = max(
                    150,
                    int(self._message_summary_target_height or 0),
                    int(self.inbox_box.sizeHint().height()),
                )
                activity_h = max(150, int(self.activity_box.sizeHint().height()))
                intersection_h = self._content_fit_group_height(self.intersection_box, floor=96)
                schedule_h = self._content_fit_group_height(self.schedule_box, floor=120)
                for widget, height in (
                    (self.freq_ctrl_box, freq_h),
                    (self.inbox_box, inbox_h),
                    (self.activity_box, activity_h),
                    (self.intersection_box, intersection_h),
                    (self.schedule_box, schedule_h),
                ):
                    widget.setMinimumHeight(height)
                    widget.setMaximumHeight(16777215)
                return
            self._set_left_activity_content_height()
            h_freq = max(140, int(self.freq_ctrl_box.sizeHint().height()))
            h_inbox = max(
                140,
                int(self._message_summary_target_height or 0),
                int(self.inbox_box.sizeHint().height()),
            )
            top_h = max(h_freq, h_inbox)
            self._freq_ctrl_fixed_height = top_h
            self.freq_ctrl_box.setMinimumHeight(top_h)
            self.freq_ctrl_box.setMaximumHeight(top_h)
            self.inbox_box.setMinimumHeight(top_h)
            self.inbox_box.setMaximumHeight(top_h)
        except Exception:
            pass

    def _set_message_summary_visible_rows(self, rows: int = 7) -> None:
        try:
            rows = max(1, int(rows))
            header_h = max(
                int(self.inbox_table.horizontalHeader().height()),
                int(self.inbox_table.horizontalHeader().sizeHint().height()),
                22,
            )
            row_h = max(int(self.inbox_table.verticalHeader().defaultSectionSize()), 18)
            frame_h = int(self.inbox_table.frameWidth()) * 2
            target_h = header_h + (row_h * rows) + frame_h + 4
            self._message_summary_target_height = max(
                target_h if self.inbox_table.isVisible() else 0,
                int(self.inbox_box.sizeHint().height()),
            )
            self.inbox_table.setMinimumHeight(target_h)
            self.inbox_table.setMaximumHeight(target_h)
            self._sync_top_panel_heights()
        except Exception:
            pass

    def _schedule_persist_ui_state(self, *_args) -> None:
        if hasattr(self, "_prefs_timer"):
            self._prefs_timer.start(300)

    def _persist_ui_state(self) -> None:
        try:
            if getattr(self, "_responsive_layout_mode", "wide") == "wide":
                self._saved_top_sizes = list(self.top_splitter.sizes())
                self._saved_left_sizes = list(self.left_splitter.sizes())
                self._saved_right_sizes = list(self.right_splitter.sizes())
            values = {
                "controlfreq_show_local": bool(self._show_local),
                "controlfreq_focus_mode": bool(self._focus_mode),
                # Focus is deliberately session-only. Persistent monitoring is
                # represented by Pins, not a restored hidden dashboard lens.
                "controlfreq_search": "",
                "controlfreq_group_filter": (self.group_combo.currentData() or "").strip().upper(),
                "controlfreq_traffic_age_seconds": int(self.traffic_age_combo.currentData() or 0),
                "controlfreq_activity_window_min": int(self.activity_window_combo.currentData() or 120),
                "controlfreq_intersection_window_min": int(self.intersection_window_combo.currentData() or 120),
                "controlfreq_view_preset": str(self._view_preset or "Operations"),
                "controlfreq_view_cards": dict(self._view_cards or {}),
                "controlfreq_top_splitter_sizes": list(self._saved_top_sizes),
                "controlfreq_left_splitter_sizes": list(self._saved_left_sizes),
                "controlfreq_right_splitter_sizes": list(self._saved_right_sizes),
            }
            self.settings.set_many(values)
        except Exception as e:
            log.debug("ControlFreq: failed to persist UI state: %s", e)

    def _restore_ui_state(self) -> None:
        try:
            self._show_local = bool(self.settings.get("controlfreq_show_local", self._show_local))
            self._focus_mode = False
            self._pending_group_filter = (
                str(self.settings.get("controlfreq_group_filter", "") or "").strip().upper()
            )
            saved_view_cards = self.settings.get("controlfreq_view_cards", None)
            if isinstance(saved_view_cards, dict) and saved_view_cards:
                self._view_cards = self._normalized_view_cards(saved_view_cards)
            else:
                self._view_cards = dict(self._view_presets().get("Operations", {}))
            saved_preset = str(self.settings.get("controlfreq_view_preset", "Operations") or "Operations").strip()
            if (
                saved_preset == "Schedule"
                and not bool(self.settings.get("controlfreq_operations_dashboard_seen", False))
            ):
                saved_preset = "Operations"
                self._view_cards = dict(self._view_presets().get("Operations", {}))
                try:
                    self.settings.set("controlfreq_operations_dashboard_seen", True)
                except Exception:
                    pass
            self._view_preset = self._preset_for_view_cards(self._view_cards)
            if saved_preset == "Custom":
                self._view_preset = "Custom"
            saved_window = int(self.settings.get("controlfreq_activity_window_min", 120) or 120)
            idx = self.activity_window_combo.findData(saved_window)
            if idx >= 0:
                self.activity_window_combo.blockSignals(True)
                self.activity_window_combo.setCurrentIndex(idx)
                self.activity_window_combo.blockSignals(False)
            saved_traffic_age = int(self.settings.get("controlfreq_traffic_age_seconds", 86400) or 0)
            idx = self.traffic_age_combo.findData(saved_traffic_age)
            if idx >= 0:
                self.traffic_age_combo.blockSignals(True)
                self.traffic_age_combo.setCurrentIndex(idx)
                self.traffic_age_combo.blockSignals(False)
            saved_intersection_window = int(self.settings.get("controlfreq_intersection_window_min", 120) or 120)
            idx = self.intersection_window_combo.findData(saved_intersection_window)
            if idx >= 0:
                self.intersection_window_combo.blockSignals(True)
                self.intersection_window_combo.setCurrentIndex(idx)
                self.intersection_window_combo.blockSignals(False)
            self._saved_top_sizes = list(self.settings.get("controlfreq_top_splitter_sizes", []) or [])
            self._saved_left_sizes = list(self.settings.get("controlfreq_left_splitter_sizes", []) or [])
            self._saved_right_sizes = list(self.settings.get("controlfreq_right_splitter_sizes", []) or [])
            self._apply_focus_mode()
            QTimer.singleShot(0, self._finalize_restored_ui_state)
        except Exception as e:
            log.debug("ControlFreq: failed to restore UI state: %s", e)

    def _finalize_restored_ui_state(self) -> None:
        try:
            self.objectName()
        except RuntimeError:
            return
        self._apply_saved_splitter_sizes()
        self._sync_view_controls_from_state()
        self._apply_view_state(animated=False)
        self._update_applied_filters_label()

    def _apply_saved_splitter_sizes(self) -> None:
        if hasattr(self, "dashboard"):
            return
        if getattr(self, "_responsive_layout_mode", "wide") != "wide":
            return
        try:
            if len(self._saved_top_sizes) == self.top_splitter.count():
                self.top_splitter.setSizes([max(1, int(v)) for v in self._saved_top_sizes])
            if len(self._saved_left_sizes) == self.left_splitter.count():
                self.left_splitter.setSizes([max(1, int(v)) for v in self._saved_left_sizes])
            if len(self._saved_right_sizes) == self.right_splitter.count():
                self.right_splitter.setSizes([max(1, int(v)) for v in self._saved_right_sizes])
        except Exception as e:
            log.debug("ControlFreq: failed to apply saved splitter sizes: %s", e)

    def _toggle_focus_mode(self) -> None:
        self._focus_mode = False
        self._apply_focus_mode()

    def _apply_focus_mode(self) -> None:
        self._focus_mode = False
        self.focus_mode_btn.setText("Focus Mode: Off")
        try:
            theme = self._theme()
            self.focus_mode_btn.setStyleSheet(button_style("secondary", theme))
        except Exception:
            pass
        # Retired from the ControlFreq body; the global Station Command Bar owns status visibility.
        self.status_group.setVisible(False)
        self._sync_view_controls_from_state()
        self._apply_view_state(animated=False)
        self._sync_top_panel_heights()

    @staticmethod
    def _view_presets() -> Dict[str, Dict[str, bool]]:
        return controlfreq_view_presets()

    def _normalized_view_cards(self, raw: object) -> Dict[str, bool]:
        defaults = dict(self._view_presets().get("All", {}))
        if isinstance(raw, dict):
            for key in defaults:
                if key in raw:
                    defaults[key] = bool(raw.get(key))
        if not any(defaults.values()):
            defaults["activity"] = True
        return defaults

    def _preset_for_view_cards(self, cards: Dict[str, bool]) -> str:
        norm = self._normalized_view_cards(cards)
        for preset, preset_cards in self._view_presets().items():
            if all(bool(norm.get(k)) == bool(preset_cards.get(k)) for k in preset_cards):
                return preset
        return "Custom"

    def _card_widget_map(self) -> Dict[str, QWidget]:
        return {
            "activity": self.activity_box,
            "intersections": self.intersection_box,
            "schedule": self.schedule_box,
            "propagation": self.bottom_row,
        }

    def _card_target_height(self, key: str, widget: QWidget) -> int:
        min_heights = {
            "activity": 180,
            "intersections": 96,
            "schedule": 120,
            "propagation": 220,
        }
        if key == "intersections" and hasattr(self, "intersection_box"):
            return self._content_fit_group_height(self.intersection_box, floor=96)
        if key == "schedule" and hasattr(self, "schedule_box"):
            return self._content_fit_group_height(self.schedule_box, floor=120)
        return max(
            int(min_heights.get(key, 160)),
            int(self._card_expanded_heights.get(key, 0) or 0),
            int(widget.sizeHint().height()),
            int(widget.height()),
        )

    def _stop_card_animation(self, key: str) -> None:
        anim = self._card_animations.pop(key, None)
        if anim:
            try:
                anim.stop()
            except Exception:
                pass

    def _set_card_visibility_now(self, key: str, visible: bool) -> None:
        widget = self._card_widget_map().get(key)
        if widget is None:
            return
        self._stop_card_animation(key)
        if visible:
            widget.setVisible(True)
            widget.setMinimumHeight(0)
            widget.setMaximumHeight(16777215)
            self._card_expanded_heights[key] = self._card_target_height(key, widget)
        else:
            self._card_expanded_heights[key] = self._card_target_height(key, widget)
            widget.setVisible(False)
            widget.setMinimumHeight(0)
            widget.setMaximumHeight(16777215)

    def _on_card_animation_finished(
        self, key: str, widget: QWidget, visible: bool, target_height: int
    ) -> None:
        try:
            if visible:
                widget.setVisible(True)
                widget.setMinimumHeight(0)
                widget.setMaximumHeight(16777215)
                self._card_expanded_heights[key] = max(
                    int(self._card_expanded_heights.get(key, 0) or 0),
                    int(target_height),
                )
            else:
                widget.setVisible(False)
                widget.setMinimumHeight(0)
                widget.setMaximumHeight(16777215)
        finally:
            self._card_animations.pop(key, None)
            self._rebalance_main_card_layout()
            self._sync_top_panel_heights()

    def _animate_card_visibility(self, key: str, visible: bool) -> None:
        widget = self._card_widget_map().get(key)
        if widget is None:
            return
        if visible and widget.isVisible():
            return
        if (not visible) and (not widget.isVisible()):
            return
        self._stop_card_animation(key)
        target_height = self._card_target_height(key, widget)
        if visible:
            widget.setVisible(True)
            widget.setMinimumHeight(0)
            widget.setMaximumHeight(0)
            anim = QPropertyAnimation(widget, b"maximumHeight", self)
            anim.setDuration(170)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.setStartValue(0)
            anim.setEndValue(target_height)
        else:
            start_h = max(1, int(widget.height()))
            self._card_expanded_heights[key] = max(
                int(self._card_expanded_heights.get(key, 0) or 0),
                int(start_h),
            )
            widget.setMaximumHeight(start_h)
            anim = QPropertyAnimation(widget, b"maximumHeight", self)
            anim.setDuration(140)
            anim.setEasingCurve(QEasingCurve.InCubic)
            anim.setStartValue(start_h)
            anim.setEndValue(0)
        self._card_animations[key] = anim
        anim.finished.connect(
            lambda k=key, w=widget, show=visible, h=target_height: self._on_card_animation_finished(k, w, show, h)
        )
        anim.start()

    def _rebalance_main_card_layout(self) -> None:
        if hasattr(self, "dashboard"):
            return
        left_activity = bool(self._view_cards.get("activity"))
        left_intersections = bool(self._view_cards.get("intersections"))
        right_schedule = bool(self._view_cards.get("schedule"))
        left_visible = left_activity
        right_visible = left_intersections or right_schedule
        self.left_col.setVisible(left_visible)
        self.right_col.setVisible(right_visible)
        self.top_splitter.setVisible(left_visible or right_visible)
        self._sync_inbox_summary_visibility()
        if left_visible:
            self._set_left_activity_content_height()
        if right_visible:
            if left_intersections and right_schedule:
                self._set_schedule_splitter_content_sizes()
            elif left_intersections:
                self.right_splitter.setSizes([1, 0])
                self._set_single_right_card_height(self.intersection_box, floor=96)
            else:
                self.right_splitter.setSizes([0, 1])
                self._set_single_right_card_height(self.schedule_box, floor=120)
        else:
            self._reset_right_splitter_height()
        if left_visible or right_visible:
            if getattr(self, "_responsive_layout_mode", "wide") == "compact":
                self.top_splitter.setSizes([1 if left_visible else 0, 1 if right_visible else 0])
                return
            if left_visible and right_visible:
                self.top_splitter.setSizes([1, 1])
            elif left_visible:
                self.top_splitter.setSizes([1, 0])
            else:
                self.top_splitter.setSizes([0, 1])

    def _sync_inbox_summary_visibility(self) -> None:
        if not hasattr(self, "inbox_box"):
            return
        # Traffic volume is first-class situational awareness, independent of
        # which detailed Ops cards are selected below it.
        self.inbox_box.setVisible(True)

    def _set_schedule_splitter_content_sizes(self) -> None:
        if not hasattr(self, "right_splitter"):
            return
        try:
            intersection_h = self._content_fit_group_height(self.intersection_box, floor=96)
            schedule_h = self._content_fit_group_height(self.schedule_box, floor=120)
            self.right_splitter.setSizes([max(1, intersection_h), max(1, schedule_h)])
            total_h = max(1, intersection_h) + max(1, schedule_h)
            try:
                total_h += max(8, int(self.right_splitter.handleWidth()))
            except Exception:
                total_h += 12
            self.right_splitter.setMinimumHeight(total_h)
            # The outer page owns vertical scrolling. A content-derived
            # maximum here can become stale after Local Nets, Shortwave, or
            # Schedule changes and make the later Propagation card unreachable.
            self.right_splitter.setMaximumHeight(16777215)
            self._request_ops_geometry_sync()
        except Exception:
            pass

    def _set_left_activity_content_height(self) -> None:
        if hasattr(self, "dashboard"):
            return
        if not hasattr(self, "left_splitter") or not hasattr(self, "activity_box"):
            return
        try:
            height = self._content_fit_group_height(self.activity_box, floor=180)
            self.activity_box.setMinimumHeight(height)
            self.activity_box.setMaximumHeight(height)
            self.left_splitter.setSizes([height])
            self.left_splitter.setMinimumHeight(height)
            self.left_splitter.setMaximumHeight(height)
            self.left_splitter.updateGeometry()
        except Exception:
            pass

    def _set_single_right_card_height(self, widget: QWidget, *, floor: int) -> None:
        if not hasattr(self, "right_splitter") or widget is None:
            return
        try:
            height = self._content_fit_group_height(widget, floor=floor)
            self.right_splitter.setMinimumHeight(height)
            self.right_splitter.setMaximumHeight(16777215)
            self._request_ops_geometry_sync()
        except Exception:
            self._reset_right_splitter_height()

    def _reset_right_splitter_height(self) -> None:
        splitter = getattr(self, "right_splitter", None)
        if splitter is None:
            return
        try:
            splitter.setMinimumHeight(0)
            splitter.setMaximumHeight(16777215)
        except Exception:
            pass

    def _apply_view_state(self, *, animated: bool) -> None:
        if hasattr(self, "dashboard"):
            # Keep persisted legacy presets without letting them hide pages or
            # suppress the dashboard's normal snapshot producers.
            return
        self._view_cards = self._normalized_view_cards(self._view_cards)
        if animated:
            left_target = bool(self._view_cards.get("activity"))
            right_target = bool(self._view_cards.get("intersections")) or bool(self._view_cards.get("schedule"))
            if left_target:
                self.left_col.setVisible(True)
            if right_target:
                self.right_col.setVisible(True)
            if left_target or right_target:
                self.top_splitter.setVisible(True)
        for key, visible in self._view_cards.items():
            if animated:
                self._animate_card_visibility(key, bool(visible))
            else:
                self._set_card_visibility_now(key, bool(visible))
        if not animated:
            self._rebalance_main_card_layout()
            self._sync_top_panel_heights()
            self._request_ops_geometry_sync()

    def _sync_view_controls_from_state(self) -> None:
        if not hasattr(self, "view_preset_combo") or not hasattr(self, "view_chip_buttons"):
            return
        self._view_syncing = True
        try:
            preset = self._preset_for_view_cards(self._view_cards)
            if self._view_preset != "Custom":
                self._view_preset = preset
            combo_preset = self._view_preset if self._view_preset in set(controlfreq_preset_names()) else "Custom"
            idx = self.view_preset_combo.findData(combo_preset)
            if idx >= 0 and self.view_preset_combo.currentIndex() != idx:
                self.view_preset_combo.setCurrentIndex(idx)
            for key, btn in self.view_chip_buttons.items():
                btn.setChecked(bool(self._view_cards.get(key, False)))
        finally:
            self._view_syncing = False
        self._update_view_chip_styles()

    def _on_view_preset_changed(self, _idx: int) -> None:
        if self._view_syncing:
            return
        preset = str(self.view_preset_combo.currentData() or "").strip()
        if not preset or preset == "Custom":
            return
        preset_cards = self._view_presets().get(preset)
        if not isinstance(preset_cards, dict):
            return
        previous_cards = dict(self._view_cards)
        self._view_cards = self._normalized_view_cards(preset_cards)
        self._view_preset = preset
        self._sync_view_controls_from_state()
        # Height animation inside the outer scroll area caused stale paint
        # regions during rapid card toggles. Apply the deterministic final
        # geometry directly; the dashboard is a working surface, not a reveal.
        self._apply_view_state(animated=False)
        self._refresh_newly_visible_cards(previous_cards)
        self._schedule_persist_ui_state()

    def _on_view_chip_toggled(self, key: str, checked: bool) -> None:
        if self._view_syncing:
            return
        next_cards = dict(self._view_cards)
        next_cards[key] = bool(checked)
        if not any(next_cards.values()):
            self._view_syncing = True
            try:
                btn = self.view_chip_buttons.get(key)
                if btn is not None:
                    btn.setChecked(True)
            finally:
                self._view_syncing = False
            return
        previous_cards = dict(self._view_cards)
        self._view_cards = self._normalized_view_cards(next_cards)
        self._view_preset = self._preset_for_view_cards(self._view_cards)
        self._sync_view_controls_from_state()
        self._apply_view_state(animated=False)
        self._refresh_newly_visible_cards(previous_cards)
        self._schedule_persist_ui_state()

    def _refresh_newly_visible_cards(self, previous_cards: Dict[str, bool]) -> None:
        if hasattr(self, "dashboard"):
            return
        try:
            if bool(self._view_cards.get("propagation", False)) and not bool(previous_cards.get("propagation", False)):
                # Trigger immediate target + forecast refresh when propagation is first shown.
                QTimer.singleShot(0, self._refresh_prop_target_controls)
                QTimer.singleShot(0, self._refresh_propagation_snapshot)
        except Exception as e:
            log.debug("ControlFreq: failed to refresh newly visible cards: %s", e)

    def _update_view_chip_styles(self, theme: Optional[Dict[str, str]] = None) -> None:
        if not hasattr(self, "view_chip_buttons"):
            return
        if theme is None:
            try:
                theme = self._theme()
            except Exception:
                theme = {}
        for key, btn in self.view_chip_buttons.items():
            role = "info" if bool(self._view_cards.get(key)) else "muted"
            try:
                btn.setStyleSheet(button_style(role, theme))
            except Exception:
                pass

    @staticmethod
    def _hex_to_rgb(value: str) -> Tuple[int, int, int]:
        text = (value or "").strip().lstrip("#")
        if len(text) != 6:
            return (0, 0, 0)
        try:
            return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
        except Exception:
            return (0, 0, 0)

    def _is_dark_theme(self) -> bool:
        try:
            theme = self._theme()
            r, g, b = self._hex_to_rgb(str(theme.get("bg", "#FFFFFF")))
            lum = (0.2126 * r) + (0.7152 * g) + (0.0722 * b)
            return lum < 128
        except Exception:
            return False

    def _urgency_palette(self) -> Dict[str, QColor]:
        if self._is_dark_theme():
            return {
                "critical": QColor("#5A2525"),
                "soon": QColor("#5B4420"),
                "upcoming": QColor("#203A5F"),
                "positive": QColor("#1D4A2E"),
                "warn": QColor("#5B4420"),
                "text": QColor("#F2F2F2"),
                "muted_text": QColor("#C8C8C8"),
            }
        return {
            "critical": QColor("#FFE2E2"),
            "soon": QColor("#FFF4D6"),
            "upcoming": QColor("#EAF2FF"),
            "positive": QColor("#EEF7EE"),
            "warn": QColor("#FFF4D6"),
            "text": QColor("#111111"),
            "muted_text": QColor("#555555"),
        }

    def _theme(self) -> Dict[str, Any]:
        if self._theme_cache is None:
            self._theme_cache = resolve_theme(self.settings)
        return self._theme_cache

    def _semantic_panel_colors(self, role: str) -> tuple[str, str, str]:
        theme = self._theme()
        is_dark = theme.get("bg") == "#0F1216"
        normalized = str(role or "secondary").strip().lower()
        if is_dark:
            palette = {
                "warning": ("#3A3015", "#FFF0BE", "#A06F18"),
                "success": ("#173822", "#E8F6EA", "#39874D"),
                "danger": (theme.get("surface_alt", "#202632"), theme.get("danger", "#FF8A80"), theme.get("danger", "#FF8A80")),
                "info": (theme.get("surface_alt", "#202632"), theme.get("info", "#76B7FF"), theme.get("info", "#76B7FF")),
                "secondary": ("#16263A", "#E8F1FF", "#2E4A68"),
                "panel": (theme.get("surface_alt", "#202632"), theme.get("text", "#E7EBF0"), theme.get("border", "#2A313A")),
            }
        else:
            palette = {
                "warning": ("#FFF4D6", "#1C1F21", "#E0B15B"),
                "success": ("#EEF7EE", "#1C1F21", "#8DCF9E"),
                "danger": (theme.get("surface", "#F0F2F4"), theme.get("danger", "#B71C1C"), theme.get("danger", "#B71C1C")),
                "info": (theme.get("surface", "#F0F2F4"), theme.get("info", "#0D47A1"), theme.get("info", "#0D47A1")),
                "secondary": ("#EAF2FF", "#1C1F21", "#B8D4E8"),
                "panel": (theme.get("surface", "#F0F2F4"), theme.get("text", "#1C1F21"), theme.get("border", "#D3D7DD")),
            }
        return palette.get(normalized, palette["panel"])

    def _invalidate_theme_cache(self) -> None:
        self._theme_cache = None

    def _apply_theme(self) -> None:
        self._invalidate_theme_cache()
        try:
            theme = self._theme()
            if hasattr(self, "dashboard"):
                self.dashboard.apply_theme(theme)
                self.dashboard_filters_btn.setStyleSheet(button_style("muted", theme))
                self.dashboard_traffic_all_btn.setStyleSheet(button_style("muted", theme))
                self.dashboard_manage_sop_btn.setStyleSheet(button_style("secondary", theme))
            self.help_btn.setStyleSheet(button_style("secondary", theme))
            self.intelligence_search_btn.setStyleSheet(button_style("secondary", theme))
            self.sop_attention_widget.apply_theme(theme)
            self.search_brief_widget.apply_theme(theme)
            self.app_search_btn.setStyleSheet(button_style("secondary", theme))
            self.command_palette_btn.setStyleSheet(button_style("muted", theme))
            focus_bg, focus_fg, focus_border = self._semantic_panel_colors("secondary")
            self.focus_banner.setStyleSheet(
                f"QFrame#controlfreqFocusBanner {{ background: {focus_bg}; color: {focus_fg}; "
                f"border: 1px solid {focus_border}; border-radius: 6px; }}"
            )
            for button in (
                self.focus_inbox_btn,
                self.focus_map_btn,
                self.focus_pin_btn,
                self.focus_history_btn,
                self.focus_more_btn,
                self.focus_clear_btn,
            ):
                button.setStyleSheet(button_style("secondary", theme))
            self.refresh_btn.setStyleSheet(button_style("muted", theme))
            self.clear_filters_btn.setStyleSheet(button_style("muted", theme))
            self.freq_action_btn.setStyleSheet(button_style("muted", theme))
            if hasattr(self, "operational_compose_btn"):
                self.operational_compose_btn.setStyleSheet(button_style("secondary", theme))
            if hasattr(self, "operational_pin_btn"):
                self.operational_pin_btn.setStyleSheet(button_style("muted", theme))
            if hasattr(self, "operational_clear_pins_btn"):
                self.operational_clear_pins_btn.setStyleSheet(button_style("muted", theme))
            if hasattr(self, "prop_details_btn"):
                self.prop_details_btn.setStyleSheet(
                    button_style("secondary" if self.prop_details_btn.isChecked() else "muted", theme)
                )
            if hasattr(self, "traffic_action_summary"):
                self.traffic_action_summary.apply_theme(theme)
            if hasattr(self, "traffic_source_detail_btn"):
                self.traffic_source_detail_btn.setStyleSheet(button_style("muted", theme))
            if hasattr(self, "traffic_group_bar_delegate"):
                self.traffic_group_bar_delegate.apply_theme(theme)
            if hasattr(self, "peer_rendezvous_delegate"):
                self.peer_rendezvous_delegate.apply_theme(theme)
            if hasattr(self, "peer_result_label"):
                self.peer_result_label.setStyleSheet(
                    f"color: {theme.get('text_muted', '#5b6875')};"
                )
            if hasattr(self, "traffic_group_hint"):
                self.traffic_group_hint.setStyleSheet(
                    f"color: {theme.get('text_muted', '#5b6875')};"
                )
            self._refresh_traffic_group_chart_theme()
            self._update_time_toggle_style(theme)
            self.focus_mode_btn.setStyleSheet(button_style("secondary", theme))
            self._update_view_chip_styles(theme)
            self._update_clear_filters_style()
            self.current_time_label.setStyleSheet(
                f"font-weight: 600; color: {theme.get('text', '#111')};"
            )
            if hasattr(self, "schedule_action_hint"):
                self.schedule_action_hint.setStyleSheet(f"color: {theme.get('text_muted', '#888')};")
            if hasattr(self, "local_nets_outlook_box"):
                bg, fg, border = self._semantic_panel_colors("panel")
                self.local_nets_outlook_box.setStyleSheet(
                    f"QFrame#controlfreqLocalNetsOutlook {{ background: {bg}; color: {fg}; "
                    f"border: 1px solid {border}; border-radius: 6px; }}"
                )
                self.local_nets_later_btn.setStyleSheet(button_style("muted", theme))
                self.local_nets_outlook_toggle.setStyleSheet(button_style("muted", theme))
            if hasattr(self, "shortwave_listening_outlook_box"):
                bg, fg, border = self._semantic_panel_colors("panel")
                self.shortwave_listening_outlook_box.setStyleSheet(
                    f"QFrame#controlfreqShortwaveListeningOutlook {{ background: {bg}; color: {fg}; "
                    f"border: 1px solid {border}; border-radius: 6px; }}"
                )
                self.shortwave_listening_outlook_toggle.setStyleSheet(button_style("muted", theme))
            self.effective_source_label.setStyleSheet(f"color: {theme.get('text_muted', '#888')};")
        except Exception:
            pass
        self._apply_frequency_display_style()
        self._set_message_summary_visible_rows(6)
        apply_text_size_accessibility_guards(self, include_widths=False)
        if hasattr(self, "dashboard") and self._dashboard_traffic_volumes is not None:
            self._render_traffic_group_volumes(self._dashboard_traffic_volumes)
        self._lock_frequency_control_height()
        self._update_time_toggle_text()
        self._refresh_clock_display()
        self._apply_focus_mode()
        self._on_freq_selection_changed()
        self._refresh_running_status()

    def _open_context_help(self, context_key: str) -> None:
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_context_help"):
            try:
                host.open_context_help(context_key)
            except Exception:
                pass

    def _set_propagation_details_visible(self, visible: bool) -> None:
        if hasattr(self, "prop_detail_widget"):
            self.prop_detail_widget.setVisible(bool(visible))
        self._sync_propagation_box_height()
        try:
            self.prop_details_btn.setStyleSheet(button_style("secondary" if visible else "muted", self._theme()))
        except Exception:
            pass

    def _apply_frequency_display_style(self) -> None:
        try:
            theme = self._theme()
        except Exception:
            theme = {}
        text_color = str(theme.get("text", "#F2F2F2" if self._is_dark_theme() else "#111111"))
        muted_color = str(theme.get("text_muted", "#888"))
        digital_font = QFont("Consolas")
        if not digital_font.exactMatch():
            digital_font = QFont("Courier New")
        if not digital_font.exactMatch():
            digital_font = QFont("Monospace")
        digital_font.setStyleHint(QFont.Monospace)
        digital_font.setPixelSize(max(14, int(self._hero_combo_font_px)))
        digital_font.setBold(True)
        self.freq_combo.setFont(digital_font)
        self.freq_combo.setStyleSheet(
            f"QComboBox {{ color: {text_color}; padding: 4px 24px 4px 8px; }}"
            f"QComboBox QAbstractItemView {{ color: {text_color}; }}"
        )
        combo_h = max(36, int(QFontMetrics(digital_font).height()) + 14)
        self.freq_combo.setMinimumHeight(combo_h)
        self.freq_combo.setMaximumHeight(combo_h)
        popup_font = QFont(self.font())
        popup_font.setBold(False)
        self._freq_popup_font = popup_font
        try:
            self.freq_combo.view().setFont(popup_font)
        except Exception:
            pass
        self.freq_meta_label.setStyleSheet(f"color: {muted_color};")
        self._apply_freq_meta_text()
        self._sync_frequency_info_row_heights()
        self._set_frequency_state_badge("unknown")

    def _apply_freq_meta_text(self) -> None:
        full = str(self._freq_meta_full_text or "").strip()
        if not full:
            full = "Scheduled: -- | Active: --"
        try:
            fm = QFontMetrics(self.freq_meta_label.font())
            avail = max(80, int(self.freq_meta_label.width()) - 6)
            display = fm.elidedText(full, Qt.ElideRight, avail)
            line_h = max(18, int(fm.height()) + 4)
            self.freq_meta_label.setMinimumHeight(line_h)
            self.freq_meta_label.setMaximumHeight(line_h)
        except Exception:
            display = full
        self.freq_meta_label.setText(display)
        self.freq_meta_label.setToolTip(full)

    def _sync_frequency_info_row_heights(self) -> None:
        labels = [
            getattr(self, "freq_meta_label", None),
            getattr(self, "effective_source_label", None),
            getattr(self, "next_change_label", None),
        ]
        for label in labels:
            if not isinstance(label, QLabel):
                continue
            try:
                fm = QFontMetrics(label.font())
                row_h = max(16, int(fm.height()) + 2)
                label.setMinimumHeight(row_h)
                label.setMaximumHeight(row_h)
            except Exception:
                continue

    def _set_frequency_state_badge(self, state: str) -> None:
        key = (state or "").strip().lower()
        if key not in {"on", "off", "blocked", "net_mode", "unknown"}:
            key = "unknown"
        labels = {
            "on": "On Schedule",
            "off": "Off Schedule",
            "blocked": "Blocked",
            "net_mode": "Net Mode Changed",
            "unknown": "Unknown",
        }
        role = {
            "on": "success",
            "off": "warning",
            "blocked": "danger",
            "net_mode": "info",
            "unknown": "panel",
        }.get(key, "panel")
        bg, _fg, border = self._semantic_panel_colors(role)
        theme = self._theme()
        self.freq_state_badge.setText(labels.get(key, "Unknown"))
        self.freq_state_badge.setStyleSheet(
            f"{label_style(role if role != 'panel' else 'text', theme, weight=600)} "
            f"border-radius: 6px; padding: 0 8px; background: {bg}; border: 1px solid {border};"
        )

    def apply_theme(self) -> None:
        self._invalidate_theme_cache()
        self._apply_theme()

    def set_tab_active(self, active: bool) -> None:
        self._active = bool(active)
        if not self._active:
            self._clear_intelligence_search()
        if self._active:
            self._ensure_operator_roster_read()
            # Give the stack switch and its first paint a chance to complete
            # before its optional index maintenance begins.  The worker itself
            # is bounded, but beginning it during shell construction can still
            # contend with the startup message projection database work.
            # Historical indexing is useful but never startup-critical. Give
            # the shell and endpoint services time to settle before beginning
            # small cooperative batches.
            QTimer.singleShot(2500, self._schedule_focus_index_backfill)
            self._reload_sop_manager_settings()
            if self._timer is None:
                self._timer = QTimer(self)
                self._timer.timeout.connect(self._on_periodic_refresh_tick)
            self._timer.start(60_000)
            if self._freq_timer is None:
                self._freq_timer = QTimer(self)
                self._freq_timer.timeout.connect(self._refresh_frequency_control_tick)
            self._freq_timer.start(5000)
            if self._status_timer is None:
                self._status_timer = QTimer(self)
                self._status_timer.timeout.connect(self._refresh_status_widgets)
            self._status_timer.start(10000)
            if self._clock_timer is None:
                self._clock_timer = QTimer(self)
                self._clock_timer.timeout.connect(self._refresh_clock_display)
            self._clock_timer.start(1000)
            # Keep tab switch snappy: defer initial refresh work until after
            # the screen change event has returned to the UI loop.
            QTimer.singleShot(75, self._refresh_frequency_control_tick)
            # Slightly delay status probing so first paint is not blocked.
            QTimer.singleShot(175, self._refresh_status_widgets)
            QTimer.singleShot(0, self._refresh_clock_display)
            if self._sop_outlook_refresh_pending and self._dashboard_card_enabled("schedule"):
                self._sop_outlook_refresh_pending = False
                QTimer.singleShot(0, self._refresh_schedule_outlook)
            return
        if self._timer:
            self._timer.stop()
        if self._freq_timer:
            self._freq_timer.stop()
        if self._status_timer:
            self._status_timer.stop()
        if self._clock_timer:
            self._clock_timer.stop()

    def on_tab_activated(self) -> None:
        with perf_span("controlfreq.on_tab_activated", settings=self.settings, min_ms=5.0):
            self._schedule_focus_index_backfill()
            now = time.time()
            stale = (self._last_refresh_ts <= 0.0) or (
                now - float(self._last_refresh_ts) >= self._activation_refresh_interval_sec
            )
            if stale and not self._activation_refresh_pending:
                self._activation_refresh_pending = True
                QTimer.singleShot(0, self._run_activation_refresh)

    def _refresh_frequency_control_tick(self) -> None:
        self._refresh_frequency_control(include_intersections=False)

    def _on_periodic_refresh_tick(self) -> None:
        # Keep periodic refresh non-blocking; defer heavy sections.
        self._refresh_all(include_secondary=False, include_heavy=False, include_status=False)
        self._schedule_deferred_secondary_refresh(force=False)

    def _run_activation_refresh(self) -> None:
        try:
            with perf_span("controlfreq.activation_refresh", settings=self.settings, min_ms=10.0):
                # Status updates run on the dedicated status timer path.
                self._refresh_all(include_secondary=False, include_heavy=False, include_status=False)
                self._schedule_deferred_secondary_refresh(force=True)
        finally:
            self._activation_refresh_pending = False

    def _schedule_deferred_secondary_refresh(self, force: bool = False) -> None:
        if self._secondary_refresh_pending:
            return
        now = time.time()
        if not force and (now - float(self._last_secondary_refresh_ts) < self._secondary_refresh_interval_sec):
            return
        self._secondary_refresh_pending = True
        QTimer.singleShot(25, self._run_deferred_secondary_refresh)

    def _run_deferred_secondary_refresh(self) -> None:
        try:
            with perf_span("controlfreq.secondary_refresh", settings=self.settings, min_ms=10.0):
                self._load_group_combo()
                if self._dashboard_card_enabled("activity"):
                    self._refresh_activity()
                if self._dashboard_card_enabled("intersections"):
                    self._refresh_intersections()
                if self._dashboard_card_enabled("schedule"):
                    self._refresh_schedule_outlook()
                if self._dashboard_card_enabled("propagation"):
                    self._refresh_prop_target_controls()
                self._last_secondary_refresh_ts = time.time()
                self._schedule_deferred_heavy_refresh(force=True)
        finally:
            self._secondary_refresh_pending = False

    def _schedule_deferred_heavy_refresh(self, force: bool = False) -> None:
        if self._heavy_refresh_pending:
            return
        now = time.time()
        if not force and (now - float(self._last_heavy_refresh_ts) < self._heavy_refresh_interval_sec):
            return
        self._heavy_refresh_pending = True
        QTimer.singleShot(50, self._run_deferred_heavy_refresh)

    def _run_deferred_heavy_refresh(self) -> None:
        try:
            with perf_span("controlfreq.heavy_refresh", settings=self.settings, min_ms=10.0):
                self._refresh_message_summary()
                if self._dashboard_card_enabled("propagation"):
                    self._refresh_propagation_snapshot()
                self._last_heavy_refresh_ts = time.time()
        finally:
            self._heavy_refresh_pending = False

    def on_settings_saved(self) -> None:
        try:
            self.plan_context_label.invalidate_context()
            self.plan_context_label.refresh_context(refresh=True)
        except Exception:
            pass
        self._invalidate_theme_cache()
        self._settings_reload_mtime_ns = 0
        try:
            self.settings.reload()
        except Exception:
            pass
        self._reload_sop_manager_settings()
        self._invalidate_sop_window_cache()
        self._invalidate_activity_cache()
        self._invalidate_schedule_row_caches()
        self._invalidate_operator_roster_cache()
        self._sop_outlook_refresh_pending = True
        self._apply_theme()
        if self._active:
            self._refresh_all()
            self._sop_outlook_refresh_pending = False
            return
        # Keep inactive-path cheap; refresh on next activation.
        self._last_refresh_ts = 0.0
        self._last_secondary_refresh_ts = 0.0
        self._last_heavy_refresh_ts = 0.0

    def on_peer_schedule_data_changed(self) -> None:
        self._peer_schedule_rows_cache = []
        self._peer_schedule_rows_cache_ts = 0.0
        self._peer_schedule_rows_cache_mtime = 0.0
        self._invalidate_operator_roster_cache()
        self._last_secondary_refresh_ts = 0.0
        if self._active:
            QTimer.singleShot(0, self._refresh_intersections)

    def _clear_status_layout(self) -> None:
        while self.status_layout.count():
            item = self.status_layout.takeAt(0)
            child_layout = item.layout()
            widget = item.widget()
            if child_layout is not None:
                while child_layout.count():
                    child_item = child_layout.takeAt(0)
                    child_widget = child_item.widget()
                    if child_widget is not None:
                        child_widget.deleteLater()
                continue
            if widget is not None:
                widget.deleteLater()

    def _readiness_snapshot(self):
        """Read worker-published setup evidence; never reload SQLite on Qt."""
        cache = getattr(self, "_readiness_read_cache", None)
        if cache is None:
            cache = self._readiness_read_cache = LatestReadCache("fio-ops-readiness")
        path = self._multi_radio_store.db_path
        def reader_factory():
            data = deepcopy(self.settings.all())
            def read():
                from types import SimpleNamespace
                profiles = list(MultiRadioStore(path).list_device_profiles())
                groups = load_operating_groups(SimpleNamespace(all=lambda: data))
                return {
                    "report": build_station_readiness_report(data, device_profiles=profiles, operating_groups=groups),
                    "visible": visible_status_programs(data, device_profiles=profiles),
                    "has_profiles": bool(profiles),
                }
            return read
        result = cache.read(str(path), reader_factory, refresh_after_s=10.0)
        if result is not None:
            self._readiness_snapshot_last = result
            self._readiness_has_device_profiles = result["has_profiles"]
        else:
            result = getattr(self, "_readiness_snapshot_last", None)
            if result is None:
                self._readiness_has_device_profiles = None
        timer = getattr(self, "_readiness_read_poll", None)
        if timer is None:
            timer = self._readiness_read_poll = QTimer(self)
            timer.setInterval(100)
            timer.timeout.connect(self._on_readiness_read_ready)
        if cache._future is not None and not timer.isActive():
            timer.start()
        elif cache._future is None:
            timer.stop()
        return result

    def _on_readiness_read_ready(self) -> None:
        previous = getattr(self, "_readiness_snapshot_last", None)
        result = self._readiness_snapshot()
        if result is not previous and getattr(self, "_active", False):
            self._refresh_running_status()

    def _current_visible_status_items(self) -> List[Tuple[str, str]]:
        snapshot = self._readiness_snapshot()
        if snapshot is not None:
            return list(snapshot["visible"])
        return visible_status_programs(self.settings.all(), device_profiles=[])

    def _rebuild_status_indicators(self) -> None:
        if not hasattr(self, "status_layout"):
            return
        self._clear_status_layout()
        self.status_labels = {}
        self._status_text_labels = {}
        theme = self._theme()
        visible_items = self._current_visible_status_items()
        self.status_group.setVisible(False)
        for key, label in visible_items:
            led = QLabel()
            led.setFixedSize(14, 14)
            led.setStyleSheet(led_style("idle", theme))
            text_label = QLabel(label)
            self.status_labels[key] = led
            self._status_text_labels[key] = text_label
            self.status_layout.addWidget(led)
            self.status_layout.addWidget(text_label)
            self.status_layout.addSpacing(12)
        self.status_layout.addStretch(1)

    def _current_readiness_report(self):
        snapshot = self._readiness_snapshot()
        return snapshot["report"] if snapshot is not None else None

    def _dismiss_readiness_review(self) -> None:
        self._readiness_banner_dismissed = True
        self._readiness_dismissed_digest = str(self._readiness_banner_digest or "").strip()
        try:
            self.settings.set("readiness_review_dismissed_digest", self._readiness_dismissed_digest)
        except Exception:
            pass
        self._update_readiness_review_banner()

    def _suppress_readiness_review_for_version(self) -> None:
        self._readiness_banner_dismissed = True
        self._readiness_suppressed_version = __version__
        try:
            self.settings.set("readiness_review_suppressed_version", self._readiness_suppressed_version)
        except Exception:
            pass
        self._update_readiness_review_banner()

    def _review_readiness_now(self) -> None:
        report = self._current_readiness_report()
        if report is None:
            return
        window = self.window()
        if self._readiness_has_device_profiles is False and hasattr(window, "open_guided_add_radio"):
            try:
                window.open_guided_add_radio()
                return
            except Exception:
                pass
        issue = report.first_actionable_issue()
        section_key = str(issue.section_key if issue else "freqinout")
        radio_id = int(issue.radio_id or 0) if issue and issue.radio_id else None
        if hasattr(window, "open_settings_section"):
            try:
                window.open_settings_section(section_key, radio_id=radio_id)
                return
            except Exception:
                pass

    def _copy_readiness_review_summary(self) -> None:
        report = self._current_readiness_report()
        if report is None:
            return
        QApplication.clipboard().setText(
            readiness_report_detail_text(report, title="CommsPlan Multi-Rig Setup Review")
        )
        if hasattr(self, "readiness_review_copy_btn"):
            self.readiness_review_copy_btn.setText("Copied")
            QTimer.singleShot(1500, lambda: self.readiness_review_copy_btn.setText("Copy Summary"))

    def _update_readiness_review_banner(self) -> None:
        if not hasattr(self, "readiness_review_widget"):
            return
        report = self._current_readiness_report()
        if report is None:
            self.readiness_review_widget.setVisible(False)
            return
        requires_first_radio = self._readiness_has_device_profiles is False
        if report.digest != self._readiness_banner_digest:
            self._readiness_banner_digest = report.digest
            self._readiness_banner_dismissed = False
        if int(getattr(report, "required_count", 0) or 0) <= 0 and not requires_first_radio:
            self.readiness_review_widget.setVisible(False)
            return
        if not requires_first_radio and (
            not should_show_startup_review(
                report,
                dismissed_digest=self._readiness_dismissed_digest,
                suppressed_version=self._readiness_suppressed_version,
                current_version=__version__,
            )
            or self._readiness_banner_dismissed
        ):
            self.readiness_review_widget.setVisible(False)
            return
        if requires_first_radio:
            self.readiness_review_label.setText(
                "Station setup: No radio is configured. Set up the first radio now, or return here when you are ready."
            )
            self.readiness_review_now_btn.setText("Set Up First Radio…")
            self.readiness_review_now_btn.setAccessibleName("Set up the first radio")
            self.readiness_review_dismiss_btn.setVisible(False)
            self.readiness_review_suppress_btn.setVisible(False)
        else:
            first_issue = report.first_actionable_issue()
            detail = f" First item: {format_readiness_issue(first_issue)}." if first_issue else ""
            self.readiness_review_label.setText(
                f"Setup review: {readiness_report_overall_text(report)}{detail}"
            )
            self.readiness_review_now_btn.setText("Review Now")
            self.readiness_review_now_btn.setAccessibleName("Review station setup")
            self.readiness_review_dismiss_btn.setVisible(True)
            self.readiness_review_suppress_btn.setVisible(True)
        theme = self._theme()
        border = theme.get("warning", "#C99700")
        bg = theme.get("surface_alt", theme.get("surface", "#f7f7f7"))
        fg = theme.get("text", "#222222")
        self.readiness_review_widget.setStyleSheet(
            "QWidget {"
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
        self.readiness_review_now_btn.setStyleSheet(
            button_style("primary" if requires_first_radio else "warning", theme)
        )
        self.readiness_review_copy_btn.setStyleSheet(button_style("secondary", theme))
        self.readiness_review_dismiss_btn.setStyleSheet(button_style("muted", theme))
        self.readiness_review_suppress_btn.setStyleSheet(button_style("muted", theme))
        self.readiness_review_widget.setToolTip(
            readiness_report_detail_text(report, title=f"Current readiness state: {readiness_state_label(report.overall_state)}")
        )
        self.readiness_review_widget.setVisible(True)

    def on_condition_levels_changed(self) -> None:
        self.on_sop_data_changed()

    def on_sop_data_changed(self) -> None:
        # SOP edits can happen while ControlFreq is inactive; invalidate refresh gates
        # so the next activation redraws immediately.
        self._reload_sop_manager_settings()
        self._invalidate_sop_window_cache()
        self._sop_outlook_refresh_pending = True
        self._last_refresh_ts = 0.0
        self._last_secondary_refresh_ts = 0.0
        self._last_heavy_refresh_ts = 0.0
        if self._active:
            self._sop_outlook_refresh_pending = False
            QTimer.singleShot(0, self._refresh_schedule_outlook)
            QTimer.singleShot(0, self._refresh_scheduler_strip)

    def _reload_sop_manager_settings(self) -> None:
        try:
            mgr_settings = getattr(self._sop_manager, "settings", None)
            if mgr_settings is not None and hasattr(mgr_settings, "reload"):
                mgr_settings.reload()
        except Exception:
            pass

    def _invalidate_sop_window_cache(self) -> None:
        self._sop_cache_epoch += 1
        self._sop_window_cache.clear()

    def _invalidate_activity_cache(self) -> None:
        self._activity_cache_key = ()
        self._activity_cache_rows = []
        self._activity_cache_ts = 0.0

    def _get_cached_sop_actions(
        self,
        *,
        key: Tuple[Any, ...],
        ttl_sec: float,
        builder: Callable[[], List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        now_ts = time.time()
        cached = self._sop_window_cache.get(key)
        if cached and (now_ts - float(cached[0])) <= max(0.0, float(ttl_sec)):
            return list(cached[1])
        try:
            rows = list(builder() or [])
        except Exception as e:
            log.debug("ControlFreq: SOP cache build failed for %s: %s", key, e)
            rows = []
        self._sop_window_cache[key] = (now_ts, rows)
        if len(self._sop_window_cache) > 16:
            oldest = sorted(self._sop_window_cache.items(), key=lambda kv: kv[1][0])[:4]
            for old_key, _ in oldest:
                self._sop_window_cache.pop(old_key, None)
        return list(rows)

    def _update_time_toggle_text(self) -> None:
        self.time_toggle_btn.setText("Times: Local" if self._show_local else "Times: UTC")
        self._update_time_toggle_style()

    def _update_time_toggle_style(self, theme: Optional[Dict[str, str]] = None) -> None:
        if theme is None:
            try:
                theme = self._theme()
            except Exception:
                theme = {}
        # Local is default context; only highlight when user switches to UTC.
        role = "info" if not self._show_local else "muted"
        try:
            self.time_toggle_btn.setStyleSheet(button_style(role, theme))
        except Exception:
            pass

    def _format_display_datetime(self, utc_dt: dt.datetime, with_seconds: bool = True) -> str:
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=dt.timezone.utc)
        display_dt = utc_dt.astimezone(self._get_display_tz()) if self._show_local else utc_dt
        fmt = "%a %b %d, %Y %H:%M %Z" if with_seconds else "%Y-%m-%d %H:%M %Z"
        return display_dt.strftime(fmt)

    def _sync_header_time_label_widths(self) -> None:
        try:
            full = str(getattr(self, "_current_time_full_text", "") or "--")
            width = int(self.current_time_label.width())
            if width <= 20:
                self.current_time_label.setText(full)
                self.current_time_label.setToolTip("")
                return
            fm = QFontMetrics(self.current_time_label.font())
            elided = fm.elidedText(full, Qt.ElideRight, max(24, width - 6))
            self.current_time_label.setText(elided)
            self.current_time_label.setToolTip(full if elided != full else "")
        except Exception:
            pass

    def _refresh_clock_display(self) -> None:
        now_utc = dt.datetime.now(dt.timezone.utc)
        self._current_time_full_text = self._format_display_datetime(now_utc, with_seconds=True)
        self.current_time_label.setText(self._current_time_full_text)
        self._sync_header_time_label_widths()

    def _toggle_time_view(self) -> None:
        self._show_local = not self._show_local
        self._update_time_toggle_text()
        self._refresh_clock_display()
        self._schedule_persist_ui_state()
        self._refresh_schedule_outlook()

    def _activate_intelligence_context(self, kind: str, context: dict) -> None:
        """Route the explicitly selected brief item, independent of dashboard selection."""
        if kind in {"sop", "open_sop"}:
            self._navigate_to_tab("SOP")
            return
        route = map_context_from_mapping(context)
        host = self.window()
        if kind == "map" and hasattr(host, "open_spotter_map"):
            host.open_spotter_map(**route.as_map_kwargs())
        elif kind == "compose" and hasattr(host, "open_messages_section"):
            intent = compose_intent_from_map_context(route, mode=str(context.get("source_family") or "")).as_dict()
            host.open_messages_section("compose", compose_intent=intent)
        elif hasattr(host, "open_messages_section"):
            host.open_messages_section("inbox", **route.as_messages_kwargs())

    def set_sop_attention_snapshot(self, snapshot) -> None:
        """Shell-published immutable data only; no queries or aggregation."""
        if snapshot != getattr(self, "_last_sop_attention_snapshot", None):
            self._last_sop_attention_snapshot = snapshot
            self.sop_attention_widget.set_snapshot(snapshot)
            if hasattr(self, "dashboard"):
                self.dashboard.set_attention(snapshot)

    def _set_sop_guidance(self, enabled: bool) -> None:
        # In-memory preference update only. One worker writes the isolated key.
        self.settings._data["controlfreq_sop_guidance"] = bool(enabled)
        self._guidance_save_generation += 1
        self._guidance_save_request = (self._guidance_save_generation, bool(enabled))
        self.sop_attention_widget.set_preference_error("")
        self._brief_poll.start()
        self._poll_intelligence_publications()

    def _start_intelligence_search(self, kind: str) -> None:
        if not self._active:
            return
        query = self.search_edit.text().strip()[:120]
        if not query:
            self.search_brief_widget.set_error("Enter a callsign, topic, or SOP event first.")
            self.search_brief_widget.show()
            if hasattr(self, "dashboard"):
                self.dashboard.select_page("search")
            return
        self._clear_intelligence_search()
        age = max(0, int(self.traffic_age_combo.currentData() or 0))
        group = str(self.group_combo.currentData() or "")
        source = str(self.traffic_source_combo.currentData() or "")
        label = " · ".join((self.group_combo.currentText(), self.traffic_source_combo.currentText(), self.traffic_age_combo.currentText()))
        self._brief_request = SearchRequest(kind, query, group, source, time.time() - age if age else 0., label)
        self._brief_sop_snapshot = getattr(self, "_last_sop_attention_snapshot", None)
        self.search_brief_widget.set_loading(kind, query)
        self.search_brief_widget.show()
        if hasattr(self, "dashboard"):
            self.dashboard.select_page("search")
        self._brief_poll.start()
        self._poll_intelligence_publications()

    def _clear_intelligence_search(self, *_args) -> None:
        self._brief_cancel.set()
        self._brief_cancel = Event()
        self._brief_generation += 1
        self._brief_request = None
        self._brief_published = None
        self._brief_cache.invalidate()
        widget = getattr(self, "search_brief_widget", None)
        if widget is not None:
            widget.hide()
        if hasattr(self, "dashboard") and self.dashboard.current_page == "search":
            self.dashboard.select_page("overview")
        if self._guidance_save_request is None:
            self._brief_poll.stop()

    def _poll_intelligence_publications(self) -> None:
        request = self._brief_request
        if request is not None:
            cancel = self._brief_cancel
            path = self._brief_db_path
            sop = getattr(self, "_brief_sop_snapshot", None)
            result = self._brief_cache.read((self._brief_generation, request),
                lambda: lambda path=path, request=request, cancel=cancel, sop=sop: read_search_brief(path, request, cancel, sop))
            if result is not None and result is not self._brief_published:
                self._brief_published = result
                self.search_brief_widget.set_result(result)
            elif self._brief_cache.failed and self._brief_published is None:
                self.search_brief_widget.set_error("Search unavailable or exceeded its time budget. Narrow the filters and search again.")
                self._brief_request = None
        saving = self._guidance_save_request
        if saving is not None:
            enabled = saving[1]
            path = self._guidance_db_path
            saved = self._guidance_save_cache.read(saving,
                lambda: lambda path=path, enabled=enabled: save_guidance_preference(path, enabled))
            if saved is not None or self._guidance_save_cache.failed:
                if self._guidance_save_cache.failed:
                    self.sop_attention_widget.set_preference_error("Preference could not be saved; applies to this session only.")
                self._guidance_save_request = None
        if (self._brief_request is None or self._brief_published is not None) and self._guidance_save_request is None:
            self._brief_poll.stop()

    def _on_filters_changed(self, *_args) -> None:
        self._clear_intelligence_search()
        self._update_clear_filters_style()
        self._update_applied_filters_label()
        self._schedule_persist_ui_state()
        if self._active_ops_focus is not None:
            self._request_focus_snapshot()
        try:
            self._filter_refresh_timer.start(220)
        except Exception:
            self._run_filter_refresh()

    def _run_filter_refresh(self) -> None:
        try:
            if self._dashboard_card_enabled("activity"):
                self._refresh_activity()
            if self._dashboard_card_enabled("intersections"):
                self._refresh_intersections()
            if self._dashboard_card_enabled("schedule"):
                self._refresh_schedule_outlook()
            # Message summary is always visible in top row.
            self._refresh_message_summary()
            self._last_secondary_refresh_ts = time.time()
            self._last_heavy_refresh_ts = self._last_secondary_refresh_ts
            self._refresh_clock_display()
            self._update_clear_filters_style()
        except Exception as e:
            log.debug("ControlFreq: filter refresh failed: %s", e)

    def _clear_filters(self) -> None:
        self._clear_intelligence_search()
        self._clear_ops_focus(clear_search=True, refresh=False)
        self.search_edit.clear()
        if self.group_combo.count() > 0:
            self.group_combo.setCurrentIndex(0)
        if self.activity_window_combo.count() > 0:
            idx = self.activity_window_combo.findData(120)
            self.activity_window_combo.setCurrentIndex(idx if idx >= 0 else 0)
        if hasattr(self, "intersection_window_combo") and self.intersection_window_combo.count() > 0:
            idx = self.intersection_window_combo.findData(120)
            self.intersection_window_combo.setCurrentIndex(idx if idx >= 0 else 0)
        if hasattr(self, "traffic_age_combo"):
            idx = self.traffic_age_combo.findData(24 * 60 * 60)
            self.traffic_age_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._source_family_filter = ""
        if hasattr(self, "traffic_source_combo"):
            previous = self.traffic_source_combo.blockSignals(True)
            try:
                self.traffic_source_combo.setCurrentIndex(0)
            finally:
                self.traffic_source_combo.blockSignals(previous)
        if hasattr(self, "source_lanes_table"):
            self.source_lanes_table.clearSelection()
        if hasattr(self, "peer_callsign_filter"):
            self.peer_callsign_filter.clear()
        for combo_name in ("peer_group_filter", "peer_region_filter", "peer_role_filter"):
            combo = getattr(self, combo_name, None)
            if isinstance(combo, QComboBox) and combo.count() > 0:
                combo.setCurrentIndex(0)
        self._awareness_topic_filter = ""
        self._operational_awareness_context = {}
        self._update_clear_filters_style()
        self._on_filters_changed()

    def _filters_active(self) -> bool:
        search_active = self._active_ops_focus is not None
        group_active = bool((self.group_combo.currentData() or "").strip())
        window_active = int(self.activity_window_combo.currentData() or 120) != 120
        intersection_combo = getattr(self, "intersection_window_combo", self.activity_window_combo)
        intersection_window_active = int(intersection_combo.currentData() or 120) != 120
        source_active = bool(str(getattr(self, "_source_family_filter", "") or "").strip())
        topic_active = bool(str(getattr(self, "_awareness_topic_filter", "") or "").strip())
        peer_filter_active = bool(
            str(getattr(getattr(self, "peer_callsign_filter", None), "text", lambda: "")() or "").strip()
            or any(
                str(getattr(getattr(self, name, None), "currentData", lambda: "")() or "").strip()
                for name in ("peer_group_filter", "peer_region_filter", "peer_role_filter")
            )
        )
        return (
            search_active
            or group_active
            or window_active
            or intersection_window_active
            or source_active
            or topic_active
            or peer_filter_active
        )

    def _show_app_search_results(self) -> None:
        """Backward-compatible route for the navigation command search."""
        self._show_command_palette()

    def _show_command_palette(self) -> None:
        query = (self.search_edit.text() or "").strip()
        if not query:
            query, ok = QInputDialog.getText(
                self,
                "Find in CommsPlan",
                "Screen, setting, radio, schedule, action, or issue:",
            )
            if not ok:
                return
            query = str(query or "").strip()
        if not query:
            return
        root = self.window()
        if hasattr(root, "show_quick_search_results"):
            try:
                root.show_quick_search_results(query, self.search_edit)
                return
            except Exception as e:
                log.debug("ControlFreq: app search failed: %s", e)

    def _ensure_focus_executor(self) -> ThreadPoolExecutor:
        if self._focus_executor is None:
            self._focus_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="fio-ops-focus",
            )
        return self._focus_executor

    def _shutdown_background_executors(self) -> None:
        self._closing = True
        self._operator_roster_generation += 1
        self._operator_roster_pending = False
        roster_executor = self._operator_roster_executor
        self._operator_roster_executor = None
        self._operator_roster_future = None
        if roster_executor is not None:
            try:
                roster_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                roster_executor.shutdown(wait=False)
            except Exception as exc:
                log.debug("ControlFreq: operator roster executor shutdown failed: %s", exc)
        self._brief_cancel.set()
        self._brief_generation += 1
        self._brief_request = None
        self._guidance_save_request = None
        for name in ("_brief_cache", "_guidance_save_cache", "_activity_read_cache", "_operational_read_cache", "_readiness_read_cache"):
            cache = getattr(self, name, None)
            if cache is not None:
                cache.close()
        for name in ("_brief_poll", "_activity_read_poll", "_readiness_read_poll"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()
        self._shutdown_message_summary_executor()
        propagation_executor = self._propagation_executor
        self._propagation_executor = None
        self._propagation_pending = False
        self._propagation_followup = False
        self._propagation_request_id += 1
        if propagation_executor is not None:
            try:
                propagation_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                propagation_executor.shutdown(wait=False)
            except Exception as exc:
                log.debug("ControlFreq: propagation executor shutdown failed: %s", exc)
        shortwave_executor = self._shortwave_listening_outlook_executor
        self._shortwave_listening_outlook_executor = None
        self._shortwave_listening_outlook_provider = None
        self._shortwave_listening_outlook_pending = False
        self._shortwave_listening_outlook_followup = False
        self._shortwave_listening_outlook_request_id += 1
        self._shortwave_listening_dismiss_provider = None
        self._shortwave_listening_action_pending = False
        if shortwave_executor is not None:
            try:
                shortwave_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                shortwave_executor.shutdown(wait=False)
            except Exception as exc:
                log.debug("ControlFreq: Shortwave Listening executor shutdown failed: %s", exc)
        local_nets_executor = self._local_nets_outlook_executor
        self._local_nets_outlook_executor = None
        self._local_nets_outlook_provider = None
        self._local_nets_outlook_pending = False
        self._local_nets_outlook_followup = False
        self._local_nets_outlook_request_id += 1
        if local_nets_executor is not None:
            try:
                local_nets_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                local_nets_executor.shutdown(wait=False)
            except Exception as exc:
                log.debug("ControlFreq: Local Nets executor shutdown failed: %s", exc)
        executor = self._focus_executor
        self._focus_executor = None
        if executor is None:
            return
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)
        except Exception as exc:
            log.debug("ControlFreq: focus executor shutdown failed: %s", exc)

    def _schedule_focus_index_backfill(self) -> None:
        if self._focus_backfill_pending or self._focus_backfill_complete:
            return
        db_path = self._db_path()
        if not db_path.exists():
            self._focus_backfill_complete = True
            return
        self._focus_backfill_pending = True

        def _work() -> tuple[int, bool]:
            perf_meta: Dict[str, object] = {"budget_ms": 150, "batch_size": 25}
            with perf_span("controlfreq.focus_index_backfill", meta=perf_meta, min_ms=0.0):
                conn = connect_sqlite(db_path, timeout=1.5, busy_timeout_ms=1500)
                try:
                    indexed = 0
                    messages_complete = False
                    observations_complete = False
                    deadline = time.monotonic() + 0.15
                    while not (messages_complete and observations_complete) and time.monotonic() < deadline:
                        message_count, messages_complete = backfill_ops_focus_index(conn, batch_size=25)
                        observation_count, observations_complete = backfill_ops_focus_observation_index(
                            conn, batch_size=25
                        )
                        indexed += message_count + observation_count
                        if message_count + observation_count == 0:
                            break
                    conn.commit()
                    perf_meta["rows_indexed"] = indexed
                    perf_meta["complete"] = messages_complete and observations_complete
                    return indexed, messages_complete and observations_complete
                finally:
                    conn.close()

        future = self._ensure_focus_executor().submit(_work)

        def _done(done: Future) -> None:
            try:
                indexed, complete = done.result()
            except Exception as exc:
                log.debug("ControlFreq: focus index backfill failed: %s", exc)
                indexed, complete = 0, True
            try:
                self._focus_backfill_ready.emit(bool(complete), int(indexed))
            except RuntimeError:
                pass

        future.add_done_callback(_done)

    def _on_focus_backfill_ready(self, complete: bool, indexed: int) -> None:
        self._focus_backfill_pending = False
        self._focus_backfill_complete = bool(complete)
        if indexed and (self.search_edit.text() or "").strip():
            self._focus_autocomplete_timer.start(0)
        if not complete and self._active:
            QTimer.singleShot(750, self._schedule_focus_index_backfill)

    def _configured_focus_entities(self) -> tuple[tuple[str, str, str], ...]:
        entities: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for index in range(self.group_combo.count()):
            group = normalize_group_name(self.group_combo.itemData(index))
            if group and ("group", group) not in seen:
                seen.add(("group", group))
                entities.append(("group", group, group))
        for state in LOWER48_STATES:
            key = ("geography", f"state:{str(state).upper()}")
            if key not in seen:
                seen.add(key)
                entities.append((key[0], key[1], str(state).upper()))
        for region in FEMA_REGIONS:
            label = str(region).upper()
            key = ("geography", f"region:{label}")
            if key not in seen:
                seen.add(key)
                entities.append((key[0], key[1], label))
        for index in range(self.traffic_source_combo.count()):
            source = str(self.traffic_source_combo.itemData(index) or "").strip().lower()
            if not source:
                continue
            label = str(self.traffic_source_combo.itemText(index) or source).replace("Traffic Source:", "").strip()
            key = ("source", source)
            if key not in seen:
                seen.add(key)
                entities.append((key[0], key[1], label))
        return tuple(entities)

    def _on_focus_search_text_changed(self, text: str) -> None:
        self._update_clear_filters_style()
        query = str(text or "").strip()
        if not query:
            self._focus_completion_model.clear()
            self._focus_suggestions = ()
            return
        self._focus_autocomplete_timer.start(125)

    def _request_focus_suggestions(self) -> None:
        query = (self.search_edit.text() or "").strip()
        if not query:
            return
        self._focus_suggestion_request_id += 1
        request_id = self._focus_suggestion_request_id
        db_path = self._db_path()
        extras = self._configured_focus_entities()
        cache_key = (query.casefold(), extras, self._focus_db_checkpoint(db_path))
        self._bounded_request_key_put(self._focus_suggestion_cache_keys, request_id, cache_key)
        cached = self._focus_suggestion_cache.get(cache_key)
        if cached is not None:
            self._focus_suggestion_cache.move_to_end(cache_key)
            self._on_focus_suggestions_ready(request_id, cached, "")

        def _work() -> tuple[OpsFocusSuggestion, ...]:
            perf_meta: Dict[str, object] = {
                "cache": "refresh" if cached is not None else "miss",
                "query_chars": len(query),
            }
            with perf_span(
                "controlfreq.focus_autocomplete",
                meta=perf_meta,
                min_ms=0.0,
            ):
                conn = connect_sqlite(db_path, timeout=1.5, busy_timeout_ms=1500)
                try:
                    result = search_focus_suggestions(conn, query, limit=8, extra_entities=extras)
                    perf_meta["result_count"] = len(result)
                    return result
                finally:
                    conn.close()

        future = self._ensure_focus_executor().submit(_work)

        def _done(done: Future) -> None:
            try:
                suggestions: object = done.result()
                error = ""
            except Exception as exc:
                suggestions = ()
                error = str(exc)
            try:
                self._focus_suggestions_ready.emit(request_id, suggestions, error)
            except RuntimeError:
                pass

        future.add_done_callback(_done)

    def _focus_icon_path(self, kind: str) -> Path:
        icon_root = Path(__file__).resolve().parents[2] / "assets" / "icons"
        mapping = {
            "callsign": icon_root / "navigation" / "operators.svg",
            "group": icon_root / "navigation" / "operators.svg",
            "event": icon_root / "ops" / "event.svg",
            "topic": icon_root / "ops" / "topic.svg",
            "geography": icon_root / "navigation" / "map.svg",
            "band": icon_root / "ops" / "rf-readiness.svg",
            "source": icon_root / "ops" / "source.svg",
        }
        return mapping.get(str(kind or "").lower(), icon_root / "navigation" / "ops.svg")

    def _on_focus_suggestions_ready(self, request_id: int, payload: object, error: object) -> None:
        if request_id != self._focus_suggestion_request_id:
            with perf_span("controlfreq.focus_stale_result_drop", meta={"kind": "suggestions"}):
                pass
            return
        if error:
            log.debug("ControlFreq: focus suggestions failed: %s", error)
        suggestions = tuple(
            item for item in (payload if isinstance(payload, (tuple, list)) else ())
            if isinstance(item, OpsFocusSuggestion)
        )
        self._focus_suggestions = suggestions
        cache_key = self._focus_suggestion_cache_keys.get(request_id)
        if cache_key is not None and not error:
            self._bounded_cache_put(self._focus_suggestion_cache, cache_key, suggestions, limit=64)
        self._focus_completion_model.clear()
        grouped: OrderedDict[str, list[OpsFocusSuggestion]] = OrderedDict()
        for suggestion in suggestions:
            grouped.setdefault(suggestion.focus.kind, []).append(suggestion)
        for kind, kind_suggestions in grouped.items():
            heading = QStandardItem(f"{kind.title()} suggestions")
            heading.setEnabled(False)
            heading.setSelectable(False)
            heading.setData(f"{kind.title()} suggestions", Qt.AccessibleTextRole)
            self._focus_completion_model.appendRow(heading)
            for suggestion in kind_suggestions:
                label = suggestion.primary_text
                if suggestion.secondary_text:
                    label = f"{label}  ·  {suggestion.secondary_text}"
                item = QStandardItem(QIcon(str(self._focus_icon_path(suggestion.focus.kind))), label)
                item.setData(suggestion, Qt.UserRole)
                item.setData(label, Qt.AccessibleTextRole)
                item.setData(suggestion.secondary_text, Qt.AccessibleDescriptionRole)
                item.setToolTip(label)
                self._focus_completion_model.appendRow(item)
        if suggestions and self.search_edit.hasFocus():
            self._focus_completer.complete()
        if bool(getattr(self, "_focus_apply_when_ready", False)):
            self._focus_apply_when_ready = False
            if suggestions:
                self._apply_ops_focus(suggestions[0].focus)

    def _activate_focus_completion(self, index: QModelIndex) -> None:
        suggestion = index.data(Qt.UserRole)
        if isinstance(suggestion, OpsFocusSuggestion):
            self._apply_ops_focus(suggestion.focus)

    def _apply_best_focus_suggestion(self) -> None:
        popup = self._focus_completer.popup()
        if popup is not None and popup.currentIndex().isValid():
            suggestion = popup.currentIndex().data(Qt.UserRole)
            if isinstance(suggestion, OpsFocusSuggestion):
                self._apply_ops_focus(suggestion.focus)
                return
        query = (self.search_edit.text() or "").strip()
        for suggestion in self._focus_suggestions:
            if suggestion.focus.query_text.strip().lower() == query.lower():
                self._apply_ops_focus(suggestion.focus)
                return
        if query:
            self._focus_apply_when_ready = True
            self._request_focus_suggestions()

    def _apply_ops_focus(self, focus: OpsFocus) -> None:
        self._active_ops_focus = focus
        self._focus_apply_when_ready = False
        previous = self.search_edit.blockSignals(True)
        try:
            self.search_edit.setText(focus.display_label)
        finally:
            self.search_edit.blockSignals(previous)
        self.focus_banner.setVisible(True)
        self.focus_title_label.setText(f"Focused on {focus.display_label} · {focus.kind.title()}")
        self.focus_current_label.setText("Current Scope · checking…")
        self.focus_history_label.setText("Last Known · checking retained evidence…")
        icon = QIcon(str(self._focus_icon_path(focus.kind)))
        self.focus_icon_label.setPixmap(icon.pixmap(24, 24))
        self.focus_icon_label.setAccessibleName(f"{focus.kind.title()} focus")
        self.focus_pin_btn.setEnabled(focus.kind in {"callsign", "group", "topic"})
        self.focus_history_btn.setEnabled(True)
        self._set_focus_action_context(focus)
        self._retarget_propagation_for_focus(focus)
        self._update_applied_filters_label()
        self._on_filters_changed()

    def _clear_ops_focus(self, *, clear_search: bool = True, refresh: bool = True) -> None:
        self._active_ops_focus = None
        self._focus_snapshot_request_id += 1
        self.focus_banner.setVisible(False)
        self._operational_awareness_context = {}
        if clear_search:
            previous = self.search_edit.blockSignals(True)
            try:
                self.search_edit.clear()
            finally:
                self.search_edit.blockSignals(previous)
        self._update_applied_filters_label()
        self._update_clear_filters_style()
        if refresh:
            self._run_filter_refresh()

    def _request_focus_snapshot(self) -> None:
        focus = self._active_ops_focus
        if focus is None:
            return
        self._focus_snapshot_request_id += 1
        request_id = self._focus_snapshot_request_id
        db_path = self._db_path()
        age_seconds = self._traffic_age_seconds()
        group_filter = normalize_group_name(self.group_combo.currentData())
        source_filter = str(self.traffic_source_combo.currentData() or "").strip().lower()
        cache_key = (
            focus.kind,
            focus.canonical_id,
            focus.operator_id,
            age_seconds,
            group_filter,
            source_filter,
            self._focus_db_checkpoint(db_path),
            int(time.time() // 15),
        )
        self._bounded_request_key_put(self._focus_snapshot_cache_keys, request_id, cache_key)
        cached = self._focus_snapshot_cache.get(cache_key)
        if cached is not None:
            self._focus_snapshot_cache.move_to_end(cache_key)
            self._on_focus_snapshot_ready(request_id, cached, "")
            self.focus_history_label.setText(self.focus_history_label.text() + " · refreshing…")

        def _work() -> OpsFocusSnapshot:
            perf_meta: Dict[str, object] = {
                "kind": focus.kind,
                "cache": "refresh" if cached is not None else "miss",
            }
            with perf_span(
                "controlfreq.focus_snapshot_build",
                meta=perf_meta,
                min_ms=0.0,
            ):
                conn = connect_sqlite(db_path, timeout=1.5, busy_timeout_ms=1500)
                try:
                    result = build_focus_snapshot(
                        conn,
                        focus,
                        age_seconds=age_seconds,
                        group_filter=group_filter,
                        source_filter=source_filter,
                    )
                    perf_meta["current_rows"] = result.current_count
                    perf_meta["has_last_known"] = result.historical_summary is not None
                    return result
                finally:
                    conn.close()

        future = self._ensure_focus_executor().submit(_work)

        def _done(done: Future) -> None:
            try:
                snapshot: object = done.result()
                error = ""
            except Exception as exc:
                snapshot = None
                error = str(exc)
            try:
                self._focus_snapshot_ready.emit(request_id, snapshot, error)
            except RuntimeError:
                pass

        future.add_done_callback(_done)

    def _on_focus_snapshot_ready(self, request_id: int, payload: object, error: object) -> None:
        if request_id != self._focus_snapshot_request_id:
            with perf_span("controlfreq.focus_stale_result_drop", meta={"kind": "snapshot"}):
                pass
            return
        if error or not isinstance(payload, OpsFocusSnapshot):
            log.debug("ControlFreq: focus snapshot failed: %s", error)
            self.focus_current_label.setText("Current Scope · unavailable")
            self.focus_history_label.setText("Last Known · unavailable; other dashboard data remains active")
            return
        snapshot = payload
        cache_key = self._focus_snapshot_cache_keys.get(request_id)
        if cache_key is not None and not error:
            self._bounded_cache_put(self._focus_snapshot_cache, cache_key, snapshot, limit=32)
        apply_started = time.perf_counter()
        aliases = tuple(alias for alias in snapshot.aliases if alias != snapshot.focus.display_label)
        title = f"Focused on {snapshot.focus.display_label} · {snapshot.focus.kind.title()}"
        if aliases:
            title += f" · formerly {', '.join(aliases)}"
        self.focus_title_label.setText(title)
        counts = []
        if snapshot.current_unread:
            counts.append(f"{snapshot.current_unread} new")
        if snapshot.current_actionable:
            counts.append(f"{snapshot.current_actionable} actionable")
        self.focus_current_label.setText(
            "Current Scope · " + snapshot.current_scope_summary + (f" · {' · '.join(counts)}" if counts else "")
        )
        history_text = format_focus_last_known(snapshot.historical_summary)
        if snapshot.historical_summary and snapshot.historical_summary.latest_summary:
            history_text += f" · {snapshot.historical_summary.latest_summary}"
        if snapshot.historical_summary and snapshot.historical_summary.scope_mismatch_notes:
            history_text += " · outside selected " + ", ".join(snapshot.historical_summary.scope_mismatch_notes)
        self.focus_history_label.setText(history_text)
        self.focus_history_label.setToolTip(history_text)
        self._set_focus_action_context(snapshot.focus)
        from freqinout.core.perf_metrics import emit_span
        emit_span(
            "controlfreq.focus_ui_apply",
            (time.perf_counter() - apply_started) * 1000.0,
            meta={"kind": snapshot.focus.kind},
        )

    @staticmethod
    def _focus_db_checkpoint(db_path: Path) -> tuple[int, int]:
        try:
            stat = db_path.stat()
            return int(stat.st_mtime_ns), int(stat.st_size)
        except OSError:
            return (0, 0)

    @staticmethod
    def _bounded_cache_put(cache: OrderedDict, key: tuple[object, ...], value: object, *, limit: int) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > max(1, int(limit)):
            cache.popitem(last=False)

    @staticmethod
    def _bounded_request_key_put(
        cache: Dict[int, tuple[object, ...]], request_id: int, key: tuple[object, ...]
    ) -> None:
        cache[int(request_id)] = key
        while len(cache) > 128:
            cache.pop(next(iter(cache)))

    def _focus_context(self, focus: OpsFocus | None = None) -> Dict[str, str]:
        active = focus or self._active_ops_focus
        if active is None:
            return {}
        context: Dict[str, str] = {"search_query": active.display_label}
        if active.kind == "callsign":
            context["callsign"] = active.display_label
        elif active.kind == "group":
            context["group_filter"] = active.display_label
        elif active.kind in {"topic", "event"}:
            context["topic_filter"] = active.display_label
        elif active.kind == "source":
            context["source_family"] = active.canonical_id
        elif active.kind == "geography":
            prefix, _, value = active.canonical_id.partition(":")
            context["grid_filter" if prefix == "grid" else "state_filter"] = value or active.display_label
        return context

    def _set_focus_action_context(self, focus: OpsFocus) -> None:
        context = self._focus_context(focus)
        self._operational_awareness_context = dict(context)
        self._operational_activity_context = dict(context)

    def _open_focus_inbox(self) -> None:
        self._set_focus_action_context(self._active_ops_focus) if self._active_ops_focus else None
        self._open_operational_activity_messages()

    def _open_focus_map(self) -> None:
        self._set_focus_action_context(self._active_ops_focus) if self._active_ops_focus else None
        self._open_operational_activity_map()

    def _pin_active_focus(self) -> None:
        if self._active_ops_focus is None:
            return
        self._set_focus_action_context(self._active_ops_focus)
        self._pin_selected_awareness_focus()

    def _open_focus_history(self) -> None:
        focus = self._active_ops_focus
        if focus is None:
            return
        context = map_context_from_mapping(self._focus_context(focus))
        host = self.window()
        if hasattr(host, "open_messages_section"):
            kwargs = context.as_messages_kwargs()
            kwargs["age_filter_seconds"] = 0
            host.open_messages_section("inbox", **kwargs)

    def _retarget_propagation_for_focus(self, focus: OpsFocus) -> None:
        target_type = ""
        target_value = ""
        if focus.kind == "callsign":
            target_type, target_value = "OPERATOR", focus.display_label
        elif focus.kind == "geography":
            prefix, _, value = focus.canonical_id.partition(":")
            if prefix == "state":
                target_type, target_value = "STATE", value
            elif prefix == "region":
                target_type, target_value = "REGION", value
        if not target_type or not hasattr(self, "prop_target_type_combo"):
            return
        self._prop_target_syncing = True
        try:
            index = self.prop_target_type_combo.findData(target_type)
            if index >= 0:
                self.prop_target_type_combo.setCurrentIndex(index)
                self._set_prop_target_value_options(target_type, target_value)
        finally:
            self._prop_target_syncing = False
        try:
            self.settings.set_many({"prop_target_type": target_type, "prop_target_value": target_value})
        except Exception:
            pass
        QTimer.singleShot(0, self._refresh_propagation_snapshot)

    def _update_clear_filters_style(self) -> None:
        try:
            theme = self._theme()
            role = "eligible_warning" if self._filters_active() else "muted"
            self.clear_filters_btn.setStyleSheet(button_style(role, theme))
        except Exception:
            pass

    def _traffic_age_seconds(self) -> int:
        try:
            return max(0, int(self.traffic_age_combo.currentData() or 0))
        except Exception:
            return 24 * 60 * 60

    def _update_applied_filters_label(self) -> None:
        label = getattr(self, "applied_filters_label", None)
        if label is None:
            return
        group = str(self.group_combo.currentText() or "All groups").strip()
        source = str(self.traffic_source_combo.currentText() or "Traffic Source: All").strip()
        source = source.replace("Traffic Source:", "").strip() or "All sources"
        if source.lower() == "all":
            source = "All sources"
        age = str(self.traffic_age_combo.currentText() or "Traffic: 24h").replace("Traffic:", "").strip()
        focus = self._active_ops_focus
        focus_text = f" · Focus: {focus.display_label}" if focus is not None else ""
        label.setText(f"Showing traffic received in {age} · {group} · {source}{focus_text}")
        try:
            theme = self._theme()
            label.setStyleSheet(f"font-weight: 600; color: {theme.get('text_muted', '#5b6875')};")
        except Exception:
            pass

    def _on_traffic_source_filter_changed(self, *_args) -> None:
        self._source_family_filter = str(self.traffic_source_combo.currentData() or "").strip().lower()
        if hasattr(self, "source_lanes_table"):
            try:
                self.source_lanes_table.clearSelection()
            except Exception:
                pass
        self._operational_snapshot_cache_key = ()
        self._activity_cache_key = ()
        self._on_filters_changed()

    def _set_traffic_source_filter(self, source_family: str) -> None:
        normalized = self._normalize_traffic_source_filter(source_family)
        self._source_family_filter = normalized
        combo = getattr(self, "traffic_source_combo", None)
        if combo is None:
            return
        previous = combo.blockSignals(True)
        try:
            idx = combo.findData(normalized)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            combo.blockSignals(previous)

    @staticmethod
    def _normalize_traffic_source_filter(source_family: str) -> str:
        source = str(source_family or "").strip().lower()
        aliases = {
            "mesh": "meshcore",
            "meshcore": "meshcore",
            "local_mesh": "meshcore",
            "js8": "js8call",
            "js8call": "js8call",
            "js8spotter": "spotter",
            "fiospotter": "spotter",
            "spotter": "spotter",
            "commstat_rf": "commstat",
            "commstat": "commstat",
            "flmsg": "nbems",
            "flamp": "nbems",
            "fastlight": "nbems",
            "nbems": "nbems",
            "varac": "varac",
            "bbs": "bbs",
        }
        return aliases.get(source, source)

    @staticmethod
    def _traffic_source_query_families(source_family: str) -> Tuple[str, ...]:
        source = ControlFreqTab._normalize_traffic_source_filter(source_family)
        families = {
            "meshcore": ("MeshCore", "meshcore", "mesh"),
            "js8call": ("js8call", "js8", "JS8Call"),
            "spotter": ("spotter", "fiospotter", "js8spotter", "spotter_traffic"),
            "commstat": ("commstat", "commstat_rf", "CommStat"),
            "nbems": ("flmsg", "flamp", "nbems", "fastlight"),
            "varac": ("varac", "VarAC"),
            "bbs": ("bbs", "BBS"),
        }.get(source, ())
        deduped: List[str] = []
        for family in families:
            if family and family not in deduped:
                deduped.append(family)
        return tuple(deduped)

    def _refresh_all(
        self,
        include_secondary: bool = True,
        include_heavy: bool = True,
        include_status: bool = True,
    ) -> None:
        with perf_span("controlfreq.refresh_all", settings=self.settings, min_ms=10.0):
            if include_secondary or include_heavy:
                self._reload_settings_if_changed()
            self._refresh_frequency_control(include_intersections=False)
            if include_status:
                self._refresh_status_widgets()
            if include_secondary:
                self._load_group_combo()
                if self._dashboard_card_enabled("activity"):
                    self._refresh_activity()
                if self._dashboard_card_enabled("intersections"):
                    self._refresh_intersections()
                if self._dashboard_card_enabled("schedule"):
                    self._refresh_schedule_outlook()
                if self._dashboard_card_enabled("propagation"):
                    self._refresh_prop_target_controls()
                self._last_secondary_refresh_ts = time.time()
            if include_heavy:
                self._refresh_message_summary()
                if self._dashboard_card_enabled("propagation"):
                    self._refresh_propagation_snapshot()
                self._last_heavy_refresh_ts = time.time()
            self._last_refresh_ts = time.time()
            self._refresh_clock_display()
            self._refresh_awareness_now_next()
            self._update_clear_filters_style()

    def _refresh_status_widgets(self) -> None:
        self._refresh_running_status()
        self._refresh_scheduler_strip()

    def _on_dependency_status_snapshot_changed(self, _snapshot: object) -> None:
        if not self._active:
            return
        self._refresh_running_status()

    def _refresh_running_status(self) -> None:
        theme = self._theme()
        visible_keys = [key for key, _label in self._current_visible_status_items()]
        if visible_keys != list(self.status_labels.keys()):
            self._rebuild_status_indicators()
        snapshot = self._status_service.software_status_snapshot()
        for program_name, lbl in self.status_labels.items():
            info = snapshot.get(program_name, {})
            state = str(info.get("state", "idle"))
            base_tooltip = str(info.get("tooltip", "Not running"))
            checked_at_ts = float(info.get("checked_at") or 0.0)
            if checked_at_ts:
                checked_at = dt.datetime.fromtimestamp(checked_at_ts).strftime("%H:%M:%S")
            else:
                checked_at = "pending"
            configured = ""
            key = PROGRAM_PATH_KEYS.get(program_name)
            if key:
                configured = str(self.settings.get(key, "") or "").strip()
            lines = [f"{program_name}: {base_tooltip}", f"Last check: {checked_at}"]
            if configured:
                lines.append(f"Configured: {configured}")
            lbl.setStyleSheet(led_style(state, theme))
            lbl.setToolTip("\n".join(lines))
            self._status_checked_at[program_name] = checked_at
        self._update_readiness_review_banner()

    def _refresh_scheduler_strip(self, status: Optional[Dict[str, Any]] = None) -> None:
        try:
            theme = self._theme()
            muted = str(theme.get("text_muted", "#888"))
            sched = getattr(self.window(), "scheduler", None)
            if not sched or not hasattr(sched, "get_status_summary"):
                self._set_frequency_state_badge("unknown")
                self._apply_frequency_action_busy_override(None)
                self.next_change_label.setText("Next Change: --")
                self.effective_source_label.setText("Active Source: --")
                self.effective_source_label.setStyleSheet(f"color: {muted};")
                return
            if not isinstance(status, dict):
                status = sched.get_status_summary()
            source = str(status.get("source") or "").strip().upper()
            net_kind = str(status.get("net_kind") or "").strip()
            source_reason_detail = str(status.get("source_reason_detail") or "").strip()
            sop_contention = bool(status.get("sop_contention"))
            sop_profiles = [str(x).strip() for x in (status.get("sop_contention_profiles") or []) if str(x).strip()]
            sop_selected = str(status.get("sop_selected_profile") or "").strip()
            next_source = str(status.get("next_source") or "").strip().upper()
            next_net_kind = str(status.get("next_net_kind") or "").strip()
            next_source_change = bool(status.get("next_source_change"))
            next_transition_note = str(status.get("next_transition_note") or "").strip()
            source_text = "Active Source: --"
            if source == "SOP":
                source_text = f"Active Source: {net_kind or 'SOP Layer'}"
                tip = "SOP Layer currently overrides the baseline HF schedule."
                if sop_contention:
                    others = [p for p in sop_profiles if p and p != sop_selected]
                    if others:
                        source_text += " (Contention)"
                        tip = f"SOP contention: selected {sop_selected or 'winner'} over {', '.join(others[:4])}."
                    else:
                        source_text += " (Contention)"
                        tip = "SOP contention detected across active profiles."
                if source_reason_detail:
                    tip = f"{tip}\nSelection: {source_reason_detail}"
                self.effective_source_label.setStyleSheet(
                    f"font-weight: 600; color: {theme.get('warning', '#C99700')};"
                )
                self.effective_source_label.setToolTip(tip)
            elif source == "NET":
                source_text = f"Active Source: {net_kind or 'Net Schedule'}"
                self.effective_source_label.setStyleSheet(
                    f"font-weight: 600; color: {theme.get('info', '#1E88E5')};"
                )
                tip = "Active net schedule has highest precedence."
                if source_reason_detail:
                    tip = f"{tip}\nSelection: {source_reason_detail}"
                self.effective_source_label.setToolTip(tip)
            elif source == "HF":
                source_text = f"Active Source: {net_kind or 'HF Schedule'}"
                self.effective_source_label.setStyleSheet(f"color: {theme.get('text', '#111')};")
                tip = "Baseline HF schedule is active."
                if source_reason_detail:
                    tip = f"{tip}\nSelection: {source_reason_detail}"
                self.effective_source_label.setToolTip(tip)
            else:
                self.effective_source_label.setStyleSheet(f"color: {muted};")
                self.effective_source_label.setToolTip("")
            self.effective_source_label.setText(source_text)
            off_schedule = bool(status.get("off_schedule"))
            off_flags = status.get("off_schedule_flags")
            if not isinstance(off_flags, dict):
                off_flags = {}
            fldigi_net_active = source == "NET" and "FLDIGI" in (net_kind or "").upper()
            mode_only_net_change = (
                off_schedule
                and fldigi_net_active
                and bool(status.get("fldigi_mode_off") or off_flags.get("mode"))
                and not any(
                    bool(off_flags.get(flag))
                    for flag in ("frequency", "offset", "fldigi_offset", "vfo")
                )
            )
            badge_state = "net_mode" if mode_only_net_change else ("off" if off_schedule else "on")
            self._set_frequency_state_badge(badge_state)
            if mode_only_net_change:
                self.freq_state_badge.setToolTip(
                    "The FLDigi net is still on the scheduled frequency. The operator changed mode during the net."
                )
            else:
                self.freq_state_badge.setToolTip("")
            self._apply_frequency_action_busy_override(self._frequency_action_busy_reason(status))

            next_change = getattr(sched, "next_change_utc", None)
            fallback_preview = self._next_schedule_outlook_preview if isinstance(self._next_schedule_outlook_preview, dict) else None
            fallback_freq = None
            if isinstance(fallback_preview, dict):
                fallback_freq = fallback_preview.get("freq_mhz")
                if not isinstance(fallback_freq, (int, float)):
                    fallback_freq = None
            next_text = "Next Change: --"
            next_entry_start = status.get("next_entry_start_utc")
            next_entry_freq = status.get("next_entry_frequency_mhz")
            next_entry_source = str(status.get("next_entry_source") or "").strip().upper()
            schedule_gap_seconds = status.get("schedule_gap_seconds")
            if not isinstance(next_change, dt.datetime) and isinstance(fallback_preview, dict):
                preview_when = fallback_preview.get("when_utc")
                if isinstance(preview_when, dt.datetime):
                    next_change = preview_when
            if isinstance(next_change, dt.datetime):
                if next_change.tzinfo is None:
                    next_change = next_change.replace(tzinfo=dt.timezone.utc)
                else:
                    next_change = next_change.astimezone(dt.timezone.utc)
                now_utc = dt.datetime.now(dt.timezone.utc)
                mins = int(max(0.0, (next_change - now_utc).total_seconds()) // 60)
                display_dt = next_change.astimezone(self._get_display_tz()) if self._show_local else next_change
                next_freq = status.get("next_frequency_mhz")
                if not isinstance(next_freq, (int, float)) and isinstance(fallback_freq, (int, float)):
                    next_freq = float(fallback_freq)
                if isinstance(next_freq, (int, float)):
                    freq_txt = f"{float(next_freq):.3f}"
                else:
                    sched_freq = current_scheduler_freq(self.window())
                    freq_txt = f"{sched_freq:.3f}" if isinstance(sched_freq, (int, float)) else "--"
                next_text = f"Next Change: {freq_txt} {display_dt:%H:%M}"
                if next_source_change and next_source and next_source != source:
                    next_label = next_net_kind or next_source
                    next_text = f"{next_text} -> {next_label}"
                if next_transition_note:
                    self.next_change_label.setToolTip(next_transition_note)
                elif next_source_change and next_source and next_source != source:
                    next_label = next_net_kind or next_source
                    self.next_change_label.setToolTip(f"Next source transition: {source} -> {next_label}.")
                elif isinstance(fallback_preview, dict) and next_change == fallback_preview.get("when_utc"):
                    preview_type = str(fallback_preview.get("type") or "").strip().upper()
                    preview_group = str(fallback_preview.get("group") or "").strip()
                    note_parts = ["Using Schedule Outlook fallback"]
                    if preview_type:
                        note_parts.append(preview_type)
                    if preview_group:
                        note_parts.append(preview_group)
                    self.next_change_label.setToolTip(" | ".join(note_parts))
                else:
                    self.next_change_label.setToolTip("")
                if mins <= 15:
                    self.next_change_label.setStyleSheet(label_style("danger", self._theme(), weight=600))
                elif mins <= 60:
                    self.next_change_label.setStyleSheet(label_style("warning", self._theme(), weight=500))
                else:
                    self.next_change_label.setStyleSheet(label_style("muted", self._theme()))
                if (
                    isinstance(schedule_gap_seconds, (int, float))
                    and schedule_gap_seconds > 60
                    and isinstance(next_entry_start, dt.datetime)
                    and isinstance(next_entry_freq, (int, float))
                ):
                    start_dt = next_entry_start
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=dt.timezone.utc)
                    else:
                        start_dt = start_dt.astimezone(dt.timezone.utc)
                    start_display = start_dt.astimezone(self._get_display_tz()) if self._show_local else start_dt
                    next_text = f"Current Ends: {display_dt:%H:%M} | Next: {float(next_entry_freq):.3f} {start_display:%H:%M}"
                    if next_entry_source and next_entry_source != "NONE":
                        next_text += f" {next_entry_source}"
                    self.next_change_label.setToolTip(
                        f"Schedule gap: the active entry ends at {display_dt:%H:%M}; "
                        f"the next planned entry starts at {start_display:%H:%M}."
                    )
            else:
                if isinstance(next_entry_start, dt.datetime) and isinstance(next_entry_freq, (int, float)):
                    start_dt = next_entry_start
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=dt.timezone.utc)
                    else:
                        start_dt = start_dt.astimezone(dt.timezone.utc)
                    now_utc = dt.datetime.now(dt.timezone.utc)
                    mins = int(max(0.0, (start_dt - now_utc).total_seconds()) // 60)
                    start_display = start_dt.astimezone(self._get_display_tz()) if self._show_local else start_dt
                    next_text = f"Next Schedule: {float(next_entry_freq):.3f} {start_display:%H:%M}"
                    if next_entry_source and next_entry_source != "NONE":
                        next_text += f" {next_entry_source}"
                    self.next_change_label.setToolTip("No schedule entry is active now; this is the next planned entry.")
                    if mins <= 15:
                        self.next_change_label.setStyleSheet(label_style("danger", self._theme(), weight=600))
                    elif mins <= 60:
                        self.next_change_label.setStyleSheet(label_style("warning", self._theme(), weight=500))
                    else:
                        self.next_change_label.setStyleSheet(label_style("muted", self._theme()))
                else:
                    self.next_change_label.setStyleSheet(label_style("muted", self._theme()))
                    self.next_change_label.setToolTip("")
            self.next_change_label.setText(next_text)
            self._sync_frequency_info_row_heights()
            self._refresh_awareness_now_next()
        except Exception as e:
            log.debug("ControlFreq: failed scheduler strip refresh: %s", e)

    @staticmethod
    def _frequency_action_busy_reason(status: Dict[str, Any]) -> Optional[str]:
        # Stable precedence keeps the button label from flickering between sources.
        try:
            if bool(status.get("ptt_active")):
                return "PTT active"
            if bool(status.get("shared_ptt_blocked")):
                owner = str(status.get("shared_ptt_owner_name") or "").strip()
                group = str(status.get("shared_ptt_group") or "").strip()
                if owner and group:
                    return f"Shared PTT ({group}: {owner})"
                if group:
                    return f"Shared PTT ({group})"
                return "Shared PTT"
            if bool(status.get("js8_busy")):
                return "JS8Call"
            if bool(status.get("varac_waiting")) or bool(status.get("varac_busy")):
                return "VarAC"
            if bool(status.get("fldigi_busy")):
                return "FLDigi"
        except Exception:
            return None
        return None

    def _apply_frequency_action_busy_override(self, reason_label: Optional[str]) -> None:
        reason = str(reason_label or "").strip()
        prev_reason = str(self._freq_action_busy_reason_label or "").strip()
        if not reason:
            self._freq_action_busy_reason_label = None
            if prev_reason:
                self._update_frequency_action_styles()
            return
        self._freq_action_busy_reason_label = reason
        if str(self._primary_freq_action_mode or "").strip().lower() == "resume":
            self.freq_action_btn.setToolTip(
                f"Resume Schedule (Ctrl+Shift+R). Traffic appears busy ({reason}), but resume is an operator override."
            )
            return
        self.freq_action_btn.setText(f"Busy: {reason}")
        self.freq_action_btn.setToolTip(
            f"Busy: {reason}. Frequency changes are blocked while traffic or PTT is active."
        )
        self.freq_action_btn.setEnabled(False)
        try:
            theme = self._theme()
        except Exception:
            theme = None
        if theme:
            self.freq_action_btn.setStyleSheet(button_style("warning", theme))

    @staticmethod
    def _normalize_state_abbr(value: str) -> str:
        state = (value or "").strip().upper()
        if not state:
            return ""
        if len(state) <= 2:
            return state
        return US_STATE_ABBR_FROM_NAME.get(state, "")

    def _load_prop_operator_geo(self) -> Dict[str, Dict[str, str]]:
        snapshot = getattr(self, "_operator_roster_snapshot", None)
        if snapshot is not None:
            self._ensure_operator_roster_read()
            return {key: dict(value) for key, value in snapshot.geo.items()}
        if hasattr(self, "_operator_roster_generation"):
            self._ensure_operator_roster_read()
            return {}
        db_path = self._db_path()
        out: Dict[str, Dict[str, str]] = {}
        if not db_path.exists():
            return out
        try:
            rows = fetch_all(
                db_path,
                """
                SELECT
                    IFNULL(callsign,''),
                    IFNULL(state,''),
                    IFNULL(grid,'')
                FROM operator_checkins
                """,
                timeout=1.5,
                row_factory=sqlite3.Row,
                span_name="controlfreq.load_prop_operator_geo",
            )
            for row in rows:
                callsign = row[0]
                state = row[1]
                grid = row[2]
                cs = (callsign or "").strip().upper()
                if not cs:
                    continue
                out[cs] = {
                    "state": self._normalize_state_abbr(state),
                    "grid": (grid or "").strip().upper(),
                }
        except Exception as e:
            log.debug("ControlFreq: failed to load propagation target operators: %s", e)
        return out

    def _set_prop_target_value_options(self, target_type: str, selected_value: str) -> None:
        target_type = (target_type or "REGION").strip().upper()
        selected_value = (selected_value or "").strip().upper()
        if target_type == "REGION" and selected_value == "NATIONAL":
            selected_value = "ALL"
        options: List[str] = []
        if target_type == "STATE":
            options = [s for s in LOWER48_STATES if s in STATE_CENTERS]
        elif target_type == "OPERATOR":
            options = sorted(self._prop_operator_geo.keys())
        else:
            options = ["ALL"] + sorted(FEMA_REGIONS.keys())
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
        elif target_type == "STATE":
            self.prop_target_value_combo.setCurrentIndex(-1)
            self.prop_target_value_combo.setEditText("")
        elif self.prop_target_value_combo.count() > 0:
            self.prop_target_value_combo.setCurrentIndex(0)
        else:
            self.prop_target_value_combo.setEditText("")
        self.prop_target_value_combo.setEditable(True)
        if self.prop_target_value_combo.lineEdit():
            self.prop_target_value_combo.lineEdit().setPlaceholderText("Type to search...")
        self.prop_target_value_combo.blockSignals(False)

    def _refresh_prop_target_controls(self) -> None:
        self._prop_target_syncing = True
        try:
            self._ensure_operator_roster_read()
            self._prop_operator_geo = self._load_prop_operator_geo()
            roster = getattr(self, "_operator_roster_snapshot", None)
            roster_status = (
                "Operator location roster is unavailable; propagation will use the configured fallback region."
                if roster is None and getattr(self, "_operator_roster_error", "")
                else "Checking operator location roster; propagation will use the configured fallback region."
                if roster is None
                else "Using the last published operator location roster while it refreshes."
                if time.monotonic() - float(self._operator_roster_published_at) >= self._operator_roster_ttl_sec
                else "Operator location roster published."
            )
            self.prop_target_value_combo.setToolTip(roster_status)
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
            if existing_type != target_type or existing_value != current_value:
                self.settings.set_many(
                    {
                        "prop_target_type": target_type,
                        "prop_target_value": current_value,
                    }
                )
        except Exception as e:
            log.debug("ControlFreq: failed to refresh propagation target controls: %s", e)
        finally:
            self._prop_target_syncing = False

    def _on_prop_target_type_changed(self, _idx: int) -> None:
        if self._prop_target_syncing:
            return
        target_type = (self.prop_target_type_combo.currentData() or "REGION").strip().upper()
        self._prop_target_syncing = True
        try:
            self._set_prop_target_value_options(target_type, "")
            value = (self.prop_target_value_combo.currentText() or "").strip().upper()
            self.settings.set_many(
                {
                    "prop_target_type": target_type,
                    "prop_target_value": value,
                }
            )
        except Exception as e:
            log.debug("ControlFreq: propagation target type change failed: %s", e)
        finally:
            self._prop_target_syncing = False
        self._refresh_propagation_snapshot()

    def _on_prop_target_value_changed(self, text: str) -> None:
        if self._prop_target_syncing:
            return
        target_type = (self.prop_target_type_combo.currentData() or "REGION").strip().upper()
        value = (text or "").strip().upper()
        if target_type == "REGION" and value == "NATIONAL":
            value = "ALL"
        if target_type == "STATE":
            value = self._normalize_state_abbr(value)
        if target_type in {"REGION", "STATE"} and value:
            idx = self.prop_target_value_combo.findText(value, Qt.MatchFixedString)
            if idx < 0:
                return
        try:
            self.settings.set_many(
                {
                    "prop_target_type": target_type,
                    "prop_target_value": value,
                }
            )
        except Exception as e:
            log.debug("ControlFreq: propagation target value change failed: %s", e)
        self._refresh_propagation_snapshot()

    def _db_path(self) -> Path:
        return get_config_dir() / "config" / "freqinout_nets.db"

    def _settings_db_path(self) -> Path:
        return get_config_dir() / "config" / "freqinout.db"

    def _reload_settings_if_changed(self) -> None:
        try:
            db_path = Path(getattr(self.settings, "db_path", self._settings_db_path()))
            mtime_ns = int(db_path.stat().st_mtime_ns) if db_path.exists() else 0
            if mtime_ns and mtime_ns == int(self._settings_reload_mtime_ns or 0):
                return
            self.settings.reload()
            self._settings_reload_mtime_ns = mtime_ns
            self._invalidate_theme_cache()
        except Exception:
            pass

    @staticmethod
    def _safe_db_mtime(db_path: Path) -> float:
        try:
            return float(db_path.stat().st_mtime) if db_path.exists() else 0.0
        except Exception:
            return 0.0

    def _load_schedule_rows(self, db_path: Path, table_name: str) -> List[Dict[str, Any]]:
        if not db_path.exists():
            return []
        conn = None
        try:
            conn = connect_sqlite(db_path, timeout=1.5, row_factory=sqlite3.Row, busy_timeout_ms=1500)
            cur = conn.cursor()
            if not table_exists(conn, table_name):
                return []
            cur.execute(f"SELECT * FROM {table_name}")
            return rows_to_dicts(cur.fetchall())
        except Exception as e:
            log.debug("ControlFreq: failed to load %s from %s: %s", table_name, db_path, e)
            return []
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def _invalidate_schedule_row_caches(self) -> None:
        self._daily_schedule_rows_cache = []
        self._daily_schedule_rows_cache_ts = 0.0
        self._daily_schedule_rows_cache_mtime = 0.0
        self._net_schedule_rows_cache = []
        self._net_schedule_rows_cache_ts = 0.0
        self._net_schedule_rows_cache_mtime = 0.0

    def _primary_schedule_target_context(self) -> Tuple[Optional[int], Optional[int]]:
        win = self.window()
        manager = getattr(win, "station_runtime_manager", None) if win is not None else None
        if manager is not None:
            try:
                runtime = manager.get_primary_runtime() if hasattr(manager, "get_primary_runtime") else None
            except Exception:
                runtime = None
            if runtime is not None:
                try:
                    profile = runtime.profile if isinstance(runtime.profile, dict) else {}
                    assignment = runtime.assignment if isinstance(runtime.assignment, dict) else {}
                    device_profile_id = int(profile.get("id", 0) or 0)
                    operating_profile_id = assignment.get("operating_profile_id")
                    return (
                        device_profile_id or None,
                        int(operating_profile_id) if operating_profile_id not in (None, "") else None,
                    )
                except Exception:
                    pass
        try:
            store = MultiRadioStore(settings_db_path())
            primary = store.get_runtime_primary_device_profile()
            if not primary:
                return None, None
            device_profile_id = int(primary.get("id", 0) or 0)
            assignment = store.get_effective_assignment_for_device(device_profile_id)
            operating_profile_id = assignment.get("operating_profile_id") if assignment else None
            return (
                device_profile_id or None,
                int(operating_profile_id) if operating_profile_id not in (None, "") else None,
            )
        except Exception:
            return None, None

    def _filter_schedule_rows_for_runtime_target(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        device_profile_id, operating_profile_id = self._primary_schedule_target_context()
        filtered: List[Dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            row = normalize_schedule_target_fields(raw)
            if schedule_row_matches_target_context(
                row,
                device_profile_id=device_profile_id,
                operating_profile_id=operating_profile_id,
            ):
                filtered.append(row)
        return filtered

    def _daily_schedule_rows(self) -> List[Dict[str, Any]]:
        db_path = self._settings_db_path()
        now_ts = time.time()
        db_mtime = self._safe_db_mtime(db_path)
        if (
            self._daily_schedule_rows_cache
            and (now_ts - float(self._daily_schedule_rows_cache_ts) < self._daily_schedule_rows_cache_ttl_sec)
            and abs(float(self._daily_schedule_rows_cache_mtime) - db_mtime) < 0.0001
        ):
            return self._filter_schedule_rows_for_runtime_target(self._daily_schedule_rows_cache)
        rows = self._load_schedule_rows(db_path, "daily_schedule_tab")
        self._daily_schedule_rows_cache = rows
        self._daily_schedule_rows_cache_ts = now_ts
        self._daily_schedule_rows_cache_mtime = db_mtime
        return self._filter_schedule_rows_for_runtime_target(rows)

    def _net_schedule_rows(self) -> List[Dict[str, Any]]:
        db_path = self._db_path()
        now_ts = time.time()
        db_mtime = self._safe_db_mtime(db_path)
        if (
            self._net_schedule_rows_cache
            and (now_ts - float(self._net_schedule_rows_cache_ts) < self._net_schedule_rows_cache_ttl_sec)
            and abs(float(self._net_schedule_rows_cache_mtime) - db_mtime) < 0.0001
        ):
            return self._filter_schedule_rows_for_runtime_target(self._net_schedule_rows_cache)
        rows = self._load_schedule_rows(db_path, "net_schedule_tab")
        self._net_schedule_rows_cache = rows
        self._net_schedule_rows_cache_ts = now_ts
        self._net_schedule_rows_cache_mtime = db_mtime
        return self._filter_schedule_rows_for_runtime_target(rows)

    def _peer_schedule_rows(self) -> List[Dict[str, Any]]:
        snapshot = getattr(self, "_operator_roster_snapshot", None)
        if snapshot is not None:
            self._ensure_operator_roster_read()
            return [dict(row) for row in snapshot.peer_schedule_rows]
        if hasattr(self, "_operator_roster_generation"):
            self._ensure_operator_roster_read()
            return []
        db_path = self._db_path()
        now_ts = time.time()
        db_mtime = self._safe_db_mtime(db_path)
        if (
            self._peer_schedule_rows_cache
            and (now_ts - float(self._peer_schedule_rows_cache_ts) < self._peer_schedule_rows_cache_ttl_sec)
            and abs(float(self._peer_schedule_rows_cache_mtime) - db_mtime) < 0.0001
        ):
            return self._peer_schedule_rows_cache
        if not db_path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        conn = None
        try:
            conn = connect_sqlite(db_path, timeout=1.5, row_factory=sqlite3.Row, busy_timeout_ms=1500)
            has_effective_view = table_exists(conn, "peer_hf_schedule_effective")
            if has_effective_view:
                query = """
                    SELECT owner_callsign, day_utc, start_utc, end_utc, band, frequency
                    FROM peer_hf_schedule_effective
                """
            else:
                query = """
                    SELECT owner_callsign, day_utc, start_utc, end_utc, band, frequency
                    FROM peer_hf_schedule
                """
            rows = rows_to_dicts(conn.execute(query).fetchall())
        except Exception as e:
            log.debug("ControlFreq: failed to load peer schedule rows: %s", e)
            rows = []
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        self._peer_schedule_rows_cache = rows
        self._peer_schedule_rows_cache_ts = now_ts
        self._peer_schedule_rows_cache_mtime = db_mtime
        return rows

    def _load_group_combo(self) -> None:
        groups = self._get_operating_groups()
        current = self.group_combo.currentData()
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem("All Groups", "")
        for g in groups:
            self.group_combo.addItem(g, g)
        # restore
        idx = self.group_combo.findData(current)
        if idx < 0 and self._pending_group_filter:
            idx = self.group_combo.findData(self._pending_group_filter)
        if idx >= 0:
            self.group_combo.setCurrentIndex(idx)
            self._pending_group_filter = ""
        self.group_combo.blockSignals(False)

    def _get_operating_groups(self) -> List[str]:
        ops = self.settings.get("operating_groups", []) or []
        out: List[str] = []
        for row in ops:
            grp = normalize_group_name(row.get("group", ""))
            if grp and grp not in out:
                out.append(grp)
        return sorted(out)

    def _group_freq_map(self) -> Dict[str, List[float]]:
        ops = self.settings.get("operating_groups", []) or []
        out: Dict[str, List[float]] = {}
        for row in ops:
            grp = normalize_group_name(row.get("group", ""))
            if not grp:
                continue
            try:
                freq = float(row.get("frequency"))
            except Exception:
                continue
            out.setdefault(grp, []).append(freq)
        return out

    def _group_band_map(self) -> Dict[str, Set[str]]:
        ops = self.settings.get("operating_groups", []) or []
        out: Dict[str, Set[str]] = {}
        for row in ops:
            grp = normalize_group_name(row.get("group", ""))
            band = str(row.get("band", "") or "").strip().upper()
            if not grp or not band:
                continue
            out.setdefault(grp, set()).add(band)
        return out

    def _ensure_operator_roster_executor(self) -> ThreadPoolExecutor:
        if self._operator_roster_executor is None:
            self._operator_roster_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="fio-ops-roster"
            )
        return self._operator_roster_executor

    def _ensure_operator_roster_read(self, *, force: bool = False) -> None:
        """Start one detached roster/schedule read when the active UI needs it."""
        if getattr(self, "_closing", False):
            return
        now = time.monotonic()
        snapshot = getattr(self, "_operator_roster_snapshot", None)
        stale = snapshot is None or now - float(getattr(self, "_operator_roster_published_at", 0.0)) >= float(
            getattr(self, "_operator_roster_ttl_sec", 60.0)
        )
        if not force and not stale:
            return
        if not bool(getattr(self, "_active", False)):
            return
        if getattr(self, "_operator_roster_future", None) is not None:
            if force:
                self._operator_roster_pending = True
            return
        if now < float(getattr(self, "_operator_roster_retry_after", 0.0)) and not force:
            return
        self._operator_roster_generation += 1
        generation = self._operator_roster_generation
        worker = _OperatorRosterReadWorker(
            generation,
            settings_db_path(),
            self._db_path(),
        )
        self._operator_roster_pending = False
        self._operator_roster_inflight_generation = generation
        future = self._ensure_operator_roster_executor().submit(worker.run)
        self._operator_roster_future = future

        def _done(done: Future) -> None:
            try:
                result = done.result()
            except Exception as exc:
                result = {"generation": generation, "ok": False, "snapshot": None, "error": str(exc)}
            try:
                self._operator_roster_ready.emit(result)
            except RuntimeError:
                pass

        future.add_done_callback(_done)

    def _invalidate_operator_roster_cache(self) -> None:
        self._operator_roster_generation += 1
        self._operator_roster_published_at = 0.0
        if self._operator_roster_future is not None:
            self._operator_roster_pending = True
        self._ensure_operator_roster_read(force=True)

    def on_operator_roster_changed(self) -> None:
        """Invalidate the published roster after a committed roster edit."""
        self._invalidate_operator_roster_cache()

    def _on_operator_roster_ready(self, result: object) -> None:
        if getattr(self, "_closing", False):
            return
        payload = dict(result or {})
        generation = int(payload.get("generation") or 0)
        if generation != self._operator_roster_inflight_generation:
            # A delayed duplicate cannot clear or schedule over a newer future.
            return
        self._operator_roster_future = None
        self._operator_roster_inflight_generation = None
        if generation != self._operator_roster_generation:
            pending = bool(self._operator_roster_pending)
            self._operator_roster_pending = False
            if pending and self._active:
                self._ensure_operator_roster_read(force=True)
            return
        if generation == self._operator_roster_generation and payload.get("ok"):
            snapshot = payload.get("snapshot")
            if isinstance(snapshot, _OperatorRosterSnapshot):
                # Publish only complete detached values; pending/error never erases
                # the last usable snapshot.
                self._operator_roster_snapshot = snapshot
                self._operator_roster_published_at = time.monotonic()
                self._operator_roster_error = ""
                self._operator_roster_retry_after = 0.0
                self._operator_groups_cache = {key: set(value) for key, value in snapshot.group_map.items()}
                self._operator_peer_meta_cache = {
                    key: {**value, "groups": set(value.get("groups") or ())}
                    for key, value in snapshot.peer_meta.items()
                }
                self._prop_operator_geo = {key: dict(value) for key, value in snapshot.geo.items()}
                self._peer_schedule_rows_cache = [dict(row) for row in snapshot.peer_schedule_rows]
                if self._active:
                    if self._dashboard_card_enabled("intersections"):
                        self._refresh_intersections()
                    if self._dashboard_card_enabled("propagation"):
                        self._refresh_prop_target_controls()
                    self._refresh_activity()
        elif generation == self._operator_roster_generation:
            self._operator_roster_error = str(payload.get("error") or "roster data unavailable")
            self._operator_roster_retry_after = time.monotonic() + 10.0
            log.debug("ControlFreq: operator roster read failed: %s", self._operator_roster_error)
            if self._active and self._dashboard_card_enabled("intersections"):
                self._refresh_intersections()
        pending = bool(self._operator_roster_pending)
        self._operator_roster_pending = False
        if pending and self._active and not getattr(self, "_closing", False):
            self._ensure_operator_roster_read(force=True)

    def _load_operator_group_map(self) -> Dict[str, Set[str]]:
        snapshot = getattr(self, "_operator_roster_snapshot", None)
        if snapshot is not None:
            self._ensure_operator_roster_read()
            return {key: set(value) for key, value in snapshot.group_map.items()}
        if hasattr(self, "_operator_roster_generation"):
            self._ensure_operator_roster_read()
            return {}
        db_path = self._db_path()
        now_ts = time.time()
        try:
            db_mtime = float(db_path.stat().st_mtime) if db_path.exists() else 0.0
        except Exception:
            db_mtime = 0.0
        if (
            self._operator_groups_cache
            and (now_ts - float(self._operator_groups_cache_ts) < self._operator_groups_cache_ttl_sec)
            and abs(float(self._operator_groups_cache_mtime) - db_mtime) < 0.0001
        ):
            return self._operator_groups_cache
        mapping: Dict[str, Set[str]] = {}
        if not db_path.exists():
            return mapping
        try:
            rows = fetch_all(
                db_path,
                "SELECT callsign, group1, group2, group3, groups_json FROM operator_checkins",
                timeout=1.5,
                row_factory=sqlite3.Row,
                span_name="controlfreq.load_operator_group_map",
            )
        except Exception as e:
            log.debug("ControlFreq: failed to load operator groups: %s", e)
            return mapping
        for r in rows:
            cs = (r["callsign"] or "").strip().upper()
            if not cs:
                continue
            groups: Set[str] = set()
            for key in ("group1", "group2", "group3"):
                g = normalize_group_name(r[key])
                if g:
                    groups.add(g)
            try:
                if r["groups_json"]:
                    gj = json.loads(r["groups_json"])
                    for g in gj or []:
                        g = normalize_group_name(g)
                        if g:
                            groups.add(g)
            except Exception:
                pass
            if groups:
                mapping[cs] = groups
        self._operator_groups_cache = mapping
        self._operator_groups_cache_ts = now_ts
        self._operator_groups_cache_mtime = db_mtime
        return mapping

    def _load_operator_peer_meta(self) -> Dict[str, Dict[str, object]]:
        """Load the compact roster fields used by peer filters and row labels."""
        snapshot = getattr(self, "_operator_roster_snapshot", None)
        if snapshot is not None:
            self._ensure_operator_roster_read()
            return {
                key: {**value, "groups": set(value.get("groups") or ())}
                for key, value in snapshot.peer_meta.items()
            }
        if hasattr(self, "_operator_roster_generation"):
            self._ensure_operator_roster_read()
            return {}
        db_path = self._db_path()
        now_ts = time.time()
        db_mtime = self._safe_db_mtime(db_path)
        if (
            self._operator_peer_meta_cache
            and now_ts - float(self._operator_peer_meta_cache_ts) < self._operator_groups_cache_ttl_sec
            and abs(float(self._operator_peer_meta_cache_mtime) - db_mtime) < 0.0001
        ):
            return self._operator_peer_meta_cache
        mapping: Dict[str, Dict[str, object]] = {}
        if not db_path.exists():
            return mapping
        try:
            rows = fetch_all(
                db_path,
                """
                SELECT callsign, group1, group2, group3, groups_json,
                       group_role, roster_region, state
                  FROM operator_checkins
                """,
                timeout=1.5,
                row_factory=sqlite3.Row,
                span_name="controlfreq.load_operator_peer_meta",
            )
        except Exception as e:
            log.debug("ControlFreq: failed to load peer filter metadata: %s", e)
            return mapping
        for row in rows:
            callsign = str(row["callsign"] or "").strip().upper()
            if not callsign:
                continue
            groups: Set[str] = set()
            for key in ("group1", "group2", "group3"):
                group = normalize_group_name(row[key])
                if group:
                    groups.add(group)
            try:
                parsed = json.loads(row["groups_json"] or "[]")
                for raw_group in parsed if isinstance(parsed, list) else ():
                    group = normalize_group_name(raw_group)
                    if group:
                        groups.add(group)
            except Exception:
                pass
            mapping[callsign] = {
                "groups": groups,
                "role": str(row["group_role"] or "").strip().upper(),
                "region": str(row["roster_region"] or row["state"] or "").strip().upper(),
            }
        self._operator_peer_meta_cache = mapping
        self._operator_peer_meta_cache_ts = now_ts
        self._operator_peer_meta_cache_mtime = db_mtime
        return mapping

    @staticmethod
    def _replace_combo_options(combo: QComboBox, all_label: str, values: Sequence[str]) -> None:
        current = str(combo.currentData() or "").strip().upper()
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(all_label, "")
            for value in sorted({str(item or "").strip().upper() for item in values if str(item or "").strip()}):
                combo.addItem(value, value)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            combo.blockSignals(False)

    def _refresh_peer_filter_options(
        self,
        operator_meta: Dict[str, Dict[str, object]],
        peer_rows: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> None:
        scheduled_calls = {
            str(row.get("owner_callsign") or "").strip().upper()
            for row in (peer_rows if peer_rows is not None else self._peer_schedule_rows())
            if str(row.get("owner_callsign") or "").strip()
        }
        scheduled_meta = [operator_meta.get(callsign, {}) for callsign in scheduled_calls]
        groups = {
            str(group or "").strip().upper()
            for meta in scheduled_meta
            for group in tuple(meta.get("groups") or ())
            if str(group or "").strip()
        }
        regions = {str(meta.get("region") or "").strip().upper() for meta in scheduled_meta}
        roles = {str(meta.get("role") or "").strip().upper() for meta in scheduled_meta}
        self._replace_combo_options(self.peer_group_filter, "All groups", tuple(groups))
        self._replace_combo_options(self.peer_region_filter, "All regions", tuple(regions))
        self._replace_combo_options(self.peer_role_filter, "All roles", tuple(roles))

    def _activity_cache_token(self) -> Tuple[float, float]:
        return (
            self._safe_db_mtime(self._settings_db_path()),
            self._safe_db_mtime(self._db_path()),
        )

    @staticmethod
    def _is_activity_callsign_token(value: object) -> bool:
        cs = str(value or "").strip().upper()
        return bool(cs) and not cs.startswith("@")

    def _compute_activity_rows(
        self,
        window_minutes: int,
        search: str,
        group_filter: str,
    ) -> List[List[str]]:
        group_freqs = self._group_freq_map()
        group_bands = self._group_band_map()
        operator_groups = self._load_operator_group_map()
        db_path = self._db_path()
        if not db_path.exists():
            return [["No activity data", "--", "--", "--"]]

        now_ts = time.time()
        since_ts = now_ts - (window_minutes * 60)

        group_seen: Dict[str, Set[str]] = {g: set() for g in group_freqs}
        group_traffic: Dict[str, int] = {g: 0 for g in group_freqs}

        def _matching_groups_for_link(band: str, mhz: float) -> List[str]:
            rows = getattr(self, "operating_groups", None)
            if rows is None:
                rows = self.settings.get("operating_groups", []) or []
            wanted_hz = normalize_frequency_hz(mhz)
            if wanted_hz is None:
                return []
            configured_groups = {
                normalize_group_name(row.get("group", ""))
                for row in rows if isinstance(row, Mapping)
            }
            matched: list[str] = []
            for grp in configured_groups:
                if not grp:
                    continue
                resolution = resolve_operating_group_frequency(
                    rows,
                    group=grp,
                    band=band,
                    frequency=mhz,
                )
                exact = any(
                    normalize_frequency_hz(row.get("frequency")) == wanted_hz
                    for row in resolution.candidates
                )
                if resolution.resolved or exact:
                    matched.append(grp)
            # Activity frequency alone cannot identify which logical group
            # owns a shared frequency. Preserve that event as unattributed.
            if len(matched) != 1 or (group_filter and matched[0] != group_filter):
                return []
            return matched

        def _add_group_traffic(cs: str) -> None:
            cs = (cs or "").strip().upper()
            if not cs:
                return
            if search and search not in cs:
                return
            groups = operator_groups.get(cs, set())
            for g in groups:
                if g in group_traffic and (not group_filter or g == group_filter):
                    group_traffic[g] += 1
                    group_seen[g].add(cs)

        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT origin, destination, band, freq_hz FROM js8_links WHERE ts >= ?",
                (since_ts,),
            )
            for origin, dest, band, freq_hz in cur.fetchall():
                band = (band or "").strip().upper()
                if freq_hz is None:
                    continue
                try:
                    mhz = float(freq_hz) / 1_000_000.0
                except Exception:
                    continue
                matched_groups = _matching_groups_for_link(band, mhz)
                if not matched_groups:
                    continue
                origin_cs = str(origin or "").strip().upper()
                dest_cs = str(dest or "").strip().upper()
                for grp in matched_groups:
                    if self._is_activity_callsign_token(origin_cs):
                        group_seen[grp].add(origin_cs)
                    if self._is_activity_callsign_token(dest_cs):
                        group_seen[grp].add(dest_cs)
                    group_traffic[grp] = group_traffic.get(grp, 0) + 1

            for sql, label in (
                ("SELECT from_call, utc_ts FROM js8_messages WHERE utc_ts >= ?", "js8_messages"),
                ("SELECT from_call, utc_ts FROM spotter_traffic WHERE utc_ts >= ?", "spotter_traffic"),
                ("SELECT from_call, ts FROM varac_messages WHERE ts >= ?", "varac_messages"),
                ("SELECT callsign, last_seen_ts FROM fldigi_checkins WHERE last_seen_ts >= ?", "fldigi_checkins"),
            ):
                try:
                    cur.execute(sql, (since_ts,))
                    for cs, _ in cur.fetchall():
                        _add_group_traffic(cs)
                except Exception as e:
                    log.debug("ControlFreq: failed to load %s for activity: %s", label, e)
        except Exception as e:
            log.debug("ControlFreq: failed to load activity rows: %s", e)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

        rows_out: List[List[str]] = []
        for grp in sorted(group_freqs.keys()):
            if group_filter and grp != group_filter:
                continue
            bands = sorted(group_bands.get(grp, set()))
            freqs = group_freqs.get(grp, [])
            freq_txt = ", ".join(f"{f:.3f}" for f in freqs[:2])
            if len(freqs) > 2:
                freq_txt += "…"
            band_txt = "/".join(bands[:2]) + ("…" if len(bands) > 2 else "")
            band_freq = f"{band_txt} {freq_txt}".strip()
            calls_seen = len(group_seen.get(grp, set()))
            msg_ct = int(group_traffic.get(grp, 0))
            if calls_seen == 0 and msg_ct == 0:
                continue
            if search and search not in grp and not any(search in cs for cs in group_seen.get(grp, set())):
                continue
            rows_out.append([grp, band_freq or "-", str(calls_seen), str(msg_ct)])
        if not rows_out:
            rows_out = [["No activity in selected window", "--", "--", "--"]]
        return rows_out

    def _refresh_activity(self) -> None:
        window_minutes = int(self.activity_window_combo.currentData() or 120)
        search = (self.search_edit.text() or "").strip().upper()
        group_filter = self.group_combo.currentData() or ""
        self._refresh_operational_activity(window_minutes, search, str(group_filter).strip().upper())
        self._ensure_operator_roster_read()
        roster = getattr(self, "_operator_roster_snapshot", None)
        if roster is None and self._active:
            status = (
                "Operator roster unavailable" if getattr(self, "_operator_roster_error", "")
                else "Checking operator roster…"
            )
            rows = [[status, "--", "--", "--"]]
            self.activity_table.setToolTip(
                "Activity grouping needs the operator roster. A result will appear when a detached snapshot is available."
            )
            if rows != getattr(self, "_activity_rendered_rows", None):
                self._activity_rendered_rows = rows
                self._set_table_rows(self.activity_table, rows)
                self._set_left_activity_content_height()
            return
        roster_stale = (
            roster is not None
            and time.monotonic() - float(self._operator_roster_published_at) >= self._operator_roster_ttl_sec
        )
        self.activity_table.setToolTip(
            "Using the last published operator roster while a refresh is pending or unavailable."
            if roster_stale
            else "Activity grouping uses the published operator roster."
        )
        # Eager/inactive helper callers (including legacy isolated fixtures)
        # retain the pre-publication activity reader. Active UI rendering waits
        # for the shared roster snapshot above.
        group_filter = str(group_filter).strip().upper()
        group_freqs, group_bands = self._group_freq_map(), self._group_band_map()
        operating_groups = [dict(row) for row in self.settings.get("operating_groups", []) or [] if isinstance(row, Mapping)]
        db_path = self._db_path()
        signature = (str(db_path), window_minutes, search, group_filter,
                     str(getattr(self, "_source_family_filter", "")),
                     repr(group_freqs), repr(group_bands), repr(operating_groups),
                     float(getattr(self, "_operator_roster_published_at", 0.0)))
        cache = getattr(self, "_activity_read_cache", None)
        if cache is None:
            cache = self._activity_read_cache = LatestReadCache("fio-ops-activity")
        def reader_factory():
            context = _ActivityReadContext(
                db_path, group_freqs, group_bands,
                operator_groups=roster.group_map if roster is not None else None,
                operating_groups=operating_groups,
            )
            return lambda: ControlFreqTab._compute_activity_rows(context, window_minutes, search, group_filter)
        rows = cache.read(signature, reader_factory, refresh_after_s=self._activity_cache_ttl_sec)
        rows = rows if rows is not None else [["Activity unavailable" if cache.failed else "Loading activity…", "--", "--", "--"]]
        if rows != getattr(self, "_activity_rendered_rows", None):
            self._activity_rendered_rows = rows
            self._set_table_rows(self.activity_table, rows)
            self._set_left_activity_content_height()
        self._poll_activity_readers()

    def _poll_activity_readers(self):
        pending = any(getattr(getattr(self, name, None), "_future", None) is not None
                      for name in ("_activity_read_cache", "_operational_read_cache"))
        timer = getattr(self, "_activity_read_poll", None)
        if pending and timer is None:
            timer = self._activity_read_poll = QTimer(self)
            timer.setInterval(100)
            timer.timeout.connect(self._refresh_activity)
        if timer is not None:
            if pending and not timer.isActive():
                timer.start()
            elif not pending:
                timer.stop()

    def _refresh_operational_activity(
        self,
        window_minutes: int,
        search: str = "",
        group_filter: str = "",
    ) -> None:
        db_path = self._db_path()
        source_families = self._traffic_source_query_families(
            str(getattr(self, "_source_family_filter", "") or "").strip())
        callsign = str(self.settings.get("operator_callsign", "") or "").strip().upper()
        pins = self._configured_awareness_pins()
        profile_db_path = settings_db_path()
        sop_db_path = getattr(self._sop_manager, "db_path", None)
        signature = (str(db_path), window_minutes, search, group_filter,
                     tuple(source_families), callsign, repr(pins), str(profile_db_path), str(sop_db_path))
        cache = getattr(self, "_operational_read_cache", None)
        if cache is None:
            cache = self._operational_read_cache = LatestReadCache("fio-ops-awareness")
        def reader_factory():
            query = ObservationQuery(
                source_families=source_families,
                since_utc=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=max(1, int(window_minutes or 120)))).replace(microsecond=0).isoformat(),
                operating_group=group_filter, search_text=search, limit=80,
            )
            def read():
                observations = query_observations(db_path, query)
                snapshot = activity_snapshot_from_observations(observations[:40])
                awareness = build_awareness_snapshot(
                    observations, local_callsign=callsign,
                    active_groups=(group_filter,) if group_filter else (),
                    pins=pins, visible_attention_limit=6,
                )
                sop_profiles = ControlFreqTab._read_condition_sop_profiles(sop_db_path)
                try:
                    profiles = MultiRadioStore(profile_db_path).list_runtime_active_device_profiles()
                except Exception:
                    profiles = []
                return snapshot, awareness, sop_profiles, tuple(profiles)
            return read
        result = cache.read(signature, reader_factory, refresh_after_s=self._operational_snapshot_cache_ttl_sec)
        if result is None:
            self._operational_activity_context = {}
            self._operational_awareness_context = {}
            self._operational_awareness_full_snapshot = None
            self._awareness_row_contexts = []
            table = getattr(self, "awareness_table", None)
            pending_signature = (signature, cache.failed)
            if table is not None and pending_signature != getattr(self, "_awareness_pending_signature", None):
                self._awareness_pending_signature = pending_signature
                table.setEnabled(False)
                self._set_table_rows(table, [["Unavailable" if cache.failed else "Checking", "-", "Current traffic view pending", ""]])
            self._set_operational_activity_text("Recent Traffic: unavailable" if cache.failed else "Recent Traffic: checking…", "")
            return
        if result is getattr(self, "_operational_rendered_result", None):
            return
        self._operational_rendered_result = result
        self._awareness_pending_signature = None
        table = getattr(self, "awareness_table", None)
        if table is not None:
            table.setEnabled(True)
        try:
            snapshot, awareness_snapshot, sop_profiles, source_profiles = result
            self._operational_source_profiles = source_profiles
            headline = self._format_operational_activity_headline(snapshot, search)
            sop_decision = self._format_condition_sop_decision(snapshot, profiles=sop_profiles)
            if sop_decision:
                headline = f"{headline} | SOP: {sop_decision}"
            self._operational_activity_context = self._activity_context_from_snapshot(snapshot, group_filter)
            self._operational_awareness_full_snapshot = awareness_snapshot
            self._set_operational_awareness_snapshot(awareness_snapshot)
            self._set_operational_activity_text(
                headline,
                self._format_operational_activity_topics(snapshot),
            )
        except Exception as e:
            log.debug("ControlFreq: failed to refresh operational activity snapshot: %s", e)
            self._operational_activity_context = {}
            self._operational_awareness_context = {}
            lanes = self._build_operational_source_lanes(())
            self._set_operational_awareness_snapshot(None, source_lanes=lanes)
            self._set_operational_activity_text("Recent Traffic: unavailable", "")

    def _refresh_awareness_now_next(self) -> None:
        label = getattr(self, "awareness_now_next_label", None)
        if label is None:
            return
        now_text = ""
        try:
            now_text = str(self.freq_combo.currentText() or "").strip()
        except Exception:
            now_text = ""
        if not now_text:
            try:
                now_text = self._format_current_schedule_label()
            except Exception:
                now_text = ""
        next_text = ""
        try:
            next_text = str(self.next_change_label.text() or "").strip()
        except Exception:
            next_text = ""
        for prefix in ("Next Change:", "Next Schedule:", "Current Ends:"):
            if next_text.startswith(prefix):
                next_text = next_text[len(prefix):].strip()
                break
        sources = self._active_source_short_names()
        if len(sources) > 1:
            text = f"Sources: {', '.join(sources[:4])}"
            if len(sources) > 4:
                text += f" +{len(sources) - 4}"
            text += f" | Primary now: {now_text or '--'} | Next: {next_text or '--'}"
        elif sources:
            text = f"Source: {sources[0]} | Now: {now_text or '--'} | Next: {next_text or '--'}"
        else:
            text = f"Now: {now_text or '--'} | Next: {next_text or '--'}"
        label.setText(text)
        label.setToolTip(text)

    def _active_source_short_names(self) -> List[str]:
        names: List[str] = []
        profiles = list(getattr(self, "_operational_source_profiles", ()))
        for profile in profiles:
            name = str((profile or {}).get("short_name") or (profile or {}).get("name") or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    def _build_operational_source_lanes(self, attention_items: Sequence[object]) -> Tuple[object, ...]:
        profiles = list(getattr(self, "_operational_source_profiles", ()))
        now_text = ""
        try:
            now_text = self._format_current_schedule_label()
        except Exception:
            now_text = ""
        next_text = ""
        try:
            next_label = getattr(self, "next_change_label", None)
            next_text = str(next_label.text() or "").strip() if next_label is not None else ""
        except Exception:
            next_text = ""
        for prefix in ("Next Change:", "Next Schedule:", "Current Ends:"):
            if next_text.startswith(prefix):
                next_text = next_text[len(prefix):].strip()
                break
        return build_radio_source_lanes(
            profiles,
            current_label=now_text,
            next_label=next_text,
            attention_items=attention_items,
        )

    def _set_source_lanes(self, lanes: Sequence[object]) -> None:
        table = getattr(self, "source_lanes_table", None)
        if table is None:
            return
        rows: List[List[str]] = []
        contexts: List[Dict[str, str]] = []
        for lane in tuple(lanes or ()):
            source = str(getattr(lane, "short_name", "") or "").strip()
            if not source:
                continue
            now_text = str(getattr(lane, "now", "") or "").strip() or "--"
            next_text = str(getattr(lane, "next", "") or "").strip() or "--"
            attention_count = int(getattr(lane, "attention_count", 0) or 0)
            attention_summary = str(getattr(lane, "attention_summary", "") or "").strip()
            attention_text = attention_summary or ("clear" if attention_count <= 0 else f"{attention_count} item(s)")
            rows.append([source, now_text, next_text, attention_text])
            contexts.append(
                {
                    "source_name": source,
                    "source_id": str(getattr(lane, "source_id", "") or "").strip(),
                    "source_family": str(getattr(lane, "source_kind", "") or "").strip(),
                }
            )
        if not rows:
            rows = [["No active source", "--", "--", "check radio setup"]]
            contexts = [{}]
        self._source_lane_syncing = True
        try:
            self._set_table_rows(table, rows)
            self._source_lane_contexts = contexts
            self._apply_elide_tooltips(table, 1)
            self._apply_elide_tooltips(table, 2)
            self._apply_elide_tooltips(table, 3)
            self._fit_table_height_to_rows(table, min_rows=1, max_rows=4, empty_rows=1)
        finally:
            self._source_lane_syncing = False
        self._render_source_lane_cards(rows, contexts)

    def _set_awareness_details_visible(self, visible: bool) -> None:
        """Keep dense evidence available without making it the default grammar."""
        self.awareness_table.setVisible(bool(visible))
        self.activity_table.setVisible(bool(visible))
        self._fit_group_box_to_contents(self.activity_box)
        self._set_left_activity_content_height()

    @staticmethod
    def _clear_widget_layout(layout: QBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child_layout is not None:
                ControlFreqTab._clear_widget_layout(child_layout)  # type: ignore[arg-type]

    def _render_source_lane_cards(
        self,
        rows: Sequence[Sequence[str]],
        contexts: Sequence[Dict[str, str]],
    ) -> None:
        layout = getattr(self, "source_lane_cards_layout", None)
        if layout is None:
            return
        self._clear_widget_layout(layout)
        theme = self._theme()
        for index, row in enumerate(tuple(rows)[:4]):
            values = list(row) + ["", "", "", ""]
            source, now_text, next_text, attention = values[:4]
            role = "warning" if attention and attention.lower() not in {"clear", "--"} else "panel"
            bg, fg, border = self._semantic_panel_colors(role)
            card = QFrame(self.source_lane_cards_container)
            card.setObjectName("controlfreqSourceLaneCard")
            card.setStyleSheet(
                f"QFrame#controlfreqSourceLaneCard {{ background: {bg}; color: {fg}; "
                f"border: 1px solid {border}; border-radius: 6px; }}"
            )
            card_row = QHBoxLayout(card)
            card_row.setContentsMargins(8, 5, 8, 5)
            card_row.setSpacing(8)
            icon = QLabel()
            icon.setPixmap(QIcon(str(self._focus_icon_path("source"))).pixmap(20, 20))
            icon.setAccessibleName("Source")
            card_row.addWidget(icon)
            source_label = QLabel(f"<b>{source}</b>")
            source_label.setTextFormat(Qt.RichText)
            source_label.setMinimumWidth(80)
            card_row.addWidget(source_label)
            now_label = QLabel(f"Now · {now_text}")
            now_label.setWordWrap(True)
            card_row.addWidget(now_label, 2)
            next_label = QLabel(f"Next · {next_text}")
            next_label.setWordWrap(True)
            card_row.addWidget(next_label, 2)
            attention_label = QLabel(str(attention or "clear"))
            attention_label.setStyleSheet(f"font-weight: 700; color: {fg};")
            attention_label.setToolTip(f"Source attention: {attention or 'clear'}")
            card_row.addWidget(attention_label)
            focus_btn = QPushButton("Focus")
            focus_btn.setMaximumWidth(68)
            focus_btn.setStyleSheet(button_style("secondary", theme))
            focus_btn.setToolTip(f"Focus Ops Center on {source} without changing the selected radio.")
            focus_btn.clicked.connect(lambda _checked=False, row_index=index: self._activate_source_lane_card(row_index))
            card_row.addWidget(focus_btn)
            layout.addWidget(card)
        if not rows:
            empty = QLabel("No active source · check radio and source setup")
            empty.setWordWrap(True)
            layout.addWidget(empty)

    def _activate_source_lane_card(self, row: int) -> None:
        contexts = list(getattr(self, "_source_lane_contexts", []) or [])
        if row < 0 or row >= len(contexts):
            return
        context = dict(contexts[row] or {})
        label = str(context.get("source_name") or "Source").strip()
        canonical = str(
            context.get("source_family") or context.get("source_id") or label
        ).strip().lower()
        if not canonical:
            return
        self._apply_ops_focus(
            OpsFocus("source", canonical, label, label, "source_lane")
        )

    def _set_source_lane_focus_from_selection(self) -> None:
        if bool(getattr(self, "_source_lane_syncing", False)):
            return
        table = getattr(self, "source_lanes_table", None)
        if table is None:
            return
        row = int(table.currentRow())
        contexts = list(getattr(self, "_source_lane_contexts", []) or [])
        if row < 0 or row >= len(contexts):
            return
        context = dict(contexts[row] or {})
        source_name = str(context.get("source_name") or "").strip()
        source_family = str(context.get("source_family") or "").strip().lower()
        is_data_source = source_family in {
            "aprs",
            "commstat",
            "fiospotter",
            "js8call",
            "local_report",
            "meshcore",
            "mqtt",
            "reticulum",
            "spotter",
            "varac",
        }
        self._source_family_filter = source_family if is_data_source else ""
        if is_data_source:
            self._set_traffic_source_filter(source_family)
        else:
            self._set_traffic_source_filter("")
        if hasattr(self, "search_edit"):
            previous = self.search_edit.blockSignals(True)
            try:
                self.search_edit.setText("" if is_data_source else source_name)
            finally:
                self.search_edit.blockSignals(previous)
        if source_name:
            self._operational_awareness_context = {
                "source_family": source_family if is_data_source else "",
                "search_query": "" if is_data_source else source_name,
            }
            if hasattr(self, "awareness_recommend_label"):
                text = f"Focused source: {source_name} | use Inbox, Reply, or Map from matching traffic."
                self.awareness_recommend_label.setText(text)
                self.awareness_recommend_label.setToolTip(text)
        self._update_clear_filters_style()
        self._run_filter_refresh()

    def _configured_awareness_pins(self) -> Tuple[Dict[str, object], ...]:
        raw_pins = self.settings.get("controlfreq_awareness_pins", ())
        if not isinstance(raw_pins, (list, tuple)):
            return ()
        pins: List[Dict[str, object]] = []
        for raw in raw_pins:
            if not isinstance(raw, dict):
                continue
            pin_type = str(raw.get("pin_type") or raw.get("type") or "").strip().lower()
            value = str(raw.get("value") or "").strip()
            if pin_type in {"topic", "callsign", "group"} and value:
                pins.append(
                    {
                        "type": pin_type,
                        "value": value,
                        "label": str(raw.get("label") or value).strip(),
                    }
                )
        return tuple(pins)

    def _set_operational_awareness_snapshot(
        self,
        snapshot: object | None,
        *,
        source_lanes: Sequence[object] | None = None,
    ) -> None:
        table = getattr(self, "awareness_table", None)
        if table is None:
            return
        display_snapshot = self._awareness_display_snapshot(snapshot)
        needs = tuple(getattr(display_snapshot, "needs_attention", ()) or ()) if display_snapshot is not None else ()
        if not needs and display_snapshot is not None:
            needs = tuple(getattr(display_snapshot, "attention_items", ()) or ())
        recent = tuple(getattr(display_snapshot, "recent_traffic", ()) or ()) if display_snapshot is not None else ()
        lanes = tuple(source_lanes or getattr(snapshot, "source_lanes", ()) or ())
        if not lanes:
            lanes = self._build_operational_source_lanes(needs)
        self._set_source_lanes(lanes)
        rows: List[List[str]] = []
        row_contexts: List[Dict[str, str]] = []
        display_items: List[Tuple[str, object]] = [("needs", item) for item in needs[:6]]
        if recent:
            display_items.extend(("recent", item) for item in recent[: max(0, 6 - len(display_items))])
        for lane_name, item in display_items:
            topic = ", ".join(tuple(getattr(item, "topics", ()) or ())[:2])
            focus_parts = [
                str(getattr(item, "subject", "") or "").strip(),
                topic,
                str(getattr(item, "group", "") or getattr(item, "to_target", "") or "").strip(),
            ]
            if lane_name == "recent":
                priority_label = "Recent"
            else:
                priority_label = "Pinned" if bool(getattr(item, "pinned", False)) else self._awareness_priority_label(int(getattr(item, "priority", 0) or 0))
            rows.append(
                [
                    priority_label,
                    str(getattr(item, "callsign", "") or "-").strip() or "-",
                    " | ".join(part for part in focus_parts if part) or "Traffic item",
                    "",
                ]
            )
            row_contexts.append(self._awareness_item_context(item))
        if not rows:
            rows = [["Ready", "-", "No traffic needs attention", "Monitor"]]
            row_contexts = [{}]
        self._set_table_rows(table, rows)
        self._awareness_row_contexts = row_contexts
        self._operational_awareness_context = next((ctx for ctx in row_contexts if ctx), {})
        self._install_awareness_action_widgets()
        self._style_awareness_rows()
        try:
            table.resizeRowsToContents()
            max_rows = min(6, max(2, table.rowCount()))
            row_height = max(28, int(table.verticalHeader().defaultSectionSize()))
            table.setMinimumHeight(36 + (max_rows * row_height))
            table.setMaximumHeight(260)
        except Exception:
            pass
        self._set_awareness_lead_text(needs[0] if needs else None)
        self._set_awareness_recommended_text(display_snapshot)
        self._set_situation_summary_text(display_snapshot)
        self._set_situation_cards(display_snapshot)
        self._set_awareness_support_text(snapshot)

    def _awareness_display_snapshot(self, snapshot: object | None) -> object | None:
        topic_filter = str(getattr(self, "_awareness_topic_filter", "") or "").strip().lower()
        if snapshot is None or not topic_filter:
            return snapshot

        def matches_topic(item: object) -> bool:
            return any(str(topic or "").strip().lower() == topic_filter for topic in tuple(getattr(item, "topics", ()) or ()))

        return replace(
            snapshot,
            needs_attention=tuple(item for item in tuple(getattr(snapshot, "needs_attention", ()) or ()) if matches_topic(item)),
            recent_traffic=tuple(item for item in tuple(getattr(snapshot, "recent_traffic", ()) or ()) if matches_topic(item)),
            attention_items=tuple(item for item in tuple(getattr(snapshot, "attention_items", ()) or ()) if matches_topic(item)),
            more_traffic=tuple(item for item in tuple(getattr(snapshot, "more_traffic", ()) or ()) if matches_topic(item)),
            topic_rollups=tuple(
                rollup
                for rollup in tuple(getattr(snapshot, "topic_rollups", ()) or ())
                if str(getattr(rollup, "topic", "") or "").strip().lower() == topic_filter
            ),
            situation_summary=None,
        )

    @staticmethod
    def _awareness_priority_label(priority: int) -> str:
        if priority >= 900:
            return "Direct"
        if priority >= 700:
            return "Alert"
        if priority >= 450:
            return "Urgent"
        if priority >= 180:
            return "Watch"
        return "Info"

    def _set_awareness_recommended_text(self, snapshot: object | None) -> None:
        label = getattr(self, "awareness_recommend_label", None)
        if label is None:
            return
        actions = tuple(getattr(snapshot, "recommended_actions", ()) or ()) if snapshot is not None else ()
        labels = [
            str(getattr(action, "label", "") or "").strip()
            for action in actions[:3]
            if str(getattr(action, "label", "") or "").strip()
        ]
        text = "Recommended: " + " | ".join(labels) if labels else "Recommended: monitor traffic."
        label.setText(text)
        label.setToolTip(text)

    def _set_situation_summary_text(self, snapshot: object | None) -> None:
        label = getattr(self, "situation_summary_label", None)
        detail_label = getattr(self, "situation_detail_label", None)
        if label is None:
            return
        situation = getattr(snapshot, "situation_summary", None) if snapshot is not None else None
        headline = str(getattr(situation, "headline", "") or "").strip()
        if not headline:
            attention = tuple(getattr(snapshot, "attention_items", ()) or ()) if snapshot is not None else ()
            topic_filter = str(getattr(self, "_awareness_topic_filter", "") or "").strip()
            if attention and topic_filter:
                headline = f"{len(attention)} {topic_filter} item{'s' if len(attention) != 1 else ''} in the current view."
            elif attention:
                headline = f"{len(attention)} traffic item{'s' if len(attention) != 1 else ''} need attention."
            else:
                headline = "No traffic needs attention."
        text = "Situation: " + headline
        if hasattr(self, "dashboard"):
            incidents = tuple(getattr(situation, "active_incidents", ()) or ()) if situation is not None else ()
            needs = tuple(getattr(situation, "top_needs", ()) or ()) if situation is not None else ()
            self.dashboard.situation_link.setText(
                f"Situation · {len(incidents)} incidents / {len(needs)} needs" if snapshot is not None else "Situation · unavailable"
            )
            self.dashboard.situation_link.setToolTip(text + " · Open traffic evidence")
        label.setText(text)
        label.setToolTip(text)
        detail_parts: list[str] = []
        needs = tuple(getattr(situation, "top_needs", ()) or ()) if situation is not None else ()
        incidents = tuple(getattr(situation, "active_incidents", ()) or ()) if situation is not None else ()
        handled = tuple(getattr(situation, "handled", ()) or ()) if situation is not None else ()
        if needs:
            detail_parts.append(
                "Needs: "
                + "; ".join(
                    str(getattr(need, "summary", "") or "").strip()
                    for need in needs[:2]
                    if str(getattr(need, "summary", "") or "").strip()
                )
            )
        if incidents:
            detail_parts.append(
                "Storylines: "
                + "; ".join(
                    str(getattr(story, "headline", "") or "").strip()
                    for story in incidents[:3]
                    if str(getattr(story, "headline", "") or "").strip()
                )
            )
        if handled:
            detail_parts.append(f"Handled: {len(handled)}")
        confidence = str(getattr(situation, "confidence", "") or "").strip()
        if confidence:
            detail_parts.append(confidence)
        detail = " | ".join(part for part in detail_parts if part.strip())
        if detail_label is not None:
            detail_label.setText(detail)
            detail_label.setToolTip(detail)
            detail_label.setVisible(bool(detail))
        try:
            has_need = bool(needs)
            urgent_story = any(str(getattr(story, "severity", "") or "") == "urgent" for story in incidents)
            if has_need or urgent_story:
                bg, fg, border = self._semantic_panel_colors("warning")
            elif incidents:
                bg, fg, border = self._semantic_panel_colors("secondary")
            else:
                bg, fg, border = self._semantic_panel_colors("success")
            label.setStyleSheet(
                f"font-weight: 700; padding: 6px; border-radius: 6px; background: {bg}; color: {fg}; border: 1px solid {border};"
            )
        except Exception:
            pass

    def _set_situation_cards(self, snapshot: object | None) -> None:
        layout = getattr(self, "situation_cards_layout", None)
        container = getattr(self, "situation_cards_container", None)
        if layout is None or container is None:
            return
        self._clear_awareness_chip_layout(layout)
        situation = getattr(snapshot, "situation_summary", None) if snapshot is not None else None
        cards: list[QWidget] = []
        needs = tuple(getattr(situation, "top_needs", ()) or ()) if situation is not None else ()
        incidents = tuple(getattr(situation, "active_incidents", ()) or ()) if situation is not None else ()
        handled = tuple(getattr(situation, "handled", ()) or ()) if situation is not None else ()
        attention = tuple(getattr(snapshot, "attention_items", ()) or ()) if snapshot is not None else ()
        topic_filter = str(getattr(self, "_awareness_topic_filter", "") or "").strip()

        if snapshot is not None:
            overview_role = "success"
            if needs:
                overview_role = "warning"
            elif incidents or attention:
                overview_role = "secondary"
            cards.append(
                self._build_situation_card(
                    "Situation",
                    self._situation_overview_text(snapshot),
                    self._situation_overview_actions(snapshot),
                    role=overview_role,
                )
            )

        if needs:
            need = needs[0]
            title = "Open Need"
            body = self._situation_need_text(need)
            cards.append(
                self._build_situation_card(
                    title,
                    body,
                    tuple(getattr(need, "actions", ()) or ()),
                    role="warning" if str(getattr(need, "severity", "") or "") == "urgent" else "secondary",
                )
            )
        if incidents:
            story = incidents[0]
            title = "Storyline"
            body = self._situation_story_text(story)
            cards.append(
                self._build_situation_card(
                    title,
                    body,
                    tuple(getattr(story, "actions", ()) or ()),
                    role="warning" if str(getattr(story, "severity", "") or "") in {"urgent", "important"} else "secondary",
                )
            )
        if handled:
            cards.append(
                self._build_situation_card(
                    "Handled",
                    f"{len(handled)} recently handled need{'s' if len(handled) != 1 else ''}.",
                    tuple(getattr(handled[0], "actions", ()) or ())[:1],
                    role="success",
                )
            )
        if len(cards) <= 1 and attention and topic_filter:
            context = {
                "topic_filter": topic_filter,
                "search_query": topic_filter,
            }
            cards.append(
                self._build_situation_card(
                    "Filtered Topic",
                    f"{len(attention)} {topic_filter} item{'s' if len(attention) != 1 else ''} in view.",
                    (
                        self._awareness_action("messages", "Inbox", context),
                        self._awareness_action("map", "Map", context),
                    ),
                    role="secondary",
                )
            )

        for card in cards[:3]:
            layout.addWidget(card)
        layout.addStretch(1)
        container.setVisible(bool(cards))

    @staticmethod
    def _awareness_action(kind: str, label: str, context: Dict[str, str]) -> object:
        return AwarenessAction(kind, label, dict(context))

    def _situation_overview_text(self, snapshot: object) -> str:
        situation = getattr(snapshot, "situation_summary", None)
        headline = str(getattr(situation, "headline", "") or "").strip()
        if not headline:
            attention = tuple(getattr(snapshot, "attention_items", ()) or ())
            if attention:
                headline = f"{len(attention)} item{'s' if len(attention) != 1 else ''} need attention."
            else:
                headline = "No traffic needs attention."
        parts = [headline]
        needs = tuple(getattr(situation, "top_needs", ()) or ()) if situation is not None else ()
        incidents = tuple(getattr(situation, "active_incidents", ()) or ()) if situation is not None else ()
        if needs:
            parts.append(f"{len(needs)} open need{'s' if len(needs) != 1 else ''}")
        if incidents:
            parts.append(f"{len(incidents)} storyline{'s' if len(incidents) != 1 else ''}")
        confidence = str(getattr(situation, "confidence", "") or "").strip()
        if confidence:
            parts.append(confidence)
        return " | ".join(parts)

    def _situation_overview_actions(self, snapshot: object) -> tuple[object, ...]:
        context: Dict[str, str] = {}
        topic_filter = str(getattr(self, "_awareness_topic_filter", "") or "").strip()
        if topic_filter:
            context["topic_filter"] = topic_filter
            context["search_query"] = topic_filter
        return (
            self._awareness_action("messages", "Inbox", context),
            self._awareness_action("map", "Map", context),
        )

    @staticmethod
    def _situation_need_text(need: object) -> str:
        summary = str(getattr(need, "summary", "") or "Review request").strip()
        category = str(getattr(need, "category", "") or "").strip()
        location = str(getattr(need, "location_hint", "") or "").strip()
        requested_by = str(getattr(need, "requested_by", "") or "").strip()
        parts = [summary]
        if category:
            parts.append(category)
        if requested_by:
            parts.append(requested_by)
        if location:
            parts.append(location)
        return " | ".join(parts)

    @staticmethod
    def _situation_story_text(story: object) -> str:
        headline = str(getattr(story, "headline", "") or "Review storyline").strip()
        source_count = int(getattr(story, "source_count", 0) or 0)
        callsign_count = int(getattr(story, "callsign_count", 0) or 0)
        detail = []
        if callsign_count:
            detail.append(f"{callsign_count} station{'s' if callsign_count != 1 else ''}")
        if source_count:
            detail.append(f"{source_count} source{'s' if source_count != 1 else ''}")
        return headline + (f" | {', '.join(detail)}" if detail else "")

    def _build_situation_card(
        self,
        title: str,
        body: str,
        actions: Sequence[object],
        *,
        role: str,
    ) -> QWidget:
        try:
            theme = self._theme()
        except Exception:
            theme = {}
        try:
            bg, fg, border = self._semantic_panel_colors(role)
        except Exception:
            bg = theme.get("surface", "#F4F6F8")
            border = theme.get("border", "#CAD6E2")
            fg = theme.get("text", "#111111")
        frame = QFrame(self.situation_cards_container)
        frame.setObjectName("controlfreqSituationCard")
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(
            f"QFrame#controlfreqSituationCard {{ background: {bg}; border: 1px solid {border}; border-radius: 6px; }}"
        )
        row = QHBoxLayout(frame)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(6)
        text = QLabel(f"<b>{title}:</b> {body}")
        text.setTextFormat(Qt.RichText)
        text.setWordWrap(True)
        text.setStyleSheet(f"color: {fg};")
        text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row.addWidget(text, 1)
        for action in tuple(actions or ())[:3]:
            label = str(getattr(action, "label", "") or getattr(action, "kind", "") or "").strip()
            kind = str(getattr(action, "kind", "") or "").strip().lower()
            context = self._situation_action_context(action)
            if not label or kind == "monitor":
                continue
            if kind in {"reply", "compose"} and not context.get("callsign"):
                continue
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            btn.setMaximumWidth(74)
            btn.setMinimumHeight(button_height_for_font(btn, floor=26))
            btn.setToolTip(self._situation_action_tooltip(kind, context))
            btn.setStyleSheet(button_style("secondary", theme))
            btn.clicked.connect(lambda _checked=False, k=kind, c=dict(context): self._activate_situation_action(k, c))
            row.addWidget(btn)
        return frame

    @staticmethod
    def _situation_action_context(action: object) -> Dict[str, str]:
        raw = getattr(action, "context", {}) or {}
        if not isinstance(raw, dict):
            return {}
        normalized = {str(key): str(value) for key, value in raw.items() if str(value or "").strip()}
        group = normalized.get("group") or normalized.get("to_target") or ""
        topic = normalized.get("topic") or normalized.get("topic_filter") or ""
        search = normalized.get("search_query") or " ".join(
            part
            for part in (
                normalized.get("callsign", ""),
                group,
                topic,
            )
            if part
        )
        return {
            "group_filter": group,
            "topic_filter": topic,
            "callsign": normalized.get("callsign", ""),
            "source_family": normalized.get("source_family", ""),
            "source_ref": normalized.get("source_ref", ""),
            "compose_mode": normalized.get("compose_mode", ""),
            "state_filter": normalized.get("state", ""),
            "grid_filter": normalized.get("grid", ""),
            "search_query": search,
        }

    @staticmethod
    def _situation_action_tooltip(kind: str, context: Dict[str, str]) -> str:
        label = context.get("callsign") or context.get("topic_filter") or context.get("group_filter") or "this item"
        if kind in {"reply", "compose"}:
            return f"Compose a reply for {label}."
        if kind == "map":
            return f"Open Map focused on {label}."
        if kind in {"sop", "open_sop"}:
            return f"Open SOP Builder for {label}."
        return f"Open Inbox focused on {label}."

    def _activate_situation_action(self, kind: str, context: Dict[str, str]) -> None:
        self._operational_awareness_context = {key: value for key, value in dict(context or {}).items() if value}
        action = str(kind or "").strip().lower()
        if action in {"reply", "compose"}:
            self._open_operational_activity_compose()
        elif action == "map":
            self._open_operational_activity_map()
        elif action in {"sop", "open_sop"}:
            self._navigate_to_tab("SOP")
        else:
            self._open_operational_activity_messages()

    def _set_awareness_lead_text(self, item: object | None) -> None:
        label = getattr(self, "awareness_lead_label", None)
        if label is None:
            return
        if item is None:
            text = "Needs Attention: clear."
            role = "positive"
        else:
            priority = "Pinned" if bool(getattr(item, "pinned", False)) else self._awareness_priority_label(int(getattr(item, "priority", 0) or 0))
            callsign = str(getattr(item, "callsign", "") or "").strip()
            subject = str(getattr(item, "subject", "") or "").strip()
            topics = ", ".join(tuple(getattr(item, "topics", ()) or ())[:2])
            group = str(getattr(item, "group", "") or getattr(item, "to_target", "") or "").strip()
            detail = " | ".join(part for part in (callsign, subject, topics, group) if part)
            text = f"Needs Attention: {priority}" + (f" | {detail}" if detail else "")
            role = "warning" if priority in {"Alert", "Urgent", "Direct"} else "secondary"
        label.setText(text)
        label.setToolTip(text)
        try:
            if role == "positive":
                bg, fg, border = self._semantic_panel_colors("success")
            elif role == "warning":
                bg, fg, border = self._semantic_panel_colors("warning")
            else:
                bg, fg, border = self._semantic_panel_colors("secondary")
            label.setStyleSheet(
                f"font-weight: 700; padding: 6px; border-radius: 6px; background: {bg}; color: {fg}; border: 1px solid {border};"
            )
        except Exception:
            pass

    @staticmethod
    def _awareness_item_context(item: object) -> Dict[str, str]:
        actions = tuple(getattr(item, "actions", ()) or ())
        ordered_actions = sorted(
            actions,
            key=lambda action: 0 if str(getattr(action, "kind", "") or "") == "reply" else 1,
        )
        for action in ordered_actions:
            context = getattr(action, "context", {}) or {}
            if isinstance(context, dict) and context:
                normalized = {str(k): str(v) for k, v in context.items() if str(v or "").strip()}
                group = normalized.get("group") or normalized.get("to_target") or ""
                topic = normalized.get("topic") or ""
                source_family = normalized.get("source_family") or ""
                compose_mode = normalized.get("compose_mode") or str(getattr(item, "reply_compose_mode", "") or "")
                search = " ".join(
                    part
                    for part in (
                        normalized.get("callsign", ""),
                        group,
                        topic,
                    )
                    if part
                )
                return {
                    "group_filter": group,
                    "topic_filter": topic,
                    "callsign": normalized.get("callsign", ""),
                    "source_family": source_family,
                    "source_ref": normalized.get("source_ref", ""),
                    "compose_mode": compose_mode,
                    "search_query": search,
                    "state_filter": normalized.get("state", ""),
                    "grid_filter": normalized.get("grid", ""),
                }
        return {
            "group_filter": str(getattr(item, "group", "") or getattr(item, "to_target", "") or "").strip(),
            "topic_filter": next(iter(tuple(getattr(item, "topics", ()) or ())), ""),
            "callsign": str(getattr(item, "callsign", "") or "").strip(),
            "source_family": str(getattr(item, "source_family", "") or "").strip(),
            "source_ref": str(getattr(item, "source_ref", "") or "").strip(),
            "compose_mode": str(getattr(item, "reply_compose_mode", "") or "").strip(),
            "state_filter": str(getattr(item, "state", "") or "").strip().upper(),
            "grid_filter": str(getattr(item, "grid", "") or "").strip().upper(),
            "search_query": " ".join(
                part
                for part in (
                    str(getattr(item, "callsign", "") or "").strip(),
                    str(getattr(item, "subject", "") or "").strip(),
                )
                if part
            ),
        }

    def _style_awareness_rows(self) -> None:
        table = getattr(self, "awareness_table", None)
        if table is None:
            return
        palette = self._urgency_palette()
        tone_for_label = {
            "PINNED": palette["upcoming"],
            "DIRECT": palette["critical"],
            "ALERT": palette["critical"],
            "URGENT": palette["soon"],
            "WATCH": palette["upcoming"],
            "RECENT": None,
            "INFO": None,
            "READY": palette["positive"],
        }
        for row in range(table.rowCount()):
            label_item = table.item(row, 0)
            label = (label_item.text() if label_item else "").strip().upper()
            context = {}
            try:
                context = dict(self._awareness_row_contexts[row] or {})
            except Exception:
                context = {}
            contract = source_contract_for(context.get("source_family", ""))
            tooltip = (
                f"{contract.display_name}: actions "
                f"{', '.join(contract.actions.enabled_names()) or 'none'}; "
                f"provenance {contract.provenance.label}; "
                f"default view {contract.default_view or 'unset'}."
            )
            tone = tone_for_label.get(label)
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item is not None:
                    item.setToolTip(tooltip)
                    if tone is not None:
                        item.setBackground(tone)

    def _install_awareness_action_widgets(self) -> None:
        table = getattr(self, "awareness_table", None)
        if table is None:
            return
        try:
            theme = self._theme()
        except Exception:
            theme = {}
        for row in range(table.rowCount()):
            context = {}
            try:
                context = dict(self._awareness_row_contexts[row] or {})
            except Exception:
                context = {}
            source_family = str(context.get("source_family") or "").strip()
            contract = source_contract_for(source_family)
            cell = QWidget(table)
            layout = QHBoxLayout(cell)
            layout.setContentsMargins(2, 0, 2, 0)
            layout.setSpacing(4)
            for label, action, tooltip in (
                ("Read", "read", "Open Messages focused to this traffic."),
                ("Reply", "reply", "Compose a reply using this traffic context."),
                ("Map", "map", "Open Map filtered to this traffic context."),
                ("Topic", "topic", "Correct this traffic category."),
            ):
                action_allowed = {
                    "read": contract.actions.read,
                    "reply": contract.actions.reply or contract.actions.compose,
                    "map": contract.actions.map,
                    "topic": bool(context.get("source_ref")),
                }.get(action, False)
                enabled = bool(context and action_allowed)
                btn = QPushButton(label, cell)
                btn.setEnabled(enabled)
                btn.setToolTip(tooltip if enabled else f"{label} is not available for {contract.display_name}.")
                btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                btn.setMaximumWidth(68)
                btn.setMinimumHeight(button_height_for_font(btn, floor=26))
                btn.setStyleSheet(button_style("secondary" if enabled else "muted", theme))
                btn.clicked.connect(lambda _checked=False, r=row, a=action: self._activate_awareness_row_action(r, a))
                layout.addWidget(btn)
            layout.addStretch(1)
            table.setCellWidget(row, 3, cell)

    def _activate_awareness_row_action(self, row: int, action: str) -> None:
        table = getattr(self, "awareness_table", None)
        if table is not None:
            table.setCurrentCell(max(0, int(row or 0)), 0)
        try:
            context = dict(self._awareness_row_contexts[int(row)] or {})
        except Exception:
            context = {}
        if context:
            self._operational_awareness_context = context
        if action == "reply":
            self._open_operational_activity_compose()
        elif action == "map":
            self._open_operational_activity_map()
        elif action == "topic":
            self._show_awareness_topic_menu(row)
        else:
            self._open_operational_activity_messages()

    def _show_awareness_topic_menu(self, row: int) -> None:
        try:
            context = dict(self._awareness_row_contexts[int(row)] or {})
        except Exception:
            context = {}
        if not context.get("source_ref"):
            return
        menu = QMenu(self)
        presets = (
            ("Mark Social", ("Social",), False),
            ("Mark Comms", ("Comms",), False),
            ("Mark Weather", ("Weather",), True),
        )
        for label, topics, attention in presets:
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, c=dict(context), t=tuple(topics), a=attention: self._override_awareness_topics(c, t, operator_attention=a)
            )
        menu.addSeparator()
        custom = menu.addAction("Change Topic...")
        custom.triggered.connect(lambda _checked=False, c=dict(context): self._prompt_awareness_topic_override(c))
        clear = menu.addAction("Clear Override")
        clear.triggered.connect(lambda _checked=False, c=dict(context): self._clear_awareness_topic_override(c))
        table = getattr(self, "awareness_table", None)
        if table is not None:
            widget = table.cellWidget(max(0, int(row or 0)), 3)
            if widget is not None:
                menu.exec(widget.mapToGlobal(widget.rect().bottomLeft()))
                return
        menu.exec(self.mapToGlobal(self.rect().center()))

    def _prompt_awareness_topic_override(self, context: Dict[str, str]) -> None:
        text, ok = QInputDialog.getText(
            self,
            "Change Traffic Topic",
            "Topic:",
            text=str(context.get("topic_filter") or "Social"),
        )
        if not ok:
            return
        topic = str(text or "").strip()
        if topic:
            self._override_awareness_topics(context, (topic,), operator_attention=topic.lower() != "social")

    def _override_awareness_topics(
        self,
        context: Dict[str, str],
        topics: Sequence[object],
        *,
        operator_attention: bool,
    ) -> None:
        source_ref = str(context.get("source_ref") or "").strip()
        source_family = str(context.get("source_family") or "").strip()
        if not source_ref:
            return
        if source_ref.startswith("mesh:"):
            try:
                set_mesh_message_topic_override(
                    default_mesh_db_path(),
                    source_ref,
                    topics,
                    operator_attention=operator_attention,
                )
            except Exception as exc:
                log.debug("ControlFreq: failed to store mesh topic override: %s", exc)
        self._update_observation_topic_override(
            source_family,
            source_ref,
            topics,
            operator_attention=operator_attention,
            override_active=True,
        )

    def _clear_awareness_topic_override(self, context: Dict[str, str]) -> None:
        source_ref = str(context.get("source_ref") or "").strip()
        source_family = str(context.get("source_family") or "").strip()
        if not source_ref:
            return
        if source_ref.startswith("mesh:"):
            try:
                clear_mesh_message_topic_override(default_mesh_db_path(), source_ref)
            except Exception as exc:
                log.debug("ControlFreq: failed to clear mesh topic override: %s", exc)
        self._operational_snapshot_cache_key = ()
        self._refresh_activity()

    def _update_observation_topic_override(
        self,
        source_family: str,
        source_ref: str,
        topics: Sequence[object],
        *,
        operator_attention: bool,
        override_active: bool,
    ) -> None:
        db_path = default_mesh_db_path() if source_ref.startswith("mesh:") else settings_db_path()
        candidates = []
        families = [source_family] if source_family else ["meshcore", "meshtastic"]
        for family in families:
            try:
                candidates.extend(list_observations(db_path, source_family=family, limit=1000))
            except Exception as exc:
                log.debug("ControlFreq: failed to load observations for topic override: %s", exc)
        normalized_topics = tuple(str(topic or "").strip() for topic in topics if str(topic or "").strip())
        for observation in candidates:
            if str(observation.source_ref or "").strip() != source_ref:
                continue
            provenance = dict(observation.provenance or {})
            if override_active:
                provenance["topic_override"] = {
                    "topics": list(normalized_topics),
                    "operator_attention": bool(operator_attention),
                }
            else:
                provenance.pop("topic_override", None)
            try:
                upsert_observation(
                    db_path,
                    replace(
                        observation,
                        observed_topics=normalized_topics,
                        operator_attention=operator_attention,
                        provenance=provenance,
                    ),
                )
            except Exception as exc:
                log.debug("ControlFreq: failed to update observation topic override: %s", exc)
        self._awareness_topic_filter = ""
        self._operational_snapshot_cache_key = ()
        self._refresh_activity()

    def _set_awareness_support_text(self, snapshot: object | None) -> None:
        topics = tuple(getattr(snapshot, "topic_rollups", ()) or ()) if snapshot is not None else ()
        topic_chip_defs: List[Tuple[str, Dict[str, str], str]] = []
        for rollup in topics[:5]:
            label = str(getattr(rollup, "topic", "") or "").strip()
            if not label:
                continue
            count = int(getattr(rollup, "count", 0) or 0)
            geo = str(getattr(rollup, "geography_hint", "") or "").strip()
            text = f"{label} {count}"
            topic_chip_defs.append(
                (
                    text,
                    {
                        "topic_filter": label,
                        "awareness_topic_chip": "1",
                    },
                    f"Focus Operational Awareness on {label}" + (f" near {geo}." if geo else "."),
                )
            )
        pins = tuple(getattr(snapshot, "pins", ()) or ()) if snapshot is not None else ()
        pin_chip_defs: List[Tuple[str, Dict[str, str], str]] = []
        for pin in pins[:4]:
            label = str(getattr(pin, "label", "") or getattr(pin, "value", "") or "").strip()
            if not label:
                continue
            pin_type = str(getattr(pin, "pin_type", "") or "").strip()
            count = int(getattr(pin, "matched_count", 0) or 0)
            key = {
                "topic": "topic_filter",
                "callsign": "callsign",
                "group": "group_filter",
            }.get(pin_type, "search_query")
            pin_chip_defs.append(
                (
                    f"{label} {count}",
                    {
                        key: str(getattr(pin, "value", "") or label).strip(),
                        "search_query": str(getattr(pin, "value", "") or label).strip(),
                    },
                    f"Focus on pinned {pin_type or 'item'} {label}.",
                )
        )
        more = tuple(getattr(snapshot, "more_traffic", ()) or ()) if snapshot is not None else ()
        more_text = self._format_more_traffic_strip(more)
        self._set_awareness_chips(self.awareness_topic_chip_layout, self.awareness_topic_chip_container, topic_chip_defs)
        self._set_awareness_chips(self.awareness_pin_chip_layout, self.awareness_pin_chip_container, pin_chip_defs)
        if hasattr(self, "awareness_pins_label"):
            self.awareness_pins_label.setText(
                "   ".join(
                    part
                    for part in (
                        "Topics" if topic_chip_defs else "",
                        "Pinned" if pin_chip_defs else "",
                    )
                    if part
                )
            )
            self.awareness_pins_label.setVisible(bool(topic_chip_defs or pin_chip_defs))
        if hasattr(self, "more_traffic_label"):
            self.more_traffic_label.setText(more_text)
            self.more_traffic_label.setVisible(bool(more_text))
        if hasattr(self, "operational_clear_pins_btn"):
            self.operational_clear_pins_btn.setEnabled(bool(pins))

    @staticmethod
    def _format_more_traffic_strip(items: Sequence[object]) -> str:
        if not items:
            return ""
        bits: List[str] = []
        for item in tuple(items)[:3]:
            callsign = str(getattr(item, "callsign", "") or "").strip()
            subject = str(getattr(item, "subject", "") or "").strip()
            topic = ", ".join(tuple(getattr(item, "topics", ()) or ())[:1])
            label = " ".join(part for part in (callsign, subject or topic) if part).strip()
            if label:
                bits.append(label)
        prefix = f"More Traffic: {len(items)}"
        return prefix + (f" | {'; '.join(bits)}" if bits else "")

    def _set_awareness_chips(
        self,
        layout: QHBoxLayout,
        container: QWidget,
        chip_defs: Sequence[Tuple[str, Dict[str, str], str]],
    ) -> None:
        self._clear_awareness_chip_layout(layout)
        try:
            theme = self._theme()
        except Exception:
            theme = {}
        for text, context, tooltip in chip_defs:
            chip = QPushButton(str(text or "").strip())
            chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            chip_h = button_height_for_font(chip, floor=28)
            chip.setMinimumHeight(chip_h)
            chip.setMaximumHeight(chip_h)
            chip.setMaximumWidth(170)
            chip.setToolTip(str(tooltip or "").strip())
            chip.clicked.connect(lambda _checked=False, ctx=dict(context): self._set_awareness_focus_context(ctx))
            chip.setStyleSheet(button_style("primary" if self._awareness_context_matches(context) else "secondary", theme))
            layout.addWidget(chip)
        layout.addStretch(1)
        container.setVisible(bool(chip_defs))

    @staticmethod
    def _clear_awareness_chip_layout(layout: QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _set_awareness_focus_context(self, context: Dict[str, str]) -> None:
        raw_context = dict(context or {})
        topic_chip = str(raw_context.get("awareness_topic_chip") or "").strip()
        topic_filter = str(raw_context.get("topic_filter") or "").strip()
        if topic_chip and topic_filter:
            if str(getattr(self, "_awareness_topic_filter", "") or "").strip().lower() == topic_filter.lower():
                self._awareness_topic_filter = ""
                self._operational_awareness_context = {}
            else:
                self._awareness_topic_filter = topic_filter
                self._operational_awareness_context = {
                    "topic_filter": topic_filter,
                    "awareness_topic_chip": "1",
                }
            snapshot = getattr(self, "_operational_awareness_full_snapshot", None)
            if snapshot is not None:
                self._set_operational_awareness_snapshot(snapshot)
            self._update_clear_filters_style()
            return
        self._operational_awareness_context = {
            str(key): str(value)
            for key, value in dict(context or {}).items()
            if str(value or "").strip()
        }
        self._awareness_topic_filter = ""
        search = (
            self._operational_awareness_context.get("topic_filter")
            or self._operational_awareness_context.get("callsign")
            or self._operational_awareness_context.get("search_query")
            or ""
        ).strip()
        if search and hasattr(self, "search_edit"):
            previous = self.search_edit.blockSignals(True)
            try:
                self.search_edit.setText(search)
            finally:
                self.search_edit.blockSignals(previous)
        table = getattr(self, "awareness_table", None)
        if table is not None:
            table.clearSelection()
            table.setCurrentCell(-1, -1)
        if hasattr(self, "awareness_recommend_label") and search:
            text = f"Focused: {search} | use Inbox, Reply, or Map from matching traffic."
            self.awareness_recommend_label.setText(text)
            self.awareness_recommend_label.setToolTip(text)
        self._update_clear_filters_style()
        self._run_filter_refresh()

    def _awareness_context_matches(self, context: Dict[str, str]) -> bool:
        if str(dict(context or {}).get("awareness_topic_chip") or "").strip():
            active_topic = str(getattr(self, "_awareness_topic_filter", "") or "").strip().lower()
            context_topic = str(dict(context or {}).get("topic_filter") or "").strip().lower()
            return bool(active_topic and active_topic == context_topic)
        active = getattr(self, "_operational_awareness_context", {}) or {}
        if not active:
            return False
        for key, value in dict(context or {}).items():
            value = str(value or "").strip()
            if value and str(active.get(str(key), "") or "").strip().lower() == value.lower():
                return True
        return False

    def _set_operational_activity_text(self, headline: str, topics: str) -> None:
        self.operational_activity_label.setText(str(headline or "").strip() or "Recent Traffic: none in the selected window")
        self.operational_topics_label.setText(str(topics or "").strip())
        self.operational_topics_label.setVisible(False)
        self._sync_operational_action_buttons()

    def _sync_operational_action_buttons(self) -> None:
        context = self._selected_awareness_context() or dict(getattr(self, "_operational_activity_context", {}) or {})
        source_family = str(context.get("source_family") or "").strip()
        contract = source_contract_for(source_family)
        available = bool(context)
        button_rules = (
            (
                getattr(self, "operational_messages_btn", None),
                contract.actions.read,
                "Open the Messages Inbox with this activity context.",
                f"Messages is not available for {contract.display_name}.",
            ),
            (
                getattr(self, "operational_map_btn", None),
                contract.actions.map,
                "Open the Map filtered to this activity context.",
                f"Map is not available for {contract.display_name}.",
            ),
            (
                getattr(self, "operational_compose_btn", None),
                contract.actions.reply or contract.actions.compose,
                "Open Compose using the selected traffic context.",
                f"Compose is not available for {contract.display_name}.",
            ),
            (
                getattr(self, "operational_pin_btn", None),
                contract.actions.pin,
                "Keep this topic, callsign, or group visible in Operational Awareness.",
                f"Pin is not available for {contract.display_name}.",
            ),
        )
        for button, allowed, enabled_tip, disabled_tip in button_rules:
            if button is None:
                continue
            enabled = bool(available and allowed)
            button.setEnabled(enabled)
            button.setToolTip(enabled_tip if enabled else disabled_tip if available else enabled_tip)
        actions_widget = getattr(self, "activity_actions_widget", None)
        if actions_widget is not None:
            buttons = [rule[0] for rule in button_rules]
            buttons.append(getattr(self, "operational_clear_pins_btn", None))
            actions_widget.setVisible(any(bool(button and button.isEnabled()) for button in buttons))

    @staticmethod
    def _activity_context_from_snapshot(snapshot: object, group_filter: str = "") -> Dict[str, str]:
        latest = tuple(getattr(snapshot, "latest", ()) or ())
        attention = tuple(getattr(snapshot, "high_attention", ()) or ())
        alerts = tuple(getattr(snapshot, "condition_alerts", ()) or ())
        observations = alerts or attention or latest
        topics = [
            str(topic or "").strip()
            for topic in tuple(getattr(snapshot, "topics", ()) or ())
            if str(topic or "").strip()
        ]
        group = normalize_group_name(str(group_filter or ""))
        source_family = ""
        if observations:
            first = observations[0]
            source_family = str(getattr(first, "source_family", "") or "").strip().lower()
            if not group:
                group = normalize_group_name(getattr(first, "to_target", "") or "")
        topic = topics[0] if topics else ""
        if not (group or topic or source_family):
            return {}
        return {
            "group_filter": group,
            "topic_filter": topic,
            "source_family": source_family,
            "search_query": " ".join(part for part in (group, topic) if part),
        }

    def _open_operational_activity_map(self) -> None:
        context = self._selected_awareness_context() or dict(getattr(self, "_operational_activity_context", {}) or {})
        if not context:
            return
        map_context = map_context_from_mapping(context)
        host = self.window()
        source_family = map_context.source_family
        if source_family == "local_report" and hasattr(host, "open_local_reports_map_context"):
            host.open_local_reports_map_context(**map_context.as_map_kwargs())
        elif source_family == "local_report" and hasattr(host, "open_local_reports_map"):
            try:
                host.open_local_reports_map(**map_context.as_map_kwargs())
            except TypeError:
                host.open_local_reports_map()
        elif hasattr(host, "open_spotter_map"):
            host.open_spotter_map(**map_context.as_map_kwargs())

    def _open_operational_activity_messages(self) -> None:
        context = self._selected_awareness_context() or dict(getattr(self, "_operational_activity_context", {}) or {})
        map_context = map_context_from_mapping(context)
        host = self.window()
        source_family = map_context.source_family
        if source_family == "local_report" and hasattr(host, "open_local_reports"):
            host.open_local_reports(
                topic_filter=map_context.topic_filter,
                query=map_context.query_filter or map_context.group_filter,
            )
            return
        if hasattr(host, "open_messages_section"):
            host.open_messages_section("inbox", **map_context.as_messages_kwargs())

    def _open_operational_activity_compose(self) -> None:
        context = self._selected_awareness_context() or dict(getattr(self, "_operational_activity_context", {}) or {})
        map_context = map_context_from_mapping(context)
        intent = compose_intent_from_map_context(
            map_context,
            mode=str(context.get("compose_mode") or context.get("source_family") or ""),
        ).as_dict()
        host = self.window()
        if hasattr(host, "open_messages_section"):
            host.open_messages_section("compose", compose_intent=intent)

    def _pin_selected_awareness_focus(self) -> None:
        context = self._selected_awareness_context() or dict(getattr(self, "_operational_activity_context", {}) or {})
        pin_type = ""
        value = ""
        label = ""
        for candidate_type, key in (("topic", "topic_filter"), ("callsign", "callsign"), ("group", "group_filter")):
            candidate = str(context.get(key) or "").strip()
            if candidate:
                pin_type = candidate_type
                value = candidate
                label = candidate
                break
        if not pin_type or not value:
            return
        pins = [dict(pin) for pin in self._configured_awareness_pins()]
        identity = (pin_type, value.upper())
        if any((str(pin.get("type") or pin.get("pin_type") or "").strip().lower(), str(pin.get("value") or "").strip().upper()) == identity for pin in pins):
            return
        pins.append({"type": pin_type, "value": value, "label": label})
        try:
            self.settings.set("controlfreq_awareness_pins", pins)
        except Exception as e:
            log.debug("ControlFreq: failed to save awareness pin: %s", e)
        self._operational_snapshot_cache_key = ()
        self._refresh_activity()

    def _clear_awareness_pins(self) -> None:
        try:
            self.settings.set("controlfreq_awareness_pins", [])
        except Exception as e:
            log.debug("ControlFreq: failed to clear awareness pins: %s", e)
        self._operational_snapshot_cache_key = ()
        self._refresh_activity()

    def _selected_awareness_context(self) -> Dict[str, str]:
        table = getattr(self, "awareness_table", None)
        contexts = list(getattr(self, "_awareness_row_contexts", []) or [])
        if table is not None and contexts:
            row = int(table.currentRow())
            if 0 <= row < len(contexts) and contexts[row]:
                return dict(contexts[row])
        return dict(getattr(self, "_operational_awareness_context", {}) or {})

    def _format_operational_activity_headline(self, snapshot: object, search: str = "") -> str:
        latest = tuple(getattr(snapshot, "latest", ()) or ())
        alerts = tuple(getattr(snapshot, "condition_alerts", ()) or ())
        attention = tuple(getattr(snapshot, "high_attention", ()) or ())
        if not latest:
            return "Recent Traffic: none in the selected window"
        if alerts:
            first = alerts[0]
            level = str(getattr(first, "status", "") or getattr(first, "subject", "") or "Condition Alert").strip()
            sender = str(getattr(first, "from_call", "") or "").strip().upper()
            target = normalize_group_name(getattr(first, "to_target", "") or "")
            route = " -> ".join(part for part in (sender, target) if part)
            return f"Condition Alert: {level}" + (f" | {route}" if route else "")
        first = attention[0] if attention else latest[0]
        family = source_family_label(str(getattr(first, "source_family", "") or "traffic"))
        subject = str(getattr(first, "subject", "") or getattr(first, "summary", "") or "").strip()
        sender = str(getattr(first, "from_call", "") or "").strip().upper()
        target = normalize_group_name(getattr(first, "to_target", "") or "")
        route = " -> ".join(part for part in (sender, target) if part)
        count = len(attention) if attention else len(latest)
        prefix = f"Recent Traffic: {len(latest)} recent"
        if attention:
            prefix += f" | {count} need attention"
        if subject:
            return f"{prefix} | {family}: {subject}" + (f" | {route}" if route else "")
        return f"{prefix} | {family}" + (f" | {route}" if route else "")

    def _format_condition_sop_decision(self, snapshot: object, *, profiles=None) -> str:
        alerts = tuple(getattr(snapshot, "condition_alerts", ()) or ())
        if not alerts:
            return ""
        if profiles is None:
            profiles = self._condition_sop_profiles()
        decisions = evaluate_condition_sop_invocations(
            alerts[:3],
            sop_profiles=profiles,
            auto_apply_enabled=False,
        )
        meaningful = [decision for decision in decisions if decision.operating_group and decision.condition_level is not None]
        if not meaningful:
            return "No matching SOP layer"
        decision = next((item for item in meaningful if not item.blocked), meaningful[0])
        name = str(decision.sop_profile_name or "matching SOP").strip()
        level = f"L{decision.condition_level}" if decision.condition_level is not None else ""
        target = " ".join(part for part in (decision.operating_group, level) if part)
        if decision.blocked:
            reason = str((decision.reasons or ("blocked",))[0])
            return f"Blocked {target}: {reason}" if target else f"Blocked: {reason}"
        if decision.decision == "suggest":
            return f"Suggested {target}: {name}" if target else f"Suggested: {name}"
        if decision.decision == "apply":
            return f"Ready {target}: {name}" if target else f"Ready: {name}"
        return f"Review {target}: {name}" if target else f"Review: {name}"

    def _condition_sop_profiles(self) -> List[Dict[str, Any]]:
        return self._condition_sop_profiles_from_reader(self._sop_manager)

    @staticmethod
    def _read_condition_sop_profiles(db_path):
        if db_path is None:
            return []
        # Read only: startup owns schema creation and profile normalization.
        reader = SOPManager.__new__(SOPManager)
        reader.db_path = db_path
        return ControlFreqTab._condition_sop_profiles_from_reader(reader)

    @staticmethod
    def _condition_sop_profiles_from_reader(reader) -> List[Dict[str, Any]]:
        try:
            summaries = reader.list_profiles()
        except Exception as e:
            log.debug("ControlFreq: failed to list SOP profiles for condition alert: %s", e)
            return []
        profiles: List[Dict[str, Any]] = []
        for summary in summaries:
            try:
                profile_id = int((summary or {}).get("id") or 0)
            except Exception:
                profile_id = 0
            if profile_id <= 0:
                continue
            try:
                full = reader.get_profile(profile_id)
            except Exception as e:
                log.debug("ControlFreq: failed to load SOP profile %s for condition alert: %s", profile_id, e)
                continue
            if isinstance(full, dict) and full.get("schedule_layer"):
                profiles.append(full)
        return profiles

    @staticmethod
    def _format_operational_activity_topics(snapshot: object) -> str:
        topics = [
            str(topic or "").strip()
            for topic in tuple(getattr(snapshot, "topics", ()) or ())
            if str(topic or "").strip()
        ]
        if not topics:
            return ""
        shown = ", ".join(topics[:6])
        if len(topics) > 6:
            shown += f", +{len(topics) - 6} more"
        return f"Topics: {shown}"

    def _refresh_frequency_control(self, include_intersections: bool = True) -> None:
        # Avoid clobbering selection while user is interacting
        try:
            if (
                self.freq_combo.view().isVisible()
                or self.freq_combo.hasFocus()
                or self.hold_duration_combo.view().isVisible()
                or self.hold_duration_combo.hasFocus()
            ):
                return
        except Exception:
            pass
        og_list = load_operating_groups(self.settings)
        refresh_hold_duration_combo(self.hold_duration_combo, self.settings, self._runtime_hold_duration_profile())
        current = selected_qsy_meta(self.freq_combo)
        current_freq = None
        try:
            if current:
                current_freq = float(current.get("freq"))
        except Exception:
            current_freq = None
        combo_items: List[Tuple[str, Dict[str, Any], float]] = []
        combo_cache_rows: List[Tuple[str, str, float, str, str, bool]] = []
        for g in sorted(
            og_list,
            key=lambda x: (
                str(x.get("group", "")).upper(),
                str(x.get("band", "")).upper(),
                float(x.get("frequency", 0) or 0),
            ),
        ):
            try:
                freq_val = float(g.get("frequency", 0))
            except Exception:
                continue
            group_txt = str(g.get("group", "")).strip().upper()
            band_txt = str(g.get("band", "")).strip().upper()
            compact_parts = [f"{freq_val:.3f}"]
            if group_txt:
                compact_parts.append(group_txt)
            if band_txt:
                compact_parts.append(band_txt)
            label = "  ".join(compact_parts)
            meta = {
                "freq": freq_val,
                "mode": g.get("mode", ""),
                "band": g.get("band", ""),
                "auto_tune": bool(g.get("auto_tune", False)),
                "vfo": (g.get("vfo") or "").strip().upper(),
                "group": group_txt,
            }
            combo_items.append((label.strip(), meta, freq_val))
            combo_cache_rows.append(
                (
                    group_txt,
                    band_txt,
                    round(float(freq_val), 6),
                    str(meta.get("mode") or "").strip().upper(),
                    str(meta.get("vfo") or "").strip().upper(),
                    bool(meta.get("auto_tune")),
                )
            )
        combo_key: Tuple[Tuple[str, str, float, str, str, bool], ...] = tuple(combo_cache_rows)
        combo_rebuilt = combo_key != self._freq_combo_cache_key
        if combo_rebuilt:
            self.freq_combo.blockSignals(True)
            self.freq_combo.clear()
            self.freq_combo.addItem("Select frequency", None)
            for label, meta, _freq_val in combo_items:
                self.freq_combo.addItem(label, meta)
            self.freq_combo.blockSignals(False)
            self._freq_combo_cache_key = combo_key

        restore_idx = -1
        if current_freq is not None:
            for idx in range(1, self.freq_combo.count()):
                meta = self.freq_combo.itemData(idx)
                try:
                    freq_val = float((meta or {}).get("freq"))
                except Exception:
                    continue
                if abs(freq_val - current_freq) < 0.0005:
                    restore_idx = idx
                    break
        force_resync = bool(self._force_hero_resync)
        status_snapshot: Optional[Dict[str, Any]] = None
        try:
            sched_obj = getattr(self.window(), "scheduler", None)
            if sched_obj and hasattr(sched_obj, "get_status_summary"):
                status_snapshot = sched_obj.get_status_summary()
        except Exception:
            status_snapshot = None
        sched_freq = current_scheduler_freq(self.window())
        scheduler_freq_changed = False
        prev_sched_freq = self._last_hero_sched_freq_mhz
        if sched_freq is None:
            scheduler_freq_changed = prev_sched_freq is not None
            self._last_hero_sched_freq_mhz = None
        else:
            try:
                sched_freq_f = float(sched_freq)
            except Exception:
                sched_freq_f = None
            if sched_freq_f is not None:
                scheduler_freq_changed = prev_sched_freq is None or abs(prev_sched_freq - sched_freq_f) > 0.0005
                self._last_hero_sched_freq_mhz = sched_freq_f
        if scheduler_freq_changed:
            force_resync = True
        sched_group = self._get_scheduled_group_name()
        active_freq = self._get_active_frequency_mhz(status_snapshot)
        display_freq = active_freq if active_freq is not None else sched_freq
        if (
            scheduler_freq_changed
            and sched_freq is not None
            and active_freq is not None
            and prev_sched_freq is not None
            and abs(active_freq - prev_sched_freq) < 0.0005
        ):
            # During a scheduler transition, a cached active-frequency poll may
            # still briefly report the old scheduled value. Prefer the new
            # scheduled target only for that narrow stale-poll case.
            display_freq = sched_freq
        current_matches_display = (
            current_freq is not None
            and display_freq is not None
            and abs(current_freq - float(display_freq)) < 0.0005
        )
        selection_is_last_hero = (
            current_freq is not None
            and self._last_hero_display_freq_mhz is not None
            and abs(current_freq - float(self._last_hero_display_freq_mhz)) < 0.0005
        )
        pending_user_selection = (
            not force_resync
            and current_freq is not None
            and not current_matches_display
            and not selection_is_last_hero
        )
        if pending_user_selection and restore_idx >= 0:
            self.freq_combo.blockSignals(True)
            self.freq_combo.setCurrentIndex(restore_idx)
            self.freq_combo.blockSignals(False)
        elif display_freq is not None:
            for idx in range(1, self.freq_combo.count()):
                meta = self.freq_combo.itemData(idx)
                try:
                    freq_val = float((meta or {}).get("freq"))
                except Exception:
                    continue
                if abs(freq_val - float(display_freq)) < 0.0005:
                    self.freq_combo.blockSignals(True)
                    self.freq_combo.setCurrentIndex(idx)
                    self.freq_combo.blockSignals(False)
                    self._last_hero_display_freq_mhz = float(display_freq)
                    break
        elif not pending_user_selection:
            self._last_hero_display_freq_mhz = None
        self._force_hero_resync = False
        sched_txt = f"{sched_freq:.3f}" if sched_freq is not None else "--"
        band_txt = self._band_for_group_freq(sched_group, sched_freq) if sched_freq is not None else "--"
        group_txt = sched_group or "--"
        line1 = f"Scheduled: {group_txt} | {band_txt} - {sched_txt}"
        self._freq_meta_full_text = line1
        self._apply_freq_meta_text()
        self._update_frequency_action_styles(sched_freq, active_freq)
        self._update_active_label_style(sched_freq, active_freq)
        self._refresh_scheduler_strip(status_snapshot)
        if include_intersections:
            self._refresh_intersections()

    def _on_peer_filters_changed(self, *_args) -> None:
        self._refresh_intersections()
        self._update_clear_filters_style()

    def _refresh_intersections(self) -> None:
        if not self._dashboard_card_enabled("intersections"):
            return
        self._ensure_operator_roster_read()
        roster = getattr(self, "_operator_roster_snapshot", None)
        if roster is None:
            failure = bool(getattr(self, "_operator_roster_error", ""))
            status = "Operator roster unavailable · retrying" if failure else "Checking operator roster…"
            self.peer_summary_label.setText(status)
            self.peer_summary_label.setToolTip(
                "Peer overlap results need the operator roster and schedules. "
                "No overlap conclusion is available until that snapshot is published."
            )
            if hasattr(self, "dashboard"):
                self.dashboard.peer_link.setText("Operators · checking roster")
                self.dashboard.peer_link.setToolTip(status)
            self._refresh_peer_finder_rows([], empty_message=status)
            self._fit_group_box_to_contents(self.intersection_box)
            self._set_schedule_splitter_content_sizes()
            return
        group_filter = normalize_group_name(self.group_combo.currentData())
        search = (self.search_edit.text() or "").strip().upper()
        intersection_combo = getattr(self, "intersection_window_combo", self.activity_window_combo)
        horizon_minutes = int(intersection_combo.currentData() or 120)
        operator_meta = {
            key: {**value, "groups": set(value.get("groups") or ())}
            for key, value in roster.peer_meta.items()
        }
        peer_rows = [dict(row) for row in roster.peer_schedule_rows]
        my_entries = self._load_my_schedule_entries(schedule_rows=roster.my_schedule_rows)
        self._refresh_peer_filter_options(operator_meta, peer_rows)
        rows = self._compute_peer_finder_rows(
            group_filter,
            search,
            horizon_minutes=horizon_minutes,
            peer_callsign=(self.peer_callsign_filter.text() or "").strip().upper(),
            peer_group=normalize_group_name(self.peer_group_filter.currentData()),
            peer_region=str(self.peer_region_filter.currentData() or "").strip().upper(),
            peer_role=str(self.peer_role_filter.currentData() or "").strip().upper(),
            operator_meta=operator_meta,
            operator_groups=roster.group_map,
            peer_rows=peer_rows,
            my_entries=my_entries,
        )
        now_count = sum(
            1
            for row in rows
            if any(int(window.get("start_offset_minutes") or 0) <= 0 for window in tuple(row.get("windows") or ()))
        )
        next_count = sum(
            1
            for row in rows
            if any(int(window.get("start_offset_minutes") or 0) > 0 for window in tuple(row.get("windows") or ()))
        )
        stale = time.monotonic() - float(self._operator_roster_published_at) >= self._operator_roster_ttl_sec
        prefix = "Roster may be out of date · " if stale else ""
        summary = f"{prefix}Now {now_count} · Next {self._format_window_label(horizon_minutes)} {next_count}"
        self.peer_summary_label.setText(summary)
        if hasattr(self, "dashboard"):
            self.dashboard.peer_link.setText(f"Operators · {now_count} now / {next_count} next")
            self.dashboard.peer_link.setToolTip(summary + " · exact-frequency schedule overlaps, not verified contact")
        self.peer_summary_label.setToolTip(
            f"Distinct operators with an exact-frequency overlap: {summary}. "
            "One operator may have both a current and later window."
        )
        self._refresh_peer_finder_rows(
            rows,
            horizon_minutes=horizon_minutes,
            empty_message=("No peer overlap in the next " + self._format_window_label(horizon_minutes)
                          + (" · roster may be out of date" if stale else "")),
        )
        self._fit_group_box_to_contents(self.intersection_box)
        self._set_schedule_splitter_content_sizes()

    def _refresh_peer_finder_rows(
        self,
        rows: Sequence[Dict[str, object]],
        *,
        horizon_minutes: int = 120,
        empty_message: Optional[str] = None,
    ) -> None:
        table = getattr(self, "peer_chart_table", None)
        if not isinstance(table, QTableWidget):
            return
        self._peer_finder_contexts = [dict(row.get("context") or {}) for row in rows]
        old_scroll = int(table.verticalScrollBar().value())
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.clearSpans()
            table.setRowCount(0)
            if not rows:
                table.insertRow(0)
                empty = QTableWidgetItem(
                    empty_message or f"No peer overlap in the next {self._format_window_label(horizon_minutes)}"
                )
                empty.setFlags(empty.flags() ^ Qt.ItemIsEditable)
                table.setItem(0, 0, empty)
                table.setSpan(0, 0, 1, 3)
                self._peer_finder_contexts = []
            else:
                for row_idx, row in enumerate(rows):
                    table.insertRow(row_idx)
                    peer = str(row.get("peer") or "-")
                    groups = tuple(str(group) for group in tuple(row.get("groups") or ()) if str(group))
                    meta_parts = [
                        str(row.get("role") or "").strip(),
                        str(row.get("region") or "").strip(),
                        "/".join(groups[:2]),
                    ]
                    operator_text = peer
                    meta_text = " · ".join(part for part in meta_parts if part)
                    if meta_text:
                        operator_text += f"\n{meta_text}"
                    operator_item = QTableWidgetItem(operator_text)
                    operator_item.setFlags(operator_item.flags() ^ Qt.ItemIsEditable)
                    operator_item.setToolTip(operator_text.replace("\n", " · "))
                    table.setItem(row_idx, 0, operator_item)

                    windows = tuple(row.get("windows") or ())
                    window_labels = [
                        f"{window.get('when') or 'Now'} · {window.get('net_band') or '-'}"
                        for window in windows
                    ]
                    timeline_text = "  •  ".join(window_labels[:3])
                    if len(window_labels) > 3:
                        timeline_text += f"  •  +{len(window_labels) - 3} more"
                    timeline_item = QTableWidgetItem(timeline_text)
                    timeline_item.setFlags(timeline_item.flags() ^ Qt.ItemIsEditable)
                    timeline_item.setData(PEER_TIMELINE_WINDOWS_ROLE, windows)
                    timeline_item.setData(PEER_TIMELINE_HORIZON_ROLE, int(horizon_minutes))
                    timeline_item.setToolTip("\n".join(window_labels))
                    table.setItem(row_idx, 1, timeline_item)

                    action_item = QTableWidgetItem("⋯")
                    action_item.setFlags(action_item.flags() ^ Qt.ItemIsEditable)
                    action_item.setTextAlignment(Qt.AlignCenter)
                    action_item.setToolTip("Message, map, or pin this operator")
                    table.setItem(row_idx, 2, action_item)
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
        table.resizeRowsToContents()
        self._fit_table_height_to_rows(table, min_rows=1, max_rows=6, empty_rows=1)
        table.verticalScrollBar().setValue(min(old_scroll, table.verticalScrollBar().maximum()))
        table.viewport().update()
        count = len(rows)
        self.peer_result_label.setText(f"{count} operator{'s' if count != 1 else ''}")

    def _activate_peer_chart_item(self, item: QTableWidgetItem) -> None:
        if item is None or item.column() != 2 or item.row() >= len(self._peer_finder_contexts):
            return
        rect = self.peer_chart_table.visualItemRect(item)
        self._show_peer_finder_actions(item.row(), self.peer_chart_table.viewport().mapToGlobal(rect.bottomLeft()))

    def _show_peer_finder_context_menu(self, pos) -> None:
        item = self.peer_chart_table.itemAt(pos)
        if item is None or item.row() >= len(self._peer_finder_contexts):
            return
        self._show_peer_finder_actions(item.row(), self.peer_chart_table.viewport().mapToGlobal(pos))

    def _show_peer_finder_actions(self, row: int, global_pos) -> None:
        menu = QMenu(self.peer_chart_table)
        menu.addAction("Message", lambda: self._open_peer_finder_compose(row))
        menu.addAction("Show on Map", lambda: self._open_peer_finder_map(row))
        menu.addAction("Pin in Operational Awareness", lambda: self._pin_peer_finder_row(row))
        menu.exec(global_pos)

    def _peer_finder_context(self, row: int) -> Dict[str, str]:
        contexts = list(getattr(self, "_peer_finder_contexts", []) or [])
        if 0 <= int(row) < len(contexts):
            return dict(contexts[int(row)] or {})
        return {}

    def _open_peer_finder_compose(self, row: int) -> None:
        context = self._peer_finder_context(row)
        if not context:
            return
        intent = compose_intent_from_map_context(
            map_context_from_mapping(context),
            mode=str(context.get("compose_mode") or "js8call"),
        ).as_dict()
        if context.get("callsign"):
            intent["target_callsign"] = context.get("callsign")
        host = self.window()
        if hasattr(host, "open_messages_section"):
            host.open_messages_section("compose", compose_intent=intent)

    def _open_peer_finder_map(self, row: int) -> None:
        context = self._peer_finder_context(row)
        if not context:
            return
        host = self.window()
        if hasattr(host, "open_spotter_map"):
            host.open_spotter_map(**map_context_from_mapping(context).as_map_kwargs())

    def _pin_peer_finder_row(self, row: int) -> None:
        context = self._peer_finder_context(row)
        callsign = str(context.get("callsign") or "").strip().upper()
        if not callsign:
            return
        pins = [dict(pin) for pin in self._configured_awareness_pins()]
        identity = ("callsign", callsign)
        if any((str(pin.get("type") or pin.get("pin_type") or "").strip().lower(), str(pin.get("value") or "").strip().upper()) == identity for pin in pins):
            return
        pins.append({"type": "callsign", "value": callsign, "label": callsign})
        try:
            self.settings.set("controlfreq_awareness_pins", pins)
        except Exception as e:
            log.debug("ControlFreq: failed to save peer finder pin: %s", e)
        self._operational_snapshot_cache_key = ()
        self._refresh_activity()

    def _compute_peer_finder_rows(
        self,
        group_filter: str,
        search: str,
        *,
        horizon_minutes: int = 120,
        peer_callsign: str = "",
        peer_group: str = "",
        peer_region: str = "",
        peer_role: str = "",
        operator_meta: Optional[Dict[str, Dict[str, object]]] = None,
        operator_groups: Optional[Dict[str, Set[str]]] = None,
        peer_rows: Optional[Sequence[Dict[str, Any]]] = None,
        my_entries: Optional[Sequence[Dict[str, object]]] = None,
    ) -> List[Dict[str, object]]:
        now_utc = dt.datetime.now(dt.timezone.utc)
        now_min = now_utc.hour * 60 + now_utc.minute
        now_day_idx = (now_utc.weekday() + 1) % 7
        now_week_min = now_day_idx * 1440 + now_min
        horizon_minutes = max(1, int(horizon_minutes or 120))
        my_entries = list(my_entries) if my_entries is not None else self._load_my_schedule_entries()
        if not my_entries:
            return []
        operator_meta = operator_meta if operator_meta is not None else self._load_operator_peer_meta()
        operator_groups = operator_groups if operator_groups is not None else self._load_operator_group_map()
        peer_rows = peer_rows if peer_rows is not None else self._peer_schedule_rows()
        if not peer_rows:
            return []
        peer_callsign = str(peer_callsign or "").strip().upper()
        peer_group = normalize_group_name(peer_group)
        peer_region = str(peer_region or "").strip().upper()
        peer_role = str(peer_role or "").strip().upper()
        by_peer: Dict[str, Dict[str, object]] = {}
        seen_windows: Set[Tuple[str, int, int, str]] = set()
        for r in peer_rows:
            cs = str(r.get("owner_callsign") or "").strip().upper()
            if not cs:
                continue
            meta = dict(operator_meta.get(cs, {}) or {})
            groups = set(meta.get("groups") or operator_groups.get(cs, set()) or set())
            role = str(meta.get("role") or "").strip().upper()
            region = str(meta.get("region") or "").strip().upper()
            if group_filter and group_filter not in groups:
                continue
            if search and search not in cs and not any(search in g for g in groups):
                continue
            if peer_callsign and peer_callsign not in cs:
                continue
            if peer_group and peer_group not in groups:
                continue
            if peer_region and peer_region != region:
                continue
            if peer_role and peer_role != role:
                continue
            peer_start = self._parse_time_minutes(str(r.get("start_utc") or ""))
            peer_end = self._parse_time_minutes(str(r.get("end_utc") or ""))
            if peer_start is None or peer_end is None:
                continue
            peer_segments = self._expand_week_segments(str(r.get("day_utc") or "ALL"), peer_start, peer_end)
            if not peer_segments:
                continue
            peer_freq = self._parse_frequency_mhz(r.get("frequency"))
            if peer_freq is None:
                continue
            for entry in my_entries:
                if abs(float(entry["freq"]) - peer_freq) > 0.001:
                    continue
                overlaps = self._next_horizon_overlaps(
                    entry.get("segments", []),
                    peer_segments,
                    now_week_min=now_week_min,
                    horizon_minutes=horizon_minutes,
                )
                if not overlaps:
                    continue
                start_abs = min(start for start, _end in overlaps)
                end_abs = max(end for start, end in overlaps if start == start_abs)
                when = self._format_peer_overlap_when(start_abs, now_week_min=now_week_min, now_utc=now_utc)
                net_band = self._format_group_band_freq_label(entry)
                start_offset = max(0, int(start_abs - now_week_min))
                end_offset = max(start_offset + 1, int(end_abs - now_week_min))
                key = (cs, start_offset, end_offset, net_band)
                if key in seen_windows:
                    continue
                seen_windows.add(key)
                group = str(entry.get("group") or next(iter(groups), "") or "").strip().upper()
                peer = by_peer.setdefault(
                    cs,
                    {
                        "peer": cs,
                        "groups": tuple(sorted(groups)),
                        "role": role,
                        "region": region,
                        "windows": [],
                        "sort": (9, 10**9, cs),
                        "context": {
                            "callsign": cs,
                            "group_filter": group,
                            "source_family": "js8",
                            "compose_mode": "js8call",
                            "search_query": cs,
                        },
                    },
                )
                windows = peer["windows"]
                if isinstance(windows, list):
                    windows.append(
                        {
                            "when": when,
                            "net_band": net_band,
                            "start_offset_minutes": start_offset,
                            "end_offset_minutes": end_offset,
                        }
                    )
                candidate_sort = (0 if start_offset <= 0 else 1, start_offset, cs)
                if candidate_sort < tuple(peer.get("sort") or (9, 10**9, cs)):
                    peer["sort"] = candidate_sort
        rows = list(by_peer.values())
        for row in rows:
            windows = row.get("windows")
            if isinstance(windows, list):
                windows.sort(
                    key=lambda window: (
                        int(window.get("start_offset_minutes") or 0),
                        str(window.get("net_band") or ""),
                    )
                )
        rows.sort(key=lambda row: row.get("sort", (9, 0, "")))
        return rows

    def _format_peer_overlap_when(
        self,
        start_abs: int,
        *,
        now_week_min: int,
        now_utc: dt.datetime,
    ) -> str:
        if int(start_abs) <= int(now_week_min):
            return "Now"
        week = 7 * 24 * 60
        delta_min = (int(start_abs) - int(now_week_min)) % week
        target = now_utc + dt.timedelta(minutes=delta_min)
        if self._show_local:
            try:
                target = target.astimezone(self._get_display_tz())
            except Exception:
                pass
        if delta_min < 24 * 60 and target.date() == dt.datetime.now(target.tzinfo).date():
            return target.strftime("%H:%M")
        return target.strftime("%a %H:%M")

    @staticmethod
    def _format_window_label(minutes: int) -> str:
        minutes = max(1, int(minutes or 0))
        if minutes < 60:
            return f"{minutes} minutes"
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"{hours} hour" if hours == 1 else f"{hours} hours"
        return f"{minutes} minutes"

    def _format_group_band_freq_label(self, entry: Dict[str, object]) -> str:
        grp = (entry.get("group") or "--").strip().upper()
        band = (entry.get("band") or "--").strip().upper()
        freq = entry.get("freq")
        try:
            freq_txt = f"{float(freq):.3f} MHz"
        except Exception:
            freq_txt = "--"
        return f"{grp} {band} {freq_txt}"

    def _format_current_schedule_label(self) -> str:
        sched_freq = current_scheduler_freq(self.window())
        if sched_freq is None:
            return "--"
        sched_group = self._get_scheduled_group_name()
        band = self._band_for_group_freq(sched_group, sched_freq)
        grp = sched_group or "--"
        band_txt = band or "--"
        return f"{grp} {band_txt} {sched_freq:.3f} MHz"

    def _band_for_group_freq(self, group: str, freq: float) -> str:
        if not group:
            return ""
        ops = self.settings.get("operating_groups", []) or []
        resolution = resolve_operating_group_frequency(ops, group=group, frequency=freq)
        wanted_hz = normalize_frequency_hz(freq)
        bands = {
            str(row.get("band") or "").strip().upper()
            for row in resolution.candidates
            if wanted_hz is not None and normalize_frequency_hz(row.get("frequency")) == wanted_hz
        }
        return next(iter(bands)) if len(bands) == 1 else ""

    def _load_my_schedule_entries(
        self, schedule_rows: Optional[Sequence[Dict[str, Any]]] = None
    ) -> List[Dict[str, object]]:
        use_snapshot = schedule_rows is not None
        if not use_snapshot:
            snapshot = getattr(self, "_operator_roster_snapshot", None)
            if snapshot is not None and hasattr(self, "_operator_roster_generation"):
                schedule_rows = snapshot.my_schedule_rows
                use_snapshot = True
                self._ensure_operator_roster_read()
            elif hasattr(self, "_operator_roster_generation"):
                self._ensure_operator_roster_read()
                return []
        cache_key = (0.0, 0.0)
        if not use_snapshot:
            try:
                settings_mtime = (
                    float(self._settings_db_path().stat().st_mtime)
                    if self._settings_db_path().exists()
                    else 0.0
                )
            except Exception:
                settings_mtime = 0.0
            try:
                nets_mtime = float(self._db_path().stat().st_mtime) if self._db_path().exists() else 0.0
            except Exception:
                nets_mtime = 0.0
            cache_key = (settings_mtime, nets_mtime)
        now_ts = time.time()
        if not use_snapshot and (
            self._my_schedule_entries_cache
            and (now_ts - float(self._my_schedule_entries_cache_ts) < self._my_schedule_entries_cache_ttl_sec)
            and cache_key == self._my_schedule_entries_cache_key
        ):
            return self._my_schedule_entries_cache

        entries: List[Dict[str, object]] = []
        seen: Set[Tuple[str, float, str, str]] = set()

        def add_entry(day: str, start: str, end: str, band: str, freq_val, group: str) -> None:
            start_min = self._parse_time_minutes(start)
            end_min = self._parse_time_minutes(end)
            if start_min is None or end_min is None:
                return
            freq_num = self._parse_frequency_mhz(freq_val)
            if freq_num is None:
                return
            segments = self._expand_week_segments(day, start_min, end_min)
            if not segments:
                return
            group_val = (group or "").strip().upper()
            band_val = (band or "").strip().upper()
            key = (
                f"{day}|{start_min}|{end_min}",
                round(float(freq_num), 3),
                band_val,
                group_val,
            )
            if key in seen:
                return
            seen.add(key)
            entries.append(
                {
                    "start_min": start_min,
                    "end_min": end_min,
                    "freq": freq_num,
                    "band": band_val,
                    "group": group_val,
                    "segments": segments,
                }
            )

        def _read_rows(db_path: Path, table_name: str, cols: str) -> None:
            if not db_path.exists():
                return
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                has_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                ).fetchone()
                if not has_table:
                    conn.close()
                    return
                cur.execute(f"SELECT {cols} FROM {table_name}")
                rows = cur.fetchall()
                conn.close()
                for r in rows:
                    group_val = ""
                    try:
                        group_val = r["group_name"] or ""
                    except Exception:
                        group_val = ""
                    if not group_val:
                        try:
                            group_val = r["net_name"] or ""
                        except Exception:
                            group_val = ""
                    add_entry(
                        r["day_utc"],
                        r["start_utc"],
                        r["end_utc"],
                        r["band"],
                        r["frequency"],
                        group_val,
                    )
            except Exception as e:
                log.debug("ControlFreq: failed to load %s for overlaps: %s", table_name, e)

        if use_snapshot:
            for row in schedule_rows or ():
                group_val = str(row.get("group_name") or row.get("net_name") or "")
                add_entry(
                    str(row.get("day_utc") or ""),
                    str(row.get("start_utc") or ""),
                    str(row.get("end_utc") or ""),
                    str(row.get("band") or ""),
                    row.get("frequency"),
                    group_val,
                )
        else:
            _read_rows(
                self._settings_db_path(),
                "daily_schedule_tab",
                "day_utc, start_utc, end_utc, band, frequency, group_name",
            )
            _read_rows(
                self._db_path(),
                "daily_schedule_tab",
                "day_utc, start_utc, end_utc, band, frequency, group_name",
            )
            _read_rows(
                self._db_path(),
                "net_schedule_tab",
                "day_utc, start_utc, end_utc, band, frequency, group_name, net_name",
            )
            _read_rows(
                self._settings_db_path(),
                "net_schedule_tab",
                "day_utc, start_utc, end_utc, band, frequency, group_name, net_name",
            )
            self._my_schedule_entries_cache = entries
            self._my_schedule_entries_cache_ts = now_ts
            self._my_schedule_entries_cache_key = cache_key
        return entries

    @staticmethod
    def _day_to_index(day: str) -> Optional[int]:
        txt = (day or "").strip().lower()
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

    def _schedule_day_indices(self, day: str) -> List[int]:
        txt = (day or "ALL").strip().upper()
        if txt in {"", "ALL", "DAILY"}:
            return list(range(7))
        for delim in ("/", ";", "|"):
            txt = txt.replace(delim, ",")
        out: List[int] = []
        for part in txt.split(","):
            idx = self._day_to_index(part)
            if idx is not None and idx not in out:
                out.append(idx)
        return out

    def _expand_week_segments(self, day: str, start_min: int, end_min: int) -> List[Tuple[int, int, int]]:
        if start_min < 0 or start_min > 1439 or end_min < 0 or end_min > 1439:
            return []
        if start_min == end_min:
            return []
        day_indices = self._schedule_day_indices(day)
        if not day_indices:
            return []
        segments: List[Tuple[int, int, int]] = []
        for day_idx in day_indices:
            if start_min < end_min:
                segments.append((day_idx, start_min, end_min))
                continue
            segments.append((day_idx, start_min, 24 * 60))
            segments.append(((day_idx + 1) % 7, 0, end_min))
        return segments

    @staticmethod
    def _next_horizon_overlaps(
        seg_a: List[Tuple[int, int, int]],
        seg_b: List[Tuple[int, int, int]],
        *,
        now_week_min: int,
        horizon_minutes: int,
    ) -> List[Tuple[int, int]]:
        if not seg_a or not seg_b:
            return []
        week = 7 * 24 * 60
        window_start = int(now_week_min)
        window_end = int(now_week_min + max(1, int(horizon_minutes)))

        def _absolute_segments(segments: List[Tuple[int, int, int]]) -> List[Tuple[int, int]]:
            out: List[Tuple[int, int]] = []
            for day_idx, start_min, end_min in segments:
                base_start = int(day_idx) * 1440 + int(start_min)
                base_end = int(day_idx) * 1440 + int(end_min)
                out.append((base_start, base_end))
                out.append((base_start + week, base_end + week))
            return out

        abs_a = _absolute_segments(seg_a)
        abs_b = _absolute_segments(seg_b)
        overlaps: List[Tuple[int, int]] = []
        for a_start, a_end in abs_a:
            if a_end <= window_start or a_start >= window_end:
                continue
            for b_start, b_end in abs_b:
                if b_end <= window_start or b_start >= window_end:
                    continue
                start = max(a_start, b_start, window_start)
                end = min(a_end, b_end, window_end)
                if end > start:
                    overlaps.append((start, end))
        if not overlaps:
            return overlaps
        overlaps.sort(key=lambda it: (it[0], it[1]))
        merged: List[Tuple[int, int]] = []
        for start, end in overlaps:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        return merged

    @staticmethod
    def _parse_frequency_mhz(value) -> Optional[float]:
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
    def _day_matches_today(day: str, today_name: str) -> bool:
        day = (day or "ALL").strip().upper()
        if day in ("ALL", "DAILY"):
            return True
        if not day:
            return False
        today = today_name.upper()
        return today.startswith(day[:3]) or day.startswith(today[:3])

    @staticmethod
    def _parse_time_minutes(value: str) -> Optional[int]:
        txt = (value or "").strip()
        if not txt:
            return None
        parts = txt.split(":")
        try:
            if len(parts) == 1 and txt.isdigit() and len(txt) in (3, 4):
                hour = int(txt[:-2])
                minute = int(txt[-2:])
            elif len(parts) == 2:
                hour = int(parts[0])
                minute = int(parts[1])
            else:
                return None
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                return None
            return hour * 60 + minute
        except Exception:
            return None

    def _format_overlap_time(self, start_min: int, end_min: int, tz: dt.tzinfo) -> str:
        base = dt.datetime.now(dt.timezone.utc).date()
        start_dt = dt.datetime(
            base.year,
            base.month,
            base.day,
            start_min // 60,
            start_min % 60,
            tzinfo=dt.timezone.utc,
        )
        end_dt = dt.datetime(
            base.year,
            base.month,
            base.day,
            end_min // 60,
            end_min % 60,
            tzinfo=dt.timezone.utc,
        )
        if self._show_local:
            start_dt = start_dt.astimezone(tz)
            end_dt = end_dt.astimezone(tz)
        return f"{start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"

    def _get_scheduled_group_name(self) -> str:
        try:
            sched = getattr(self.window(), "scheduler", None)
            entry = getattr(sched, "current_schedule_entry", {}) if sched else {}
            return (entry.get("group_name") or entry.get("group") or "").strip().upper()
        except Exception:
            return ""

    def _get_active_frequency_mhz(self, status: Optional[Dict[str, Any]] = None) -> Optional[float]:
        try:
            if not isinstance(status, dict):
                sched = getattr(self.window(), "scheduler", None)
                if not sched or not hasattr(sched, "get_status_summary"):
                    return None
                status = sched.get_status_summary()
            freq_label = status.get("freq_label") or ""
            return self._parse_freq_label(freq_label)
        except Exception:
            return None

    @staticmethod
    def _parse_freq_label(label: str) -> Optional[float]:
        try:
            parts = str(label).replace("MHz", "").strip().split()
            for token in parts:
                try:
                    return float(token)
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _selected_qsy_pending(self, active: Optional[float]) -> bool:
        meta = selected_qsy_meta(self.freq_combo)
        if not meta:
            return False
        try:
            selected = float(meta.get("freq", 0.0))
            return active is None or abs(selected - float(active)) > 0.0005
        except Exception as e:
            log.debug("ControlFreq: selected QSY metadata could not be compared: %s", e)
            return True

    def _update_frequency_action_styles(
        self,
        scheduled: Optional[float] = None,
        active: Optional[float] = None,
    ) -> None:
        try:
            theme = self._theme()
        except Exception:
            theme = None
        if not theme:
            return
        if scheduled is None:
            scheduled = current_scheduler_freq(self.window())
        if active is None:
            active = self._get_active_frequency_mhz()
        qsy_pending = self._selected_qsy_pending(active)
        hold_snapshot = self._hold_state_snapshot if isinstance(self._hold_state_snapshot, dict) else None
        if not isinstance(hold_snapshot, dict):
            hold_snapshot = suspend_snapshot(self.settings)
        if hold_snapshot.get("active") and not qsy_pending:
            remaining_sec = hold_snapshot.get("remaining_sec")
            self._primary_freq_action_mode = "resume"
            self.freq_action_btn.setText(active_hold_button_text(remaining_sec))
            self.freq_action_btn.setToolTip(active_hold_status_text(remaining_sec))
            self.freq_action_btn.setEnabled(True)
            self.freq_action_btn.setStyleSheet(button_style(active_hold_button_role(remaining_sec), theme))
            if self._freq_action_busy_reason_label:
                self._apply_frequency_action_busy_override(self._freq_action_busy_reason_label)
            return
        mismatch = (
            scheduled is not None
            and active is not None
            and abs(scheduled - active) > 0.0005
        )
        try:
            sched = getattr(self.window(), "scheduler", None)
            if sched is not None and hasattr(sched, "get_status_summary"):
                status = sched.get_status_summary(live=False)
                if isinstance(status, dict):
                    mismatch = bool(status.get("off_schedule"))
        except Exception:
            pass
        if qsy_pending:
            self._primary_freq_action_mode = "qsy"
            mins = self._selected_hold_minutes()
            self.freq_action_btn.setText("QSY + Hold")
            self.freq_action_btn.setToolTip(f"QSY now and pause schedule control for {mins} minutes (Ctrl+Enter)")
            self.freq_action_btn.setEnabled(True)
            self.freq_action_btn.setStyleSheet(button_style("warning", theme))
            if self._freq_action_busy_reason_label:
                self._apply_frequency_action_busy_override(self._freq_action_busy_reason_label)
            return
        if mismatch:
            self._primary_freq_action_mode = "resume"
            self.freq_action_btn.setText("Resume Schedule")
            self.freq_action_btn.setToolTip("Resume Schedule (Ctrl+Shift+R)")
            self.freq_action_btn.setEnabled(True)
            self.freq_action_btn.setStyleSheet(button_style("info", theme))
            if self._freq_action_busy_reason_label:
                self._apply_frequency_action_busy_override(self._freq_action_busy_reason_label)
            return
        self._primary_freq_action_mode = "none"
        mins = self._selected_hold_minutes()
        self.freq_action_btn.setText("QSY + Hold")
        self.freq_action_btn.setToolTip(f"QSY now and pause schedule control for {mins} minutes (Ctrl+Enter)")
        self.freq_action_btn.setEnabled(False)
        self.freq_action_btn.setStyleSheet(button_style("muted", theme))
        if self._freq_action_busy_reason_label:
            self._apply_frequency_action_busy_override(self._freq_action_busy_reason_label)

    def _on_primary_freq_action_clicked(self) -> None:
        mode = str(self._primary_freq_action_mode or "none").strip().lower()
        if mode == "resume":
            self._on_resume_schedule_clicked()
            return
        self._on_freq_set_clicked()

    def _selected_hold_minutes(self) -> int:
        return selected_hold_duration(self.hold_duration_combo, self.settings, self._runtime_hold_duration_profile())

    def _on_hold_duration_changed(self) -> None:
        mins = self._selected_hold_minutes()
        set_hold_duration_default(self.settings, mins)
        notify_hold_duration_default_changed(self.window())
        self._update_frequency_action_styles()

    def on_hold_state_changed(self, snapshot: Optional[Dict[str, object]] = None) -> None:
        self._hold_state_snapshot = snapshot if isinstance(snapshot, dict) else suspend_snapshot(self.settings)
        try:
            if (
                not self.hold_duration_combo.view().isVisible()
                and not self.hold_duration_combo.hasFocus()
            ):
                refresh_hold_duration_combo(self.hold_duration_combo, self.settings, self._runtime_hold_duration_profile())
        except Exception:
            pass
        self._update_frequency_action_styles()

    def _runtime_hold_duration_profile(self) -> Optional[Dict[str, object]]:
        try:
            root = self.window()
        except Exception:
            root = None
        profile = getattr(root, "_active_runtime_profile", None) if root is not None else None
        return profile if isinstance(profile, dict) else None

    def _update_active_label_style(
        self, scheduled: Optional[float], active: Optional[float]
    ) -> None:
        try:
            theme = self._theme()
        except Exception:
            theme = None
        mismatch = False
        if scheduled is not None and active is not None:
            mismatch = abs(scheduled - active) > 0.0005
        if theme:
            color = theme["warning"] if mismatch else theme.get("text_muted", theme["text"])
            self.freq_meta_label.setStyleSheet(f"color: {color};")

    def _qsy_feedback_target(self) -> Tuple[Optional[str], str]:
        try:
            root = self.window()
        except Exception:
            root = None
        profile = getattr(root, "_active_runtime_profile", None) if root is not None else None
        if isinstance(profile, dict):
            profile_id = profile.get("id")
            label = str(profile.get("name") or profile.get("label") or "").strip()
            return (str(profile_id) if profile_id not in (None, "") else None, label or "Radio")
        return None, "Radio"

    @staticmethod
    def _qsy_feedback_frequency_label(meta: Dict[str, Any]) -> str:
        try:
            freq = f"{float(meta.get('freq')):.3f}"
        except Exception:
            freq = str(meta.get("freq") or "").strip()
        mode = str(meta.get("mode") or "").strip()
        return f"{freq} {mode}".strip()

    def _publish_qsy_action_feedback(self, meta: Dict[str, Any], minutes: int, *, source_surface: str) -> None:
        try:
            root = self.window()
            service = getattr(root, "action_feedback_service", None) if root is not None else None
        except Exception:
            service = None
        if service is None or not hasattr(service, "publish"):
            log.debug("ControlFreq: action feedback service unavailable for QSY success.")
            return
        radio_profile_id, target_label = self._qsy_feedback_target()
        freq_label = self._qsy_feedback_frequency_label(meta)
        summary_target = target_label or "Radio"
        summary = f"QSY sent to {summary_target}: {freq_label}" if freq_label else f"QSY sent to {summary_target}"
        detail = f"Frequency changed and scheduling paused for {int(minutes)} minutes."
        try:
            service.publish(
                scope="radio",
                action_type="qsy",
                status="succeeded",
                summary=summary,
                radio_profile_id=radio_profile_id,
                target_label=target_label,
                detail=detail,
                source_surface=source_surface,
            )
        except Exception as e:
            log.debug("ControlFreq: failed to publish QSY action feedback: %s", e)

    def _publish_qsy_blocked_feedback(self, summary: str, detail: str = "", *, source_surface: str) -> None:
        try:
            root = self.window()
            service = getattr(root, "action_feedback_service", None) if root is not None else None
        except Exception:
            service = None
        if service is None or not hasattr(service, "publish"):
            log.debug("ControlFreq: action feedback service unavailable for QSY blocked feedback.")
            return
        radio_profile_id, target_label = self._qsy_feedback_target()
        try:
            service.publish(
                scope="radio",
                action_type="qsy",
                status="blocked",
                summary=str(summary or "").strip(),
                radio_profile_id=radio_profile_id,
                target_label=target_label,
                detail=str(detail or "").strip(),
                source_surface=source_surface,
            )
        except Exception as e:
            log.debug("ControlFreq: failed to publish QSY blocked feedback: %s", e)

    def _on_freq_set_clicked(self) -> None:
        control_via = (self.settings.get("control_via", "") or "").strip()
        if control_via not in {"FLRig", "RIGCTLD", "JS8Call"}:
            self._publish_qsy_blocked_feedback(
                "QSY blocked: frequency control is not configured.",
                "Frequency control is available when Control Via is FLRig, RIGCTLD, or JS8Call.",
                source_surface="controlfreq",
            )
            return
        meta = selected_qsy_meta(self.freq_combo)
        if not meta:
            self._publish_qsy_blocked_feedback(
                "QSY blocked: select a frequency first.",
                "Choose an Ops Center frequency before sending QSY.",
                source_surface="controlfreq",
            )
            return
        mins = perform_qsy_with_hold(self.window(), self.settings, meta, self._selected_hold_minutes())
        ok = mins > 0
        try:
            theme = self._theme()
        except Exception:
            theme = None
        if theme:
            self.freq_action_btn.setStyleSheet(
                button_style("success" if ok else "warning", theme)
            )
        if ok:
            self._publish_qsy_action_feedback(meta, mins, source_surface="controlfreq")
            QTimer.singleShot(0, self._refresh_frequency_control)
            QTimer.singleShot(800, self._refresh_frequency_control)
        else:
            QTimer.singleShot(0, self._refresh_frequency_control)

    def _on_freq_selection_changed(self, *_args) -> None:
        self._update_frequency_action_styles()

    def _on_resume_schedule_clicked(self) -> None:
        try:
            self.freq_action_btn.setStyleSheet(button_style("info", self._theme()))
        except Exception:
            pass
        resumed = False
        try:
            sched = getattr(self.window(), "scheduler", None)
            if sched and hasattr(sched, "resume_schedule"):
                resumed = bool(resume_schedule_hold(self.window(), self.settings))
            elif sched:
                sched.apply_current_entry(
                    force=True,
                    ignore_wait_prompt=True,
                    ignore_suspend=True,
                    ignore_js8_busy=True,
                    ignore_varac_busy=True,
                    ignore_fldigi_busy=True,
                )
                resumed = True
        except Exception:
            pass
        if resumed:
            self.on_schedule_resumed()

    def on_schedule_resumed(self) -> None:
        """
        Keep hero/status readouts responsive after resume regardless of trigger origin.
        """
        self._force_hero_resync = True
        if not self._active:
            return
        self._refresh_frequency_control(include_intersections=False)

        def _pulse_refresh() -> None:
            self._force_hero_resync = True
            self._refresh_frequency_control(include_intersections=False)

        # Short pulses absorb asynchronous scheduler/radio apply completion.
        for delay_ms in (180, 700, 1500):
            QTimer.singleShot(delay_ms, _pulse_refresh)

    def set_local_nets_outlook_items(self, items: Any) -> None:
        """Receive an already-bounded immutable Local Nets projection from the host.

        This tab deliberately does not open the Local Nets database or expand
        recurrence.  The projection owner supplies active, next, and no more
        than 50 later items; the view only performs a cheap ordering and render.
        """
        # Accept the core snapshot directly as well as its visible-item tuple.
        # This keeps the renderer independent of projection/storage ownership.
        snapshot_items = getattr(items, "visible_items", items)
        self._local_nets_outlook_items = tuple(snapshot_items or ())
        self._local_nets_outlook_revision += 1
        self._update_local_nets_outlook_header()
        if self.local_nets_outlook_toggle.isChecked():
            self._refresh_local_nets_outlook()

    def set_local_nets_outlook_provider(
        self,
        provider: Optional[Callable[[dt.datetime], Any]],
    ) -> None:
        """Install the host-owned projection callback without reading data here."""
        self._local_nets_outlook_provider = provider
        if provider is not None and self._active and self.local_nets_outlook_toggle.isChecked():
            self._schedule_local_nets_outlook_refresh()

    def _schedule_local_nets_outlook_refresh(
        self,
        now_utc: Optional[dt.datetime] = None,
    ) -> None:
        if self._local_nets_outlook_provider is None or not self.local_nets_outlook_toggle.isChecked():
            return
        if self._local_nets_outlook_pending:
            self._local_nets_outlook_followup = True
            return
        self._local_nets_outlook_pending = True
        self._local_nets_outlook_request_id += 1
        request_id = self._local_nets_outlook_request_id
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        if self._local_nets_outlook_executor is None:
            self._local_nets_outlook_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="fio-local-nets-outlook",
            )
        future = self._local_nets_outlook_executor.submit(
            self._local_nets_outlook_provider,
            now,
        )

        def done(future: Future) -> None:
            try:
                payload, error = future.result(), None
            except Exception as exc:
                payload, error = None, exc
            try:
                self._local_nets_outlook_ready.emit(request_id, payload, error)
            except RuntimeError:
                # The tab may have been destroyed while the bounded read was
                # finishing; shutdown invalidates the request above.
                return

        future.add_done_callback(done)

    def refresh_local_nets_outlook(self) -> None:
        """Request a bounded refresh without exposing executor internals."""
        self._schedule_local_nets_outlook_refresh()

    def _on_local_nets_outlook_ready(
        self,
        request_id: int,
        payload: object,
        error: object,
    ) -> None:
        if request_id != self._local_nets_outlook_request_id:
            return
        self._local_nets_outlook_pending = False
        if error is not None:
            log.debug("ControlFreq: Local Nets outlook unavailable: %s", error)
        else:
            self.set_local_nets_outlook_items(payload)
        if self._local_nets_outlook_followup:
            self._local_nets_outlook_followup = False
            self._schedule_local_nets_outlook_refresh()

    # A descriptive alias for hosts that use change-notification naming.
    on_local_nets_outlook_changed = set_local_nets_outlook_items

    @staticmethod
    def _local_net_item_value(item: Any, *names: str, default: Any = None) -> Any:
        for name in names:
            if isinstance(item, dict) and name in item:
                value = item.get(name)
            else:
                value = getattr(item, name, None)
            if value not in (None, ""):
                return value
        return default

    def _local_net_item_start(self, item: Any) -> Optional[dt.datetime]:
        value = self._local_net_item_value(item, "start_utc", "when_utc", "occurrence_start_utc")
        if isinstance(value, dt.datetime):
            return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)
        if isinstance(value, str):
            try:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)
            except ValueError:
                return None
        return None

    def _local_net_item_end(self, item: Any, start: dt.datetime) -> dt.datetime:
        value = self._local_net_item_value(item, "end_utc", "occurrence_end_utc")
        if isinstance(value, dt.datetime):
            return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)
        if isinstance(value, str):
            try:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)
            except ValueError:
                pass
        duration = self._local_net_item_value(item, "duration_minutes", default=60)
        try:
            return start + dt.timedelta(minutes=max(1, int(duration)))
        except (TypeError, ValueError):
            return start + dt.timedelta(minutes=60)

    def _local_net_outlook_partition(
        self,
        now_utc: Optional[dt.datetime] = None,
    ) -> Tuple[List[Any], Optional[Any], List[Any]]:
        """Return active, next, and a bounded later list without persistence work."""
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        ordered: List[Tuple[dt.datetime, Any]] = []
        for item in self._local_nets_outlook_items:
            start = self._local_net_item_start(item)
            if start is not None:
                ordered.append((start, item))
        ordered.sort(key=lambda row: row[0])
        active: List[Any] = []
        future: List[Any] = []
        for start, item in ordered:
            state = str(self._local_net_item_value(item, "state", "occurrence_state", default="pending")).lower()
            dismissed = bool(self._local_net_item_value(item, "dismissed", "is_dismissed", default=False))
            if dismissed or state in {"dismissed", "missed", "completed"}:
                continue
            is_active = bool(self._local_net_item_value(item, "is_active", "active", default=False))
            if is_active or (start <= now < self._local_net_item_end(item, start)):
                active.append(item)
            elif start >= now:
                future.append(item)
        next_item = future[0] if future else None
        return active[:1], next_item, future[1:51]

    def _update_local_nets_outlook_header(self) -> None:
        """Keep collapse-state updates to a count/urgency pass only."""
        if not hasattr(self, "local_nets_outlook_title"):
            return
        active, next_item, later = self._local_net_outlook_partition()
        count = len(active) + (1 if next_item is not None else 0) + len(later)
        attention = sum(
            1
            for item in self._local_nets_outlook_items
            if str(
                self._local_net_item_value(item, "source_health", "resource_status", "health", default="")
            ).lower()
            in {"update_available", "retired", "missing", "needs_review"}
        )
        suffix = f" · {count} upcoming" if count else " · no active reminders"
        if attention:
            suffix += f" · {attention} needs review"
        self.local_nets_outlook_title.setText(f"Local Nets · reminders{suffix}")
        self.local_nets_later_btn.setText(f"Later ({len(later)})")

    def _set_local_nets_outlook_visible(self, visible: bool) -> None:
        self.local_nets_outlook_list.setVisible(bool(visible))
        self.local_nets_later_btn.setVisible(bool(visible))
        self.local_nets_outlook_toggle.setText("Hide" if visible else "Show")
        if visible:
            self._schedule_local_nets_outlook_refresh()
            self._refresh_local_nets_outlook()
        else:
            # Do not clear the host projection: reopening only needs a bounded
            # presentation rebuild, not another data fetch.
            self._local_nets_outlook_rendered_revision = -1
        self._fit_group_box_to_contents(self.schedule_box)

    def _refresh_local_nets_outlook(self, now_utc: Optional[dt.datetime] = None) -> None:
        """Render the separate, non-commandable Local Nets outlook surface."""
        if not hasattr(self, "local_nets_outlook_toggle") or not self.local_nets_outlook_toggle.isChecked():
            self._update_local_nets_outlook_header()
            return
        self._update_local_nets_outlook_header()
        layout = self.local_nets_outlook_list_layout
        self._clear_widget_layout(layout)
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        active, next_item, later = self._local_net_outlook_partition(now)
        rows: List[Tuple[str, Any]] = [("active", item) for item in active]
        if next_item is not None:
            rows.append(("next", next_item))
        if self.local_nets_later_btn.isChecked():
            rows.extend(("later", item) for item in later)
        if not rows:
            empty = QLabel("No active Local Net reminders. Reminder only — CommsPlan will not tune a radio.")
            empty.setWordWrap(True)
            empty.setObjectName("controlfreqLocalNetsOutlookEmpty")
            layout.addWidget(empty)
        else:
            for placement, item in rows:
                layout.addWidget(self._build_local_net_outlook_row(item, placement, now))
        layout.addStretch(1)
        self._local_nets_outlook_rendered_revision = self._local_nets_outlook_revision
        self._fit_group_box_to_contents(self.schedule_box)

    def _build_local_net_outlook_row(self, item: Any, placement: str, now_utc: dt.datetime) -> QWidget:
        start = self._local_net_item_start(item) or now_utc
        end = self._local_net_item_end(item, start)
        mins = int((start - now_utc).total_seconds() // 60)
        reminder_minutes = self._local_net_item_value(item, "reminder_minutes", default=15)
        try:
            is_reminding = bool(self._local_net_item_value(item, "is_reminding", "reminding", default=False)) or (
                0 <= mins <= int(reminder_minutes)
            )
        except (TypeError, ValueError):
            is_reminding = 0 <= mins <= 15
        if placement == "active":
            urgency_text = f"ACTIVE — ends {self._format_display_time(end, False, self._get_display_tz())}"
            role = "warning"
        elif is_reminding:
            urgency_text = f"REMINDER — due in {max(0, mins)}m"
            role = "warning"
        elif mins <= 30:
            urgency_text = f"SOON — due in {max(0, mins)}m"
            role = "secondary"
        elif placement == "next":
            urgency_text = f"NEXT — in {max(0, mins)}m"
            role = "secondary"
        else:
            urgency_text = f"LATER — in {max(0, mins)}m"
            role = "panel"
        bg, fg, border = self._semantic_panel_colors(role)
        frame = QFrame(self.local_nets_outlook_list_container)
        frame.setObjectName("controlfreqLocalNetsOutlookRow")
        frame.setStyleSheet(
            f"QFrame#controlfreqLocalNetsOutlookRow {{ background: {bg}; color: {fg}; "
            f"border-left: 3px solid {border}; border-top: 1px solid {border}; "
            f"border-right: 1px solid {border}; border-bottom: 1px solid {border}; border-radius: 5px; }}"
        )
        row = QHBoxLayout(frame)
        row.setContentsMargins(8, 5, 8, 5)
        row.setSpacing(8)
        icon = QLabel("◷")
        icon.setAccessibleName("Local Net reminder")
        icon.setToolTip("Local Net reminder — informational only")
        icon.setMinimumWidth(18)
        row.addWidget(icon)
        details = QVBoxLayout()
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(1)
        name = str(self._local_net_item_value(item, "name", "net_name", default="Local Net"))
        group = str(self._local_net_item_value(item, "operating_group_name", "group_name", "group", default="Community / Unassigned"))
        service = str(self._local_net_item_value(item, "service", default="Local"))
        where = str(self._local_net_item_value(item, "where_text", "frequency_text", "channel_text", "band_freq", default="Location not configured"))
        when_text = self._format_display_time(start, True, self._get_display_tz())
        headline = QLabel(f"<b>{urgency_text}</b> · Reminder · Local Net · {when_text}")
        headline.setTextFormat(Qt.RichText)
        headline.setWordWrap(True)
        details.addWidget(headline)
        summary = QLabel(f"{name} · {group} · {service} / {where}")
        summary.setWordWrap(True)
        summary.setToolTip(summary.text())
        details.addWidget(summary)
        source_health = str(
            self._local_net_item_value(item, "source_health_text", "resource_status_text", default="")
        ).strip()
        if source_health:
            source_label = QLabel(f"Source: {source_health}")
            source_label.setWordWrap(True)
            source_label.setToolTip(source_health)
            details.addWidget(source_label)
        why = str(self._local_net_item_value(item, "why_text", "why", default="")).strip()
        if why:
            why_label = QLabel(f"Why: {why}")
            why_label.setWordWrap(True)
            why_label.setToolTip(why)
            details.addWidget(why_label)
        row.addLayout(details, 1)
        details_btn = QPushButton("Details")
        details_btn.setToolTip("Open this Local Net reminder in read-only detail.")
        details_btn.setStyleSheet(button_style("secondary", self._theme()))
        details_btn.clicked.connect(lambda _checked=False, payload=item: self.local_net_details_requested.emit(payload))
        row.addWidget(details_btn)
        if placement == "active" or is_reminding:
            dismiss_btn = QPushButton("Dismiss")
            dismiss_btn.setToolTip("Dismiss this occurrence only; future reminders remain enabled.")
            dismiss_btn.setStyleSheet(button_style("muted", self._theme()))
            dismiss_btn.clicked.connect(lambda _checked=False, payload=item: self.local_net_dismiss_requested.emit(payload))
            row.addWidget(dismiss_btn)
        sop_id = self._local_net_item_value(item, "sop_id", "sop_profile_id", "linked_sop_id")
        if sop_id not in (None, "", 0):
            sop_btn = QPushButton("Open SOP")
            sop_btn.setToolTip("Open the linked SOP with this Local Net as context. It will not activate the SOP.")
            sop_btn.setStyleSheet(button_style("primary", self._theme()))
            sop_btn.clicked.connect(lambda _checked=False, payload=item: self.local_net_open_sop_requested.emit(payload))
            row.addWidget(sop_btn)
        return frame

    # ---- Shortwave Listening: distinct, manual-only reminder projection ----
    def set_shortwave_listening_outlook_items(self, items: Any) -> None:
        snapshot_items = getattr(items, "visible_items", items)
        self._shortwave_listening_outlook_items = tuple(snapshot_items or ())[:52]
        self._update_shortwave_listening_header()
        if self.shortwave_listening_outlook_toggle.isChecked():
            self._render_shortwave_listening_outlook()

    def set_shortwave_listening_outlook_provider(self, provider: Optional[Callable[[dt.datetime], Any]]) -> None:
        self._shortwave_listening_outlook_provider = provider
        # Deliberately do not issue a DB request while the collapsed surface is hidden.
        if provider is not None and self._active and self.shortwave_listening_outlook_toggle.isChecked():
            self._schedule_shortwave_listening_outlook_refresh()

    def set_shortwave_listening_dismiss_provider(self, provider: Optional[Callable[[Any], None]]) -> None:
        """Install the host write operation; it runs on this bounded lane."""
        self._shortwave_listening_dismiss_provider = provider

    def refresh_shortwave_listening_outlook(self) -> None:
        self._schedule_shortwave_listening_outlook_refresh()

    def _schedule_shortwave_listening_outlook_refresh(self, now_utc: Optional[dt.datetime] = None) -> None:
        if not self._active or not self.shortwave_listening_outlook_toggle.isChecked() or self._shortwave_listening_outlook_provider is None:
            return
        if self._shortwave_listening_outlook_pending:
            self._shortwave_listening_outlook_followup = True
            return
        self._shortwave_listening_outlook_pending = True
        self._shortwave_listening_outlook_request_id += 1
        request_id = self._shortwave_listening_outlook_request_id
        if self._shortwave_listening_outlook_executor is None:
            self._shortwave_listening_outlook_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fio-shortwave-listening")
        future = self._shortwave_listening_outlook_executor.submit(self._shortwave_listening_outlook_provider, now_utc or dt.datetime.now(dt.timezone.utc))

        def done(completed: Future) -> None:
            try:
                payload, error = completed.result(), None
            except Exception as exc:
                payload, error = None, exc
            try:
                self._shortwave_listening_outlook_ready.emit(request_id, payload, error)
            except RuntimeError:
                return
        future.add_done_callback(done)

    def _on_shortwave_listening_outlook_ready(self, request_id: int, payload: object, error: object) -> None:
        if request_id != self._shortwave_listening_outlook_request_id:
            return
        self._shortwave_listening_outlook_pending = False
        if error is not None:
            log.debug("ControlFreq: Shortwave Listening outlook unavailable: %s", error)
        else:
            self.set_shortwave_listening_outlook_items(payload)
        if self._shortwave_listening_outlook_followup:
            self._shortwave_listening_outlook_followup = False
            self._schedule_shortwave_listening_outlook_refresh()

    def _request_shortwave_listening_dismiss(self, item: Any) -> None:
        provider = self._shortwave_listening_dismiss_provider
        if provider is None or self._shortwave_listening_action_pending:
            return
        if self._shortwave_listening_outlook_executor is None:
            self._shortwave_listening_outlook_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fio-shortwave-listening")
        self._shortwave_listening_action_pending = True
        future = self._shortwave_listening_outlook_executor.submit(provider, item)

        def done(completed: Future) -> None:
            try:
                error: object = None
                completed.result()
            except Exception as exc:
                error = exc
            try:
                self._shortwave_listening_action_ready.emit(error)
            except RuntimeError:
                return
        future.add_done_callback(done)

    def _on_shortwave_listening_action_ready(self, error: object) -> None:
        self._shortwave_listening_action_pending = False
        if error is not None:
            log.debug("ControlFreq: Shortwave Listening action failed: %s", error)
            return
        self._schedule_shortwave_listening_outlook_refresh()

    def _set_shortwave_listening_outlook_visible(self, visible: bool) -> None:
        self.shortwave_listening_outlook_list.setVisible(bool(visible))
        self.shortwave_listening_outlook_toggle.setText("Hide" if visible else "Show")
        if visible:
            self._schedule_shortwave_listening_outlook_refresh()
            self._render_shortwave_listening_outlook()
        self._fit_group_box_to_contents(self.schedule_box)

    @staticmethod
    def _shortwave_item_value(item: Any, name: str, default: Any = None) -> Any:
        return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)

    def _shortwave_item_start(self, item: Any) -> Optional[dt.datetime]:
        value = self._shortwave_item_value(item, "start_utc")
        if isinstance(value, dt.datetime):
            return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)
        return None

    def _update_shortwave_listening_header(self) -> None:
        if not hasattr(self, "shortwave_listening_outlook_title"):
            return
        rows = tuple(self._shortwave_listening_outlook_items)
        attention = sum(1 for row in rows if str(self._shortwave_item_value(row, "source_health", "")).lower() in {"changed", "missing"})
        suffix = f" · {len(rows)} visible" if rows else " · manual reminders"
        if attention:
            suffix += f" · {attention} needs review"
        self.shortwave_listening_outlook_title.setText("Shortwave Listening · reminders" + suffix)

    def _render_shortwave_listening_outlook(self, now_utc: Optional[dt.datetime] = None) -> None:
        if not self.shortwave_listening_outlook_toggle.isChecked():
            self._update_shortwave_listening_header()
            return
        self._update_shortwave_listening_header()
        layout = self.shortwave_listening_outlook_list_layout
        self._clear_widget_layout(layout)
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        rows = sorted(self._shortwave_listening_outlook_items, key=lambda row: self._shortwave_item_start(row) or now)[:52]
        if not rows:
            empty = QLabel("No saved Shortwave Listening reminders. Manual tuning only — CommsPlan will not control a receiver.")
            empty.setWordWrap(True)
            layout.addWidget(empty)
        else:
            for item in rows:
                layout.addWidget(self._build_shortwave_listening_row(item, now))
        layout.addStretch(1)
        self._fit_group_box_to_contents(self.schedule_box)

    def _build_shortwave_listening_row(self, item: Any, now_utc: dt.datetime) -> QWidget:
        start = self._shortwave_item_start(item) or now_utc
        end = self._shortwave_item_value(item, "end_utc")
        if not isinstance(end, dt.datetime):
            end = start + dt.timedelta(minutes=60)
        urgency = str(self._shortwave_item_value(item, "urgency_text", "Upcoming listening reminder"))
        name = str(self._shortwave_item_value(item, "name", "Shortwave listing"))
        station = str(self._shortwave_item_value(item, "station_name", ""))
        frequency = self._shortwave_item_value(item, "frequency_hz", 0)
        try:
            freq_text = f"{int(frequency) / 1_000_000:.3f} MHz"
        except (TypeError, ValueError):
            freq_text = "Frequency not supplied"
        health = str(self._shortwave_item_value(item, "source_health_text", ""))
        frame = QFrame(self.shortwave_listening_outlook_list_container)
        frame.setObjectName("controlfreqShortwaveListeningOutlookRow")
        state = str(self._shortwave_item_value(item, "urgency", ""))
        role = "warning" if state in {"active", "reminding"} else "secondary" if state == "soon" else "panel"
        bg, fg, border = self._semantic_panel_colors(role)
        frame.setStyleSheet(
            f"QFrame#controlfreqShortwaveListeningOutlookRow {{ background: {bg}; color: {fg}; "
            f"border-left: 3px solid {border}; border-top: 1px solid {border}; "
            f"border-right: 1px solid {border}; border-bottom: 1px solid {border}; border-radius: 5px; }}"
        )
        row = QHBoxLayout(frame)
        row.setContentsMargins(8, 5, 8, 5)
        row.setSpacing(8)
        icon = QLabel("◉")
        icon.setAccessibleName("Shortwave listening reminder")
        icon.setToolTip("Manual Shortwave listening reminder")
        row.addWidget(icon)
        text = QVBoxLayout()
        headline = QLabel(f"<b>{urgency}</b> · Manual listening · {self._format_display_time(start, True, self._get_display_tz())}")
        headline.setTextFormat(Qt.RichText)
        headline.setWordWrap(True)
        text.addWidget(headline)
        summary = QLabel(f"{name} · {station} · {freq_text}")
        summary.setWordWrap(True)
        text.addWidget(summary)
        if health:
            health_label = QLabel(f"Source: {health}")
            health_label.setWordWrap(True)
            text.addWidget(health_label)
        notice = QLabel("Manual tuning only; no radio or receiver control.")
        notice.setWordWrap(True)
        text.addWidget(notice)
        row.addLayout(text, 1)
        details = QPushButton("Details")
        details.setToolTip("Open this saved Shortwave reminder.")
        details.setStyleSheet(button_style("secondary", self._theme()))
        details.clicked.connect(lambda _checked=False, payload=item: self.shortwave_listening_details_requested.emit(payload))
        row.addWidget(details)
        if state in {"active", "reminding"}:
            dismiss = QPushButton("Dismiss")
            dismiss.setToolTip("Dismiss this occurrence only; the saved listening reminder remains enabled.")
            dismiss.setStyleSheet(button_style("muted", self._theme()))
            dismiss.clicked.connect(lambda _checked=False, payload=item: self._request_shortwave_listening_dismiss(payload))
            row.addWidget(dismiss)
        return frame

    def _refresh_schedule_outlook(self) -> None:
        if not self._dashboard_card_enabled("schedule"):
            return
        self._reload_sop_manager_settings()
        now_utc = dt.datetime.now(dt.timezone.utc)
        if self._show_local:
            # "Today" should respect local day boundaries in local display mode.
            tz = self._get_display_tz()
            now_local = now_utc.astimezone(tz)
            today_end_local = now_local.replace(hour=23, minute=59, second=59, microsecond=0)
            today_end = today_end_local.astimezone(dt.timezone.utc)
            tomorrow_start = (today_end_local + dt.timedelta(seconds=1)).astimezone(dt.timezone.utc)
            tomorrow_end = (
                (today_end_local + dt.timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
            ).astimezone(dt.timezone.utc)
        else:
            today_end = now_utc.replace(hour=23, minute=59, second=59, microsecond=0)
            tomorrow_start = today_end + dt.timedelta(seconds=1)
            tomorrow_end = tomorrow_start.replace(hour=23, minute=59, second=59, microsecond=0)
        day_mode = 1 if self._show_local else 0
        today_horizon_hours = max(1, int(math.ceil(max(0.0, (today_end - now_utc).total_seconds()) / 3600.0)))
        today_key = (
            "today",
            int(now_utc.timestamp() // 60),
            int(today_end.timestamp()),
            day_mode,
            int(self._sop_cache_epoch),
        )
        sop_actions_today = self._get_cached_sop_actions(
            key=today_key,
            ttl_sec=self._sop_today_cache_ttl_sec,
            builder=lambda: self._sop_manager.build_upcoming_actions(
                horizon_hours=today_horizon_hours,
                only_active=True,
                now_utc=now_utc,
            ),
        )
        tomorrow_key = (
            "tomorrow",
            int(tomorrow_start.timestamp()),
            int(tomorrow_end.timestamp()),
            day_mode,
            int(self._sop_cache_epoch),
        )
        sop_actions_tomorrow = self._get_cached_sop_actions(
            key=tomorrow_key,
            ttl_sec=self._sop_tomorrow_cache_ttl_sec,
            builder=lambda: self._build_sop_actions_in_window(
                tomorrow_start,
                tomorrow_end,
                only_active=True,
            ),
        )
        today_rows = self._collect_schedule_rows(
            now_utc,
            today_end,
            include_day=False,
            include_hf=True,
            resolve_multiday=bool(self._show_local),
            preloaded_sop_actions=sop_actions_today,
        )
        week_rows = self._collect_schedule_rows(
            tomorrow_start,
            tomorrow_end,
            include_day=True,
            include_hf=False,
            preloaded_sop_actions=sop_actions_tomorrow,
        )
        week_rows = [
            r
            for r in week_rows
            if not isinstance(r.get("when_utc"), dt.datetime)
            or (tomorrow_start <= r.get("when_utc") <= tomorrow_end)
        ]
        self._next_schedule_outlook_preview = self._next_schedule_outlook_entry(now_utc, today_rows + week_rows)
        if hasattr(self, "dashboard"):
            self.dashboard.set_upcoming(today_rows + week_rows)
        self._set_awareness_sop_summary(today_rows, week_rows)
        self._render_schedule_timeline(today_rows, week_rows, now_utc=now_utc)
        # Local Nets has an independent bounded projection and never enters the
        # commandable HF/SOP schedule table below.  A collapsed section only
        # receives its cheap header invalidation inside its own renderer.
        self._schedule_local_nets_outlook_refresh(now_utc)
        self._refresh_local_nets_outlook(now_utc)
        # Shortwave Listening remains entirely dormant while collapsed.  When
        # open, it shares the same bounded minute-level outlook cadence but is
        # never merged into the commandable schedule rows.
        if self.shortwave_listening_outlook_toggle.isChecked():
            self._schedule_shortwave_listening_outlook_refresh(now_utc)
            self._render_shortwave_listening_outlook(now_utc)
        self.schedule_table.setRowCount(0)
        self._schedule_entries_by_row.clear()
        self._append_section_row_to(self.schedule_table, "Today")
        if today_rows:
            for entry in today_rows:
                self._append_schedule_data_row(entry)
        else:
            self._append_schedule_data_row(
                {
                    "when_text": "--",
                    "type": "--",
                    "group": "No upcoming events today",
                    "band_freq": "--",
                    "action": "",
                    "action_kind": "",
                    "when_utc": None,
                }
            )
        self._append_section_row_to(self.schedule_table, "Tomorrow")
        if week_rows:
            for entry in week_rows:
                self._append_schedule_data_row(entry)
        else:
            self._append_schedule_data_row(
                {
                    "when_text": "--",
                    "type": "--",
                    "group": "No upcoming events tomorrow",
                    "band_freq": "--",
                    "action": "",
                    "action_kind": "",
                    "when_utc": None,
                }
            )
        self._apply_elide_tooltips(self.schedule_table, 2)
        self._apply_elide_tooltips(self.schedule_table, 3)
        self._fit_table_height_to_rows(self.schedule_table, min_rows=0, max_rows=8, empty_rows=1)
        self._fit_group_box_to_contents(self.schedule_box)
        self._refresh_scheduler_strip()

    def _set_schedule_details_visible(self, visible: bool) -> None:
        self.schedule_table.setVisible(bool(visible))
        self.schedule_action_hint.setVisible(bool(visible))
        self._fit_group_box_to_contents(self.schedule_box)

    def _render_schedule_timeline(
        self,
        today_rows: Sequence[Dict[str, Any]],
        later_rows: Sequence[Dict[str, Any]],
        *,
        now_utc: dt.datetime,
    ) -> None:
        layout = getattr(self, "schedule_timeline_layout", None)
        if layout is None:
            return
        self._clear_widget_layout(layout)
        valid_today = [row for row in today_rows if isinstance(row.get("when_utc"), dt.datetime)]
        valid_later = [row for row in later_rows if isinstance(row.get("when_utc"), dt.datetime)]
        self.schedule_later_btn.setText(f"Later ({len(valid_later)})")
        visible_rows = list(valid_today[:5])
        if self.schedule_later_btn.isChecked():
            visible_rows.extend(valid_later[:5])
        if not visible_rows:
            empty = QLabel("No upcoming schedule or SOP events in this view")
            empty.setWordWrap(True)
            layout.addWidget(empty)
            return
        theme = self._theme()
        for entry in visible_rows:
            when_utc = entry.get("when_utc")
            if not isinstance(when_utc, dt.datetime):
                continue
            normalized_when = when_utc if when_utc.tzinfo else when_utc.replace(tzinfo=dt.timezone.utc)
            mins = int((normalized_when.astimezone(dt.timezone.utc) - now_utc).total_seconds() // 60)
            role = "warning" if mins <= 30 else "secondary" if mins <= 180 else "panel"
            bg, fg, border = self._semantic_panel_colors(role)
            frame = QFrame(self.schedule_timeline_container)
            frame.setObjectName("controlfreqScheduleTimelineEvent")
            frame.setStyleSheet(
                f"QFrame#controlfreqScheduleTimelineEvent {{ background: {bg}; color: {fg}; "
                f"border-left: 3px solid {border}; border-top: 1px solid {border}; "
                f"border-right: 1px solid {border}; border-bottom: 1px solid {border}; border-radius: 5px; }}"
            )
            row_layout = QHBoxLayout(frame)
            row_layout.setContentsMargins(8, 5, 8, 5)
            row_layout.setSpacing(8)
            event_type = str(entry.get("type") or "Schedule")
            icon_kind = "event" if "SOP" in event_type.upper() or "CONDITION" in event_type.upper() else "schedule"
            icon = QLabel()
            icon.setPixmap(QIcon(str(self._focus_icon_path(icon_kind))).pixmap(20, 20))
            icon.setAccessibleName(event_type)
            row_layout.addWidget(icon)
            when_text = str(entry.get("when_text") or "--")
            when_label = QLabel(f"<b>{when_text}</b>")
            when_label.setTextFormat(Qt.RichText)
            when_label.setMinimumWidth(72)
            row_layout.addWidget(when_label)
            group = str(entry.get("group") or "--")
            band_freq = str(entry.get("band_freq") or "--")
            summary = QLabel(f"{event_type} · {group} · {band_freq}")
            summary.setWordWrap(True)
            summary.setToolTip(summary.text())
            row_layout.addWidget(summary, 1)
            action_label = str(entry.get("action") or "").strip()
            action_kind = str(entry.get("action_kind") or "").strip()
            if action_label and action_kind:
                action = QPushButton(action_label)
                action.setMaximumWidth(132)
                action.setToolTip(self._schedule_action_tooltip(action_kind))
                action.setStyleSheet(button_style(self._schedule_action_button_role(action_kind), theme))
                action.clicked.connect(
                    lambda _checked=False, payload=dict(entry): self._on_schedule_action_clicked(payload)
                )
                row_layout.addWidget(action)
            layout.addWidget(frame)

    def _set_awareness_sop_summary(
        self,
        today_rows: Sequence[Dict[str, Any]],
        tomorrow_rows: Sequence[Dict[str, Any]],
    ) -> None:
        label = getattr(self, "awareness_sop_label", None)
        if label is None:
            return
        next_entry = None
        for entry in tuple(today_rows or ()) + tuple(tomorrow_rows or ()):
            when_utc = entry.get("when_utc") if isinstance(entry, dict) else None
            if isinstance(when_utc, dt.datetime):
                next_entry = entry
                break
        if not isinstance(next_entry, dict):
            text = "SOP: no upcoming net or SOP action in this view."
        else:
            when_text = str(next_entry.get("when_text") or "").strip()
            kind = str(next_entry.get("type") or "Action").strip()
            group = str(next_entry.get("group") or "").strip()
            band_freq = str(next_entry.get("band_freq") or "").strip()
            action = str(next_entry.get("action") or "").strip()
            parts = [part for part in (when_text, kind, group, band_freq, action) if part and part != "--"]
            text = "SOP: " + " | ".join(parts[:5]) if parts else "SOP: review Schedule Outlook."
        label.setText(text)
        label.setToolTip(text)

    def _next_schedule_outlook_entry(
        self,
        now_utc: dt.datetime,
        rows: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for row in rows:
            when_utc = row.get("when_utc")
            if not isinstance(when_utc, dt.datetime):
                continue
            if when_utc.tzinfo is None:
                when_utc = when_utc.replace(tzinfo=dt.timezone.utc)
            else:
                when_utc = when_utc.astimezone(dt.timezone.utc)
            if when_utc < now_utc:
                continue
            freq_mhz = row.get("freq_mhz")
            if not isinstance(freq_mhz, (int, float)):
                freq_mhz = self._parse_freq_label(str(row.get("band_freq") or ""))
            if not isinstance(freq_mhz, (int, float)):
                continue
            preview = dict(row)
            preview["when_utc"] = when_utc
            preview["freq_mhz"] = float(freq_mhz)
            return preview
        return None

    def _append_schedule_data_row(self, entry: Dict[str, Any]) -> None:
        row = self.schedule_table.rowCount()
        self.schedule_table.insertRow(row)
        self._schedule_entries_by_row[row] = entry
        cols = [
            str(entry.get("when_text") or "--"),
            str(entry.get("type") or "--"),
            str(entry.get("group") or "--"),
            str(entry.get("band_freq") or "--"),
        ]
        for col, value in enumerate(cols):
            item = QTableWidgetItem(value)
            item.setFlags(item.flags() ^ Qt.ItemIsEditable)
            self.schedule_table.setItem(row, col, item)
        self._style_schedule_row(row, entry)
        action_label = str(entry.get("action") or "").strip()
        action_kind = str(entry.get("action_kind") or "").strip()
        if action_label and action_kind:
            btn = QPushButton(action_label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumWidth(96)
            btn.setMaximumWidth(132)
            btn.setToolTip(self._schedule_action_tooltip(action_kind))
            btn.clicked.connect(lambda _=False, payload=entry: self._on_schedule_action_clicked(payload))
            try:
                theme = self._theme()
                btn.setStyleSheet(button_style(self._schedule_action_button_role(action_kind), theme))
            except Exception:
                pass
            self.schedule_table.setCellWidget(row, 4, btn)

    @staticmethod
    def _schedule_action_button_role(action_kind: str) -> str:
        kind = (action_kind or "").strip().lower()
        if kind == "qsy":
            return "warning"
        if kind in {"open_net", "open_sop"}:
            return "primary"
        return "secondary"

    @staticmethod
    def _schedule_action_tooltip(action_kind: str) -> str:
        kind = (action_kind or "").strip().lower()
        if kind == "qsy":
            return "Tune radio now and pause schedule control for the selected hold duration."
        if kind == "open_net":
            return "Open Net Schedule tab."
        if kind == "open_sop":
            return "Open SOP Builder tab."
        return "Run row action."

    def _on_schedule_context_menu(self, pos) -> None:
        try:
            row = self.schedule_table.rowAt(int(pos.y()))
            if row < 0:
                return
            entry = self._schedule_entries_by_row.get(row) or {}
            action_kind = str(entry.get("action_kind") or "").strip().lower()
            action_label = str(entry.get("action") or "").strip()
            if not action_kind or not action_label:
                return
            menu = QMenu(self.schedule_table)
            act = menu.addAction(action_label)
            chosen = menu.exec(self.schedule_table.viewport().mapToGlobal(pos))
            if chosen == act:
                self._on_schedule_action_clicked(entry)
        except Exception as e:
            log.debug("ControlFreq: schedule row context-menu failed: %s", e)

    def _style_schedule_row(self, row: int, entry: Dict[str, Any]) -> None:
        when_utc = entry.get("when_utc")
        if not isinstance(when_utc, dt.datetime):
            return
        palette = self._urgency_palette()
        now_utc = dt.datetime.now(dt.timezone.utc)
        mins = int((when_utc - now_utc).total_seconds() // 60)
        bg = None
        if mins <= 15:
            bg = palette["critical"]
        elif mins <= 60:
            bg = palette["soon"]
        elif mins <= 180:
            bg = palette["upcoming"]
        if bg is not None:
            for col in range(4):
                item = self.schedule_table.item(row, col)
                if item:
                    item.setBackground(bg)
                    item.setForeground(palette["text"])
        type_item = self.schedule_table.item(row, 1)
        if type_item and mins <= 15:
            f = type_item.font()
            f.setBold(True)
            type_item.setFont(f)

    def _on_schedule_action_clicked(self, entry: Dict[str, Any]) -> None:
        kind = str(entry.get("action_kind") or "").strip().lower()
        if kind == "open_net":
            self._navigate_to_tab("Net Schedule")
            return
        if kind == "open_sop":
            self._navigate_to_tab("SOP")
            return
        if kind != "qsy":
            return
        meta = self._schedule_qsy_meta(entry)
        if not meta:
            self._publish_qsy_blocked_feedback(
                "QSY blocked: no matching frequency is configured.",
                "No matching operating-group frequency is configured for this schedule row.",
                source_surface="controlfreq_schedule",
            )
            return
        mins = perform_qsy_with_hold(self.window(), self.settings, meta, self._selected_hold_minutes())
        ok = mins > 0
        if ok:
            self._force_hero_resync = True
            self._refresh_frequency_control()
            self._publish_qsy_action_feedback(meta, mins, source_surface="controlfreq_schedule")

            def _refresh_qsy_hero() -> None:
                self._force_hero_resync = True
                self._refresh_frequency_control()

            QTimer.singleShot(800, _refresh_qsy_hero)

    def _schedule_qsy_meta(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        group = str(entry.get("group") or "").strip().upper()
        band = str(entry.get("band") or "").strip().upper()
        try:
            freq = float(entry.get("freq_mhz"))
        except Exception:
            return None
        for row in load_operating_groups(self.settings):
            grp = str(row.get("group") or "").strip().upper()
            if grp != group:
                continue
            try:
                row_freq = float(row.get("frequency", 0))
            except Exception:
                continue
            if abs(row_freq - freq) > 0.0005:
                continue
            return {
                "freq": row_freq,
                "mode": row.get("mode", ""),
                "band": row.get("band", "") or band,
                "auto_tune": bool(row.get("auto_tune", False)),
                "vfo": (row.get("vfo") or "").strip().upper(),
                "group": grp,
            }
        return {
            "freq": freq,
            "mode": "",
            "band": band,
            "auto_tune": False,
            "vfo": "",
            "group": group,
        }

    def _navigate_to_tab(self, label: str) -> None:
        try:
            win = self.window()
            screens = getattr(win, "_screens", [])
            for idx, (tab_label, _widget) in enumerate(screens):
                if str(tab_label) == label and hasattr(win, "_set_screen"):
                    win._set_screen(idx)
                    return
        except Exception as e:
            log.debug("ControlFreq: failed to navigate to tab %s: %s", label, e)

    def _refresh_today(self) -> None:
        # Backward-compat shim for legacy callers.
        self._refresh_schedule_outlook()

    def _refresh_week(self) -> None:
        # Backward-compat shim for legacy callers.
        self._refresh_schedule_outlook()

    @staticmethod
    def _action_enabled(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return int(value) != 0
        txt = str(value).strip().lower()
        if not txt:
            return False
        return txt in {"1", "true", "yes", "y", "on", "enabled"}

    def _build_sop_actions_in_window(
        self,
        start: dt.datetime,
        end: dt.datetime,
        *,
        only_active: bool = True,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            if end <= start:
                return out
            max_total_rows = 400
            condition_levels = self._sop_manager._condition_level_map()  # type: ignore[attr-defined]
            profiles = self._sop_manager.list_profiles()
            for profile in profiles:
                if only_active and not bool(profile.get("active")):
                    continue
                profile_id = int(profile.get("id") or 0)
                if profile_id <= 0:
                    continue
                full = self._sop_manager.get_profile(profile_id)
                if not full:
                    continue
                operating_group = str(full.get("operating_group") or "")
                for action in list(full.get("actions") or []):
                    if not self._action_enabled(action.get("enabled", True)):
                        continue
                    action_group = str(action.get("group_name") or "").strip().upper() or str(operating_group).strip().upper()
                    group_level = condition_levels.get(action_group)
                    if not self._sop_manager._action_condition_match(  # type: ignore[attr-defined]
                        str(action.get("condition_levels") or "ALL"),
                        group_level,
                    ):
                        continue
                    interval_m = int(action.get("interval_minutes") or 0)
                    if interval_m <= 0:
                        interval_m = max(1, int(action.get("interval_hours") or 3)) * 60
                    interval_m = max(1, interval_m)
                    interval_phase_m = max(0, int(action.get("interval_phase_minutes") or 0)) % interval_m
                    action_for_occurrence = dict(action)
                    if not str(action_for_occurrence.get("daily_start_utc") or "").strip():
                        action_for_occurrence["daily_start_utc"] = str(full.get("sop_start_utc") or "00:00")
                    if not str(action_for_occurrence.get("daily_end_utc") or "").strip():
                        action_for_occurrence["daily_end_utc"] = "23:59"
                    action_for_occurrence["interval_minutes"] = interval_m
                    action_for_occurrence["interval_phase_minutes"] = interval_phase_m
                    occurrences = self._sop_manager.build_action_occurrences_in_window(
                        action_for_occurrence,
                        window_start_utc=start,
                        window_end_utc=end + dt.timedelta(seconds=1),
                    )
                    if not occurrences:
                        continue
                    max_per_action = min(
                        240,
                        int(math.ceil((end - start).total_seconds() / max(60, interval_m * 60))) + 2,
                    )
                    emitted = 0
                    rule = str(action.get("contact_rule") or "none").strip()
                    selected_target = (action.get("contact_target") or "").strip().upper()
                    targets: List[str] = []
                    if rule in {"hub_or_hub_alt", "ncs_or_ancs"}:
                        if selected_target and selected_target != "__ANY_ROLE__":
                            targets = [selected_target]
                        else:
                            targets = ["Any (Role Match)"]
                    elif rule == "group":
                        targets = [selected_target] if selected_target else ["Any (Group Match)"]
                    elif rule in {"callsign", "peer", "local_profile", "local_group"}:
                        targets = [selected_target] if selected_target else []
                    action_band = (action.get("band") or "").strip().upper()
                    action_freq = (action.get("frequency") or "").strip() or str(full.get("frequency") or "")
                    action_id = int(action.get("id") or 0)
                    software = str(action.get("software") or "")
                    mode = str(action.get("mode") or "").strip().upper()
                    action_key = str(action.get("action_key") or "")
                    action_label = str(action.get("action_label") or "")
                    description = str(action.get("description") or "")
                    for due, _due_end in occurrences:
                        if due < start or due > end:
                            continue
                        if emitted >= max_per_action:
                            break
                        if len(out) >= max_total_rows:
                            break
                        # ControlFreq outlook does not render alignment warnings for SOP rows;
                        # keep this lightweight to avoid per-occurrence DB schedule scans.
                        aligned = True
                        out.append(
                            {
                                "profile_id": profile_id,
                                "profile_name": str(full.get("name") or ""),
                                "operating_group": action_group or operating_group,
                                "band": action_band,
                                "frequency": action_freq,
                                "action_id": action_id,
                                "software": software,
                                "mode": mode,
                                "action_key": action_key,
                                "action_label": action_label,
                                "description": description,
                                "contact_rule": rule,
                                "contact_target": selected_target,
                                "contact_targets": targets,
                                "interval_minutes": interval_m,
                                "interval_phase_minutes": interval_phase_m,
                                "next_due_utc": due,
                                "aligned": aligned,
                                "status": "Upcoming",
                                "is_completed": False,
                            }
                        )
                        emitted += 1
                    if len(out) >= max_total_rows:
                        break
                if len(out) >= max_total_rows:
                    break
        except Exception as e:
            log.debug("ControlFreq: SOP window build failed: %s", e)
        out.sort(
            key=lambda x: (
                x.get("next_due_utc") if isinstance(x.get("next_due_utc"), dt.datetime) else dt.datetime.max.replace(tzinfo=dt.timezone.utc),
                str(x.get("profile_name") or ""),
                str(x.get("action_label") or ""),
            )
        )
        return out

    def _collect_schedule_rows(
        self,
        start: dt.datetime,
        end: dt.datetime,
        include_day: bool = False,
        include_hf: bool = True,
        resolve_multiday: bool = False,
        preloaded_sop_actions: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        rows_out: List[Dict[str, Any]] = []
        group_filter = self.group_combo.currentData() or ""
        search = (self.search_edit.text() or "").strip().upper()
        tz = self._get_display_tz()
        scan_multi_day = bool(resolve_multiday or include_day)

        # SOP upcoming actions
        try:
            if preloaded_sop_actions is None:
                horizon_hours = max(1, int(math.ceil(max(0.0, (end - start).total_seconds()) / 3600.0)))
                actions = self._sop_manager.build_upcoming_actions(
                    horizon_hours=horizon_hours, only_active=True, now_utc=start
                )
            else:
                actions = preloaded_sop_actions
            for a in actions:
                due = a.get("next_due_utc") or a.get("due_utc")
                if not isinstance(due, dt.datetime):
                    continue
                if due.tzinfo is None:
                    due = due.replace(tzinfo=dt.timezone.utc)
                else:
                    due = due.astimezone(dt.timezone.utc)
                status_txt = str(a.get("status") or "").strip().upper()
                due_window_start = start
                if status_txt in {"DUE NOW", "OVERDUE", "COMPLETED"}:
                    # SOP due-now items are represented by the current interval start,
                    # which may be a few minutes before "now". Keep them visible.
                    due_window_start = start - dt.timedelta(minutes=30)
                if not (due_window_start <= due <= end):
                    continue
                grp = (a.get("operating_group") or "").strip().upper()
                if group_filter and grp != group_filter:
                    continue
                label = a.get("action_label") or a.get("action") or "SOP"
                band = (a.get("band") or "").strip().upper()
                freq = (a.get("frequency") or "").strip()
                software = str(a.get("software") or "").strip()
                software_norm = software.lower()
                action_key = str(a.get("action_key") or "").strip()
                action_key_norm = action_key.lower()
                contact_rule = str(a.get("contact_rule") or "").strip().lower()
                profile_name = str(a.get("profile_name") or "").strip()
                description = str(a.get("description") or "").strip()
                targets = [str(x).strip() for x in (a.get("contact_targets") or []) if str(x).strip()]
                contact_target = str(a.get("contact_target") or "").strip()
                if not contact_target and targets:
                    contact_target = targets[0]
                is_local_net_action = (
                    software_norm in {"local net", "local"}
                    or contact_rule in {"local_profile", "local_group"}
                    or action_key_norm.startswith("local_")
                )
                if is_local_net_action:
                    # Keep Local Net Group/Net text predictable for quick NCS scanning.
                    profile_part = profile_name or "Local Net"
                    target_part = contact_target or "-"
                    description_part = description or str(label).strip() or "-"
                    group_text = f"{profile_part} - {target_part} - {description_part}"
                else:
                    group_text = f"{grp} {label}".strip() or profile_name or "SOP"
                display_due = due if due >= start else start
                when = self._format_display_time(display_due, include_day, tz)
                search_blob = " ".join(
                    [
                        grp,
                        str(label),
                        group_text,
                        profile_name,
                        contact_target,
                        description,
                        "SOP",
                    ]
                ).upper()
                if search and search not in search_blob:
                    continue
                rows_out.append(
                    {
                        "when_text": when,
                        "when_utc": due,
                        "type": "SOP",
                        "group": group_text,
                        "band_freq": f"{band} {freq}".strip(),
                        "band": band,
                        "freq_mhz": None,
                        "action": "Open SOP",
                        "action_kind": "open_sop",
                    }
                )
        except Exception as e:
            log.debug("ControlFreq: SOP load failed: %s", e)

        # HF + Net schedule (simple view)
        if include_hf:
            rows_out.extend(self._load_hf_schedule(start, end, include_day, tz, scan_multi_day=scan_multi_day))
        rows_out.extend(self._load_net_schedule(start, end, include_day, tz, scan_multi_day=scan_multi_day))
        rows_out.sort(
            key=lambda r: (
                r.get("when_utc") if isinstance(r.get("when_utc"), dt.datetime) else dt.datetime.max.replace(tzinfo=dt.timezone.utc),
                str(r.get("type") or ""),
                str(r.get("group") or ""),
            )
        )
        return rows_out[:200]

    def _load_hf_schedule(
        self,
        start: dt.datetime,
        end: dt.datetime,
        include_day: bool,
        tz: dt.tzinfo,
        *,
        scan_multi_day: bool = False,
    ) -> List[Dict[str, Any]]:
        rows_out: List[Dict[str, Any]] = []
        rows = self._daily_schedule_rows()
        if not rows:
            return rows_out
        group_filter = self.group_combo.currentData() or ""
        search = (self.search_edit.text() or "").strip().upper()
        for r in rows:
            row = r
            day = (row.get("day") or row.get("day_utc") or row.get("day_name") or "").strip()
            grp = (row.get("group_name") or row.get("group") or "").strip().upper()
            if group_filter and grp != group_filter:
                continue
            start_hm = (row.get("start") or row.get("start_utc") or "").strip()
            band = (row.get("band") or "").strip().upper()
            freq_raw = row.get("frequency") or row.get("freq") or ""
            recurrence = row.get("recurrence") or "Weekly"
            month_weeks = row.get("month_weeks") or ""
            when_utc = self._resolve_schedule_time_utc(
                start,
                end,
                day or "ALL",
                start_hm,
                scan_multi_day,
                recurrence,
                month_weeks,
            )
            if when_utc is None:
                continue
            try:
                freq_mhz = float(str(freq_raw).strip())
            except Exception:
                freq_mhz = None
            freq_txt = f"{freq_mhz:.3f}" if isinstance(freq_mhz, float) else str(freq_raw).strip()
            when = self._format_display_time(when_utc, include_day, tz)
            if search and search not in grp and search not in (band + freq_txt).upper() and search not in "HF":
                continue
            rows_out.append(
                {
                    "when_text": when,
                    "when_utc": when_utc,
                    "type": "HF",
                    "group": grp,
                    "band_freq": f"{band} {freq_txt}".strip(),
                    "band": band,
                    "freq_mhz": freq_mhz,
                    "action": "QSY + Hold" if isinstance(freq_mhz, float) else "",
                    "action_kind": "qsy" if isinstance(freq_mhz, float) else "",
                }
            )
        return rows_out

    def _scheduled_group_freqs(
        self, window_minutes: int
    ) -> Tuple[Dict[str, Set[float]], Dict[str, Set[str]]]:
        sched_freqs: Dict[str, Set[float]] = {}
        sched_bands: Dict[str, Set[str]] = {}
        rows = self._daily_schedule_rows()
        if not rows:
            return sched_freqs, sched_bands
        now_utc = dt.datetime.now(dt.timezone.utc)
        today_utc_name = now_utc.strftime("%A").upper()

        def parse_hhmm(value: str) -> Optional[Tuple[int, int]]:
            txt = (value or "").strip()
            if not txt:
                return None
            parts = txt.split(":")
            if len(parts) == 1 and txt.isdigit() and len(txt) in (3, 4):
                h = int(txt[:-2])
                m = int(txt[-2:])
            elif len(parts) == 2:
                h = int(parts[0])
                m = int(parts[1])
            else:
                return None
            if h < 0 or h > 23 or m < 0 or m > 59:
                return None
            return h, m

        for r in rows:
            row = r
            day = (row.get("day_utc") or row.get("day") or "ALL").strip().upper()
            if day not in {"ALL", today_utc_name}:
                continue
            start_hm = row.get("start_utc") or row.get("start") or ""
            hm = parse_hhmm(str(start_hm))
            if not hm:
                continue
            start_utc = now_utc.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            delta_min = abs((start_utc - now_utc).total_seconds()) / 60.0
            if delta_min > float(window_minutes):
                continue
            grp = (row.get("group_name") or row.get("group") or "").strip().upper()
            if not grp:
                continue
            band = (row.get("band") or "").strip().upper()
            freq_val = row.get("frequency") or row.get("freq")
            if band:
                sched_bands.setdefault(grp, set()).add(band)
            try:
                freq = float(freq_val)
                sched_freqs.setdefault(grp, set()).add(freq)
            except Exception:
                continue
        return sched_freqs, sched_bands

    def _load_net_schedule(
        self,
        start: dt.datetime,
        end: dt.datetime,
        include_day: bool,
        tz: dt.tzinfo,
        *,
        scan_multi_day: bool = False,
    ) -> List[Dict[str, Any]]:
        rows_out: List[Dict[str, Any]] = []
        rows = self._net_schedule_rows()
        if not rows:
            return rows_out
        group_filter = self.group_combo.currentData() or ""
        search = (self.search_edit.text() or "").strip().upper()
        for r in rows:
            row = r
            day = (row.get("day") or row.get("day_utc") or "").strip()
            grp = (row.get("group_name") or row.get("group") or "").strip().upper()
            if group_filter and grp != group_filter:
                continue
            start_hm = (row.get("start") or row.get("start_utc") or "").strip()
            band = (row.get("band") or "").strip().upper()
            freq_raw = row.get("frequency") or row.get("freq") or ""
            net_name = (row.get("net_name") or "").strip()
            recurrence = row.get("recurrence") or "Weekly"
            month_weeks = row.get("month_weeks") or ""
            when_utc = self._resolve_schedule_time_utc(
                start,
                end,
                day or "ALL",
                start_hm,
                scan_multi_day,
                recurrence,
                month_weeks,
            )
            if when_utc is None:
                continue
            try:
                freq_mhz = float(str(freq_raw).strip())
            except Exception:
                freq_mhz = None
            freq_txt = f"{freq_mhz:.3f}" if isinstance(freq_mhz, float) else str(freq_raw).strip()
            when = self._format_display_time(when_utc, include_day, tz)
            if search and search not in grp and search not in net_name.upper() and search not in "NET":
                continue
            rows_out.append(
                {
                    "when_text": when,
                    "when_utc": when_utc,
                    "type": "NET",
                    "group": net_name or grp,
                    "band_freq": f"{band} {freq_txt}".strip(),
                    "band": band,
                    "freq_mhz": freq_mhz,
                    "action": "Open Net",
                    "action_kind": "open_net",
                }
            )
        return rows_out

    def _resolve_schedule_time_utc(
        self,
        start_utc: dt.datetime,
        end_utc: dt.datetime,
        day_value: str,
        hhmm: str,
        include_day: bool,
        recurrence_value: str = "Weekly",
        month_weeks_value: str = "",
    ) -> Optional[dt.datetime]:
        hm = self._parse_hhmm(hhmm)
        if not hm:
            return None
        recurrence = self._normalize_recurrence(recurrence_value)
        month_weeks = self._parse_month_weeks(month_weeks_value)
        day_norm = (day_value or "ALL").strip().upper()
        day_span = max(0, (end_utc.date() - start_utc.date()).days)
        for i in range(day_span + 1):
            day_dt = start_utc + dt.timedelta(days=i)
            candidate = day_dt.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if candidate < start_utc or candidate > end_utc:
                continue
            if self._row_applies_on_date(day_norm, candidate, recurrence, month_weeks):
                return candidate
            if not include_day:
                break
        return None

    def _normalize_recurrence(self, value: object) -> str:
        recurrence = str(value or "Weekly").strip().upper()
        if recurrence == "MONTHLY":
            recurrence = "PERIODIC"
        if recurrence not in {"WEEKLY", "DAILY", "PERIODIC", "BI-WEEKLY"}:
            return "WEEKLY"
        return recurrence

    def _parse_month_weeks(self, txt: object) -> List[int]:
        weeks: List[int] = []
        for token in str(txt or "").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                val = int(token)
            except Exception:
                continue
            if 1 <= val <= 5:
                weeks.append(val)
        return sorted(set(weeks))

    def _month_week_index(self, date_val: dt.date) -> int:
        return 1 + ((date_val.day - 1) // 7)

    def _day_matches(self, day_norm: str, candidate_utc: dt.datetime) -> bool:
        if day_norm in {"", "ALL", "DAILY"}:
            return True
        day_name = candidate_utc.strftime("%A").upper()
        day_key = day_norm[:3]
        return day_name.startswith(day_key) or day_norm.startswith(day_name[:3])

    def _row_applies_on_date(
        self,
        day_norm: str,
        candidate_utc: dt.datetime,
        recurrence: str,
        month_weeks: List[int],
    ) -> bool:
        if recurrence == "DAILY":
            return True
        if not self._day_matches(day_norm, candidate_utc):
            return False
        if recurrence == "PERIODIC":
            weeks = month_weeks or [1]
            return self._month_week_index(candidate_utc.date()) in weeks
        return True

    def _get_display_tz(self) -> dt.tzinfo:
        if not self._show_local:
            return dt.timezone.utc
        try:
            tz_name = self.settings.get("timezone", "UTC") or "UTC"
            return get_timezone(tz_name)
        except Exception:
            return dt.timezone.utc

    def _format_display_time(
        self, utc_dt: dt.datetime, include_day: bool, tz: dt.tzinfo
    ) -> str:
        if not isinstance(utc_dt, dt.datetime):
            return "--:--"
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=dt.timezone.utc)
        local_dt = utc_dt.astimezone(tz) if self._show_local else utc_dt.astimezone(dt.timezone.utc)
        return local_dt.strftime("%a %H:%M") if include_day else local_dt.strftime("%H:%M")

    def _parse_hhmm(self, value: str) -> Optional[Tuple[int, int]]:
        txt = (value or "").strip()
        if not txt:
            return None
        parts = txt.split(":")
        if len(parts) == 1 and txt.isdigit() and len(txt) in (3, 4):
            h = int(txt[:-2])
            m = int(txt[-2:])
        elif len(parts) == 2:
            h = int(parts[0])
            m = int(parts[1])
        else:
            return None
        if h < 0 or h > 23 or m < 0 or m > 59:
            return None
        return h, m

    def _format_hhmm_display(
        self, base_utc: dt.datetime, hhmm: str, include_day: bool, tz: dt.tzinfo
    ) -> str:
        hm = self._parse_hhmm(hhmm)
        if not hm:
            return "--:--"
        dt_utc = base_utc.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=dt.timezone.utc)
        return self._format_display_time(dt_utc, include_day, tz)

    def _refresh_message_summary(self) -> None:
        self._schedule_message_summary_refresh()

    def _toggle_traffic_source_detail(self, visible: bool) -> None:
        self.inbox_table.setVisible(bool(visible))
        self.traffic_source_detail_btn.setText("Hide Sources" if visible else "Sources")
        self._set_message_summary_visible_rows(6)

    def _toggle_traffic_group_detail(self, visible: bool, *, persist: bool = True) -> None:
        expanded = bool(visible)
        if hasattr(self, "dashboard"):
            self._dashboard_traffic_expanded = expanded
        if hasattr(self, "traffic_group_table"):
            self.traffic_group_table.setVisible(expanded)
        if hasattr(self, "traffic_group_hint"):
            compact = getattr(self, "_responsive_layout_mode", "wide") == "compact"
            self.traffic_group_hint.setVisible(expanded and not compact)
        if hasattr(self, "traffic_group_title"):
            self.traffic_group_title.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        if persist:
            try:
                self.settings.set("controlfreq_traffic_group_expanded", expanded)
            except Exception:
                pass
        if hasattr(self, "freq_ctrl_box"):
            self._sync_top_panel_heights()

    def _open_traffic_action_bucket(self, bucket: str) -> None:
        if not bucket:
            return
        message_ids, count = self.traffic_action_summary.navigation_snapshot(bucket)
        host = self.window()
        if hasattr(host, "open_messages_section"):
            host.open_messages_section(
                "inbox",
                action_filter=bucket,
                traffic_message_ids=message_ids,
                traffic_count_at_click=count,
                age_filter_seconds=self._traffic_age_seconds(),
                group_filter=str(self.group_combo.currentData() or ""),
                source_family=str(self.traffic_source_combo.currentData() or ""),
            )

    def _open_traffic_group_row(self, item: QTableWidgetItem) -> None:
        table = getattr(self, "traffic_group_table", None)
        if table is None:
            return
        group = str(item.data(Qt.UserRole) or "").strip()
        if not group:
            group_item = table.item(item.row(), 0)
            group = str(group_item.text() if group_item is not None else "").strip()
        if not group or group in {"No traffic", "DIRECT", "UNASSIGNED"}:
            return
        host = self.window()
        if hasattr(host, "open_messages_section"):
            host.open_messages_section(
                "inbox",
                group_filter=group,
                age_filter_seconds=self._traffic_age_seconds(),
                source_family=str(self.traffic_source_combo.currentData() or ""),
            )

    def _ensure_message_summary_executor(self) -> ThreadPoolExecutor:
        if self._message_summary_executor is None:
            self._message_summary_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="fio-controlfreq-summary",
            )
        return self._message_summary_executor

    def _shutdown_message_summary_executor(self) -> None:
        executor = self._message_summary_executor
        self._message_summary_executor = None
        if executor is None:
            return
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)
        except Exception as exc:
            log.debug("ControlFreq: message summary executor shutdown failed: %s", exc)

    def _schedule_message_summary_refresh(self) -> None:
        if self._message_summary_pending:
            self._message_summary_followup = True
            if not self._message_summary_cache_rows:
                self._set_table_rows(self.inbox_table, [["Checking messages...", "", ""]])
            return
        search = (self.search_edit.text() or "").strip().upper()
        try:
            group_filter = normalize_group_name(self.group_combo.currentData())
        except Exception:
            group_filter = ""
        try:
            local_operator_call = str(self.settings.get("operator_callsign", "") or "").strip().upper()
        except Exception:
            local_operator_call = ""
        try:
            message_paths = self.settings.get("message_paths", {}) or {}
        except Exception:
            message_paths = {}
        if not isinstance(message_paths, dict):
            message_paths = {}
        flmsg_dir_txt = str(message_paths.get("flmsg", "") or "").strip()
        flamp_dir_txt = str(message_paths.get("flamp", "") or "").strip()
        db_path = self._db_path()
        traffic_age_seconds = self._traffic_age_seconds()
        source_family = str(self.traffic_source_combo.currentData() or "").strip().lower()
        now_ts = time.time()
        hf_groups, local_groups = configured_group_names(self.settings)
        self._message_summary_request_id += 1
        request_id = self._message_summary_request_id
        self._message_summary_pending = True
        if not self._message_summary_cache_rows:
            self._set_table_rows(self.inbox_table, [["Checking messages...", "", ""]])

        def _work() -> Dict[str, object]:
            message_rows = self._collect_inbox_rows(
                search,
                db_path=db_path,
                group_filter=group_filter,
                local_operator_call=local_operator_call,
            )
            bbs_rows = self._collect_bbs_rows(search)
            file_rows = self._collect_flmsg_flamp_rows(
                search,
                flmsg_dir_txt=flmsg_dir_txt,
                flamp_dir_txt=flamp_dir_txt,
            )
            context = load_operator_traffic_context(
                db_path,
                callsign=local_operator_call,
                configured_operating_groups=hf_groups,
                configured_local_groups=local_groups,
            )
            try:
                attention_rows = (
                    [
                        dict(row)
                        for row in list_projected_attention_messages(
                            db_path,
                            limit=200,
                        )
                    ]
                    if db_path.exists()
                    else []
                )
            except Exception as exc:
                log.debug("ControlFreq: actionable traffic projection unavailable: %s", exc)
                attention_rows = []
            scoped_rows = filter_traffic_messages(
                attention_rows,
                age_seconds=traffic_age_seconds,
                now_ts=now_ts,
                source_family=source_family,
                group_filter=group_filter,
            )
            return {
                "source_rows": self._message_summary_rows(message_rows, bbs_rows, file_rows),
                "traffic_summary": build_traffic_action_summary(scoped_rows, context),
                "traffic_group_volumes": load_projected_traffic_group_volumes(
                    db_path,
                    age_seconds=traffic_age_seconds,
                    now_ts=now_ts,
                    source_family=source_family,
                    group_filter=group_filter,
                    operator_groups=context.groups,
                ),
            }

        future = self._ensure_message_summary_executor().submit(_work)
        future.add_done_callback(lambda done, rid=request_id: self._handle_message_summary_future(rid, done))

    def _handle_message_summary_future(self, request_id: int, future: Future) -> None:
        payload: Dict[str, object]
        error = ""
        try:
            result = future.result()
            payload = result if isinstance(result, dict) else {}
        except Exception as exc:
            error = str(exc)
            payload = {
                "source_rows": [["Messages", "0", "Message summary unavailable"]],
                "traffic_summary": TrafficActionSummary(),
            }
        try:
            self._message_summary_ready.emit(int(request_id), payload, error)
        except RuntimeError:
            pass

    def _on_message_summary_ready(self, request_id: int, rows: object, error: object) -> None:
        self._message_summary_pending = False
        if request_id != int(getattr(self, "_message_summary_request_id", 0) or 0):
            return
        if request_id < self._message_summary_applied_id:
            return
        if error:
            log.debug("ControlFreq: message summary worker failed: %s", error)
        payload = rows if isinstance(rows, dict) else {}
        rows_value = payload.get("source_rows", [])
        rows_out = rows_value if isinstance(rows_value, list) else [["Messages", "0", "Message summary unavailable"]]
        traffic_summary = payload.get("traffic_summary")
        if not isinstance(traffic_summary, TrafficActionSummary):
            traffic_summary = TrafficActionSummary()
        self._message_summary_applied_id = int(request_id)
        self._message_summary_cache_rows = rows_out
        self.traffic_action_summary.set_summary(traffic_summary)
        volume_rows = payload.get("traffic_group_volumes", ())
        self._render_traffic_group_volumes(
            tuple(row for row in volume_rows if isinstance(row, TrafficGroupVolume))
            if isinstance(volume_rows, (tuple, list))
            else ()
        )
        self._set_table_rows(self.inbox_table, rows_out)
        self._style_message_summary_rows()
        self._apply_elide_tooltips(self.inbox_table, 2)
        if self._message_summary_followup:
            self._message_summary_followup = False
            QTimer.singleShot(0, self._schedule_message_summary_refresh)

    def _render_traffic_group_volumes(self, volumes: tuple[TrafficGroupVolume, ...]) -> None:
        table = getattr(self, "traffic_group_table", None)
        if table is None:
            return
        if hasattr(self, "dashboard"):
            self._dashboard_traffic_volumes = volumes
        overview = hasattr(self, "dashboard") and self.dashboard.current_page == "overview"
        visible_volumes = volumes[:4 if overview else 8]
        selected = table.currentItem().data(Qt.UserRole) if table.currentItem() is not None else None
        table.setUpdatesEnabled(False)
        table.clearSpans()
        table.setRowCount(0)
        if not visible_volumes:
            table.insertRow(0)
            empty_item = QTableWidgetItem("No traffic in the current scope")
            empty_item.setFlags(empty_item.flags() & ~Qt.ItemIsEditable)
            empty_item.setToolTip("No projected traffic matches the selected age, group, and source scope.")
            table.setItem(0, 0, empty_item)
            table.setSpan(0, 0, 1, 3)
        else:
            scale = max(
                1,
                max(max(volume.current_count, volume.previous_count) for volume in visible_volumes),
            )
            row_height = max(42, (QFontMetrics(table.font()).height() * 2) + 10)
            for row_index, volume in enumerate(visible_volumes):
                table.insertRow(row_index)
                source_summary = self._traffic_source_summary(volume.sources)
                source_detail = " · ".join(
                    f"{source} {count}" for source, count in volume.sources
                ) or "Unknown source"
                latest = self._relative_traffic_age(volume.latest_ts)
                trend_detail = (
                    f"{volume.trend} · {volume.unread_count} new · "
                    f"{source_summary} · latest {latest}"
                )
                tooltip = (
                    f"{volume.group}: {volume.current_count} in the current window; "
                    f"{volume.previous_count} in the prior equal window; {volume.unread_count} new. "
                    f"Trend: {volume.trend}. Latest: {latest}. Sources: {source_detail}. "
                    f"{'Associated operating or membership group. ' if volume.is_operator_group else ''}"
                    "Double-click or press Enter to open this group in Messages."
                )

                group_item = QTableWidgetItem(volume.group)
                group_font = group_item.font()
                group_font.setBold(volume.is_operator_group)
                group_item.setFont(group_font)

                comparison_label = (
                    f"{volume.current_count} total"
                    if volume.trend == "All time"
                    else f"{volume.current_count} current · {volume.previous_count} prior"
                )
                if overview:
                    comparison_label = f"{volume.current_count} total" if volume.trend == "All time" else f"{volume.current_count} / {volume.previous_count}"
                    detail_item_text = f"{volume.trend} · {volume.unread_count} new"
                else:
                    detail_item_text = trend_detail
                bar_item = QTableWidgetItem(comparison_label)
                bar_item.setData(TRAFFIC_CHART_CURRENT_ROLE, volume.current_count)
                bar_item.setData(TRAFFIC_CHART_PREVIOUS_ROLE, volume.previous_count)
                bar_item.setData(TRAFFIC_CHART_SCALE_ROLE, scale)
                bar_item.setData(TRAFFIC_CHART_TREND_ROLE, volume.trend)

                detail_item = QTableWidgetItem(detail_item_text)
                for chart_item in (group_item, bar_item, detail_item):
                    chart_item.setFlags(chart_item.flags() & ~Qt.ItemIsEditable)
                    chart_item.setData(Qt.UserRole, volume.group)
                    chart_item.setToolTip(tooltip)
                    chart_item.setSizeHint(QSize(-1, row_height))
                table.setItem(row_index, 0, group_item)
                table.setItem(row_index, 1, bar_item)
                table.setItem(row_index, 2, detail_item)
                table.setRowHeight(row_index, row_height)
        table.setUpdatesEnabled(True)
        table.clearSelection()
        table.setCurrentCell(-1, -1)
        if selected is not None:
            for row_index, volume in enumerate(visible_volumes):
                if volume.group == selected:
                    table.setCurrentCell(row_index, 0)
                    break
        total = sum(volume.current_count for volume in volumes)
        unread = sum(volume.unread_count for volume in volumes)
        increasing = sum(
            1 for volume in volumes if volume.trend.startswith(("Spike", "Rising"))
        )
        aggregate = f"Traffic by group · {total} total / {unread} new · {increasing} increasing"
        self.traffic_group_title.setText(f"Traffic by group · {increasing} increasing" if hasattr(self, "dashboard") else aggregate)
        self.traffic_group_title.setToolTip(aggregate + " · Show or hide group detail")
        if hasattr(self, "dashboard"):
            self.inbox_box.setTitle(f"Traffic · {total} total / {unread} new")
        self._fit_table_height_to_rows(table, min_rows=1, max_rows=4 if overview else 5, empty_rows=1)
        self._sync_top_panel_heights()
        self._apply_ops_table_column_layout(
            getattr(self, "_responsive_layout_mode", "wide") == "compact"
        )
        self._refresh_traffic_group_chart_theme()

    def _refresh_traffic_group_chart_theme(self) -> None:
        table = getattr(self, "traffic_group_table", None)
        if table is None:
            return
        try:
            delegate = getattr(self, "traffic_group_bar_delegate", None)
            if isinstance(delegate, TrafficVolumeBarDelegate):
                delegate.apply_theme(self._theme())
            table.viewport().update()
        except Exception:
            pass

    @staticmethod
    def _traffic_source_summary(sources: tuple[tuple[str, int], ...]) -> str:
        if not sources:
            return "Unknown"
        visible = [f"{source} {count}" for source, count in sources[:3]]
        remaining = len(sources) - len(visible)
        if remaining > 0:
            visible.append(f"+{remaining}")
        return " · ".join(visible)

    @staticmethod
    def _relative_traffic_age(timestamp: float) -> str:
        if not timestamp:
            return "—"
        seconds = max(0, int(time.time() - float(timestamp)))
        if seconds < 60:
            return "now"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"

    @staticmethod
    def _message_summary_rows(
        message_rows: List[List[str]],
        bbs_rows: List[List[str]],
        file_rows: Optional[List[List[str]]] = None,
    ) -> List[List[str]]:
        rows_out: List[List[str]] = []
        rows_out.extend(message_rows)
        rows_out.extend(bbs_rows)
        rows_out.extend(file_rows or [])
        if rows_out and rows_out[0][0] not in {"No matches", "No data"}:
            rows_out.sort(key=lambda row: str(row[0]).strip().upper())
        return rows_out

    def _style_message_summary_rows(self) -> None:
        palette = self._urgency_palette()
        for row in range(self.inbox_table.rowCount()):
            type_item = self.inbox_table.item(row, 0)
            count_item = self.inbox_table.item(row, 1)
            detail_item = self.inbox_table.item(row, 2)
            if type_item is None or count_item is None:
                continue
            label = (type_item.text() or "").strip().upper()
            try:
                count_val = int((count_item.text() or "0").strip())
            except Exception:
                count_val = 0
            detail_txt = (detail_item.text() if detail_item else "").strip()
            if label == "VARAC BBS FILES":
                detail_up = detail_txt.upper()
                if "MISSING" in detail_up or "AGING" in detail_up or "DUE NOW" in detail_up or "DUE SOON" in detail_up:
                    tone = palette["warn"]
                elif count_val > 0:
                    tone = palette["positive"]
                else:
                    tone = None
                if tone is not None:
                    for c in range(self.inbox_table.columnCount()):
                        it = self.inbox_table.item(row, c)
                        if it:
                            it.setBackground(tone)
                            it.setForeground(palette["text"])
                continue
            if label == "VARAC BBS" and detail_item and detail_item.text().strip() not in {"", "-"}:
                for c in range(self.inbox_table.columnCount()):
                    it = self.inbox_table.item(row, c)
                    if it:
                        it.setBackground(palette["warn"])
                        it.setForeground(palette["text"])
                continue
            if label.startswith("SITREP"):
                red_ct = 0
                if detail_item:
                    txt = (detail_item.text() or "").upper()
                    try:
                        red_ct = int(txt.split("R:", 1)[1].split()[0])
                    except Exception:
                        red_ct = 0
                tone = palette["warn"] if red_ct > 0 else palette["positive"]
                for c in range(self.inbox_table.columnCount()):
                    it = self.inbox_table.item(row, c)
                    if it:
                        it.setBackground(tone)
                        it.setForeground(palette["text"])
                continue
            if count_val > 0:
                for c in range(self.inbox_table.columnCount()):
                    it = self.inbox_table.item(row, c)
                    if it:
                        it.setBackground(palette["positive"])
                        it.setForeground(palette["text"])

    def _collect_inbox_rows(
        self,
        search: str,
        *,
        db_path: Path,
        group_filter: str,
        local_operator_call: str,
    ) -> List[List[str]]:
        if not db_path.exists():
            return [["No data", "0", "Messages DB unavailable"]]
        counts = {"JS8": 0, "Spotter": 0, "CommStat": 0, "VarAC": 0}
        top_senders: Dict[str, Dict[str, int]] = {
            "JS8": {}, "Spotter": {}, "CommStat": {}, "VarAC": {}
        }
        sitrep_counts = {"red": 0, "yellow": 0, "green": 0}

        def _load_operator_groups(cur: sqlite3.Cursor) -> Dict[str, Set[str]]:
            out: Dict[str, Set[str]] = {}
            try:
                cur.execute(
                    """
                    SELECT callsign, group1, group2, group3, groups_json
                    FROM operator_checkins
                    """
                )
                for callsign, g1, g2, g3, groups_json in cur.fetchall():
                    cs = (callsign or "").strip().upper()
                    if not cs:
                        continue
                    groups: Set[str] = set()
                    for g in (g1, g2, g3):
                        gg = normalize_group_name(g)
                        if gg:
                            groups.add(gg)
                    try:
                        parsed = json.loads(groups_json) if groups_json else []
                        if isinstance(parsed, list):
                            for g in parsed:
                                gg = normalize_group_name(g)
                                if gg:
                                    groups.add(gg)
                    except Exception:
                        pass
                    out[cs] = groups
            except Exception:
                return {}
            return out

        def _status_word(key: str) -> str:
            return "RED" if key == "red" else ("YELLOW" if key == "yellow" else "GREEN")

        def _source_terms(source_summary_json: object) -> Set[str]:
            terms: Set[str] = set()
            try:
                parsed = json.loads(source_summary_json) if source_summary_json else {}
                if isinstance(parsed, dict):
                    for raw_source in parsed.keys():
                        raw = str(raw_source or "").strip().upper()
                        if raw:
                            terms.add(raw)
                            terms.add(source_family_label(raw).upper())
            except Exception:
                pass
            return terms

        def _matches_sitrep_search(
            callsign: str,
            status_key: str,
            report_group: str = "",
            source_summary_json: object = "",
        ) -> bool:
            if not search:
                return True
            terms = {"SITREP", callsign, _status_word(status_key), normalize_group_name(report_group)}
            terms.update(_source_terms(source_summary_json))
            return any(search in term for term in terms if term)

        try:
            conn = connect_sqlite(db_path, timeout=1.5, busy_timeout_ms=1500)
            cur = conn.cursor()
            cur.execute("SELECT from_call, state FROM js8_messages")
            for cs, state in cur.fetchall():
                if (state or "").upper() == "READ":
                    continue
                cs = (cs or "").strip().upper()
                if search and search not in cs and search not in "JS8":
                    continue
                counts["JS8"] += 1
                top_senders["JS8"][cs] = top_senders["JS8"].get(cs, 0) + 1
            cur.execute("SELECT from_call, state FROM spotter_traffic")
            for cs, state in cur.fetchall():
                if (state or "").upper() == "READ":
                    continue
                cs = (cs or "").strip().upper()
                if search and search not in cs and search not in "SPOTTER":
                    continue
                counts["Spotter"] += 1
                top_senders["Spotter"][cs] = top_senders["Spotter"].get(cs, 0) + 1
            cur.execute("SELECT from_call, read_status FROM varac_messages")
            for cs, read_status in cur.fetchall():
                if int(read_status or 0) != 0:
                    continue
                cs = (cs or "").strip().upper()
                if search and search not in cs and search not in "VARAC":
                    continue
                counts["VarAC"] += 1
                top_senders["VarAC"][cs] = top_senders["VarAC"].get(cs, 0) + 1
            try:
                cur.execute(
                    """
                    SELECT from_call, read_state, status, group_name
                      FROM message_projection
                     WHERE deleted=0 AND archived=0
                       AND LOWER(source_family) IN ('commstat', 'commstat_rf')
                    """
                )
                for cs, read_state, status, projected_group in cur.fetchall():
                    state = str(read_state or "").strip().lower()
                    status_up = str(status or "").strip().upper()
                    if state == "read":
                        continue
                    if state not in {"new", "unread", "alert"} and status_up not in {
                        "NEW", "UNREAD", "ALERT", "YELLOW", "RED"
                    }:
                        continue
                    cs = (cs or "").strip().upper()
                    projected_group = normalize_group_name(projected_group)
                    if group_filter and projected_group != group_filter:
                        continue
                    if search and not any(
                        search in value
                        for value in (cs, projected_group, "COMMSTAT")
                        if value
                    ):
                        continue
                    counts["CommStat"] += 1
                    top_senders["CommStat"][cs] = top_senders["CommStat"].get(cs, 0) + 1
            except Exception:
                pass
            operator_groups: Dict[str, Set[str]] = {}
            local_operator_groups: Set[str] = set()
            if group_filter or local_operator_call:
                operator_groups = _load_operator_groups(cur)
                if local_operator_call:
                    local_operator_groups = operator_groups.get(local_operator_call, set())
            loaded_unified_sitreps = False
            try:
                cur.execute(
                    """
                    SELECT callsign, effective_status, latest_report_group, source_summary_json
                    FROM sitrep_latest_by_callsign
                    """
                )
                unified_rows = cur.fetchall()
                loaded_unified_sitreps = bool(unified_rows)
                for cs, status_key, report_group, source_summary_json in unified_rows:
                    cs_up = (cs or "").strip().upper()
                    key = (status_key or "").strip().lower()
                    if key == "not_reported":
                        key = "unknown"
                    if not cs_up or key not in sitrep_counts:
                        continue
                    group = normalize_group_name(report_group)
                    if not group or group not in local_operator_groups:
                        continue
                    if group_filter:
                        if group != group_filter:
                            continue
                    if not _matches_sitrep_search(cs_up, key, group, source_summary_json):
                        continue
                    sitrep_counts[key] += 1
            except Exception:
                pass
            if not loaded_unified_sitreps:
                try:
                    cur.execute("SELECT from_call, status_key FROM spotter_station_status")
                    for cs, status_key in cur.fetchall():
                        cs_up = (cs or "").strip().upper()
                        key = (status_key or "").strip().lower()
                        if not cs_up or key not in sitrep_counts:
                            continue
                        if not local_operator_groups:
                            continue
                        station_groups = operator_groups.get(cs_up, set())
                        if local_operator_groups and not station_groups.intersection(local_operator_groups):
                            continue
                        if group_filter and group_filter not in station_groups:
                            continue
                        if not _matches_sitrep_search(cs_up, key):
                            continue
                        sitrep_counts[key] += 1
                except Exception:
                    pass
            conn.close()
        except Exception as e:
            log.debug("ControlFreq: inbox summary load failed: %s", e)
        rows_out: List[List[str]] = []
        display_labels = {
            "JS8": "JS8",
            "Spotter": "Spotter",
            "CommStat": "CommStat",
            "VarAC": "VarAC Direct",
        }
        for key in ("JS8", "Spotter", "CommStat", "VarAC"):
            if search and search not in key.upper() and counts[key] == 0:
                continue
            senders = sorted(top_senders[key].items(), key=lambda kv: kv[1], reverse=True)[:3]
            sender_txt = ", ".join([f"{c}({n})" for c, n in senders])
            if sender_txt:
                sender_txt = f"Unread from {sender_txt}"
            elif key == "VarAC":
                sender_txt = "No unread direct VarAC messages"
            else:
                sender_txt = "No unread messages"
            rows_out.append([display_labels[key], str(counts[key]), sender_txt])
        sitrep_total = sitrep_counts["red"] + sitrep_counts["yellow"] + sitrep_counts["green"]
        sitrep_details = (
            "Aggregated station status · "
            f"R:{sitrep_counts['red']}  Y:{sitrep_counts['yellow']}  G:{sitrep_counts['green']}"
        )
        if not search or search in "SITREP" or sitrep_total > 0:
            rows_out.append(["SitRep Summary", str(sitrep_total), sitrep_details])
        return rows_out or [["No matches", "0", "-"]]

    def _collect_flmsg_flamp_rows(
        self,
        search: str,
        *,
        flmsg_dir_txt: str,
        flamp_dir_txt: str,
    ) -> List[List[str]]:
        label = "FLMsg / FLAmp"
        if search and search not in "FLMSG" and search not in "FLAMP" and search not in label.upper():
            return []

        now_ts = time.time()
        cutoff_ts = now_ts - FLMSG_FLAMP_RECENT_SECONDS
        stats: Dict[str, int] = {"FLMsg": 0, "FLAmp": 0}
        scanned_cap_hit = False

        def _scan_recent(root_txt: str, bucket: str) -> None:
            nonlocal scanned_cap_hit
            root = Path(root_txt).expanduser() if root_txt else None
            if not root or not root.exists() or not root.is_dir():
                return
            dirs_seen = 0
            files_seen = 0
            stack: List[Path] = [root]
            while stack and dirs_seen < FLMSG_FLAMP_SUMMARY_MAX_DIRS and files_seen < FLMSG_FLAMP_SUMMARY_MAX_FILES:
                current = stack.pop()
                dirs_seen += 1
                try:
                    children = current.iterdir()
                except OSError:
                    continue
                for child in children:
                    if files_seen >= FLMSG_FLAMP_SUMMARY_MAX_FILES:
                        scanned_cap_hit = True
                        break
                    name = child.name
                    if name.startswith("."):
                        continue
                    try:
                        if child.is_dir():
                            if dirs_seen + len(stack) < FLMSG_FLAMP_SUMMARY_MAX_DIRS:
                                stack.append(child)
                            else:
                                scanned_cap_hit = True
                            continue
                        if not child.is_file():
                            continue
                        files_seen += 1
                        if child.suffix.lower() not in FLMSG_FLAMP_SUMMARY_EXTS:
                            continue
                        st = child.stat()
                    except OSError:
                        continue
                    if float(st.st_mtime) >= cutoff_ts:
                        stats[bucket] += 1
            if stack:
                scanned_cap_hit = True

        _scan_recent(flmsg_dir_txt, "FLMsg")
        _scan_recent(flamp_dir_txt, "FLAmp")

        total = int(stats["FLMsg"] + stats["FLAmp"])
        if not flmsg_dir_txt and not flamp_dir_txt:
            detail = "FLMsg and FLAmp folders not configured"
        elif total:
            detail = f"Last 24h: FLMsg {stats['FLMsg']}, FLAmp {stats['FLAmp']}"
        else:
            detail = "No new FLMsg or FLAmp files in 24h"
        if scanned_cap_hit:
            detail += " (large folder, showing bounded count)"
        return [[label, str(total), detail]]

    def _collect_bbs_rows(self, search: str) -> List[List[str]]:
        worker_settings = SettingsManager()
        try:
            inventory = build_bbs_inventory(worker_settings)
        finally:
            try:
                worker_settings.close()
            except Exception:
                pass
        count = str(inventory.live_file_count if inventory.bbs_enabled and inventory.live_exists else 0)
        detail = ""
        if not inventory.bbs_enabled:
            detail = "BBS disabled"
        elif not inventory.live_dir:
            detail = "BBS folder not configured"
        elif not inventory.live_exists:
            detail = "BBS folder missing"
        else:
            detail_parts: List[str] = []
            if inventory.live_file_count:
                detail_parts.append(f"{inventory.live_file_count} files in BBS folder")
            else:
                detail_parts.append("No BBS files needing attention")
            aging_total = int(inventory.live_due_now_count or 0) + int(inventory.live_due_soon_count or 0)
            if aging_total:
                detail_parts.append(f"{aging_total} aging soon")
            if inventory.vault_enabled:
                detail_parts.append("Managed BBS Library active")
            detail = " | ".join(detail_parts)
        row = ["VarAC BBS Files", count, detail or "-"]
        haystack_parts = [row[0], "VarAC BBS", row[1], row[2], inventory.live_dir]
        try:
            haystack_parts.append(format_bbs_inventory_detail(inventory))
        except Exception:
            pass
        haystack_parts.extend(loc.name for loc in inventory.locations)
        haystack_parts.extend(loc.alias for loc in inventory.locations)
        haystack_parts.extend(loc.source_dir for loc in inventory.locations)
        if not search or any(search in str(part or "").upper() for part in haystack_parts):
            return [row]
        return [["No matches", "0", "-"]]

    def _refresh_inbox(self) -> None:
        self._refresh_message_summary()

    def _refresh_bbs_files(self) -> None:
        self._refresh_message_summary()

    @staticmethod
    def _append_section_row_to(
        table: QTableWidget,
        text: str,
        col1: str = "",
        col2: str = "",
    ) -> None:
        row = table.rowCount()
        table.insertRow(row)
        labels = [text, col1, col2]
        use_three_cols = bool(col1 or col2)
        for col, val in enumerate(labels):
            if not use_three_cols and col > 0:
                continue
            item = QTableWidgetItem(val if val else "")
            item.setFlags(item.flags() ^ Qt.ItemIsEditable)
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            item.setBackground(table.palette().alternateBase())
            item.setForeground(table.palette().text())
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            table.setItem(row, col, item)
        if not use_three_cols:
            table.setSpan(row, 0, 1, table.columnCount())

    @staticmethod
    def _append_rows_to(table: QTableWidget, rows: List[List[str]]) -> None:
        for row in rows:
            r = table.rowCount()
            table.insertRow(r)
            for c, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() ^ Qt.ItemIsEditable)
                table.setItem(r, c, item)

    def _set_prop_window_headers(
        self,
        dawn_local: Optional[dt.datetime] = None,
        day_start_local: Optional[dt.datetime] = None,
        sunset_local: Optional[dt.datetime] = None,
        night_end_local: Optional[dt.datetime] = None,
    ) -> None:
        if not hasattr(self, "prop_table"):
            return
        if not (dawn_local and day_start_local and sunset_local and night_end_local):
            self.prop_table.setHorizontalHeaderLabels(
                ["Zone", "Morning (Dawn-10:00)", "Day (10:00-Sunset)", "Night (Sunset-Dawn)"]
            )
            return
        morning_from = dawn_local.strftime("%H:%M")
        day_from = day_start_local.strftime("%H:%M")
        day_to = sunset_local.strftime("%H:%M")
        night_to = night_end_local.strftime("%H:%M")
        self.prop_table.setHorizontalHeaderLabels(
            [
                "Zone",
                f"Morning ({morning_from}-{day_from})",
                f"Day ({day_from}-{day_to})",
                f"Night ({day_to}-{night_to})",
            ]
        )

    def _refresh_propagation_snapshot(self) -> None:
        if not self._dashboard_card_enabled("propagation"):
            return
        try:
            tz_name = self.settings.get("timezone", "UTC") or "UTC"
            tz = get_timezone(tz_name)
        except Exception:
            tz = dt.timezone.utc
        now_utc = dt.datetime.now(dt.timezone.utc)
        now_local = now_utc.astimezone(tz)

        user_grid = (
            self.settings.get("operator_grid6", "")
            or self.settings.get("operator_grid", "")
            or ""
        ).strip().upper()
        user_ll = maidenhead_to_latlon(user_grid) if user_grid else None
        if not user_ll:
            self._set_prop_window_headers()
            self._set_table_rows(self.prop_table, [])
            self.prop_hint.setText(
                "Tip: Set Grid 6 in Configuration to enable forecast."
            )
            self._set_prop_summary("RF Readiness: set operator grid to enable forecast.")
            self._render_prop_band_ladder((), target_label="", evidence_label="")
            self._sync_propagation_box_height()
            return

        dawn_local, sunset_local = self._sunrise_sunset_local(
            now_local.date(), user_ll[0], user_ll[1], tz
        )
        if dawn_local is None or sunset_local is None:
            # Fallback for polar regions
            dawn_local = now_local.replace(hour=6, minute=0, second=0, microsecond=0)
            sunset_local = now_local.replace(hour=18, minute=0, second=0, microsecond=0)
        day_start_local = now_local.replace(hour=10, minute=0, second=0, microsecond=0)
        if dawn_local.date() != now_local.date():
            dawn_local = dawn_local.replace(
                year=now_local.year, month=now_local.month, day=now_local.day
            )
        if sunset_local.date() != now_local.date():
            sunset_local = sunset_local.replace(
                year=now_local.year, month=now_local.month, day=now_local.day
            )
        night_start = sunset_local
        night_end = dawn_local + dt.timedelta(days=1) if dawn_local <= sunset_local else dawn_local
        self._set_prop_window_headers(dawn_local, day_start_local, sunset_local, night_end)

        schedule_entries = self._load_today_schedule_local(now_local, tz)
        band_rows: Dict[str, List[Tuple[dt.datetime, str]]] = {}
        for when_local, band, label in schedule_entries:
            if not band:
                continue
            band_rows.setdefault(band, []).append((when_local, label))

        # Build window bands from schedule
        window_bands = {"morning": set(), "day": set(), "night": set()}
        for band, entries in band_rows.items():
            entries.sort(key=lambda e: e[0])
            for when_local, _label in entries:
                if dawn_local <= when_local < day_start_local:
                    window_bands["morning"].add(band)
                if day_start_local <= when_local < sunset_local:
                    window_bands["day"].add(band)
                if when_local >= night_start or when_local < dawn_local:
                    window_bands["night"].add(band)

        all_bands = sorted(band_rows.keys(), key=lambda b: (b not in PROP_BANDS, b))
        target_label, regional_points, target_type, target_id = self._resolve_prop_target_points()
        national_points = [STATE_CENTERS[s] for s in LOWER48_STATES if s in STATE_CENTERS]
        blend_settings = self._blend_settings_snapshot()

        # Capture immutable inputs on the GUI thread, then run propagation
        # scoring off-thread.  Mature empirical history can make this operation
        # CPU-heavy; it must never monopolize Qt's event loop during first paint
        # or a tab activation.
        morning_mid = dawn_local + (day_start_local - dawn_local) / 2
        day_mid = day_start_local + (sunset_local - day_start_local) / 2
        night_mid = night_start + (night_end - night_start) / 2
        window_mid = {"morning": morning_mid, "day": day_mid, "night": night_mid}
        if self._propagation_pending:
            self._propagation_followup = True
            return
        self._propagation_request_id += 1
        request_id = self._propagation_request_id
        self._propagation_pending = True
        self._set_prop_summary("RF Readiness: updating propagation guidance…")

        def _work() -> Dict[str, object]:
            nat_scores: Dict[str, List[Tuple[str, float]]] = {}
            reg_scores: Dict[str, List[Tuple[str, float]]] = {}
            modeled_nat: Dict[str, List[Tuple[str, float]]] = {}
            modeled_reg: Dict[str, List[Tuple[str, float]]] = {}
            for window, mid_local in window_mid.items():
                bands = sorted(window_bands.get(window) or all_bands)
                nat_scores[window] = self._top_bands_modeled(
                    bands, mid_local, user_ll, points=national_points
                )
                reg_scores[window] = self._top_bands_modeled(
                    bands,
                    mid_local,
                    user_ll,
                    points=regional_points,
                    origin_grid6=user_grid,
                    target_type=target_type,
                    target_id=target_id,
                    blend_settings=blend_settings,
                )
                modeled_nat[window] = self._top_bands_modeled(
                    list(PROP_BANDS), mid_local, user_ll, points=national_points
                )
                modeled_reg[window] = self._top_bands_modeled(
                    list(PROP_BANDS),
                    mid_local,
                    user_ll,
                    points=regional_points,
                    origin_grid6=user_grid,
                    target_type=target_type,
                    target_id=target_id,
                    blend_settings=blend_settings,
                )
            return {
                "nat_scores": nat_scores,
                "reg_scores": reg_scores,
                "modeled_nat": modeled_nat,
                "modeled_reg": modeled_reg,
                "target_label": target_label,
                "user_grid": user_grid,
                "has_schedule_bands": bool(all_bands),
                "now_local": now_local,
                "blend_enabled": bool(blend_settings.get("prop_blend_enabled", 1)),
            }

        if self._propagation_executor is None:
            self._propagation_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="fio-ops-propagation",
            )
        future = self._propagation_executor.submit(_work)
        future.add_done_callback(
            lambda done, rid=request_id: self._handle_propagation_future(rid, done)
        )

    def _handle_propagation_future(self, request_id: int, future: Future) -> None:
        try:
            payload: object = future.result()
            error = ""
        except Exception as exc:
            payload = {}
            error = str(exc)
        try:
            self._propagation_ready.emit(int(request_id), payload, error)
        except RuntimeError:
            pass

    def _on_propagation_ready(self, request_id: int, payload: object, error: object) -> None:
        self._propagation_pending = False
        if request_id != self._propagation_request_id:
            return
        if error:
            log.debug("ControlFreq: propagation worker failed: %s", error)
            self._set_prop_summary("RF Readiness: propagation guidance is temporarily unavailable.")
        else:
            result = dict(payload) if isinstance(payload, dict) else {}
            nat_scores = dict(result.get("nat_scores") or {})
            reg_scores = dict(result.get("reg_scores") or {})
            modeled_nat = dict(result.get("modeled_nat") or {})
            modeled_reg = dict(result.get("modeled_reg") or {})

            def _rows(label: str, national: Dict[str, object], regional: Dict[str, object]) -> List[List[str]]:
                return [
                    [label, *(self._format_band_list(list(national.get(window) or ())) or "--" for window in ("morning", "day", "night"))],
                    ["Regional", *(self._format_band_list(list(regional.get(window) or ())) or "--" for window in ("morning", "day", "night"))],
                ]

            self._set_sectioned_prop_rows(
                "Schedule-based Forecast",
                _rows("National", nat_scores, reg_scores),
                "Modeled Forecast",
                _rows("National", modeled_nat, modeled_reg),
            )
            target_label = str(result.get("target_label") or "Region --")
            user_grid = str(result.get("user_grid") or "")
            schedule_note = "" if bool(result.get("has_schedule_bands")) else " | no scheduled bands"
            self.prop_hint.setText(f"Tip: {target_label} | origin {user_grid}{schedule_note}")
            now_local = result.get("now_local")
            if not isinstance(now_local, dt.datetime):
                now_local = dt.datetime.now(dt.timezone.utc)
            self._set_prop_summary(
                self._format_prop_readiness_summary(
                    target_label=target_label,
                    schedule_scores=reg_scores,
                    modeled_scores=modeled_reg,
                    now_local=now_local,
                )
            )
            active_window = self._prop_window_for_time(now_local)
            active_scores = list(reg_scores.get(active_window) or modeled_reg.get(active_window) or ())
            self._render_prop_band_ladder(
                active_scores,
                target_label=target_label,
                evidence_label=(
                    "modeled + observed outcomes" if bool(result.get("blend_enabled")) else "modeled"
                ),
            )
            self._sync_propagation_box_height()
        if self._propagation_followup:
            self._propagation_followup = False
            QTimer.singleShot(0, self._refresh_propagation_snapshot)

    def _set_prop_summary(self, text: str) -> None:
        label = getattr(self, "prop_summary_label", None)
        if label is None:
            return
        label.setText(str(text or "").strip() or "RF Readiness: unavailable")
        label.setToolTip(label.text())
        if hasattr(self, "dashboard"):
            self.dashboard.prop_link.setToolTip(label.text())
            self.dashboard.prop_link.setText("RF · details")

    def _render_prop_band_ladder(
        self,
        scores: Sequence[Tuple[str, float]],
        *,
        target_label: str,
        evidence_label: str,
    ) -> None:
        layout = getattr(self, "prop_band_ladder_layout", None)
        if layout is None:
            return
        if hasattr(self, "dashboard"):
            preview = " / ".join(f"{band} {int(round(score))}" for band, score in tuple(scores)[:2])
            self.dashboard.prop_link.setText("RF · " + (preview or "unavailable"))
        self._clear_widget_layout(layout)
        self.prop_band_ladder_container.setMinimumHeight(0)
        if not scores:
            empty = QLabel("No current band recommendation")
            empty.setWordWrap(True)
            layout.addWidget(empty)
            layout.addStretch(1)
            return
        theme = self._theme()
        for rank, (band, score) in enumerate(tuple(scores)[:3]):
            card = QFrame(self.prop_band_ladder_container)
            card.setObjectName("controlfreqBandLadderCard")
            bg, fg, border = self._semantic_panel_colors("secondary" if rank == 0 else "panel")
            card.setStyleSheet(
                f"QFrame#controlfreqBandLadderCard {{ background: {bg}; color: {fg}; "
                f"border: {2 if rank == 0 else 1}px solid {border}; border-radius: 6px; }}"
            )
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(8, 5, 8, 5)
            card_layout.setSpacing(3)
            title = QLabel(f"<b>{str(band).upper()}</b> · {'Best now' if rank == 0 else 'Alternative'}")
            title.setTextFormat(Qt.RichText)
            card_layout.addWidget(title)
            confidence = QProgressBar()
            confidence.setRange(0, 100)
            confidence.setValue(max(0, min(100, int(round(float(score or 0.0))))))
            confidence.setFormat(f"{self._score_to_qual(float(score or 0.0))} · {int(round(float(score or 0.0)))}")
            confidence.setAccessibleName(f"{band} RF readiness")
            confidence.setAccessibleDescription(
                f"{evidence_label or 'modeled'} confidence toward {target_label or 'selected target'}"
            )
            confidence.setStyleSheet(
                "QProgressBar { text-align: center; border: 1px solid "
                f"{theme.get('border', '#D3D7DD')}; border-radius: 4px; "
                f"background: {theme.get('surface_alt', '#E9EDF2')}; color: {theme.get('text', '#1C1F21')}; }}"
                f"QProgressBar::chunk {{ background: {theme.get('accent', '#2E6F9E')}; border-radius: 3px; }}"
            )
            card_layout.addWidget(confidence)
            evidence = QLabel(f"{evidence_label} · {target_label}")
            evidence.setStyleSheet(f"color: {theme.get('text_muted', '#5B6570')};")
            evidence.setWordWrap(True)
            card_layout.addWidget(evidence)
            # The compact forecast used to let Qt compress these cards below
            # their layout height when display or font scaling was increased.
            # Preserve enough vertical room for the title, meter, and evidence.
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            card.setMinimumHeight(card_layout.sizeHint().height())
            layout.addWidget(card, 1)
        layout.addStretch(1)
        layout.activate()
        card_heights = [
            card.layout().sizeHint().height()
            for card in self.prop_band_ladder_container.findChildren(
                QFrame, "controlfreqBandLadderCard", Qt.FindDirectChildrenOnly
            )
            if card.layout() is not None
        ]
        if card_heights:
            self.prop_band_ladder_container.setMinimumHeight(max(card_heights))

    def _format_prop_readiness_summary(
        self,
        *,
        target_label: str,
        schedule_scores: Dict[str, List[Tuple[str, float]]],
        modeled_scores: Dict[str, List[Tuple[str, float]]],
        now_local: dt.datetime,
    ) -> str:
        window = self._prop_window_for_time(now_local)
        now_band = self._first_band_label(schedule_scores.get(window) or modeled_scores.get(window) or [])
        later_window = "night" if window != "night" else "morning"
        later_band = self._first_band_label(schedule_scores.get(later_window) or modeled_scores.get(later_window) or [])
        target = str(target_label or "selected target").strip()
        if now_band and later_band and later_band != now_band:
            return f"RF Readiness: use {now_band} now toward {target}; watch {later_band} {later_window}."
        if now_band:
            return f"RF Readiness: use {now_band} now toward {target}."
        if later_band:
            return f"RF Readiness: no strong current band; watch {later_band} {later_window}."
        return f"RF Readiness: no clear band recommendation for {target}."

    @staticmethod
    def _first_band_label(scores: Sequence[Tuple[str, float]]) -> str:
        if not scores:
            return ""
        band = str(scores[0][0] or "").strip().upper()
        if not band:
            return ""
        try:
            score = float(scores[0][1])
        except Exception:
            score = 0.0
        return f"{band} ({int(round(score))})" if score > 0 else band

    @staticmethod
    def _prop_window_for_time(value: dt.datetime) -> str:
        hour = int(value.hour)
        if 6 <= hour < 10:
            return "morning"
        if 10 <= hour < 18:
            return "day"
        return "night"

    def _points_for_region(self, region_id: str) -> List[Tuple[float, float]]:
        region_id = (region_id or "").strip().upper()
        states = FEMA_REGIONS.get(region_id, []) if region_id else []
        return [STATE_CENTERS[s] for s in states if s in STATE_CENTERS and s in LOWER48_STATES]

    def _blend_settings_snapshot(self) -> Dict[str, Any]:
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

    def _resolve_prop_target_points(self) -> Tuple[str, List[Tuple[float, float]], str, str]:
        target_type = (self.settings.get("prop_target_type", "REGION") or "REGION").strip().upper()
        target_value = (self.settings.get("prop_target_value", "") or "").strip().upper()
        if hasattr(self, "prop_target_type_combo"):
            target_type = (self.prop_target_type_combo.currentData() or target_type).strip().upper()
        if hasattr(self, "prop_target_value_combo"):
            target_value = (self.prop_target_value_combo.currentText() or target_value).strip().upper()
        if target_type not in {"REGION", "STATE", "OPERATOR"}:
            target_type = "REGION"
        if target_type == "STATE":
            target_value = self._normalize_state_abbr(target_value)
        if target_type == "OPERATOR" and not self._prop_operator_geo:
            self._prop_operator_geo = self._load_prop_operator_geo()

        # Default/fallback target from local operator state -> FEMA region.
        operator_state = self._normalize_state_abbr(self.settings.get("operator_state", "") or "")
        fallback_region = STATE_TO_FEMA_REGION.get(operator_state, "")
        fallback_label = f"Region {fallback_region}" if fallback_region else "Region --"
        fallback_points = self._points_for_region(fallback_region)
        fallback_type = "REGION"
        fallback_id = fallback_region
        national_points = [STATE_CENTERS[s] for s in LOWER48_STATES if s in STATE_CENTERS]

        if target_type == "REGION":
            if target_value in {"ALL", "NATIONAL"}:
                return "National", national_points, "REGION", "NATIONAL"
            points = self._points_for_region(target_value)
            if points:
                return f"Region {target_value}", points, "REGION", target_value
            return fallback_label, fallback_points, fallback_type, fallback_id

        if target_type == "STATE":
            state_abbr = target_value if target_value in STATE_CENTERS and target_value in LOWER48_STATES else ""
            if state_abbr:
                return state_abbr, [STATE_CENTERS[state_abbr]], "STATE", state_abbr
            return fallback_label, fallback_points, fallback_type, fallback_id

        callsign = target_value
        if callsign:
            meta = self._prop_operator_geo.get(callsign, {})
            grid = (meta.get("grid") or "").strip().upper()
            state_abbr = (meta.get("state") or "").strip().upper()
            ll = maidenhead_to_latlon(grid) if grid else None
            if ll:
                return callsign, [ll], "OPERATOR", callsign
            if state_abbr in STATE_CENTERS and state_abbr in LOWER48_STATES:
                return callsign, [STATE_CENTERS[state_abbr]], "OPERATOR", callsign
        return fallback_label, fallback_points, fallback_type, fallback_id

    def _load_today_schedule_local(
        self, now_local: dt.datetime, tz: dt.tzinfo
    ) -> List[Tuple[dt.datetime, str, str]]:
        entries: List[Tuple[dt.datetime, str, str]] = []
        today_local = now_local.date()
        now_utc = now_local.astimezone(dt.timezone.utc)
        today_utc_name = now_utc.strftime("%A")
        group_filter = self.group_combo.currentData() or ""
        search = (self.search_edit.text() or "").strip().upper()

        def parse_hhmm(value: str) -> Optional[Tuple[int, int]]:
            txt = (value or "").strip()
            if not txt:
                return None
            parts = txt.split(":")
            if len(parts) == 1 and txt.isdigit() and len(txt) in (3, 4):
                h = int(txt[:-2])
                m = int(txt[-2:])
            elif len(parts) == 2:
                h = int(parts[0])
                m = int(parts[1])
            else:
                return None
            if h < 0 or h > 23 or m < 0 or m > 59:
                return None
            return h, m

        # HF schedule from settings DB
        for row in self._daily_schedule_rows():
            day = (row.get("day_utc") or row.get("day") or "ALL").strip().upper()
            if day not in {"ALL", today_utc_name.upper()}:
                continue
            start_hm = row.get("start_utc") or row.get("start") or ""
            hm = parse_hhmm(str(start_hm))
            if not hm:
                continue
            band = (row.get("band") or "").strip().upper()
            label = (row.get("group_name") or row.get("group") or "").strip().upper()
            if group_filter and label != group_filter:
                continue
            if search and search not in label and search not in band:
                continue
            start_utc = now_utc.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            start_local = start_utc.astimezone(tz)
            if start_local.date() == today_local:
                entries.append((start_local, band, label))

        # Net schedule from nets DB
        for row in self._net_schedule_rows():
            day = (row.get("day_utc") or row.get("day") or "ALL").strip().upper()
            if day not in {"ALL", today_utc_name.upper()}:
                continue
            start_hm = row.get("start_utc") or row.get("start") or ""
            hm = parse_hhmm(str(start_hm))
            if not hm:
                continue
            band = (row.get("band") or "").strip().upper()
            label = (row.get("net_name") or row.get("group_name") or "").strip().upper()
            if group_filter and label != group_filter:
                continue
            if search and search not in label and search not in band:
                continue
            start_utc = now_utc.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            start_local = start_utc.astimezone(tz)
            if start_local.date() == today_local:
                entries.append((start_local, band, label))
        return entries

    def _top_bands_modeled(
        self,
        bands: List[str],
        mid_local: dt.datetime,
        user_ll: Tuple[float, float],
        points: List[Tuple[float, float]],
        origin_grid6: str = "",
        target_type: str = "",
        target_id: str = "",
        blend_settings: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[str, float]]:
        return self._prop_service.top_bands_modeled(
            bands=bands,
            mid_utc=mid_local.astimezone(dt.timezone.utc),
            user_ll=user_ll,
            points=points,
            origin_grid6=origin_grid6,
            target_type=target_type,
            target_id=target_id,
            blend_settings=blend_settings,
            limit=2,
        )

    def _format_band_list(self, bands: List[Tuple[str, float]]) -> str:
        if not bands:
            return ""
        out = []
        for band, score in bands:
            qual = self._score_to_qual(score)
            out.append(f"{band} ({qual})")
        return "/".join(out)

    @staticmethod
    def _score_to_qual(score: float) -> str:
        if score >= 70:
            return "HIGH"
        if score >= 40:
            return "MED"
        return "LOW"

    def _format_best_band(
        self, nat: List[Tuple[str, float]], reg: List[Tuple[str, float]]
    ) -> str:
        if reg:
            band, score = reg[0]
            return f"{band} ({self._score_to_qual(score)})"
        if nat:
            band, score = nat[0]
            return f"{band} ({self._score_to_qual(score)})"
        return ""

    def _set_sectioned_prop_rows(
        self,
        label_a: str,
        rows_a: List[List[str]],
        label_b: str,
        rows_b: List[List[str]],
    ) -> None:
        self.prop_table.setRowCount(0)
        self._append_section_row(label_a)
        self._append_rows(rows_a)
        self._append_section_row(label_b)
        self._append_rows(rows_b)
        self.prop_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._fit_table_height_to_rows(self.prop_table, min_rows=0, max_rows=6, empty_rows=0)

    def _sync_propagation_box_height(self) -> None:
        if hasattr(self, "dashboard"):
            return
        box = getattr(self, "prop_box", None)
        if box is None:
            return
        try:
            details_visible = bool(getattr(getattr(self, "prop_detail_widget", None), "isVisible", lambda: False)())
            if details_visible and hasattr(self, "prop_table"):
                self._fit_table_height_to_rows(self.prop_table, min_rows=0, max_rows=6, empty_rows=0)
            box.setMinimumHeight(0)
            box.setMaximumHeight(16777215)
            if box.layout() is not None:
                box.layout().activate()
            height = max(96, int(box.sizeHint().height()) + 8)
            box.setMinimumHeight(height)
            box.updateGeometry()
            bottom_row = getattr(self, "bottom_row", None)
            if bottom_row is not None:
                bottom_row.updateGeometry()
            self._request_ops_geometry_sync()
        except Exception:
            box.setMinimumHeight(0)
            box.setMaximumHeight(16777215)

    def _request_ops_geometry_sync(self) -> None:
        """Coalesce dynamic card changes into one page-extent publication."""

        if self._ops_geometry_sync_running:
            return
        if not self._ops_geometry_timer.isActive():
            self._ops_geometry_timer.start()

    def _run_ops_geometry_sync(self) -> None:
        """Publish the current content extent to the sole page scroll owner."""

        if self._ops_geometry_sync_running:
            return
        if hasattr(self, "dashboard"):
            # Hidden detail-page hints never enlarge the dashboard. Each
            # selected page owns local overflow; outer scroll is only fallback.
            self.controlfreq_content.setMinimumHeight(0)
            self.controlfreq_root_layout.invalidate()
            self.controlfreq_root_layout.activate()
            self._ops_geometry_sync_count += 1
            return
        scroll = getattr(self, "controlfreq_scroll", None)
        content = getattr(self, "controlfreq_content", None)
        root = getattr(self, "controlfreq_root_layout", None)
        if scroll is None or content is None or root is None:
            return
        self._ops_geometry_sync_running = True
        try:
            bar = scroll.verticalScrollBar()
            old_value = int(bar.value())
            old_maximum = int(bar.maximum())
            anchored_to_bottom = old_maximum > 0 and old_value >= old_maximum - 2

            # Clear only the page-level minimum before measuring so the page
            # can shrink as well as grow. Child table/list bounds remain the
            # responsibility of their existing fit helpers.
            content.setMinimumHeight(0)
            self._sync_top_panel_heights()
            root.invalidate()
            root.activate()
            target_height = max(
                int(scroll.viewport().height()),
                int(root.minimumSize().height()),
                int(root.sizeHint().height()),
            )
            content.setMinimumHeight(target_height)
            content.updateGeometry()
            scroll.updateGeometry()
            self._ops_geometry_sync_count += 1

            def restore_scroll_position() -> None:
                try:
                    if anchored_to_bottom:
                        bar.setValue(bar.maximum())
                    else:
                        bar.setValue(min(old_value, bar.maximum()))
                except RuntimeError:
                    pass

            QTimer.singleShot(0, restore_scroll_position)
        finally:
            self._ops_geometry_sync_running = False

    def _append_section_row(self, text: str) -> None:
        row = self.prop_table.rowCount()
        self.prop_table.insertRow(row)
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() ^ Qt.ItemIsEditable)
        item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        item.setBackground(self.prop_table.palette().alternateBase())
        item.setForeground(self.prop_table.palette().text())
        self.prop_table.setItem(row, 0, item)
        self.prop_table.setSpan(row, 0, 1, self.prop_table.columnCount())

    def _append_rows(self, rows: List[List[str]]) -> None:
        for row in rows:
            r = self.prop_table.rowCount()
            self.prop_table.insertRow(r)
            for c, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() ^ Qt.ItemIsEditable)
                self.prop_table.setItem(r, c, item)

    def _sunrise_sunset_local(
        self, date_val: dt.date, lat: float, lon: float, tz: dt.tzinfo
    ) -> Tuple[Optional[dt.datetime], Optional[dt.datetime]]:
        def _calc(is_sunrise: bool) -> Optional[dt.datetime]:
            n = date_val.timetuple().tm_yday
            lng_hour = lon / 15.0
            t = n + ((6 - lng_hour) / 24.0) if is_sunrise else n + ((18 - lng_hour) / 24.0)
            m = (0.9856 * t) - 3.289
            l = m + (1.916 * math.sin(math.radians(m))) + (0.020 * math.sin(math.radians(2 * m))) + 282.634
            l = (l + 360.0) % 360.0
            ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l))))
            ra = (ra + 360.0) % 360.0
            l_quadrant = (math.floor(l / 90.0)) * 90.0
            ra_quadrant = (math.floor(ra / 90.0)) * 90.0
            ra = ra + (l_quadrant - ra_quadrant)
            ra /= 15.0
            sin_dec = 0.39782 * math.sin(math.radians(l))
            cos_dec = math.cos(math.asin(sin_dec))
            cos_h = (math.cos(math.radians(90.833)) - (sin_dec * math.sin(math.radians(lat)))) / (
                cos_dec * math.cos(math.radians(lat))
            )
            if cos_h > 1 or cos_h < -1:
                return None
            if is_sunrise:
                h = 360.0 - math.degrees(math.acos(cos_h))
            else:
                h = math.degrees(math.acos(cos_h))
            h /= 15.0
            t_local = h + ra - (0.06571 * t) - 6.622
            ut = (t_local - lng_hour) % 24.0
            dt_utc = dt.datetime(
                date_val.year, date_val.month, date_val.day, tzinfo=dt.timezone.utc
            ) + dt.timedelta(hours=ut)
            return dt_utc.astimezone(tz)

        sunrise = _calc(True)
        sunset = _calc(False)
        return sunrise, sunset

    def _modeled_band_score(
        self,
        band: str,
        user_ll: Tuple[float, float],
        dest_lat: float,
        dest_lon: float,
        now_utc: dt.datetime,
        distance_km: float,
    ) -> float:
        return self._prop_service.modeled_band_score(
            band=band,
            user_ll=user_ll,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            now_utc=now_utc,
            distance_km=distance_km,
        )

    def _load_prop_db_cache(self) -> None:
        self._prop_service.load_climatology_cache()

    def _lookup_db_score(self, band: str, lat: float, lon: float, month: int) -> Optional[float]:
        return self._prop_service.lookup_db_score(band, lat, lon, month)

    def _band_score_db(self, band: str, lat: float, lon: float, month: int) -> Optional[float]:
        return self._prop_service.band_score_db(band, lat, lon, month)

    def _band_score(self, band: str, distance_km: float, hour_utc: int) -> float:
        return self._prop_service.band_score(band, distance_km, hour_utc)

    def _diurnal_weight(self, band: str, hour_local: int) -> float:
        return self._prop_service.diurnal_weight(band, hour_local)

    def _local_hour_from_lon(self, utc_dt: dt.datetime, lon: float) -> int:
        return self._prop_service.local_hour_from_lon(utc_dt, lon)

    def _path_band_weight(self, band: str, distance_km: float, hour_local: int) -> float:
        return self._prop_service.path_band_weight(band, distance_km, hour_local)

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        return PropagationService.haversine_km(lat1, lon1, lat2, lon2)

    @staticmethod
    def _set_table_rows(table: QTableWidget, rows: List[List[str]]) -> None:
        table.setRowCount(0)
        for r, row in enumerate(rows):
            table.insertRow(r)
            for c, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() ^ Qt.ItemIsEditable)
                table.setItem(r, c, item)

    @staticmethod
    def _fit_table_height_to_rows(
        table: QTableWidget,
        *,
        min_rows: int = 1,
        max_rows: int = 8,
        empty_rows: int | None = None,
    ) -> None:
        try:
            table.resizeRowsToContents()
            actual_rows = int(table.rowCount() or 0)
            if actual_rows <= 0:
                reserve_rows = int(empty_rows if empty_rows is not None else min_rows)
                row_count = max(0, reserve_rows)
            else:
                row_count = max(int(min_rows), min(int(max_rows), actual_rows))
            header_h = (
                0
                if table.horizontalHeader().isHidden()
                else max(int(table.horizontalHeader().height()), int(table.horizontalHeader().sizeHint().height()), 24)
            )
            frame = int(table.frameWidth() or 0) * 2
            rows_h = 0
            for row in range(row_count):
                if row < actual_rows:
                    row_h = int(table.rowHeight(row))
                else:
                    row_h = int(table.verticalHeader().defaultSectionSize())
                rows_h += max(row_h, int(table.verticalHeader().defaultSectionSize()), 24)
            target_h = header_h + rows_h + frame + 8
            table.setMinimumHeight(target_h)
            table.setMaximumHeight(target_h)
        except Exception:
            pass

    def _fit_group_box_to_contents(self, group_box: QGroupBox) -> None:
        if hasattr(self, "dashboard"):
            group_box.setMinimumHeight(0)
            group_box.setMaximumHeight(16777215)
            group_box.updateGeometry()
            return
        try:
            height = ControlFreqTab._content_fit_group_height(group_box, floor=96)
            group_box.setMinimumHeight(height)
            group_box.setMaximumHeight(height)
            group_box.updateGeometry()
            self._request_ops_geometry_sync()
        except Exception:
            pass

    @staticmethod
    def _content_fit_group_height(group_box: QGroupBox, *, floor: int = 96) -> int:
        try:
            layout = group_box.layout()
            if layout is None:
                return max(int(floor), int(group_box.sizeHint().height()))
            margins = layout.contentsMargins()
            spacing = max(0, int(layout.spacing()))
            total = int(margins.top()) + int(margins.bottom()) + 28
            visible_items = 0
            for index in range(layout.count()):
                item = layout.itemAt(index)
                widget = item.widget()
                nested = item.layout()
                if widget is not None:
                    if bool(widget.isHidden()):
                        continue
                    total += int(widget.maximumHeight() if widget.maximumHeight() < 16777215 else widget.sizeHint().height())
                    visible_items += 1
                elif nested is not None:
                    total += int(nested.sizeHint().height())
                    visible_items += 1
            if visible_items > 1:
                total += spacing * (visible_items - 1)
            return max(int(floor), total + 8)
        except Exception:
            return max(int(floor), int(group_box.sizeHint().height()))

    @staticmethod
    def _apply_elide_tooltips(table: QTableWidget, col: int) -> None:
        if col < 0:
            return
        width = table.columnWidth(col) - 10
        if width <= 0:
            return
        for r in range(table.rowCount()):
            item = table.item(r, col)
            if item is None:
                continue
            text = item.text()
            fm = QFontMetrics(item.font())
            elided = fm.elidedText(text, Qt.ElideRight, width)
            if elided != text:
                item.setText(elided)
                item.setToolTip(text)
            else:
                item.setToolTip("")
