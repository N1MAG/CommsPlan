from __future__ import annotations

import datetime
from dataclasses import replace
import json
import math
import re
import sqlite3
import sys
import time
import uuid
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QVBoxLayout,
    QPushButton,
    QButtonGroup,
    QSizePolicy,
    QLabel,
    QCheckBox,
    QComboBox,
    QSpinBox,
    QGroupBox,
    QMessageBox,
    QProgressDialog,
    QDialog,
    QLayout,
    QSpacerItem,
    QScrollArea,
    QFrame,
    QStyle,
    QToolButton,
    QMenu,
    QInputDialog,
)
from PySide6.QtGui import QPixmap, QIcon, QFontMetrics, QAction, QShortcut, QKeySequence
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QMetaObject, QSize, Qt, QThread, QTimer, Signal, Slot, QSettings
from pathlib import Path

from freqinout.core.logger import log
from freqinout.core.product_identity import DISPLAY_NAME
from freqinout.core.radio_operator_controls import SETTINGS_KEY as OPERATOR_CONTROLS_KEY, radio_choices, validated_timer_policy, POLICY_MODES, POLICY_INTERVALS
from freqinout.core.logger import set_log_level
from freqinout.core.config_paths import get_config_dir
from freqinout.core.resource_catalog_migration import resource_catalog_authority_state
from freqinout.core.shortwave_store import ShortwaveStore
from freqinout.core.multi_radio_store import (
    FIRST_RUN_ONBOARDING_ACK_KEY,
    MultiRadioStore,
    SUPPORTED_RUNTIME_CONTROL_BACKENDS,
)
from freqinout.core.multi_rig_runtime_status import should_present_first_run_onboarding
from freqinout.core.navigation_intent import NavigationIntent
from freqinout.core.perf_metrics import emit_span, span as perf_span
from freqinout.core.plan_context_service import PlanContextService
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.operating_group_frequency import (
    ensure_operating_group_frequency_keys,
    format_frequency_mhz,
    normalize_frequency_hz,
    resolve_operating_group_frequency,
)
from freqinout.core.shared_state import ActionFeedbackEvent, ActionFeedbackService
from freqinout.core.station_command_state import (
    manual_qsy_meta_for_radio as station_command_manual_qsy_meta_for_radio,
    scheduler_entry_radio_id as station_command_scheduler_entry_radio_id,
    scheduler_manual_qsy_active_for_radio,
    scheduler_suspended_manually_for_radio,
    timed_suspend_active_for_radio,
)
from freqinout.core.condition_sop_audit import (
    condition_sop_audit_display,
    condition_sop_audit_observability_item,
    condition_sop_audit_summary,
)
from freqinout.core.station_runtime_manager import (
    StationRuntimeManager,
    apply_endpoint_operational_summary,
)
from freqinout.core.hardware_lifecycle import shared_hardware_lifecycle_service
from freqinout.core.scheduler_engine import SchedulerEngine
from freqinout.gui.endpoint_display import (
    project_endpoint_display, application_display_text, varac_display_text, display_activity_text,
    fldigi_receive_active,
)
from freqinout.core.receiver_qualification_service import (
    QUALIFICATION_REQUEST_ID_FIELD,
    ReceiverQualificationCoordinator,
)
from freqinout.core.scheduler_coordination import EndpointResult
from freqinout.core.background_ingest import BackgroundIngestController
from freqinout.core.message_projection_maintenance import MessageProjectionMaintenanceService
from freqinout.core.dependency_status_service import get_dependency_status_service, shutdown_dependency_status_service
from freqinout.core.dependency_health import get_dependency_health_registry
from freqinout.core.station_readiness import (
    build_station_readiness_report,
    format_readiness_issue,
    visible_status_programs,
)
from freqinout.core.js8spotter_archive import load_js8spotter_archive_records
from freqinout.core.js8_expect_runtime import build_expect_rf_guard_preflight
from freqinout.core.ingest_runtime_status import active_runtime_source_view_rows, runtime_source_view_rows_from_skip_reasons
from freqinout.core.station_health_summary import runtime_observability_items, summarize_station_health
from freqinout.core.launch_orchestrator import LAUNCH_APP_ORDER
from freqinout.core.mesh import (
    MeshConnectionWorker,
    MeshSendRequest,
    MeshSendResult,
    activate_mesh_connection_config,
    default_mesh_db_path,
    load_mesh_connection_configs,
    mesh_connection_config_key,
    mesh_health_matches_config,
)
from freqinout.core.mesh.settings import MeshConnectionConfig
from freqinout.core.ncs_session_contract import (
    active_ncs_session_flags,
    active_ncs_session_summaries_by_kind,
    clear_persisted_active_ncs_sessions,
)
from freqinout.core.source_control_rail import (
    SourceControlItem,
    source_control_mesh_items_from_configs,
)
from freqinout.radio_interface.js8_api_client import JS8ApiClientRegistry
from freqinout.core.ui_watchdog import ProcessCpuWatchdog, UiEventLoopWatchdog
from freqinout.core.worker_lifecycle import WorkerShutdownRegistry
from freqinout.core.view_contracts import (
    compose_intent_from_mapping,
    map_context_from_mapping,
    station_command_radio_from_mapping,
)
from freqinout.utils.timezones import get_timezone
from freqinout.radio_interface.rigctl_client import rig_control_client_from_settings
from freqinout.core.sdrpp_rigctl_receiver import receiver_control_client_from_profile
from freqinout.radio_interface.js8_status import JS8ControlClient, VarACStatusClient
from freqinout.radio_interface.fldigi_status import FldigiLogStatusClient
from freqinout.radio_interface.js8_rx_hub import JS8RxHub
from freqinout.version import __version__

from freqinout.gui.settings_tab import SettingsTab
from freqinout.gui.current_page_stack import CurrentPageStack
from freqinout.gui.daily_schedule_tab import DailyScheduleTab  # HF Frequency Schedule tab
from freqinout.gui.net_schedule_tab import NetScheduleTab
from freqinout.gui.fldigi_net_control_tab import FldigiNetControlTab
from freqinout.gui.net_tuning_actions import show_net_tuning_dialog
from freqinout.gui.js8call_net_control_tab import JS8CallNetControlTab
from freqinout.gui.freq_planner_tab import FreqPlannerTab
from freqinout.gui.sop_tab import SOPTab
from freqinout.gui.operator_history_tab import OperatorHistoryTab
from freqinout.gui.local_operator_tab import LocalOperatorTab
from freqinout.gui.local_ncs_tab import LocalNCSTab
from freqinout.gui.local_report_history_tab import LocalReportHistoryTab
from freqinout.gui.log_viewer import LogViewerTab
from freqinout.gui.stations_map_tab import (
    StationsMapTab,
    FEMA_REGIONS,
    LOWER48_STATES,
    STATE_CENTERS,
)
from freqinout.gui.map_window import PersistentMapWindow
from freqinout.gui.message_viewer_tab import MessageViewerTab
from freqinout.gui.peer_sched_tab import PeerSchedTab
from freqinout.gui.context_help_dialog import ContextHelpDialog
from freqinout.gui.help_tab import HelpTab
from freqinout.gui.help_registry import get_help_context
from freqinout.gui.controlfreq_tab import ControlFreqTab
from freqinout.gui.station_overview_tab import StationOverviewTab
from freqinout.gui.station_health_tab import StationHealthTab
from freqinout.gui.station_bbs_tab import StationBbsTab
from freqinout.gui.fio_spotter_tab import FioSpotterTab
from freqinout.gui.responsive_geometry import WindowScreenFitController, restore_full_window_geometry, save_full_window_geometry
from freqinout.core.latest_read_cache import LatestReadCache
from freqinout.gui.widget_updates import set_widget_value
from freqinout.gui.radio_control_panel import (
    RadioCardCallbacks,
    RadioCardDisplay,
    ResponsiveAwarenessRail,
    RadioControlDockWindow,
    RadioControlPanel,
)
from freqinout.gui.station_command_presenter import (
    application_readback_text,
    authority_text,
    planned_transition_text,
    compact_card_next_text,
    radio_activity_state_text,
    sop_action_text,
    relevant_sop_actions,
    countdown_text as station_command_countdown_text,
    frequency_controls_available,
    next_action_state,
    primary_context_text,
    qsy_action_state,
    scheduler_action_state,
    shell_layout_state,
    source_chip_text,
    timed_qsy_text,
)
from freqinout.core.sop_awareness import SopAwarenessCache, detached_sop_reader
from freqinout.gui.qsy_helper import (
    build_qsy_options,
    load_operating_groups,
    parse_frequency_mhz,
    perform_qsy,
    perform_qsy_with_hold,
    refresh_hold_duration_combo,
    selected_hold_duration,
    selected_qsy_meta,
    suspend_snapshot,
    suspend_schedule_hold,
    set_scheduler_enabled_override,
    set_active_hold_duration,
    set_suspend_until,
    resume_schedule_hold,
    set_hold_duration_default,
    set_suspend_until,
    active_hold_button_role,
    active_hold_button_text,
    active_hold_status_text,
)
from freqinout.gui.theme import (
    apply_app_theme,
    button_style,
    control_height_for_font,
    fit_child_combo_boxes,
    label_style,
    led_style,
    resolve_theme,
    resolve_ui_text_scale,
)

_MESH_RUNTIME_SHUTDOWN_GUARD: list[tuple[QThread, MeshConnectionWorker]] = []


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__("", parent)
        self._full_text = ""
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = str(text or "")
        self._update_elided_text()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_elided_text()

    def _update_elided_text(self) -> None:
        width = max(24, int(self.width() or self.sizeHint().width() or 120) - 12)
        text = self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, width)
        super().setText(text)
        if text != self._full_text and not self.toolTip():
            super().setToolTip(self._full_text)


class ReadbackLabel(ElidedLabel):
    """Prefer natural readback width while allowing compact-row elision."""

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self._full_text) + 14, hint.height())

    def minimumSizeHint(self):
        return self.sizeHint()


class MainWindow(QMainWindow):
    """
    Main application window for CommsPlan.

    Tabs:
      - Settings
      - HF Frequency Schedule
      - Net Schedule
      - FLDigi Net Control
      - JS8Call Net Control
      - Plan Builder
      - Operator History
      - Help
    """

    # Health is worker-published at a bounded cadence.  A stale cached success
    # must never remain green merely because the worker stopped publishing.
    _STATION_COMMAND_MESH_HEALTH_TTL_SECONDS = 20.0

    _message_projection_cycle_finished = Signal(object)
    _message_projection_progressed = Signal(object)
    _receiver_qualification_finished = Signal(object)
    _mesh_retry_requested = Signal(str)
    _mesh_send_requested = Signal(object)

    def __init__(self, startup_status: Callable[[str], None] | None = None):
        super().__init__()
        # A packaged Windows process can expose the native QMainWindow surface
        # while its long constructor is still establishing child geometry,
        # even though CommsPlan has not called show() yet.  Keep that surface out of
        # the compositor until main.py deliberately presents the finished
        # shell.  Other platforms retain their existing lifecycle.
        self._startup_surface_shielded = sys.platform == "win32"
        if self._startup_surface_shielded:
            self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self._startup_status_callback = startup_status
        self._shutting_down = False
        self._shutdown_close_pending = False
        self._allow_final_close = False
        self._shutdown_wait_started = 0.0
        self._shutdown_wait_last_log = 0.0
        self._shutdown_deadline_reported = False
        self._shutdown_registry = WorkerShutdownRegistry()
        self._post_shell_services_started = False
        self._first_run_onboarding_presented = False
        self._first_run_onboarding_dialog: QMessageBox | None = None
        self._background_ingest_start_pending = False
        self._message_projection_future = None
        self._message_projection_catchup_pending = False
        self._message_projection_followup_reason = ""
        self._message_projection_refresh_sequence = 0
        # Request IDs, rather than database IDs, own pending qualification
        # state.  Guided setup can test an unsaved receiver draft (profile 0),
        # and a newer test must not let a late endpoint-lane completion update
        # the dialog that initiated it.
        self._receiver_qualification_profiles: dict[str, dict[str, object]] = {}
        self._message_projection_cycle_finished.connect(
            self._on_message_projection_cycle_finished
        )
        self._message_projection_progressed.connect(
            self._on_message_projection_progressed
        )
        self._receiver_qualification_finished.connect(
            self._on_receiver_qualification_finished
        )
        self._app_active = True
        self._ui_resume_pending = False
        self._ui_refresh_dirty = False
        self._heavy_content_refresh_active = False
        self._ui_timers_paused_for_inactive = False
        self._observed_application_state = Qt.ApplicationActive
        self._ui_inactive_pending = False
        # QApplication can report an inactive launch state while the native
        # window is still being presented.  The first transition to Active is
        # startup completion, not an OS resume, and must not invalidate the
        # scheduler state established moments earlier.
        self._ui_scheduler_resume_required = False
        self._status_refresh_pending = False
        self._status_refresh_running = False
        self._station_command_refresh_pending = False
        self._station_command_refresh_force = False
        self._station_command_layout_pending = False
        self._action_feedback_geometry_pending = False
        self._settings_saved_refresh_pending = False
        self._applied_appearance_signature = None
        self._help_dialog_settle_until = 0.0
        self._ui_resume_settle_timer = QTimer(self)
        self._ui_resume_settle_timer.setSingleShot(True)
        self._ui_resume_settle_timer.setInterval(350)
        self._ui_resume_settle_timer.timeout.connect(self._on_ui_resume_settled)
        self._ui_inactive_settle_timer = QTimer(self)
        self._ui_inactive_settle_timer.setSingleShot(True)
        # Window-manager focus transfers (including opening the nonmodal Map)
        # may briefly make the application inactive. Sustained backgrounding
        # still pauses work, but a short transfer must not trigger a second
        # refresh cycle.
        self._ui_inactive_settle_timer.setInterval(1500)
        self._ui_inactive_settle_timer.timeout.connect(self._on_ui_inactive_settled)

        def _construct_startup_component(name: str, factory: Callable[[], object]):
            with perf_span(f"startup.construct.{name}", min_ms=0.0):
                return factory()

        self.settings = _construct_startup_component("settings_manager", SettingsManager)
        with perf_span("startup.construct.action_feedback", min_ms=0.0):
            self.action_feedback_service = ActionFeedbackService()
        with perf_span("startup.construct.plan_context", min_ms=0.0):
            self.plan_context_service = PlanContextService()
        self._action_feedback_unsubscribe = None
        self._notify_startup_status("Loading application settings...")
        self._clear_stale_ncs_activity_on_startup()
        self.dependency_status_service = _construct_startup_component(
            "dependency_status",
            lambda: get_dependency_status_service(self.settings),
        )
        self._shutdown_registry.register(
            "dependency_status",
            request_stop=self.dependency_status_service.stop,
            is_stopped=self.dependency_status_service.is_stopped,
        )
        self.multi_radio_store = _construct_startup_component("multi_radio_store", MultiRadioStore)
        self.station_runtime_manager = _construct_startup_component(
            "station_runtime_manager",
            lambda: StationRuntimeManager(
                store=self.multi_radio_store,
                settings=self.settings,
                receiver_client_factory=receiver_control_client_from_profile,
            ),
        )
        self.station_runtime_manager.sync_with_store(include_varac_sync_status=False)
        self.hardware_lifecycle_service = shared_hardware_lifecycle_service(self.multi_radio_store)
        self.hardware_lifecycle_service.add_listener(
            self.station_runtime_manager.set_hardware_lifecycle_snapshot
        )
        cached_hardware = self.hardware_lifecycle_service.latest()
        if cached_hardware is not None:
            self.station_runtime_manager.set_hardware_lifecycle_snapshot(cached_hardware)
        self.hardware_lifecycle_service.request_refresh()
        self._runtime_profile_signature: tuple[object, ...] | None = None
        self._active_runtime_profile = self._load_runtime_active_device_profile()
        try:
            self._station_command_profile_cache = list(
                self.multi_radio_store.list_runtime_active_device_profiles()
            )
        except Exception:
            self._station_command_profile_cache = []
        self._active_runtime_policy = self._primary_runtime_policy()
        self._suppressed_screen_labels: set[str] = set()
        self._launch_startup_suppressed = False
        self._station_health_scope_map: dict[str, str] = {}
        self._quick_search_cache: tuple[float, list[dict[str, object]]] = (0.0, [])
        self._mesh_worker_thread: QThread | None = None
        self._mesh_worker: MeshConnectionWorker | None = None
        self._mesh_runtime_signature: tuple[tuple[object, ...], ...] = tuple()
        self._mesh_runtime_restart_pending = False
        self._mesh_runtime_stopping = False
        self._mesh_manager_dialog: QDialog | None = None
        self.setWindowTitle(f"{DISPLAY_NAME} de N1MAG (v{__version__})")
        self._set_window_icon()

        # Central widget with sidebar navigation + stacked pages
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        self.setCentralWidget(central)

        # Keep the first shell limited to Settings, Ops Center, and the SOP
        # context needed by the Station Control Bar. The bar must be available
        # before secondary workspaces build tables, maps, or data indexes.
        # Secondary widgets retain stable stack slots until first navigation.
        self.settings_tab = _construct_startup_component(
            "settings_tab",
            lambda: SettingsTab(
                self,
                action_feedback_service=self.action_feedback_service,
                defer_initial_load=True,
            ),
        )
        self._sync_settings_runtime_status(
            refresh_store=False,
            include_varac_sync_status=False,
        )
        self.launch_orchestrator = self.settings_tab.launch_orchestrator
        # Launch bundles are database-backed.  Publish the small monitor
        # selection projection once at this lifecycle boundary so station-bar
        # health repaint never reconstructs a bundle.
        self._station_command_launch_monitor_cache: dict[int, tuple[tuple[str, bool], ...]] = {}
        self._refresh_station_command_launch_monitor_cache()
        self._launch_progress_dialog: QProgressDialog | None = None
        self._launch_progress_total = 0
        self._launch_progress_done = 0
        self.hf_schedule_tab: DailyScheduleTab | None = None
        self.net_tab: NetScheduleTab | None = None
        self.fldigi_tab: FldigiNetControlTab | None = None
        self.js8_tab: JS8CallNetControlTab | None = None
        self.sop_tab = _construct_startup_component(
            "sop_tab",
            lambda: SOPTab(
                self,
                plan_context_service=self.plan_context_service,
                defer_initial_load=True,
            ),
        )
        if hasattr(self.sop_tab, "local_net_return_requested"):
            self.sop_tab.local_net_return_requested.connect(self._return_navigation_intent)
        self.operator_history_tab: OperatorHistoryTab | None = None
        self.local_operator_tab: LocalOperatorTab | None = None
        self.local_report_history_tab: LocalReportHistoryTab | None = None
        self.local_ncs_tab: LocalNCSTab | None = None
        self.log_tab: LogViewerTab | None = None
        self._log_dialog: QDialog | None = None
        self.peer_sched_tab: PeerSchedTab | None = None
        self.help_tab: HelpTab | None = None
        self._context_help_dialog: ContextHelpDialog | None = None
        self.controlfreq_tab = _construct_startup_component(
            "ops_center",
            lambda: ControlFreqTab(
                self,
                plan_context_service=self.plan_context_service,
                defer_initial_refresh=True,
            ),
        )
        if hasattr(self.controlfreq_tab, "set_local_nets_outlook_provider"):
            self.controlfreq_tab.set_local_nets_outlook_provider(self._build_local_nets_outlook)
        if hasattr(self.controlfreq_tab, "set_shortwave_listening_outlook_provider"):
            self.controlfreq_tab.set_shortwave_listening_outlook_provider(self._build_shortwave_listening_outlook)
        if hasattr(self.controlfreq_tab, "set_shortwave_listening_dismiss_provider"):
            self.controlfreq_tab.set_shortwave_listening_dismiss_provider(self._dismiss_shortwave_listening_occurrence)
        if hasattr(self.controlfreq_tab, "local_net_details_requested"):
            self.controlfreq_tab.local_net_details_requested.connect(self._open_local_net_details)
        if hasattr(self.controlfreq_tab, "local_net_dismiss_requested"):
            self.controlfreq_tab.local_net_dismiss_requested.connect(self._dismiss_local_net_occurrence)
        if hasattr(self.controlfreq_tab, "local_net_open_sop_requested"):
            self.controlfreq_tab.local_net_open_sop_requested.connect(self._open_local_net_sop)
        if hasattr(self.controlfreq_tab, "shortwave_listening_details_requested"):
            self.controlfreq_tab.shortwave_listening_details_requested.connect(self._open_shortwave_listening_details)
        self.command_palette_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self.command_palette_shortcut.setContext(Qt.ApplicationShortcut)
        self.command_palette_shortcut.activated.connect(self._open_command_palette)
        self.station_overview_tab: StationOverviewTab | None = None
        self.station_health_tab: StationHealthTab | None = None
        self.station_bbs_tab: StationBbsTab | None = None
        self.fio_spotter_tab: FioSpotterTab | None = None
        self.resources_tab: QWidget | None = None
        self._refresh_station_health_scope_map()
        self._sop_data_refresh_pending = False
        self._sop_data_refresh_timer = QTimer(self)
        self._sop_data_refresh_timer.setSingleShot(True)
        self._sop_data_refresh_timer.setInterval(90)
        self._sop_data_refresh_timer.timeout.connect(self._flush_sop_data_changed)

        self.freq_planner_tab = None
        self.message_viewer_tab = None
        self.stations_map_tab: StationsMapTab | None = None
        self.map_window: PersistentMapWindow | None = None
        self._pending_map_focus: tuple[str, dict[str, str]] | None = None
        self._map_prop_target_syncing = False

        self._lazy_placeholders = {}
        self._lazy_factories = {
            "FreqPlanner": self._create_freq_planner_tab,
            "Messages": self._create_message_viewer_tab,
            "HF Schedule": self._create_hf_schedule_tab,
            "Net Schedule": self._create_net_schedule_tab,
            "Local Nets": self._create_local_nets_tab,
            "NCS-FLDigi/SSB": self._create_fldigi_ncs_tab,
            "NCS-JS8": self._create_js8_ncs_tab,
            "NCS-Local": self._create_local_ncs_tab,
            "Station Overview": self._create_station_overview_tab,
            "Station Health": self._create_station_health_tab,
            "Managed BBS": self._create_station_bbs_tab,
            "CommsPlan Spotter": self._create_fio_spotter_tab,
            "Resources": self._create_resources_tab,
            "Shortwave": self._create_shortwave_tab,
            "HF Operators": self._create_operator_history_tab,
            "Local Operators": self._create_local_operator_tab,
            "Call Finder": self._create_call_finder_tab,
            "Local Reports": self._create_local_report_history_tab,
            "Peer Schedules": self._create_peer_sched_tab,
            "Help": self._create_help_tab,
        }

        # Internal screen registry (stable keys used by cross-tab navigation/lazy loading)
        self._screens = [
            ("ControlFreq", self.controlfreq_tab),
            ("Station Overview", self._placeholder_widget("Station Overview")),
            ("Managed BBS", self._placeholder_widget("Managed BBS")),
            ("CommsPlan Spotter", self._placeholder_widget("CommsPlan Spotter")),
            ("Resources", self._placeholder_widget("Resources")),
            ("Shortwave", self._placeholder_widget("Shortwave")),
            ("FreqPlanner", self._placeholder_widget("FreqPlanner")),
            ("SOP", self.sop_tab),
            ("Messages", self._placeholder_widget("Messages")),
            ("NCS-FLDigi/SSB", self._placeholder_widget("NCS-FLDigi/SSB")),
            ("NCS-JS8", self._placeholder_widget("NCS-JS8")),
            ("NCS-Local", self._placeholder_widget("NCS-Local")),
            ("HF Operators", self._placeholder_widget("HF Operators")),
            ("Local Operators", self._placeholder_widget("Local Operators")),
            ("Call Finder", self._placeholder_widget("Call Finder")),
            ("Local Reports", self._placeholder_widget("Local Reports")),
            ("Map", self._placeholder_widget("Map")),
            ("HF Schedule", self._placeholder_widget("HF Schedule")),
            ("Net Schedule", self._placeholder_widget("Net Schedule")),
            ("Local Nets", self._placeholder_widget("Local Nets")),
            ("Peer Schedules", self._placeholder_widget("Peer Schedules")),
            ("Station Health", self._placeholder_widget("Station Health")),
            ("Settings", self.settings_tab),
            ("Help", self._placeholder_widget("Help")),
        ]
        self._notify_startup_status("Building station dashboard...")
        self._screen_index_by_label = {label: idx for idx, (label, _w) in enumerate(self._screens)}
        self._condition_levels_signature: tuple[tuple[str, int], ...] = tuple()
        self._condition_levels_refresh_pending = False
        self._scheduler_status_reason_lines_signature: tuple[str, ...] | None = None
        self._hold_state_snapshot: dict[str, object] | None = None
        self._hold_state_signature: tuple[object, ...] | None = None
        self._station_command_selected_profile_id: int | None = None
        self._adaptive_station_shell_enabled = True
        self._station_command_controls_expanded = False
        self._station_command_bar_loading = False
        self._station_command_radio_admin_expanded = False
        self._station_command_manual_qsy_meta: dict[str, object] | None = None
        self._station_command_manual_qsy_profile_id: int | None = None
        self._station_command_scheduler_suspended_manual = False
        self._station_command_timed_suspend_profile_id = 0
        self._station_command_lane_cache_data: dict[int, dict[str, object]] | None = None
        self._station_command_lane_cache_expires = 0.0
        self._station_command_off_schedule_by_radio: dict[int, dict[str, object]] = {}
        self._settings_nav_context = "main"
        self._settings_nav_button_indices: dict[str, int] = {}
        self._messages_nav_context = "inbox"
        self._messages_nav_filter_context: dict[str, str] = {}
        self._messages_nav_button_indices: dict[str, int] = {}
        self._resources_nav_context = "frequency_catalog"
        self._resources_nav_button_indices: dict[str, int] = {}
        # Sidebar button order/text requested by user.
        self._nav_specs = [
            ("Ops Center", "ControlFreq"),
            ("Map", "Map"),
            ("Inbox", "Messages"),
            ("Compose", "Messages"),
            ("BBS", "Managed BBS"),
            ("CommsPlan Spotter", "CommsPlan Spotter"),
            ("FLDigi / SSB", "NCS-FLDigi/SSB"),
            ("JS8Call", "NCS-JS8"),
            ("VHF/UHF", "NCS-Local"),
            ("HF Callsigns", "HF Operators"),
            ("Local Callsigns", "Local Operators"),
            ("Call Finder", "Call Finder"),
            ("Local Reports", "Local Reports"),
            ("Frequencies", "Resources"),
            ("Plan Builder", "FreqPlanner"),
            ("SOP Builder", "SOP"),
            ("HF Daily", "HF Schedule"),
            ("HF Nets", "Net Schedule"),
            ("Local Nets", "Local Nets"),
            ("HF Peer Scheds", "Peer Schedules"),
            ("Control Center", "Station Overview"),
            ("Health Details", "Station Health"),
            ("Main", "Settings"),
            ("Radios", "Settings"),
            ("Software", "Settings"),
            ("Help", "Help"),
        ]
        if resource_catalog_authority_state(get_config_dir() / "config" / "freqinout_nets.db") != "canonical":
            self._nav_specs.remove(("Frequencies", "Resources"))
        elif ShortwaveStore(get_config_dir() / "config" / "freqinout_nets.db").schema_available():
            # The Shortwave route is shown only after startup has established
            # both canonical resource ownership and the additive SW schema.
            self._nav_specs.insert(self._nav_specs.index(("Frequencies", "Resources")) + 1, ("Shortwave", "Shortwave"))
        self._nav_screen_index_map: dict[int, int] = {}
        self._nav_base_labels: list[str] = []

        # Build sidebar with scrollable nav zone + persistent status dock.
        self.nav_widget = QWidget()
        self.nav_widget.setMinimumWidth(150)
        self.nav_widget.setMaximumWidth(280)
        nav_main_layout = QVBoxLayout(self.nav_widget)
        nav_main_layout.setContentsMargins(4, 4, 4, 4)
        nav_main_layout.setSpacing(6)

        self._main_nav_collapsed = False
        self._main_nav_auto_collapsed = False
        self.nav_collapse_btn = QToolButton(self.nav_widget)
        self.nav_collapse_btn.setObjectName("mainNavCollapseButton")
        self.nav_collapse_btn.setText("≪")
        self.nav_collapse_btn.setAccessibleName("Collapse navigation")
        self.nav_collapse_btn.setToolTip("Collapse navigation to a compact workflow rail.")
        self.nav_collapse_btn.clicked.connect(lambda _checked=False: self._toggle_main_navigation())
        nav_main_layout.addWidget(self.nav_collapse_btn, 0, Qt.AlignRight)

        # Logo above nav area (optional if file exists)
        self.logo_label = QLabel()
        self.logo_label.setAlignment(Qt.AlignCenter)
        nav_main_layout.addWidget(self.logo_label)
        self._set_logo_pixmap()

        # Global clock stays visible above navigation so individual tabs do not
        # need to compete for live Local/UTC clock space.
        self.ledge_clock_widget = QFrame(self.nav_widget)
        self.ledge_clock_widget.setObjectName("mainLedgeClock")
        self.ledge_clock_widget.setFrameShape(QFrame.StyledPanel)
        self.ledge_clock_widget.setAccessibleName("Local and UTC clock")
        ledge_clock_layout = QVBoxLayout(self.ledge_clock_widget)
        ledge_clock_layout.setContentsMargins(6, 5, 6, 5)
        ledge_clock_layout.setSpacing(2)
        self.ledge_local_time_label = QLabel("Local --")
        self.ledge_local_time_label.setObjectName("ledgeLocalTime")
        self.ledge_local_time_label.setAlignment(Qt.AlignCenter)
        self.ledge_local_time_label.setAccessibleName("Local time")
        self.ledge_utc_time_label = QLabel("UTC --")
        self.ledge_utc_time_label.setObjectName("ledgeUtcTime")
        self.ledge_utc_time_label.setAlignment(Qt.AlignCenter)
        self.ledge_utc_time_label.setAccessibleName("UTC time")
        ledge_clock_layout.addWidget(self.ledge_local_time_label)
        ledge_clock_layout.addWidget(self.ledge_utc_time_label)
        nav_main_layout.addWidget(self.ledge_clock_widget)

        # Scrollable navigation zone (buttons + map filters + group toggles).
        self.nav_scroll = QScrollArea(self.nav_widget)
        self.nav_scroll.setWidgetResizable(True)
        self.nav_scroll.setFrameShape(QFrame.NoFrame)
        self.nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.nav_scroll.setMinimumHeight(0)
        self.nav_content = QWidget()
        nav_layout = QVBoxLayout(self.nav_content)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(4)
        self.nav_scroll.setWidget(self.nav_content)
        nav_main_layout.addWidget(self.nav_scroll, 1)

        self.nav_buttons = []
        self.button_group = QButtonGroup(self)
        self.button_group.setExclusive(True)
        self._map_nav_index = None
        self._station_health_nav_index = None
        self._station_health_alert_signature: tuple[object, ...] | None = None
        self._station_health_alert_inputs_signature: tuple[object, ...] | None = None
        self._ncs_nav_indices: dict[str, int] = {}
        self._ncs_net_active: dict[str, bool] = self._active_ncs_session_flags_from_settings()
        self._nav_group_headers: dict[str, QPushButton] = {}
        self._nav_group_bodies: dict[str, QWidget] = {}
        self._nav_group_layouts: dict[str, QVBoxLayout] = {}
        self._nav_group_sections: dict[str, QWidget] = {}
        self._nav_group_order: list[str] = [
            "Messages",
            "NCS",
            "Operators",
            "Resources",
            "Plan Builder",
            "Station",
            "Configuration",
        ]
        self._nav_group_states: dict[str, bool] = self._load_nav_group_states()
        self._suppress_initial_nav_group_auto_expand = True

        for nav_idx, (button_label, screen_label) in enumerate(self._nav_specs):
            screen_idx = self._screen_index_by_label.get(screen_label)
            if screen_idx is None:
                continue
            group_key = self._nav_group_for_label(button_label, screen_label)
            target_layout = nav_layout
            if group_key:
                target_layout = self._ensure_nav_group_layout(group_key, nav_layout)
            btn = QPushButton(button_label)
            btn.setCheckable(True)
            btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            btn.setMinimumWidth(120)
            btn.setStyleSheet(self._nav_button_alignment_style())
            if screen_label == "Settings" and button_label == "Main":
                btn.clicked.connect(lambda _=False: self.open_settings_section("operator_info", settings_nav_context="main"))
            elif screen_label == "Settings" and button_label == "Radios":
                btn.clicked.connect(lambda _=False: self.open_settings_section("radio_profiles", settings_nav_context="radios"))
            elif screen_label == "Settings" and button_label == "Software":
                btn.clicked.connect(
                    lambda _=False: self.open_settings_section(
                        "software_administration",
                        settings_nav_context="software",
                    )
                )
            elif screen_label == "Messages" and button_label == "Inbox":
                btn.clicked.connect(lambda _=False: self.open_messages_section("inbox"))
            elif screen_label == "Messages" and button_label == "Compose":
                btn.clicked.connect(lambda _=False: self.open_messages_section("compose"))
            elif screen_label == "Resources":
                # Frequencies represents the Resources browser; its contextual
                # Catalog/Directory/Import routes remain internal browser tabs.
                section = "last"
                btn.clicked.connect(lambda _=False, key=section: self.open_resources_section(key))
            else:
                btn.clicked.connect(lambda _=False, i=screen_idx: self._set_screen(i))
            if screen_label in {"Settings", "Messages"}:
                self.button_group.addButton(btn)
            else:
                self.button_group.addButton(btn, screen_idx)
            self.nav_buttons.append(btn)
            btn_idx = len(self.nav_buttons) - 1
            if screen_label == "Settings" and button_label == "Main":
                self._settings_nav_button_indices["main"] = btn_idx
                self._nav_screen_index_map.setdefault(screen_idx, btn_idx)
            elif screen_label == "Settings" and button_label == "Radios":
                self._settings_nav_button_indices["radios"] = btn_idx
            elif screen_label == "Settings" and button_label == "Software":
                self._settings_nav_button_indices["software"] = btn_idx
            elif screen_label == "Messages" and button_label == "Inbox":
                self._messages_nav_button_indices["inbox"] = btn_idx
                self._nav_screen_index_map.setdefault(screen_idx, btn_idx)
            elif screen_label == "Messages" and button_label == "Compose":
                self._messages_nav_button_indices["compose"] = btn_idx
            elif screen_label == "Resources":
                section = "frequency_catalog"
                # One main-navigation button represents every browser tab so a
                # contextual deep link still highlights Resources.
                for resource_section in ("frequency_catalog", "net_directory", "import_export"):
                    self._resources_nav_button_indices[resource_section] = btn_idx
                self._nav_screen_index_map.setdefault(screen_idx, btn_idx)
            else:
                self._nav_screen_index_map[screen_idx] = btn_idx
            self._nav_base_labels.append(button_label)
            target_layout.addWidget(btn)
            if screen_label == "Map":
                self._map_nav_index = nav_idx
            elif screen_label == "Station Health":
                self._station_health_nav_index = btn_idx
            elif screen_label == "NCS-FLDigi/SSB":
                self._ncs_nav_indices["FLDIGI"] = btn_idx
            elif screen_label == "NCS-JS8":
                self._ncs_nav_indices["JS8"] = btn_idx
            elif screen_label == "NCS-Local":
                self._ncs_nav_indices["LOCAL"] = btn_idx

        self.nav_compact_widget = self._build_compact_navigation_widget()
        self.nav_compact_widget.setVisible(False)
        nav_main_layout.insertWidget(4, self.nav_compact_widget, 1)

        # Placeholder for map filters (shown only on Map view)
        self.map_filters_container = QWidget()
        self.map_filters_container.setMinimumWidth(120)
        self.map_filters_container.setMaximumWidth(240)
        self.map_filters_layout = QVBoxLayout(self.map_filters_container)
        self.map_filters_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.addWidget(self.map_filters_container)
        self._init_map_filters()
        nav_layout.addStretch(1)

        # Persistent status dock (outside nav scroll area; always visible).
        self.status_dock_widget = QWidget()
        self.status_dock_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.status_dock_widget.setMinimumHeight(0)
        status_dock_layout = QVBoxLayout(self.status_dock_widget)
        status_dock_layout.setContentsMargins(0, 0, 0, 0)
        status_dock_layout.setSpacing(6)

        # Scheduler status panel (hidden on Map view)
        self.scheduler_status_container = QGroupBox("Schedule Status")
        self.scheduler_status_container.setCheckable(False)
        self.scheduler_status_container.setVisible(False)
        self.scheduler_status_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        status_title_style = (
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 4px; }"
        )
        self.scheduler_status_container.setStyleSheet(
            status_title_style
        )
        status_layout = QVBoxLayout(self.scheduler_status_container)
        status_layout.setContentsMargins(4, 4, 4, 4)
        status_layout.setSpacing(4)
        status_layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.scheduler_status_header = QLabel("On Schedule")
        self.scheduler_status_header.setAlignment(Qt.AlignCenter)
        self.scheduler_status_header.setWordWrap(True)
        self.scheduler_status_header.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.scheduler_status_reasons = QWidget()
        self.scheduler_status_reasons_layout = QVBoxLayout(self.scheduler_status_reasons)
        self.scheduler_status_reasons_layout.setContentsMargins(0, 0, 0, 0)
        self.scheduler_status_reasons_layout.setSpacing(2)
        self.resume_schedule_btn = QPushButton("Resume Schedule", self.scheduler_status_container)
        self.resume_schedule_btn.setFixedWidth(140)
        self.resume_schedule_btn.clicked.connect(self._on_resume_schedule_clicked)
        self.suspend_schedule_btn = QPushButton("Suspend", self.scheduler_status_container)
        self.suspend_schedule_btn.setFixedWidth(140)
        self.suspend_schedule_btn.clicked.connect(self._on_suspend_schedule_clicked)
        self.suspend_duration_combo = QComboBox(self.scheduler_status_container)
        self.suspend_duration_combo.setMinimumWidth(96)
        self.suspend_duration_combo.setMaximumWidth(112)
        self.suspend_duration_combo.setToolTip("Temporary schedule hold duration.")
        self.suspend_duration_combo.currentIndexChanged.connect(self._on_sidebar_hold_duration_changed)
        refresh_hold_duration_combo(self.suspend_duration_combo, self.settings, self._active_runtime_profile)
        self.logs_active_btn = QPushButton("Logs Active")
        self.logs_active_btn.setFixedWidth(140)
        self.logs_active_btn.clicked.connect(self._open_logs_window)
        self.logs_active_btn.setVisible(False)
        try:
            theme = resolve_theme(self.settings)
            self.resume_schedule_btn.setStyleSheet(button_style("muted", theme))
            self.suspend_schedule_btn.setStyleSheet(button_style("warning", theme))
            self.logs_active_btn.setStyleSheet(button_style("warning", theme))
        except Exception:
            pass
        status_layout.addWidget(self.scheduler_status_header)
        status_layout.addWidget(self.scheduler_status_reasons)
        self.suspend_duration_label = QLabel("Hold", self.scheduler_status_container)
        self.suspend_duration_label.setVisible(False)
        self.suspend_duration_combo.setVisible(False)
        self.suspend_schedule_btn.setVisible(False)
        self.resume_schedule_btn.setVisible(False)
        status_layout.addWidget(self.logs_active_btn, alignment=Qt.AlignCenter)
        status_dock_layout.addWidget(self.scheduler_status_container)

        # Condition levels panel (global; per-HF operating group status card).
        self.condition_level_container = QGroupBox("Condition Level")
        self.condition_level_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        condition_layout = QVBoxLayout(self.condition_level_container)
        condition_layout.setContentsMargins(4, 4, 4, 4)
        condition_layout.setSpacing(4)
        self.condition_levels_rows = QWidget()
        self.condition_levels_rows_layout = QVBoxLayout(self.condition_levels_rows)
        self.condition_levels_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.condition_levels_rows_layout.setSpacing(2)
        self.condition_levels_summary = QLabel("No condition levels configured.")
        self.condition_levels_summary.setWordWrap(True)
        self.condition_levels_summary.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.condition_levels_summary.setVisible(False)
        self.condition_sop_automation_label = QLabel("")
        self.condition_sop_automation_label.setWordWrap(True)
        self.condition_sop_automation_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.condition_sop_automation_label.setVisible(False)
        self.condition_levels_edit_btn = QToolButton(self.condition_level_container)
        self.condition_levels_edit_btn.setText("Edit Levels")
        self.condition_levels_edit_btn.setAutoRaise(True)
        self.condition_levels_edit_btn.clicked.connect(self._open_condition_levels_editor)
        condition_layout.addWidget(self.condition_levels_rows)
        condition_layout.addWidget(self.condition_sop_automation_label)
        condition_layout.addWidget(self.condition_levels_edit_btn, alignment=Qt.AlignLeft)
        self.condition_level_container.setStyleSheet(status_title_style)
        status_dock_layout.addWidget(self.condition_level_container)

        nav_main_layout.addWidget(self.status_dock_widget, 0)

        # Clock, condition and schedule awareness now live in the adaptive shell.
        # Keep the legacy widgets available to older integrations without spending
        # persistent workspace in the navigation rail.
        self.ledge_clock_widget.setVisible(False)
        self.condition_level_container.setVisible(False)

        self._update_scheduler_action_button_widths()
        self._update_nav_layout_metrics()
        QTimer.singleShot(0, self._sync_status_box_width)

        # Stacked content
        # Hidden primary pages must not inflate the active page or the native
        # window. This is especially important when a tall deferred Compose or
        # another content-heavy page is constructed for the first time.
        self.stack = CurrentPageStack()
        for _label, widget in self._screens:
            self.stack.addWidget(widget)

        # Right-side layout (notice bar + stacked content)
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        self._right_shell_container = right_container
        self._right_shell_layout = right_layout
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        self.runtime_mode_banner = QFrame(right_container)
        self.runtime_mode_banner.setVisible(False)
        banner_layout = QHBoxLayout(self.runtime_mode_banner)
        banner_layout.setContentsMargins(10, 8, 10, 8)
        banner_layout.setSpacing(6)
        self.runtime_mode_label = QLabel("")
        self.runtime_mode_label.setWordWrap(True)
        banner_layout.addWidget(self.runtime_mode_label)
        right_layout.addWidget(self.runtime_mode_banner, 0)

        self.action_feedback_banner = QFrame(right_container)
        self.action_feedback_banner.setVisible(False)
        self.action_feedback_banner.setAccessibleName("Action feedback")
        feedback_layout = QHBoxLayout(self.action_feedback_banner)
        feedback_layout.setContentsMargins(10, 6, 8, 6)
        feedback_layout.setSpacing(8)
        self.action_feedback_label = QLabel("")
        self.action_feedback_label.setWordWrap(True)
        self.action_feedback_label.setAccessibleName("Action feedback message")
        feedback_layout.addWidget(self.action_feedback_label, 1)
        self.action_feedback_dismiss_btn = QToolButton()
        self.action_feedback_dismiss_btn.setText("x")
        self.action_feedback_dismiss_btn.setToolTip("Dismiss status")
        self.action_feedback_dismiss_btn.clicked.connect(self._hide_action_feedback_banner)
        self.action_feedback_history_btn = QToolButton()
        self.action_feedback_history_btn.setText("History")
        self.action_feedback_history_btn.setToolTip("Show recent actions")
        self.action_feedback_history_btn.setAccessibleName("Recent actions")
        self.action_feedback_history_btn.clicked.connect(self._show_recent_actions_dialog)
        feedback_layout.addWidget(self.action_feedback_history_btn, 0)
        feedback_layout.addWidget(self.action_feedback_dismiss_btn, 0)
        right_layout.addWidget(self.action_feedback_banner, 0)
        self._action_feedback_clear_timer = QTimer(self)
        self._action_feedback_clear_timer.setSingleShot(True)
        self._action_feedback_clear_timer.timeout.connect(self._hide_action_feedback_banner)
        self._action_feedback_unsubscribe = self.action_feedback_service.subscribe(self._on_action_feedback_event)
        self._recent_actions_dialog: QDialog | None = None

        self.station_command_bar = QFrame(right_container)
        self.station_command_bar.setObjectName("stationCommandBar")
        self.station_command_bar.setAccessibleName("Station command context")
        # The bar's natural height changes with responsive/card mode.  Minimum
        # prevents a transient sibling banner from squeezing it below its
        # current size hint while still allowing the shell to grow as needed.
        self.station_command_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.station_command_layout = QGridLayout(self.station_command_bar)
        command_layout = self.station_command_layout
        command_layout.setContentsMargins(10, 8, 10, 8)
        command_layout.setSpacing(8)
        self._station_command_layout_mode = ""
        self.station_command_radio_label = QLabel("Radio")
        self.station_command_radio_label.setObjectName("stationCommandRadioLabel")
        self.station_command_radio_combo = QComboBox(self.station_command_bar)
        self.station_command_radio_combo.setObjectName("stationCommandRadioSelector")
        self.station_command_radio_combo.setMinimumWidth(160)
        self.station_command_radio_combo.setMaximumWidth(340)
        self.station_command_radio_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.station_command_radio_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.station_command_radio_combo.currentIndexChanged.connect(self._on_station_command_radio_changed)
        self.station_command_radio_separator = QFrame(self.station_command_bar)
        self.station_command_radio_separator.setObjectName("stationCommandRadioSeparator")
        self.station_command_radio_separator.setFrameShape(QFrame.VLine)
        self.station_command_radio_separator.setFrameShadow(QFrame.Plain)
        self.station_command_now_caption = QLabel("Now")
        self.station_command_now_caption.setObjectName("stationCommandNowCaption")
        self.station_command_now_label = ElidedLabel("Now: unavailable", self.station_command_bar)
        self.station_command_now_label.setObjectName("stationCommandNow")
        self.station_command_now_label.setWordWrap(False)
        self.station_command_now_label.setMinimumWidth(0)
        self.station_command_now_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.station_command_now_label.setToolTip("Current frequency/control target for the selected radio.")
        self.station_command_freq_combo = QComboBox(self.station_command_bar)
        self.station_command_freq_combo.setObjectName("stationCommandFrequencySelector")
        self.station_command_freq_combo.setMinimumWidth(150)
        self.station_command_freq_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.station_command_freq_combo.setToolTip("Select the operating group and band for manual QSY.")
        self.station_command_state_label = QLabel("State: unknown")
        self.station_command_state_label.setObjectName("stationCommandState")
        self.station_command_state_label.setWordWrap(False)
        self.station_command_state_label.setMinimumWidth(0)
        self.station_command_state_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.station_command_now_separator = QFrame(self.station_command_bar)
        self.station_command_now_separator.setObjectName("stationCommandNowSeparator")
        self.station_command_now_separator.setFrameShape(QFrame.VLine)
        self.station_command_now_separator.setFrameShadow(QFrame.Plain)
        self.station_command_action_label = QLabel("Action")
        self.station_command_action_label.setObjectName("stationCommandActionLabel")
        self.station_command_next_label = QLabel("Next: none")
        self.station_command_next_label.setObjectName("stationCommandNext")
        self.station_command_next_label.setWordWrap(False)
        self.station_command_next_label.setMinimumWidth(0)
        self.station_command_next_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.station_command_health_label = QLabel("Health:")
        self.station_command_health_label.setObjectName("stationCommandHealthLabel")
        self.station_command_health_widget = QWidget(self.station_command_bar)
        self.station_command_health_widget.setObjectName("stationCommandHealth")
        self.station_command_health_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.station_command_health_widget.setCursor(Qt.PointingHandCursor)
        self.station_command_health_widget.mousePressEvent = (
            lambda event, widget=self.station_command_health_widget: self._on_station_command_health_clicked(event, anchor=widget)
        )
        self.station_command_health_layout = QHBoxLayout(self.station_command_health_widget)
        self.station_command_health_layout.setContentsMargins(0, 0, 0, 0)
        self.station_command_health_layout.setSpacing(6)
        self.station_command_health_leds: dict[str, QLabel] = {}
        self.station_command_health_text_labels: dict[str, QLabel] = {}
        self.station_command_duration_combo = QComboBox(self.station_command_bar)
        self.station_command_duration_combo.setObjectName("stationCommandDuration")
        self.station_command_duration_combo.setToolTip(
            "Duration for QSY Suspend."
        )
        self.station_command_duration_combo.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.station_command_duration_combo.currentIndexChanged.connect(self._on_station_command_hold_duration_changed)
        refresh_hold_duration_combo(self.station_command_duration_combo, self.settings, self._active_runtime_profile)
        self.station_command_qsy_btn = QPushButton("QSY Now")
        self.station_command_qsy_btn.setObjectName("stationCommandQsy")
        self.station_command_hold_btn = QPushButton("QSY Suspend")
        self.station_command_hold_btn.setObjectName("stationCommandHold")
        self.station_command_suspend_btn = QPushButton("Suspend Scheduler")
        self.station_command_suspend_btn.setObjectName("stationCommandSuspend")
        self.station_command_resume_btn = QPushButton("Resume Schedule")
        self.station_command_resume_btn.setObjectName("stationCommandResume")
        self.station_command_qsy_btn.clicked.connect(self._on_station_command_qsy_now_clicked)
        self.station_command_hold_btn.clicked.connect(self._on_station_command_qsy_hold_clicked)
        self.station_command_suspend_btn.clicked.connect(self._on_station_command_pause_clicked)
        self.station_command_resume_btn.clicked.connect(self._on_station_command_resume_clicked)
        for btn in (
            self.station_command_qsy_btn,
            self.station_command_hold_btn,
            self.station_command_suspend_btn,
            self.station_command_resume_btn,
        ):
            btn.setEnabled(False)
            btn.setToolTip("Station command wiring is not enabled yet.")
            btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self._station_command_qsy_suspend_base_text = "QSY Suspend"
        self._station_command_suspend_base_text = "Suspend Scheduler"
        self.station_command_radio_summary_label = QLabel("Radios")
        self.station_command_radio_summary_label.setObjectName("stationCommandRadioSummaryLabel")
        self.station_command_radio_summary_scroll = QScrollArea(self.station_command_bar)
        self.station_command_radio_summary_scroll.setObjectName("stationCommandRadioSummaryScroll")
        self.station_command_radio_summary_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.station_command_radio_summary_scroll.setMinimumWidth(0)
        self.station_command_radio_summary_scroll.setWidgetResizable(True)
        self.station_command_radio_summary_scroll.setFrameShape(QFrame.NoFrame)
        self.station_command_radio_summary_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.station_command_radio_summary_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        summary_h = control_height_for_font(self.station_command_radio_summary_scroll, vertical_padding=18, floor=42)
        self.station_command_radio_summary_scroll.setMinimumHeight(summary_h)
        self.station_command_radio_summary_scroll.setMaximumHeight(summary_h)
        self.station_command_radio_summary_widget = QWidget()
        self.station_command_radio_summary_widget.setObjectName("stationCommandRadioSummary")
        self.station_command_radio_summary_widget.setMinimumWidth(0)
        self.station_command_radio_summary_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.station_command_radio_summary_layout = QVBoxLayout(self.station_command_radio_summary_widget)
        self.station_command_radio_summary_layout.setContentsMargins(0, 0, 0, 0)
        self.station_command_radio_summary_layout.setSpacing(6)
        self.station_command_radio_summary_scroll.setWidget(self.station_command_radio_summary_widget)
        # The approved multi-radio card surface is retained independently of
        # the legacy selected-radio widgets below.  Its callbacks stay scoped
        # to the existing guarded command handlers; card publication itself is
        # cache-only and does not touch a runtime endpoint.
        self.radio_control_panel = RadioControlPanel(
            callbacks=RadioCardCallbacks(
                select=self._on_station_command_summary_radio_clicked,
                qsy=self._on_radio_card_qsy,
                hold=self._on_radio_card_hold,
                resume=self._on_radio_card_resume,
                sop=self._on_radio_card_sop,
                net=self._on_radio_card_net,
                sources=self._on_radio_card_sources,
                policy=self._on_radio_card_policy,
                advanced=self._on_radio_card_advanced,
                health=self._on_radio_card_health,
                plan=self._on_radio_card_plan,
                launch=self._on_radio_card_launch,
                js8_offset=self._on_radio_card_js8_offset,
                navigation=self._on_radio_card_navigation,
                dock=self._on_radio_card_dock,
            ),
            theme=resolve_theme(self.settings),
            parent=self.station_command_radio_summary_widget,
        )
        self.radio_control_panel.setVisible(False)
        self.radio_control_panel.full_requested.connect(self._restore_radio_control_panel_from_dock)
        self.radio_control_panel.minimized_changed.connect(self._on_radio_control_panel_minimized_changed)
        self.radio_control_panel.geometry_changed.connect(self._schedule_station_command_summary_geometry)
        self._radio_control_panel_dock_window = None
        self.station_command_radio_prev_btn = QPushButton("Prev", self.station_command_bar)
        self.station_command_radio_prev_btn.setObjectName("stationCommandRadioPagePrev")
        self.station_command_radio_prev_btn.setToolTip("Show previous radios.")
        self.station_command_radio_prev_btn.clicked.connect(lambda _checked=False: self._change_station_command_radio_page(-1))
        self.station_command_radio_prev_btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.station_command_radio_next_btn = QPushButton("Next", self.station_command_bar)
        self.station_command_radio_next_btn.setObjectName("stationCommandRadioPageNext")
        self.station_command_radio_next_btn.setToolTip("Show next radios.")
        self.station_command_radio_next_btn.clicked.connect(lambda _checked=False: self._change_station_command_radio_page(1))
        self.station_command_radio_next_btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self._station_command_radio_page = 0
        self._station_command_radio_summary_signature: tuple[object, ...] | None = None
        # The station shell is a presentation surface.  Mesh worker callbacks
        # publish these immutable snapshots; periodic rendering must not reopen
        # a database, run schema checks, or query a transport.
        self._station_command_mesh_configs: tuple[MeshConnectionConfig, ...] = ()
        self._station_command_mesh_health_rows: tuple[Mapping[str, object], ...] = ()
        self._station_command_mesh_health_published_monotonic_by_adapter: dict[str, float] = {}
        self._mesh_health_command_refresh_timer = QTimer(self)
        self._mesh_health_command_refresh_timer.setSingleShot(True)
        self._mesh_health_command_refresh_timer.setInterval(350)
        self._mesh_health_command_refresh_timer.timeout.connect(
            lambda: self._refresh_station_command_bar(force=False)
        )
        self.station_command_radio_admin_btn = QPushButton("All Radios")
        self.station_command_radio_admin_btn.setObjectName("stationCommandRadioAdminToggle")
        self.station_command_radio_admin_btn.setToolTip("Show or hide the all-radio status and assignment panel.")
        self.station_command_radio_admin_btn.clicked.connect(self._toggle_station_command_radio_admin)
        self.station_command_radio_admin_btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.station_command_radio_admin_panel = QWidget(self.station_command_bar)
        self.station_command_radio_admin_panel.setObjectName("stationCommandRadioAdminPanel")
        self.station_command_radio_admin_layout = QVBoxLayout(self.station_command_radio_admin_panel)
        self.station_command_radio_admin_layout.setContentsMargins(0, 0, 0, 0)
        self.station_command_radio_admin_layout.setSpacing(6)
        self.station_command_radio_admin_panel.setVisible(False)
        self._apply_station_command_bar_layout(force=True)
        try:
            self.dependency_status_service.snapshot_changed.connect(
                lambda _snapshot: self._schedule_station_command_bar_refresh("dependency_status", force=False)
            )
            self.dependency_status_service.snapshot_changed.connect(self._refresh_application_status_scopes)
        except Exception:
            pass
        right_layout.addWidget(self.station_command_bar, 0)

        right_layout.addWidget(self.stack, stretch=1)

        # Layout composition
        layout.addWidget(self.nav_widget)
        layout.addWidget(right_container, stretch=1)
        # The shell, not the active page's transient native size hint, owns the
        # top-level window geometry. Layout stretch still gives the current page
        # all available space; Ignored prevents transient child hints from
        # resizing or repositioning the application window during activation.
        self.stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

        # Every primary page owns compact overflow.  Do not impose a shell
        # floor that prevents the audited 900x560 workflow or a smaller
        # platform work area from remaining reachable.
        self.setMinimumSize(0, 0)

        self._apply_app_theme()
        self._ui_watchdog = UiEventLoopWatchdog(self)
        self._ui_watchdog.start()
        self._cpu_watchdog = ProcessCpuWatchdog()
        self._cpu_watchdog.start()
        self._sop_next_due_cache_ts = 0.0
        self._sop_next_due_minutes = None
        self._sop_next_action_label = ""
        self._sop_next_action_count = 0
        self._sop_awareness = SopAwarenessCache()
        self._sop_awareness_timer = QTimer(self)
        self._sop_awareness_timer.setInterval(1000)
        self._sop_awareness_timer.timeout.connect(self._refresh_sop_awareness)
        self._sop_awareness_timer.start()
        self._active_tab_index = None
        # Every queued screen-local callback is bound to this generation.
        # A later navigation invalidates it before it can mutate a hidden page.
        self._navigation_epoch = 0
        self._lazy_prewarm_labels = ["Messages", "FreqPlanner"]
        self._lazy_prewarm_index = 0
        self._startup_deferred_prewarm_enabled = self._should_prewarm_deferred_screens_at_startup()
        self._runtime_client_signature: tuple[object, ...] | None = None
        self._station_command_last_refresh_monotonic = 0.0

        startup_policy = self._primary_runtime_policy()
        startup_suppressed = self._suppressed_screens_for_runtime(self._active_runtime_profile, startup_policy)
        self._lazy_prewarm_labels = self._runtime_lazy_prewarm_labels(startup_suppressed)
        # Default selection
        if self.nav_buttons:
            self.nav_buttons[0].setChecked(True)
            first_screen_index = self.button_group.id(self.nav_buttons[0])
            self._set_screen(first_screen_index if first_screen_index >= 0 else 0)
        self._suppress_initial_nav_group_auto_expand = False
        if self._startup_deferred_prewarm_enabled:
            # Opt-in only: automatic construction can still monopolize the GUI
            # event loop on a production-sized station database.  The shell
            # remains responsive until the operator explicitly opens a screen.
            QTimer.singleShot(3000, self._start_lazy_prewarm)

        # Optional: apply callsign to tab captions if already configured
        self._apply_callsign_to_tab_titles()

        # Start scheduler engine
        primary_runtime = self.station_runtime_manager.get_primary_runtime()
        self.rig_client = (
            primary_runtime.rig_client
            if primary_runtime is not None and primary_runtime.rig_client is not None
            else rig_control_client_from_settings(self.settings)
        )
        self.js8_control = (
            primary_runtime.js8_control_client
            if primary_runtime is not None and primary_runtime.js8_control_client is not None
            else self._new_js8_control_client()
        )
        self.varac_status = (
            primary_runtime.varac_status_client
            if primary_runtime is not None and primary_runtime.varac_status_client is not None
            else self._new_varac_status_client()
        )
        self.fldigi_log_status = FldigiLogStatusClient()
        self.scheduler = _construct_startup_component(
            "scheduler",
            lambda: SchedulerEngine(
                self,
                rig=self.rig_client,
                js8=self.js8_control,
                varac=self.varac_status,
                fldigi_log=self.fldigi_log_status,
                station_runtime_manager=self.station_runtime_manager,
            ),
        )
        self._runtime_client_signature = self._runtime_client_signature_for_settings()
        try:
            if hasattr(self.scheduler, "set_runtime_scheduler_enabled"):
                self.scheduler.set_runtime_scheduler_enabled(bool(startup_policy.get("scheduler_enabled", True)))
        except Exception:
            pass
        try:
            if hasattr(self.scheduler, "set_runtime_timer_policy"):
                self.scheduler.set_runtime_timer_policy(self._runtime_timer_policy_for(self._active_runtime_profile))
        except Exception as e:
            log.debug("MainWindow: failed to apply startup runtime timer policy: %s", e)
        try:
            set_scheduler_enabled_override(bool(startup_policy.get("scheduler_enabled", True)))
        except Exception:
            pass
        self._notify_startup_status("Starting scheduler services...")
        self.scheduler.start()
        self.receiver_qualification = ReceiverQualificationCoordinator(
            self.scheduler.shared_endpoint_lane_registry(),
            receiver_control_client_from_profile,
        )
        self._shutdown_registry.register(
            "receiver_qualification",
            request_stop=self.receiver_qualification.stop,
            is_stopped=self.receiver_qualification.is_stopped,
        )
        self.settings_tab.set_receiver_control_test_service_ready(True)
        self.background_ingest = _construct_startup_component(
            "background_ingest",
            lambda: BackgroundIngestController(
                self.settings,
                expect_guard_preflight=build_expect_rf_guard_preflight(self.station_runtime_manager),
            ),
        )
        self._shutdown_registry.register(
            "background_ingest",
            request_stop=getattr(self.background_ingest, "stop", lambda: None),
            is_stopped=getattr(
                self.background_ingest,
                "is_stopped",
                lambda: not bool(
                    getattr(self.background_ingest, "is_running", lambda: False)()
                ),
            ),
        )
        self.message_projection_maintenance = _construct_startup_component(
            "message_projection_maintenance",
            lambda: MessageProjectionMaintenanceService(
                get_config_dir() / "config" / "freqinout_nets.db"
            ),
        )
        self.message_projection_maintenance.set_progress_callback(
            self._message_projection_progressed.emit
        )
        self._shutdown_registry.register(
            "message_projection_maintenance",
            request_stop=getattr(
                self.message_projection_maintenance, "close", lambda: None
            ),
            is_stopped=getattr(
                self.message_projection_maintenance, "is_stopped", lambda: True
            ),
        )
        self._message_projection_reconcile_timer = QTimer(self)
        self._message_projection_reconcile_timer.setSingleShot(True)
        self._message_projection_reconcile_timer.timeout.connect(
            self._on_message_projection_reconcile_timer
        )
        self._message_projection_followup_timer = QTimer(self)
        self._message_projection_followup_timer.setSingleShot(True)
        self._message_projection_followup_timer.setInterval(1000)
        self._message_projection_followup_timer.timeout.connect(
            self._on_message_projection_followup_timer
        )
        try:
            self.background_ingest.condition_sop_invocation_audited.connect(
                lambda _result=None: self.notify_condition_levels_changed()
            )
            self.background_ingest.condition_sop_invocation_applied.connect(
                lambda _result=None: self.notify_condition_levels_changed()
            )
            self.background_ingest.job_finished.connect(
                self._on_background_ingest_projection_work_ready
            )
        except Exception:
            pass
        if self._runtime_background_ingest_enabled(self._active_runtime_profile, startup_policy):
            self._background_ingest_start_pending = True
        else:
            log.info("MainWindow: background ingest disabled for current runtime policy")
        try:
            if hasattr(self.launch_orchestrator, "set_runtime_launch_enabled"):
                self.launch_orchestrator.set_runtime_launch_enabled(
                    self._runtime_launch_enabled(self._active_runtime_profile, startup_policy),
                    reason="Launch Control is disabled by the primary operating model.",
                )
            self._launch_startup_suppressed = not self._runtime_launch_enabled(self._active_runtime_profile, startup_policy)
        except Exception:
            pass
        try:
            self.scheduler.off_schedule_detected.connect(self._on_off_schedule_detected)
        except Exception:
            pass
        try:
            self.scheduler.off_schedule_cleared.connect(self._on_off_schedule_cleared)
        except Exception:
            pass
        try:
            self.scheduler.varac_wait_detected.connect(self._on_varac_wait_detected)
        except Exception:
            pass
        try:
            self.scheduler.varac_wait_cleared.connect(self._dismiss_varac_wait_prompt)
        except Exception:
            pass
        try:
            self.scheduler.coordination_conflict_detected.connect(self._on_coordination_conflict_detected)
        except Exception:
            pass
        try:
            self.scheduler.coordination_conflict_cleared.connect(self._dismiss_coordination_conflict_prompt)
        except Exception:
            pass
        try:
            self.scheduler.active_entry_changed.connect(self._on_scheduler_active_entry_changed)
        except Exception:
            pass
        self._refresh_ncs_activity_from_snapshots()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(5000)
        self._status_timer.timeout.connect(self._schedule_status_refresh)
        self._status_timer.start()
        self._ledge_clock_timer = QTimer(self)
        self._ledge_clock_timer.setInterval(1000)
        self._ledge_clock_timer.timeout.connect(self._update_ledge_clock)
        self._ledge_clock_timer.start()
        self._update_ledge_clock()
        self._condition_levels_refresh_timer = QTimer(self)
        self._condition_levels_refresh_timer.setSingleShot(True)
        self._condition_levels_refresh_timer.setInterval(90)
        self._condition_levels_refresh_timer.timeout.connect(self._apply_condition_levels_changed)
        self._hold_state_timer = QTimer(self)
        self._hold_state_timer.setInterval(1000)
        self._hold_state_timer.timeout.connect(self._on_hold_state_tick)

        app = QApplication.instance()
        if app is not None:
            try:
                self._app_active = app.applicationState() == Qt.ApplicationActive
                self._observed_application_state = app.applicationState()
                app.applicationStateChanged.connect(self._on_application_state_changed)
            except Exception as e:
                log.debug("MainWindow: UI lifecycle state wiring failed: %s", e)
            app.aboutToQuit.connect(self._on_app_about_to_quit)

        self.on_hold_state_changed(force_reload=True)

        # Wire settings_saved signal
        def _connect_or_log(label, signal, slot) -> None:
            try:
                signal.connect(slot)
            except Exception as e:
                log.debug("MainWindow signal wiring failed: %s: %s", label, e)

        self.settings_tab.settings_saved.connect(self._on_settings_saved_for_lazy_tabs)
        # SOP reconstruction is intentionally lazy. The coalesced Settings
        # callback refreshes it only while SOP is the active surface and marks
        # an inactive tab dirty for its next activation.
        try:
            if hasattr(self.settings_tab, "local_net_profiles_changed"):
                self.settings_tab.local_net_profiles_changed.connect(self.sop_tab.on_local_net_profiles_updated)
        except Exception as e:
            log.debug("MainWindow signal wiring failed: local_net_profiles_changed -> sop_tab: %s", e)
        try:
            if hasattr(self.sop_tab, "sop_data_changed"):
                self.sop_tab.sop_data_changed.connect(self._on_sop_data_changed)
        except Exception as e:
            log.debug("MainWindow signal wiring failed: sop_data_changed -> main_window: %s", e)
        # Message tab settings saved handled by _on_settings_saved_for_lazy_tabs
        self._wire_lazy_local_data_links()
        appearance_signal = getattr(self.settings_tab, "appearance_changed", None)
        if appearance_signal is not None:
            _connect_or_log("appearance_changed -> apply theme", appearance_signal, self._apply_app_theme)
        _connect_or_log("settings_saved -> runtime settings", self.settings_tab.settings_saved, self._on_runtime_settings_saved)
        _connect_or_log("settings_saved -> sync runtime status", self.settings_tab.settings_saved, self._sync_settings_runtime_status)
        try:
            if hasattr(self.settings_tab, "device_profiles_changed"):
                self.settings_tab.device_profiles_changed.connect(self._on_runtime_device_profiles_changed)
        except Exception as e:
            log.debug("MainWindow signal wiring failed: device_profiles_changed -> runtime profile: %s", e)
        try:
            if hasattr(self.settings_tab, "operating_groups_changed"):
                self.settings_tab.operating_groups_changed.connect(self._on_operating_groups_changed)
        except Exception as e:
            log.debug("MainWindow signal wiring failed: operating_groups_changed -> settings consumers: %s", e)
        _connect_or_log("settings_saved -> log indicator", self.settings_tab.settings_saved, self._update_log_indicator)
        _connect_or_log("settings_saved -> background ingest", self.settings_tab.settings_saved, self.background_ingest.refresh_runtime_settings)
        _connect_or_log("settings_saved -> station health", self.settings_tab.settings_saved, self._on_station_health_settings_saved)
        _connect_or_log("settings_saved -> local mesh runtime", self.settings_tab.settings_saved, self._restart_mesh_runtime_if_needed)
        receiver_test_signal = getattr(self.settings_tab, "receiver_control_test_requested", None)
        if receiver_test_signal is not None:
            _connect_or_log(
                "receiver control test requested",
                receiver_test_signal,
                self._on_receiver_control_test_requested,
            )
        mesh_connect_signal = getattr(self.settings_tab, "mesh_connect_requested", None)
        if mesh_connect_signal is not None:
            _connect_or_log("mesh connect requested", mesh_connect_signal, self._connect_saved_mesh_from_station_command)
        mesh_disconnect_signal = getattr(self.settings_tab, "mesh_disconnect_requested", None)
        if mesh_disconnect_signal is not None:
            _connect_or_log("mesh disconnect requested", mesh_disconnect_signal, self._disconnect_mesh_runtime)
        mesh_channel_cancel_signal = getattr(self.settings_tab, "mesh_channel_cancel_requested", None)
        if mesh_channel_cancel_signal is not None:
            _connect_or_log(
                "mesh channel cancel -> local mesh runtime",
                mesh_channel_cancel_signal,
                self._cancel_mesh_runtime_operation,
            )
        _connect_or_log("open_logs_requested -> log window", self.settings_tab.open_logs_requested, self._open_logs_window)
        _connect_or_log("log_level_changed -> log indicator", self.settings_tab.log_level_changed, self._update_log_indicator)
        log.info("Main window initialized.")
        # Sync sidebar filters initially
        self._sync_map_filters_from_tab()
        self._update_log_indicator()
        self._refresh_scheduler_status_panel()
        self._refresh_condition_level_panel()
        self._refresh_station_overview(force=True)
        self._refresh_station_health_alert()
        self._apply_runtime_profile_state(force=True)

        try:
            self.launch_orchestrator.sequence_started.connect(self._on_launch_sequence_started)
        except Exception:
            pass
        try:
            self.launch_orchestrator.sequence_progress.connect(self._on_launch_sequence_progress)
        except Exception:
            pass
        try:
            self.launch_orchestrator.sequence_finished.connect(self._on_launch_sequence_finished)
        except Exception:
            pass
        self.launch_orchestrator.set_schedule_handoff_provider(self.scheduler.launch_schedule_apply_status)
        QTimer.singleShot(1200, self._start_launch_control_startup)
        self._window_preferences = QSettings(str(self.settings.config_dir / "window-state.ini"), QSettings.IniFormat)
        restore_full_window_geometry(self, self._window_preferences)
        self._screen_fit_controller = WindowScreenFitController(self, self._reflow_screen_geometry)
        self._notify_startup_status("Opening CommsPlan...")

    def _reflow_screen_geometry(self) -> None:
        if getattr(self, "_shutting_down", False):
            return
        self._reflow_adaptive_station_shell()
        self._schedule_station_command_summary_geometry()

    def _schedule_station_command_summary_geometry(self) -> None:
        timer = getattr(self, "_station_command_summary_geometry_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._sync_station_command_radio_summary_height(card_mode=True)
                                  if not getattr(self, "_shutting_down", False) else None)
            self._station_command_summary_geometry_timer = timer
        if not timer.isActive():
            timer.start(0)

    def release_startup_surface_shield(self) -> None:
        """Allow the completed Windows shell to be mapped exactly once."""

        if not bool(getattr(self, "_startup_surface_shielded", False)):
            return
        # If construction caused any logical visibility transition, normalize
        # it before removing the stronger native-surface shield.  main.py owns
        # the one intentional show() immediately after this method returns.
        self.hide()
        self.setAttribute(Qt.WA_DontShowOnScreen, False)
        self._startup_surface_shielded = False

    def _notify_startup_status(self, message: str) -> None:
        callback = getattr(self, "_startup_status_callback", None)
        if callback is None:
            return
        try:
            callback(message)
        except Exception as e:
            log.debug("MainWindow startup status update failed: %s", e)

    @staticmethod
    def _action_feedback_banner_role(status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized == "succeeded":
            return "success"
        if normalized in {"blocked", "failed", "partial"}:
            return "warning"
        if normalized in {"requested", "in_progress"}:
            return "info"
        return "secondary"

    def _action_feedback_banner_style(self, status: str) -> str:
        theme = resolve_theme(self.settings)
        role = self._action_feedback_banner_role(status)
        if role == "success":
            border = theme.get("success", "#2E7D32")
        elif role == "warning":
            border = theme.get("warning", "#C99700")
        elif role == "info":
            border = theme.get("accent", "#2a6fd3")
        else:
            border = theme.get("border", "#cccccc")
        bg = theme.get("surface", "#ffffff")
        text = theme.get("text", "#222222")
        return (
            "QFrame {"
            f" background: {bg};"
            f" color: {text};"
            f" border: 1px solid {border};"
            " border-radius: 6px;"
            "}"
            " QLabel { border: none; background: transparent; }"
            " QToolButton { border: none; background: transparent; padding: 2px 6px; }"
        )

    @staticmethod
    def _action_feedback_display_ms(status: str) -> int:
        normalized = str(status or "").strip().lower()
        if normalized in {"blocked", "failed", "partial"}:
            return 12000
        if normalized in {"requested", "in_progress"}:
            return 7000
        return 6000

    def _hide_action_feedback_banner(self) -> None:
        if hasattr(self, "_action_feedback_clear_timer"):
            self._action_feedback_clear_timer.stop()
        if hasattr(self, "action_feedback_banner"):
            self.action_feedback_banner.setVisible(False)
            self._schedule_action_feedback_geometry_sync()

    def _schedule_action_feedback_geometry_sync(self) -> None:
        """Coalesce shell geometry repair after a feedback visibility change."""
        if bool(getattr(self, "_action_feedback_geometry_pending", False)):
            return
        self._action_feedback_geometry_pending = True
        QTimer.singleShot(0, self._flush_action_feedback_geometry_sync)

    def _flush_action_feedback_geometry_sync(self) -> None:
        """Release stale sibling geometry without refreshing station data."""
        self._action_feedback_geometry_pending = False
        if bool(getattr(self, "_shutting_down", False)):
            return
        banner = getattr(self, "action_feedback_banner", None)
        bar = getattr(self, "station_command_bar", None)
        shell = getattr(self, "_right_shell_container", None)
        shell_layout = getattr(self, "_right_shell_layout", None)
        banner_update_geometry = getattr(banner, "updateGeometry", None)
        if callable(banner_update_geometry):
            banner_update_geometry()
        if bar is not None:
            bar_layout_getter = getattr(bar, "layout", None)
            bar_layout = bar_layout_getter() if callable(bar_layout_getter) else None
            if bar_layout is not None:
                bar_layout.invalidate()
                bar_layout.activate()
            # The internal layout mode may be unchanged even though Linux has
            # cached a compressed sibling allocation. Reapply the same cached
            # widget arrangement so its natural size hint is republished.
            self._station_command_layout_signature = None
            apply_layout = getattr(self, "_apply_station_command_bar_layout", None)
            if callable(apply_layout):
                apply_layout(force=True)
            bar_update_geometry = getattr(bar, "updateGeometry", None)
            if callable(bar_update_geometry):
                bar_update_geometry()
        if shell_layout is not None:
            shell_layout.invalidate()
            shell_layout.activate()
        shell_update_geometry = getattr(shell, "updateGeometry", None)
        if callable(shell_update_geometry):
            shell_update_geometry()
        shell_update = getattr(shell, "update", None)
        if callable(shell_update):
            shell_update()
        bar_update = getattr(bar, "update", None)
        if callable(bar_update):
            bar_update()

    @staticmethod
    def _action_feedback_banner_scopes() -> set[str]:
        return {"settings", "radio", "scheduler"}

    def _on_action_feedback_event(self, event: ActionFeedbackEvent) -> None:
        if str(event.scope or "").strip().lower() not in self._action_feedback_banner_scopes():
            return
        if str(getattr(event, "source_surface", "") or "").strip().lower() == "station_command_bar":
            return
        if not hasattr(self, "action_feedback_banner"):
            return
        summary = str(event.summary or "").strip()
        if not summary:
            return
        detail = str(event.detail or "").strip()
        status = str(event.status or "").strip().lower()
        self.action_feedback_label.setText(summary)
        self.action_feedback_label.setToolTip(detail or summary)
        self.action_feedback_banner.setStyleSheet(self._action_feedback_banner_style(status))
        self.action_feedback_banner.setVisible(True)
        self._schedule_action_feedback_geometry_sync()
        timeout_ms = self._action_feedback_display_ms(status)
        if timeout_ms > 0:
            self._action_feedback_clear_timer.start(timeout_ms)

    @staticmethod
    def _recent_action_line(event: ActionFeedbackEvent) -> str:
        status = str(event.status or "").strip().upper() or "STATUS"
        summary = str(event.summary or "").strip() or str(event.action_type or "Action").strip() or "Action"
        target = str(event.target_label or "").strip()
        time_txt = str(event.timestamp_utc or "").strip()
        if "T" in time_txt:
            time_txt = time_txt.split("T", 1)[1].replace("Z", "")
            time_txt = time_txt[:8]
        bits = [status]
        if time_txt:
            bits.append(time_txt)
        if target:
            bits.append(target)
        return f"{' | '.join(bits)}: {summary}"

    def _show_recent_actions_dialog(self) -> None:
        existing = getattr(self, "_recent_actions_dialog", None)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Recent Actions")
        dialog.setAccessibleName("Recent actions")
        dialog.setModal(False)
        dialog.setMinimumWidth(520)
        dialog.setMinimumHeight(260)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        self._recent_actions_dialog = dialog

        root = QVBoxLayout(dialog)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        title = QLabel("Recent Actions")
        title.setStyleSheet("font-weight: 700;")
        root.addWidget(title)

        events = self.action_feedback_service.recent()[:20]
        if not events:
            empty = QLabel("No recent actions.")
            empty.setWordWrap(True)
            root.addWidget(empty)
        else:
            scroll = QScrollArea(dialog)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setAccessibleName("Recent actions list")
            rows_widget = QWidget()
            rows_widget.setAccessibleName("Recent actions")
            rows_layout = QVBoxLayout(rows_widget)
            rows_layout.setContentsMargins(0, 0, 0, 0)
            rows_layout.setSpacing(6)
            for event in events:
                line = QLabel(self._recent_action_line(event))
                line.setWordWrap(True)
                line.setAccessibleName(line.text())
                detail = str(event.detail or "").strip()
                line.setToolTip(detail or str(event.summary or ""))
                rows_layout.addWidget(line)
            rows_layout.addStretch(1)
            scroll.setWidget(rows_widget)
            root.addWidget(scroll, 1)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)
        dialog.destroyed.connect(lambda *_args: setattr(self, "_recent_actions_dialog", None))
        dialog.show()

    def _sync_settings_runtime_status(
        self,
        *,
        refresh_store: bool = True,
        include_varac_sync_status: bool = True,
    ) -> None:
        try:
            if refresh_store:
                self.station_runtime_manager.sync_with_store(
                    refresh_runtime_status=True,
                    include_varac_sync_status=include_varac_sync_status,
                )
            status = self.station_runtime_manager.runtime_status()
        except Exception:
            status = None
        try:
            if hasattr(self, "settings_tab") and hasattr(self.settings_tab, "set_multi_rig_runtime_status"):
                self.settings_tab.set_multi_rig_runtime_status(status)
        except Exception as exc:
            log.debug("MainWindow: failed syncing runtime status to Settings: %s", exc)

    def _ui_refresh_allowed(self) -> bool:
        return bool(
            not getattr(self, "_shutting_down", False)
            and getattr(self, "_app_active", True)
            and not getattr(self, "_ui_resume_pending", False)
            and not getattr(self, "_heavy_content_refresh_active", False)
        )

    def _set_heavy_content_refresh_active(self, active: bool) -> None:
        active = bool(active)
        if active == bool(getattr(self, "_heavy_content_refresh_active", False)):
            return
        self._heavy_content_refresh_active = active
        if active:
            self._mark_ui_refresh_dirty("heavy_content_refresh")
            return
        if getattr(self, "_ui_refresh_dirty", False):
            QTimer.singleShot(250, lambda: self._flush_visible_ui_refresh("heavy_content_refresh_complete"))

    def _mark_ui_refresh_dirty(self, reason: str = "") -> None:
        self._ui_refresh_dirty = True
        if reason:
            log.debug("UI_LIFECYCLE|refresh_deferred reason=%s", reason)

    def _run_timed_ui_refresh(self, label: str, callback: Callable[[], None]) -> None:
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty(label)
            return
        start = time.perf_counter()
        try:
            callback()
        except Exception:
            log.debug("UI_PERF|refresh_failed label=%s", label, exc_info=True)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        emit_span(
            "ui.callback",
            elapsed_ms,
            settings=self.settings,
            meta={"label": str(label)},
            min_ms=16.0,
            level="warning" if elapsed_ms >= 50.0 else "info",
        )
        if elapsed_ms >= 50.0:
            log.warning("UI_PERF|slow_refresh label=%s elapsed_ms=%.1f", label, elapsed_ms)
        elif elapsed_ms >= 16.0:
            log.info("UI_PERF|refresh label=%s elapsed_ms=%.1f", label, elapsed_ms)

    def _status_refresh_callbacks(self) -> tuple[tuple[str, Callable[[], None]], ...]:
        return (
            ("scheduler_status", self._refresh_scheduler_status_panel),
            ("condition_level_panel", self._refresh_condition_level_panel),
            ("station_overview", lambda: self._refresh_station_overview(force=False)),
            ("station_health_alert", self._refresh_station_health_alert),
            ("timed_debug_expiry", self._check_timed_debug_expiry),
        )

    def _schedule_status_refresh(self) -> None:
        if getattr(self, "_status_refresh_pending", False) or getattr(self, "_status_refresh_running", False):
            return
        self._status_refresh_pending = True
        QTimer.singleShot(0, self._flush_status_refresh)

    def _flush_status_refresh(self) -> None:
        self._status_refresh_pending = False
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty("status_timer")
            return
        callbacks = self._status_refresh_callbacks()
        self._status_refresh_running = True
        for offset_ms, (label, callback) in enumerate(callbacks):
            QTimer.singleShot(
                offset_ms * 60,
                lambda refresh_label=label, refresh_callback=callback: self._run_timed_ui_refresh(
                    refresh_label,
                    refresh_callback,
                ),
            )
        QTimer.singleShot(len(callbacks) * 60 + 10, lambda: setattr(self, "_status_refresh_running", False))

    def _schedule_station_command_bar_refresh(self, reason: str = "", *, force: bool = False) -> None:
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty(reason or "station_command_bar")
            return
        self._station_command_refresh_force = bool(getattr(self, "_station_command_refresh_force", False) or force)
        if getattr(self, "_station_command_refresh_pending", False):
            return
        self._station_command_refresh_pending = True
        QTimer.singleShot(90, self._flush_station_command_bar_refresh)

    def _on_scheduler_active_entry_changed(self, *_args) -> None:
        """Coalesce endpoint events into a calm, bounded UI presentation."""

        self._schedule_station_command_bar_refresh("scheduler_active_entry", force=False)
        entry = _args[0] if _args and isinstance(_args[0], Mapping) else {}
        source = str(_args[1] if len(_args) > 1 else "").strip().upper()
        signature = (
            source,
            entry.get("target_device_profile_id"),
            str(entry.get("group") or entry.get("group_name") or "").strip(),
            str(entry.get("band") or "").strip(),
            str(entry.get("frequency") or entry.get("freq") or "").strip(),
        )
        now = time.monotonic()
        if (
            signature == getattr(self, "_scheduler_status_signal_signature", None)
            and now
            - float(getattr(self, "_scheduler_status_signal_last_monotonic", 0.0) or 0.0)
            < 5.0
        ):
            return
        self._scheduler_status_signal_signature = signature
        self._scheduler_status_signal_last_monotonic = now
        if getattr(self, "_scheduler_status_signal_refresh_pending", False):
            return
        self._scheduler_status_signal_refresh_pending = True
        last_rendered = float(
            getattr(self, "_scheduler_status_signal_rendered_monotonic", 0.0) or 0.0
        )
        min_interval_sec = 2.0
        delay_ms = 350
        if last_rendered > 0.0 and now - last_rendered < min_interval_sec:
            delay_ms = max(delay_ms, int((min_interval_sec - (now - last_rendered)) * 1000.0))
        QTimer.singleShot(delay_ms, self._flush_scheduler_status_signal_refresh)

    def _flush_scheduler_status_signal_refresh(self) -> None:
        self._scheduler_status_signal_refresh_pending = False
        self._scheduler_status_signal_rendered_monotonic = time.monotonic()
        self._run_timed_ui_refresh(
            "scheduler_status_signal",
            self._refresh_scheduler_status_panel,
        )

    def _flush_station_command_bar_refresh(self) -> None:
        self._station_command_refresh_pending = False
        force = bool(getattr(self, "_station_command_refresh_force", False))
        self._station_command_refresh_force = False
        now = time.monotonic()
        last_refresh = float(getattr(self, "_station_command_last_refresh_monotonic", 0.0) or 0.0)
        min_interval_sec = 15.0
        if not force and last_refresh > 0.0 and now - last_refresh < min_interval_sec:
            self._station_command_refresh_pending = True
            delay_ms = max(40, int((min_interval_sec - (now - last_refresh)) * 1000.0))
            QTimer.singleShot(delay_ms, self._flush_station_command_bar_refresh)
            return
        self._station_command_last_refresh_monotonic = now
        self._run_timed_ui_refresh(
            "station_command_bar",
            lambda: self._refresh_station_command_bar(force=force),
        )

    def _schedule_station_command_layout(self, *, force: bool = False) -> None:
        if force:
            self._apply_station_command_bar_layout(force=True)
            return
        if getattr(self, "_station_command_layout_pending", False):
            return
        self._station_command_layout_pending = True
        QTimer.singleShot(80, self._flush_station_command_layout)

    def _flush_station_command_layout(self) -> None:
        self._station_command_layout_pending = False
        self._run_timed_ui_refresh(
            "station_command_layout",
            lambda: self._apply_station_command_bar_layout(force=False),
        )

    def _pause_noncritical_ui_timers(self) -> None:
        for timer_name in ("_status_timer", "_hold_state_timer", "_condition_levels_refresh_timer"):
            timer = getattr(self, timer_name, None)
            if isinstance(timer, QTimer) and timer.isActive():
                timer.stop()
                self._ui_timers_paused_for_inactive = True

    def _resume_noncritical_ui_timers(self) -> None:
        timer = getattr(self, "_status_timer", None)
        if isinstance(timer, QTimer) and not timer.isActive():
            timer.start()
        try:
            self.on_hold_state_changed(force_reload=False)
        except Exception:
            pass
        if bool(getattr(self, "_condition_levels_refresh_pending", False)):
            timer = getattr(self, "_condition_levels_refresh_timer", None)
            if isinstance(timer, QTimer):
                timer.start()
        self._ui_timers_paused_for_inactive = False

    def _set_child_app_active(self, active: bool) -> None:
        seen: set[int] = set()
        for label, widget in getattr(self, "_screens", []):
            seen.add(id(widget))
            try:
                if hasattr(widget, "set_app_active"):
                    widget.set_app_active(bool(active))
            except Exception as e:
                log.debug("UI_LIFECYCLE|child_state_failed label=%s err=%s", label, e)
        map_tab = getattr(self, "stations_map_tab", None)
        if map_tab is not None and id(map_tab) not in seen:
            try:
                if hasattr(map_tab, "set_app_active"):
                    map_tab.set_app_active(bool(active))
            except Exception as e:
                log.debug("UI_LIFECYCLE|child_state_failed label=MapWindow err=%s", e)

    def _flush_visible_ui_refresh(self, reason: str = "resume") -> None:
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty(reason)
            return
        self._ui_refresh_dirty = False
        log.info("UI_LIFECYCLE|visible_refresh reason=%s", reason)
        self._flush_status_refresh()
        try:
            widget = self.stack.currentWidget() if hasattr(self, "stack") else None
            if widget is not None and hasattr(widget, "on_tab_activated"):
                QTimer.singleShot(0, widget.on_tab_activated)
        except Exception:
            pass

    def _on_ui_resume_settled(self) -> None:
        self._ui_resume_pending = False
        self._resume_noncritical_ui_timers()
        self._set_child_app_active(True)
        scheduler_resume_required = bool(
            getattr(self, "_ui_scheduler_resume_required", False)
        )
        self._ui_scheduler_resume_required = False
        if scheduler_resume_required:
            try:
                scheduler = getattr(self, "scheduler", None)
                if scheduler is not None and hasattr(scheduler, "handle_resume"):
                    try:
                        scheduler.handle_resume(force_recompute=True)
                    except TypeError:
                        # Keep compatibility with test doubles and external
                        # scheduler facades that predate the lifecycle hint.
                        scheduler.handle_resume()
            except Exception as exc:
                log.debug("UI_LIFECYCLE|scheduler_resume_failed err=%s", exc)
        else:
            log.info("UI_LIFECYCLE|initial_activation scheduler_resume_skipped=True")
        self._flush_visible_ui_refresh("app_resume")

    def _on_ui_inactive_settled(self) -> None:
        """Pause only after inactivity survives the native-surface grace period."""
        if bool(getattr(self, "_shutting_down", False)):
            self._ui_inactive_pending = False
            return
        if not bool(getattr(self, "_ui_inactive_pending", False)):
            return
        self._ui_inactive_pending = False
        if getattr(self, "_observed_application_state", Qt.ApplicationActive) == Qt.ApplicationActive:
            return
        if not bool(getattr(self, "_app_active", True)):
            return
        self._app_active = False
        # Losing desktop focus is not an operating-system resume boundary.
        # Retiring scheduler lanes for an ordinary ApplicationInactive event
        # discards valid expected/readback state and can provoke a redundant
        # rig write when focus returns.  Only native hidden/suspended states
        # request destructive scheduler lifecycle recovery; the scheduler's
        # monotonic/wall-clock observer independently detects sleep/wake on
        # platforms that do not report ApplicationSuspended reliably.
        observed_state = getattr(
            self,
            "_observed_application_state",
            Qt.ApplicationInactive,
        )
        scheduler_resume_states = {
            getattr(Qt, "ApplicationHidden", None),
            getattr(Qt, "ApplicationSuspended", None),
        }
        self._ui_scheduler_resume_required = observed_state in scheduler_resume_states
        log.info(
            "UI_LIFECYCLE|app_active=False state=%s scheduler_resume_required=%s",
            observed_state,
            self._ui_scheduler_resume_required,
        )
        self._ui_resume_pending = False
        self._ui_resume_settle_timer.stop()
        self._pause_noncritical_ui_timers()
        self._set_child_app_active(False)
        self._mark_ui_refresh_dirty("app_inactive")

    def _on_application_state_changed(self, state) -> None:
        active = state == Qt.ApplicationActive
        self._observed_application_state = state
        if active:
            pending_inactive = bool(getattr(self, "_ui_inactive_pending", False))
            self._ui_inactive_pending = False
            self._ui_inactive_settle_timer.stop()
            if bool(getattr(self, "_app_active", True)):
                if pending_inactive:
                    log.info("UI_LIFECYCLE|transient_inactive_ignored state=%s", state)
                return
            self._app_active = True
            log.info("UI_LIFECYCLE|app_active=True state=%s", state)
            self._ui_resume_pending = True
            self._pause_noncritical_ui_timers()
            self._set_child_app_active(False)
            self._ui_resume_settle_timer.start()
            return
        if not bool(getattr(self, "_app_active", True)):
            return
        self._ui_inactive_pending = True
        immediate_states = {
            getattr(Qt, "ApplicationHidden", None),
            getattr(Qt, "ApplicationSuspended", None),
        }
        if state in immediate_states:
            self._ui_inactive_settle_timer.stop()
            self._on_ui_inactive_settled()
            return
        log.info("UI_LIFECYCLE|inactive_pending state=%s grace_ms=1500", state)
        self._ui_inactive_settle_timer.start()

    def refresh_operator_history_views(self):
        """
        Reload operator history across tabs so new entries (e.g., CSV import, JS8 load)
        are visible without restarting.
        """
        try:
            if hasattr(self.operator_history_tab, "_load_data"):
                self.operator_history_tab._load_data()
        except Exception as e:
            log.debug("MainWindow: operator_history_tab refresh failed: %s", e)
        try:
            if self.stations_map_tab is not None and hasattr(self.stations_map_tab, "_load_operator_history"):
                self.stations_map_tab._load_operator_history()
                map_visible = bool(getattr(self.stations_map_tab, "_map_visible", False))
                if map_visible and hasattr(self.stations_map_tab, "_schedule_render"):
                    self.stations_map_tab._schedule_render()
                elif not map_visible and hasattr(self.stations_map_tab, "_map_dirty"):
                    self.stations_map_tab._map_dirty = True
        except Exception as e:
            log.debug("MainWindow: stations_map_tab refresh failed: %s", e)
        try:
            if hasattr(self.fldigi_tab, "_load_known_operators"):
                self.fldigi_tab._load_known_operators()
        except Exception as e:
            log.debug("MainWindow: fldigi_tab refresh failed: %s", e)

        try:
            tab = getattr(self, "controlfreq_tab", None)
            if tab is not None and hasattr(tab, "on_operator_roster_changed"):
                tab.on_operator_roster_changed()
        except Exception as exc:
            log.debug("MainWindow: Ops roster invalidation failed: %s", exc)

    def _on_operator_history_local_update(self) -> None:
        """
        Lightweight fanout for updates that originated inside Operators tab.
        The Operators table already has local state applied; avoid reloading it.
        """
        try:
            if self.stations_map_tab is not None and hasattr(self.stations_map_tab, "_load_operator_history"):
                self.stations_map_tab._load_operator_history()
                map_visible = bool(getattr(self.stations_map_tab, "_map_visible", False))
                if map_visible and hasattr(self.stations_map_tab, "_schedule_render"):
                    self.stations_map_tab._schedule_render()
                elif not map_visible and hasattr(self.stations_map_tab, "_map_dirty"):
                    self.stations_map_tab._map_dirty = True
        except Exception as e:
            log.debug("MainWindow: stations_map_tab local refresh failed: %s", e)
        try:
            if hasattr(self.fldigi_tab, "_load_known_operators"):
                self.fldigi_tab._load_known_operators()
        except Exception as e:
            log.debug("MainWindow: fldigi_tab local refresh failed: %s", e)

        try:
            tab = getattr(self, "controlfreq_tab", None)
            if tab is not None and hasattr(tab, "on_operator_roster_changed"):
                tab.on_operator_roster_changed()
        except Exception as exc:
            log.debug("MainWindow: Ops roster invalidation failed: %s", exc)

    def on_peer_schedule_data_changed(self) -> None:
        try:
            if hasattr(self, "controlfreq_tab") and self.controlfreq_tab is not None:
                if hasattr(self.controlfreq_tab, "on_peer_schedule_data_changed"):
                    self.controlfreq_tab.on_peer_schedule_data_changed()
        except Exception as e:
            log.debug("MainWindow: controlfreq peer schedule refresh failed: %s", e)
        try:
            if self.stations_map_tab is not None:
                map_visible = bool(getattr(self.stations_map_tab, "_map_visible", False))
                if map_visible and hasattr(self.stations_map_tab, "_schedule_render"):
                    self.stations_map_tab._schedule_render()
                elif hasattr(self.stations_map_tab, "_map_dirty"):
                    self.stations_map_tab._map_dirty = True
        except Exception as e:
            log.debug("MainWindow: stations_map_tab peer schedule refresh failed: %s", e)

    def _update_log_indicator(self) -> None:
        try:
            level = (self.settings.get("log_level", "") or "DISABLED").upper()
            if level == "DISABLED":
                self.logs_active_btn.setVisible(False)
            else:
                self.logs_active_btn.setVisible(True)
                self.logs_active_btn.setText(f"Logs: {level}")
                self.logs_active_btn.setToolTip(
                    "Logging is active. Disable in Configuration unless you are troubleshooting."
                )
                try:
                    theme = resolve_theme(self.settings)
                    self.logs_active_btn.setStyleSheet(button_style("warning", theme))
                except Exception:
                    pass
        except Exception as e:
            log.debug("MainWindow: log indicator update failed: %s", e)

    def _open_logs_window(self) -> None:
        try:
            if self.log_tab is None:
                self.log_tab = LogViewerTab(self)
                try:
                    self.log_tab.log_level_changed.connect(self._update_log_indicator)
                except Exception:
                    pass
            if self._log_dialog is None:
                dlg = QDialog(self)
                dlg.setWindowTitle("Logs")
                dlg.resize(980, 620)
                layout = QVBoxLayout(dlg)
                layout.setContentsMargins(8, 8, 8, 8)
                layout.addWidget(self.log_tab)
                try:
                    dlg.finished.connect(lambda _=0: self.log_tab.set_tab_active(False))
                except Exception:
                    pass
                self._log_dialog = dlg
            self._log_dialog.show()
            self._log_dialog.raise_()
            self._log_dialog.activateWindow()
            try:
                self.log_tab.set_tab_active(True)
            except Exception:
                pass
            try:
                self.log_tab._refresh()  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception as e:
            log.debug("MainWindow: failed to open logs window: %s", e)

    def _check_timed_debug_expiry(self) -> None:
        try:
            until_txt = (self.settings.get("timed_debug_until_utc", "") or "").strip()
            if not until_txt:
                return
            try:
                until_dt = datetime.datetime.fromisoformat(until_txt)
            except Exception:
                until_dt = None
            if until_dt is None:
                return
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=datetime.timezone.utc)
            else:
                until_dt = until_dt.astimezone(datetime.timezone.utc)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            if now_utc < until_dt:
                return
            prev = (self.settings.get("timed_debug_prev_level", "") or "DISABLED").strip().upper()
            if prev not in {"DISABLED", "ERROR", "WARNING", "INFO", "DEBUG"}:
                prev = "DISABLED"
            self.settings.set_many(
                {
                    "log_level": prev,
                    "timed_debug_until_utc": "",
                    "timed_debug_prev_level": "",
                }
            )
            set_log_level(prev)
            if self.log_tab is not None:
                try:
                    idx = self.log_tab.level_combo.findText(prev)
                    if idx >= 0:
                        self.log_tab.level_combo.setCurrentIndex(idx)
                except Exception:
                    pass
            self._update_log_indicator()
        except Exception as e:
            log.debug("MainWindow: timed debug expiry check failed: %s", e)

    def _init_map_filters(self) -> None:
        """
        Build a static sidebar panel for map display filters (no reparenting).
        """
        box = QGroupBox("Map Layers")
        box.setCheckable(False)
        v = QVBoxLayout(box)
        v.setContentsMargins(4, 4, 4, 4)
        self.map_cb_callsigns = QCheckBox("Callsigns")
        self.map_cb_regions = QCheckBox("Regions")
        self.map_cb_grids = QCheckBox("Grids")
        self.map_cb_states = QCheckBox("States")
        self.map_cb_cities = QCheckBox("Cities")
        v.addWidget(self.map_cb_callsigns)
        grid_row1 = QHBoxLayout()
        self.map_cb_regions.setMinimumWidth(90)
        self.map_cb_states.setMinimumWidth(90)
        grid_row1.addWidget(self.map_cb_regions)
        grid_row1.addWidget(self.map_cb_grids)
        grid_row1.setAlignment(Qt.AlignLeft)
        v.addLayout(grid_row1)
        grid_row2 = QHBoxLayout()
        grid_row2.addWidget(self.map_cb_states)
        grid_row2.addWidget(self.map_cb_cities)
        grid_row2.setAlignment(Qt.AlignLeft)
        v.addLayout(grid_row2)
        for cb in (
            self.map_cb_callsigns,
            self.map_cb_states,
            self.map_cb_cities,
            self.map_cb_grids,
            self.map_cb_regions,
        ):
            cb.stateChanged.connect(self._on_sidebar_map_filter_changed)

        # Population threshold
        self.map_pop_combo = QComboBox()
        self.map_pop_options = [
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
        for label, val in self.map_pop_options:
            self.map_pop_combo.addItem(label, val)
        self.map_pop_combo.currentIndexChanged.connect(self._on_sidebar_map_filter_changed)
        pop_row = QHBoxLayout()
        pop_row.addWidget(QLabel("City Pop."))
        pop_row.addWidget(self.map_pop_combo)
        pop_row.addStretch()
        v.addLayout(pop_row)

        v.addSpacing(8)
        v.addWidget(QLabel("Propagation"))
        self.map_cb_prop_overlay = QCheckBox("Propagation Overlay")
        v.addWidget(self.map_cb_prop_overlay)
        history_label = "History (7 days)"
        prop_combo_width = max(120, self.fontMetrics().horizontalAdvance(history_label) + 42)
        prop_label_width = 54

        def _prop_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setMinimumWidth(prop_label_width)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return lbl

        def _style_prop_combo(combo: QComboBox) -> None:
            combo.setMinimumWidth(prop_combo_width)
            combo.setMaximumWidth(prop_combo_width)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            try:
                combo.view().setMinimumWidth(prop_combo_width)
            except Exception:
                pass

        mode_row = QHBoxLayout()
        mode_row.addWidget(_prop_label("Mode:"))
        self.map_prop_mode_combo = QComboBox()
        self.map_prop_mode_combo.addItem("Actual", "actual")
        self.map_prop_mode_combo.addItem("Blended", "blended")
        self.map_prop_mode_combo.addItem("Modeled", "model")
        _style_prop_combo(self.map_prop_mode_combo)
        mode_row.addWidget(self.map_prop_mode_combo)
        mode_row.addStretch()
        v.addLayout(mode_row)
        window_row = QHBoxLayout()
        window_row.addWidget(_prop_label("Window:"))
        self.map_prop_window_combo = QComboBox()
        self.map_prop_window_combo.addItem("1h", 1)
        self.map_prop_window_combo.addItem("3h", 3)
        self.map_prop_window_combo.addItem("6h", 6)
        self.map_prop_window_combo.addItem("12h", 12)
        self.map_prop_window_combo.addItem("24h", 24)
        self.map_prop_window_combo.addItem("7 Days", 168)
        _style_prop_combo(self.map_prop_window_combo)
        window_row.addWidget(self.map_prop_window_combo)
        window_row.addStretch()
        v.addLayout(window_row)
        target_type_row = QHBoxLayout()
        target_type_row.addWidget(_prop_label("Target:"))
        self.map_prop_target_type_combo = QComboBox()
        self.map_prop_target_type_combo.addItem("Region", "REGION")
        self.map_prop_target_type_combo.addItem("State", "STATE")
        self.map_prop_target_type_combo.addItem("Operator", "OPERATOR")
        _style_prop_combo(self.map_prop_target_type_combo)
        target_type_row.addWidget(self.map_prop_target_type_combo)
        target_type_row.addStretch()
        v.addLayout(target_type_row)
        target_value_row = QHBoxLayout()
        target_value_row.addWidget(_prop_label("Value:"))
        self.map_prop_target_value_combo = QComboBox()
        self.map_prop_target_value_combo.setEditable(True)
        self.map_prop_target_value_combo.setInsertPolicy(QComboBox.NoInsert)
        self.map_prop_target_value_combo.setDuplicatesEnabled(False)
        _style_prop_combo(self.map_prop_target_value_combo)
        target_value_row.addWidget(self.map_prop_target_value_combo)
        target_value_row.addStretch()
        v.addLayout(target_value_row)
        self.map_prop_badge = QLabel("Best Band: --")
        theme = resolve_theme(self.settings)
        self.map_prop_badge.setStyleSheet(label_style("info", theme, weight=700))
        v.addWidget(self.map_prop_badge)
        self.map_cb_prop_overlay.stateChanged.connect(self._on_sidebar_prop_changed)
        self.map_prop_mode_combo.currentIndexChanged.connect(self._on_sidebar_prop_mode_changed)
        self.map_prop_window_combo.currentIndexChanged.connect(self._on_sidebar_prop_window_changed)
        self.map_prop_target_type_combo.currentIndexChanged.connect(self._on_sidebar_prop_target_type_changed)
        self.map_prop_target_value_combo.currentTextChanged.connect(self._on_sidebar_prop_target_value_changed)
        self._refresh_map_prop_target_controls()
        v.addStretch()
        self.map_filters_layout.addWidget(box)

    def _sync_map_filters_from_tab(self) -> None:
        """
        Update sidebar controls from current map tab state.
        """
        tab = getattr(self, "stations_map_tab", None)
        if tab is None:
            return
        block = [
            self.map_cb_callsigns,
            self.map_cb_states,
            self.map_cb_cities,
            self.map_cb_grids,
            self.map_cb_regions,
            self.map_cb_prop_overlay,
            self.map_prop_mode_combo,
            self.map_prop_window_combo,
        ]
        for cb in block:
            cb.blockSignals(True)
        self.map_cb_callsigns.setChecked(bool(getattr(tab, "show_callsigns", False)))
        self.map_cb_states.setChecked(bool(getattr(tab, "show_states", False)))
        self.map_cb_cities.setChecked(bool(getattr(tab, "show_cities", False)))
        self.map_cb_grids.setChecked(bool(getattr(tab, "show_grids", False)))
        self.map_cb_regions.setChecked(bool(getattr(tab, "show_regions", False)))
        self.map_cb_prop_overlay.setChecked(bool(getattr(tab, "prop_overlay_enabled", False)))
        mode = getattr(tab, "prop_mode", "blended") or "blended"
        idx = self.map_prop_mode_combo.findData(str(mode).lower())
        if idx >= 0:
            self.map_prop_mode_combo.setCurrentIndex(idx)
        try:
            window_hours = int(getattr(tab, "prop_window_hours", 6))
        except Exception:
            window_hours = 6
        idx = self.map_prop_window_combo.findData(window_hours)
        if idx >= 0:
            self.map_prop_window_combo.setCurrentIndex(idx)
        for cb in block:
            cb.blockSignals(False)
        # Pop combo sync
        try:
            current_min = int(getattr(tab, "city_pop_min", 100000))
        except Exception:
            current_min = 100000
        idx = self.map_pop_combo.findData(current_min)
        if idx < 0:
            idx = 4  # default 100k+
        self.map_pop_combo.blockSignals(True)
        self.map_pop_combo.setCurrentIndex(idx)
        self.map_pop_combo.blockSignals(False)
        self._refresh_map_prop_target_controls()

    def _load_map_prop_operator_callsigns(self) -> list[str]:
        db_path = get_config_dir() / "config" / "freqinout_nets.db"
        if not db_path.exists():
            return []
        out: list[str] = []
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT IFNULL(callsign, '')
                FROM operator_checkins
                ORDER BY callsign COLLATE NOCASE
                """
            )
            for (callsign,) in cur.fetchall():
                cs = (callsign or "").strip().upper()
                if cs and cs not in out:
                    out.append(cs)
            conn.close()
        except Exception as e:
            log.debug("MainWindow: failed to load map propagation operator options: %s", e)
        return out

    def _map_prop_target_options(self, target_type: str) -> list[str]:
        target_type = (target_type or "REGION").strip().upper()
        if target_type == "STATE":
            return [s for s in LOWER48_STATES if s in STATE_CENTERS]
        if target_type == "OPERATOR":
            return self._load_map_prop_operator_callsigns()
        return ["ALL"] + sorted(FEMA_REGIONS.keys())

    def _set_map_prop_target_value_options(self, target_type: str, selected_value: str) -> None:
        target_type = (target_type or "REGION").strip().upper()
        selected_value = (selected_value or "").strip().upper()
        if target_type == "REGION" and selected_value == "NATIONAL":
            selected_value = "ALL"
        options = self._map_prop_target_options(target_type)
        self.map_prop_target_value_combo.blockSignals(True)
        self.map_prop_target_value_combo.clear()
        for value in options:
            self.map_prop_target_value_combo.addItem(value)
        if selected_value:
            idx = self.map_prop_target_value_combo.findText(selected_value, Qt.MatchFixedString)
            if idx >= 0:
                self.map_prop_target_value_combo.setCurrentIndex(idx)
            else:
                self.map_prop_target_value_combo.setEditText(selected_value)
        elif self.map_prop_target_value_combo.count() > 0:
            self.map_prop_target_value_combo.setCurrentIndex(0)
        else:
            self.map_prop_target_value_combo.setEditText("")
        self.map_prop_target_value_combo.setEditable(target_type == "OPERATOR")
        self.map_prop_target_value_combo.blockSignals(False)

    def _refresh_map_prop_target_controls(self) -> None:
        if not hasattr(self, "map_prop_target_type_combo") or not hasattr(self, "map_prop_target_value_combo"):
            return
        self._map_prop_target_syncing = True
        try:
            self.settings.reload()
            target_type = (self.settings.get("prop_target_type", "REGION") or "REGION").strip().upper()
            if target_type not in {"REGION", "STATE", "OPERATOR"}:
                target_type = "REGION"
            target_value = (self.settings.get("prop_target_value", "") or "").strip().upper()
            idx = self.map_prop_target_type_combo.findData(target_type)
            if idx < 0:
                idx = 0
            self.map_prop_target_type_combo.blockSignals(True)
            self.map_prop_target_type_combo.setCurrentIndex(idx)
            self.map_prop_target_type_combo.blockSignals(False)
            self._set_map_prop_target_value_options(target_type, target_value)
            current_value = (self.map_prop_target_value_combo.currentText() or "").strip().upper()
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
            log.debug("MainWindow: failed to sync map propagation target controls: %s", e)
        finally:
            self._map_prop_target_syncing = False

    def _on_sidebar_map_filter_changed(self, _=None) -> None:
        """
        Push sidebar filter changes into the map tab and refresh the map.
        """
        tab = getattr(self, "stations_map_tab", None)
        if tab is None:
            return
        tab.show_callsigns = self.map_cb_callsigns.isChecked()
        tab.show_states = self.map_cb_states.isChecked()
        tab.show_cities = self.map_cb_cities.isChecked()
        tab.show_grids = self.map_cb_grids.isChecked()
        tab.show_grid_labels = tab.show_grids
        tab.show_regions = self.map_cb_regions.isChecked()
        # Pop min
        try:
            pop_val = int(self.map_pop_combo.currentData())
        except Exception:
            pop_val = 100000
        tab.city_pop_min = pop_val
        # Mirror into map tab's own combo for consistency
        if hasattr(tab, "city_pop_combo"):
            try:
                idx = tab.city_pop_combo.findData(pop_val)
                if idx >= 0:
                    tab.city_pop_combo.blockSignals(True)
                    tab.city_pop_combo.setCurrentIndex(idx)
                    tab.city_pop_combo.blockSignals(False)
            except Exception:
                pass
        # Persist and redraw
        if hasattr(tab, "_save_display_preferences"):
            tab._save_display_preferences()
        if hasattr(tab, "_request_map_refresh"):
            tab._request_map_refresh(level="medium", reason="sidebar_layers")

    def _on_sidebar_prop_changed(self, _=None) -> None:
        tab = getattr(self, "stations_map_tab", None)
        if tab is None:
            return
        tab.prop_overlay_enabled = self.map_cb_prop_overlay.isChecked()
        mode = self.map_prop_mode_combo.currentData() if hasattr(self, "map_prop_mode_combo") else None
        if mode:
            tab.prop_mode = str(mode)
        if hasattr(tab, "_save_display_preferences"):
            tab._save_display_preferences()
        if hasattr(tab, "_request_map_refresh"):
            tab._request_map_refresh(level="full", reason="sidebar_prop_overlay")

    def _on_sidebar_prop_mode_changed(self, _=None) -> None:
        tab = getattr(self, "stations_map_tab", None)
        if tab is None:
            return
        mode = self.map_prop_mode_combo.currentData()
        if mode:
            tab.prop_mode = str(mode)
        if hasattr(tab, "_save_display_preferences"):
            tab._save_display_preferences()
        if hasattr(tab, "_request_map_refresh"):
            tab._request_map_refresh(level="full", reason="sidebar_prop_mode")

    def _on_sidebar_prop_window_changed(self, _=None) -> None:
        tab = getattr(self, "stations_map_tab", None)
        if tab is None:
            return
        try:
            hours = int(self.map_prop_window_combo.currentData())
        except Exception:
            hours = 6
        tab.prop_window_hours = hours
        if hasattr(tab, "_save_display_preferences"):
            tab._save_display_preferences()
        if hasattr(tab, "_request_map_refresh"):
            tab._request_map_refresh(level="full", reason="sidebar_prop_window")

    def _on_sidebar_prop_target_type_changed(self, _=None) -> None:
        if self._map_prop_target_syncing:
            return
        target_type = (self.map_prop_target_type_combo.currentData() or "REGION").strip().upper()
        self._map_prop_target_syncing = True
        try:
            self._set_map_prop_target_value_options(target_type, "")
            value = (self.map_prop_target_value_combo.currentText() or "").strip().upper()
            self.settings.set_many(
                {
                    "prop_target_type": target_type,
                    "prop_target_value": value,
                }
            )
        except Exception as e:
            log.debug("MainWindow: propagation target type change failed: %s", e)
        finally:
            self._map_prop_target_syncing = False
        tab = getattr(self, "stations_map_tab", None)
        if tab is not None and hasattr(tab, "_request_map_refresh"):
            tab._request_map_refresh(level="full", reason="sidebar_prop_target_type")

    def _on_sidebar_prop_target_value_changed(self, text: str) -> None:
        if self._map_prop_target_syncing:
            return
        target_type = (self.map_prop_target_type_combo.currentData() or "REGION").strip().upper()
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
            log.debug("MainWindow: propagation target value change failed: %s", e)
        tab = getattr(self, "stations_map_tab", None)
        if tab is not None and hasattr(tab, "_request_map_refresh"):
            tab._request_map_refresh(level="full", reason="sidebar_prop_target_value")

    def _update_map_filters_visibility(self, index: int) -> None:
        """
        Keep the persistent Map lifecycle independent from main-stack selection.

        The index is retained for the shared screen-switch call site; Map work is
        active only while its own top-level window is actually available.
        """
        window = getattr(self, "map_window", None)
        is_map = bool(window is not None and window.is_available_for_work())
        try:
            if self.stations_map_tab is not None and hasattr(self.stations_map_tab, "set_map_visible"):
                self.stations_map_tab.set_map_visible(is_map)
        except Exception:
            pass
        if hasattr(self, "map_filters_container"):
            self.map_filters_container.setVisible(False)

    def _schedule_feedback_target(self) -> tuple[str | None, str]:
        profile = getattr(self, "_active_runtime_profile", None)
        if isinstance(profile, dict):
            profile_id = profile.get("id")
            label = str(profile.get("name") or profile.get("label") or "").strip()
            return (str(profile_id) if profile_id not in (None, "") else None, label or "Radio")
        return None, "Radio"

    def _publish_schedule_control_feedback(
        self,
        *,
        action_type: str,
        status: str,
        summary: str,
        detail: str = "",
    ) -> None:
        service = getattr(self, "action_feedback_service", None)
        if service is None or not hasattr(service, "publish"):
            log.debug("MainWindow: action feedback service unavailable for schedule control.")
            return
        radio_profile_id, target_label = self._schedule_feedback_target()
        try:
            service.publish(
                scope="scheduler",
                action_type=action_type,
                status=status,
                summary=str(summary or "").strip(),
                radio_profile_id=radio_profile_id,
                target_label=target_label,
                detail=str(detail or "").strip(),
                source_surface="main_window_schedule_control",
            )
        except Exception as e:
            log.debug("MainWindow: failed to publish schedule control feedback: %s", e)

    def _on_resume_schedule_clicked(self) -> None:
        resumed = False
        try:
            if hasattr(self, "scheduler"):
                if hasattr(self.scheduler, "resume_schedule"):
                    resumed = bool(resume_schedule_hold(self, self.settings))
                    if not resumed:
                        return
                else:
                    try:
                        set_suspend_until(self.scheduler.settings, None)
                        self.on_hold_state_changed(force_reload=False)
                    except Exception:
                        pass
                    result = self.scheduler.apply_current_entry(
                        force=True,
                        ignore_wait_prompt=True,
                        ignore_suspend=True,
                        ignore_js8_busy=True,
                        ignore_varac_busy=True,
                        ignore_fldigi_busy=True,
                    )
                    if result is False:
                        self._publish_schedule_control_feedback(
                            action_type="resume_schedule",
                            status="failed",
                            summary="Resume failed: schedule control could not return to plan.",
                            detail="Scheduler reported that the current schedule entry could not be applied.",
                        )
                        return
                    resumed = True
        except Exception as e:
            log.debug("MainWindow: resume schedule action failed: %s", e)
            self._publish_schedule_control_feedback(
                action_type="resume_schedule",
                status="failed",
                summary="Resume failed: schedule control could not return to plan.",
                detail=str(e) or "Schedule control failed while resuming.",
            )
            return
        if not resumed:
            self._publish_schedule_control_feedback(
                action_type="resume_schedule",
                status="blocked",
                summary="Resume blocked: schedule control did not return to plan.",
                detail="The scheduler was unavailable or RF Safety Guard blocked the resume.",
            )
            return
        _radio_id, target_label = self._schedule_feedback_target()
        self._publish_schedule_control_feedback(
            action_type="resume_schedule",
            status="succeeded",
            summary=f"{target_label} returned to schedule.",
            detail="Schedule control resumed for the active radio.",
        )
        try:
            self._refresh_scheduler_status_panel()
        except Exception:
            pass
        try:
            if hasattr(self, "controlfreq_tab") and self.controlfreq_tab is not None:
                self.controlfreq_tab.on_schedule_resumed()
        except Exception:
            pass
        # Follow-up pulses help UI converge quickly while scheduler/radio apply completes.
        for delay_ms in (300, 1100):
            try:
                QTimer.singleShot(delay_ms, self._refresh_scheduler_status_panel)
            except Exception:
                pass

    def _on_suspend_schedule_clicked(self) -> None:
        try:
            if not hasattr(self, "scheduler"):
                self._publish_schedule_control_feedback(
                    action_type="suspend_schedule",
                    status="blocked",
                    summary="Suspend blocked: scheduler is unavailable.",
                    detail="Schedule control could not pause because the scheduler is unavailable.",
                )
                return
            hold_snapshot = suspend_snapshot(self.settings)
            if hold_snapshot.get("active"):
                self._on_resume_schedule_clicked()
                return
            mins = self._selected_sidebar_hold_minutes()
            suspend_schedule_hold(self, self.settings, mins)
            _radio_id, target_label = self._schedule_feedback_target()
            self._publish_schedule_control_feedback(
                action_type="suspend_schedule",
                status="succeeded",
                summary=f"{target_label} suspended for {mins} minutes.",
                detail=f"Schedule control paused for {mins} minutes.",
            )
        except Exception as e:
            log.debug("MainWindow: suspend schedule action failed: %s", e)
            self._publish_schedule_control_feedback(
                action_type="suspend_schedule",
                status="failed",
                summary="Suspend failed: schedule control could not pause.",
                detail=str(e) or "Schedule control failed while suspending.",
            )
            return
        self._refresh_scheduler_status_panel()

    def _selected_sidebar_hold_minutes(self) -> int:
        return selected_hold_duration(
            getattr(self, "suspend_duration_combo", None),
            self.settings,
            getattr(self, "_active_runtime_profile", None),
        )

    def _on_sidebar_hold_duration_changed(self) -> None:
        mins = self._selected_sidebar_hold_minutes()
        set_hold_duration_default(self.settings, mins)
        self.on_hold_duration_default_changed()

    def _hold_state_targets(self) -> list[object]:
        return [
            getattr(self, "controlfreq_tab", None),
            getattr(self, "hf_schedule_tab", None),
            getattr(self, "fldigi_tab", None),
            getattr(self, "js8_tab", None),
        ]

    def _hold_duration_combos(self) -> list[QComboBox]:
        combos: list[QComboBox] = []
        for combo in (
            getattr(self, "suspend_duration_combo", None),
            getattr(getattr(self, "controlfreq_tab", None), "hold_duration_combo", None),
            getattr(getattr(self, "hf_schedule_tab", None), "hold_duration_combo", None),
            getattr(getattr(self, "fldigi_tab", None), "hold_duration_combo", None),
            getattr(getattr(self, "js8_tab", None), "hold_duration_combo", None),
        ):
            if isinstance(combo, QComboBox):
                combos.append(combo)
        return combos

    @staticmethod
    def _hold_snapshot_signature(snapshot: dict[str, object] | None) -> tuple[object, ...]:
        snap = snapshot if isinstance(snapshot, dict) else {}
        return (
            bool(snap.get("active")),
            int(snap.get("remaining_minutes") or 0),
            str(snap.get("severity") or "idle"),
            int(bool(snap.get("about_to_resume"))),
        )

    def _sync_hold_duration_combos(self) -> None:
        for combo in self._hold_duration_combos():
            try:
                if combo.view().isVisible() or combo.hasFocus():
                    continue
            except Exception:
                pass
            try:
                refresh_hold_duration_combo(combo, self.settings, getattr(self, "_active_runtime_profile", None))
            except Exception:
                continue

    def _broadcast_hold_state(self, snapshot: dict[str, object]) -> None:
        for tab in self._hold_state_targets():
            if tab is None or not hasattr(tab, "on_hold_state_changed"):
                continue
            try:
                tab.on_hold_state_changed(snapshot=snapshot)
            except Exception:
                continue

    def _apply_active_hold_status_panel(self, hold_snapshot: dict[str, object]) -> None:
        suspended_until = hold_snapshot.get("until")
        if not isinstance(suspended_until, datetime.datetime):
            return
        local_dt = suspended_until.astimezone()
        remaining_sec = hold_snapshot.get("remaining_sec")
        remaining_min = hold_snapshot.get("remaining_minutes") or 0
        severity_role = active_hold_button_role(remaining_sec)
        self.scheduler_status_header.setText(
            "Resuming Soon" if hold_snapshot.get("about_to_resume") else "Schedule Paused"
        )
        self._set_scheduler_reasons(
            [
                f"Auto resume in {remaining_min} min",
                f"At {local_dt:%Y-%m-%d %H:%M}",
            ]
        )
        self.resume_schedule_btn.setVisible(False)
        self.suspend_duration_label.setVisible(True)
        self.suspend_duration_combo.setVisible(True)
        self.suspend_schedule_btn.setVisible(True)
        self.suspend_schedule_btn.setText(active_hold_button_text(remaining_sec))
        self.suspend_schedule_btn.setToolTip(active_hold_status_text(remaining_sec))
        try:
            theme = resolve_theme(self.settings)
            self.suspend_schedule_btn.setStyleSheet(button_style(severity_role, theme))
            highlight = theme.get("surface_alt", theme.get("surface", "#FFFFFF"))
            border = theme.get(
                "danger" if severity_role == "danger" else "warning",
                theme.get("border", "#CCCCCC"),
            )
            self.scheduler_status_container.setStyleSheet(
                "QGroupBox { background-color: %s; border: 1px solid %s; border-radius: 6px; }"
                "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 4px; }"
                % (highlight, border)
            )
        except Exception:
            pass
        try:
            self._update_scheduler_action_button_widths()
            self.scheduler_status_container.adjustSize()
        except Exception:
            pass
        self._auto_collapse_inactive_nav_groups()
        self._hide_sidebar_schedule_controls()

    def _hide_sidebar_schedule_controls(self) -> None:
        for widget in (
            getattr(self, "resume_schedule_btn", None),
            getattr(self, "suspend_schedule_btn", None),
            getattr(self, "suspend_duration_label", None),
            getattr(self, "suspend_duration_combo", None),
        ):
            if widget is not None:
                try:
                    widget.setVisible(False)
                except Exception:
                    pass

    def _dispatch_hold_snapshot(
        self,
        snapshot: dict[str, object],
        *,
        force: bool = False,
        sync_combos: bool = False,
    ) -> None:
        previous_snapshot = self._hold_state_snapshot if isinstance(self._hold_state_snapshot, dict) else {}
        was_active = bool(previous_snapshot.get("active"))
        signature = self._hold_snapshot_signature(snapshot)
        signature_changed = force or signature != self._hold_state_signature
        self._hold_state_snapshot = snapshot
        self._hold_state_signature = signature
        card_controls = getattr(self, "_station_command_radio_tile_controls", None)
        cards_active = isinstance(card_controls, Mapping) and bool(card_controls)
        if sync_combos:
            self._sync_hold_duration_combos()
            if snapshot.get("active"):
                try:
                    if not self._hold_state_timer.isActive():
                        self._hold_state_timer.start()
                except Exception:
                    pass
            if signature_changed:
                self._broadcast_hold_state(snapshot)
                self._apply_active_hold_status_panel(snapshot)
                if cards_active and not force:
                    self._update_station_command_hold_button_labels(snapshot)
                    self._update_station_command_radio_tile_hold_controls(snapshot)
                else:
                    self._refresh_station_command_bar(force=False)
            elif snapshot.get("active"):
                self._broadcast_hold_state(snapshot)
                self._update_station_command_hold_button_labels(snapshot)
                self._update_station_command_radio_tile_hold_controls(snapshot)
            return
        try:
            if self._hold_state_timer.isActive():
                self._hold_state_timer.stop()
        except Exception:
            pass
        if signature_changed:
            self._broadcast_hold_state(snapshot)
            self._refresh_scheduler_status_panel()
            if cards_active and not force:
                self._update_station_command_hold_button_labels(snapshot)
                self._update_station_command_radio_tile_hold_controls(snapshot)
            else:
                self._refresh_station_command_bar(force=False)
        elif was_active:
            self._broadcast_hold_state(snapshot)
            self._update_station_command_hold_button_labels(snapshot)
            self._update_station_command_radio_tile_hold_controls(snapshot)

    def on_hold_state_changed(self, force_reload: bool = False) -> None:
        snapshot = suspend_snapshot(self.settings, allow_reload=bool(force_reload))
        if snapshot.get("until") and not snapshot.get("active"):
            resume_schedule_hold(self, self.settings)
            return
        self._dispatch_hold_snapshot(snapshot, force=bool(force_reload))

    def _on_hold_state_tick(self) -> None:
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty("hold_state_tick")
            return
        timed_cards = self._refresh_radio_card_hold_state() if getattr(self, "radio_control_panel", None) is not None else False
        snapshot = suspend_snapshot(self.settings, allow_reload=False)
        if snapshot.get("until") and not snapshot.get("active"):
            resume_schedule_hold(self, self.settings)
        else:
            self._dispatch_hold_snapshot(snapshot)
        # Legacy dispatch may stop this shared timer. Either owner can need it.
        timer = getattr(self, "_hold_state_timer", None)
        if timer is not None and (timed_cards or snapshot.get("active")) and not timer.isActive():
            timer.start()

    def _refresh_radio_card_hold_state(self) -> bool:
        """Existing timer projects worker-published manual state, never legacy DB."""
        panel = getattr(self, "radio_control_panel", None)
        if panel is None:
            return False
        timed = False
        for ident, card in panel.cards.items():
            try:
                state = self._station_command_manual_control_state_for_radio(ident)
                held = str(getattr(state, "state", "")) in {"manual_hold", "manual_qsy", "manual_suspend"}
                hold = self._station_command_hold_snapshot_for_radio(ident)
                seconds = hold.get("remaining_sec") if hold.get("active") else None
                minutes = max(0, int(math.ceil(float(seconds) / 60))) if seconds is not None else None
            except (AttributeError, TypeError, ValueError, OverflowError):
                timed = timed or card.property("radioCardHoldRemainingMinutes") is not None
                continue
            timed = timed or minutes is not None
            card.update_hold_state(held, minutes)
        return timed

    def on_hold_duration_default_changed(self) -> None:
        snapshot = self._hold_state_snapshot if isinstance(self._hold_state_snapshot, dict) else None
        if not isinstance(snapshot, dict):
            snapshot = suspend_snapshot(self.settings)
            if snapshot.get("until") and not snapshot.get("active"):
                resume_schedule_hold(self, self.settings)
                return
        if snapshot.get("active"):
            set_active_hold_duration(
                self,
                self.settings,
                notify=False,
                profile=getattr(self, "_active_runtime_profile", None),
            )
            snapshot = suspend_snapshot(self.settings, allow_reload=False)
        self._dispatch_hold_snapshot(snapshot, force=True, sync_combos=True)

    def _set_scheduler_reasons(self, lines: list[str]) -> None:
        if not hasattr(self, "scheduler_status_reasons_layout"):
            return
        sig = tuple(str(line) for line in (lines or []))
        if sig == self._scheduler_status_reason_lines_signature:
            return
        self._scheduler_status_reason_lines_signature = sig
        layout = self.scheduler_status_reasons_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for line in sig:
            lbl = QLabel(line)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setWordWrap(True)
            layout.addWidget(lbl)

    @staticmethod
    def _rf_guard_status_reason(status: Mapping[str, object]) -> str:
        if not bool(status.get("rf_conflict_warning")):
            return ""
        peer_name = str(status.get("rf_conflict_peer_name") or "").strip()
        if bool(status.get("rf_conflict_peer_status_unknown")) or bool(status.get("rf_conflict_peer_status_stale")):
            return f"RF Guard: verify {peer_name}" if peer_name else "RF Guard: verify peer radio"
        if peer_name:
            return f"RF Guard: {peer_name}"
        return str(status.get("rf_conflict_summary") or "").strip() or "RF Guard: review"

    def _refresh_scheduler_status_panel(self, *_args) -> None:
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty("scheduler_status")
            return
        if not hasattr(self, "scheduler") or not hasattr(self, "scheduler_status_container"):
            return
        if not self.scheduler_status_container.isVisible():
            return
        try:
            status = self.scheduler.get_status_summary()
        except Exception:
            return
        if getattr(self, "suspend_duration_combo", None) is not None:
            try:
                if (
                    not self.suspend_duration_combo.view().isVisible()
                    and not self.suspend_duration_combo.hasFocus()
                ):
                    refresh_hold_duration_combo(
                        self.suspend_duration_combo,
                        self.settings,
                        getattr(self, "_active_runtime_profile", None),
                    )
            except Exception:
                pass
        control_mode = status.get("control_mode")
        use_scheduler = bool(status.get("use_scheduler", True))
        freq_label = status.get("freq_label") or ""
        hold_snapshot = self._hold_state_snapshot if isinstance(self._hold_state_snapshot, dict) else None
        if not isinstance(hold_snapshot, dict):
            hold_snapshot = suspend_snapshot(self.settings)
        if hold_snapshot.get("until") and not hold_snapshot.get("active"):
            try:
                resume_schedule_hold(self, self.settings)
            except Exception:
                pass
            hold_snapshot = suspend_snapshot(self.settings)
        suspended_until = hold_snapshot.get("until") if hold_snapshot.get("active") else None
        off_schedule = bool(status.get("off_schedule"))
        varac_waiting = bool(status.get("varac_waiting"))
        ptt_active = bool(status.get("ptt_active"))
        shared_ptt_blocked = bool(status.get("shared_ptt_blocked"))
        shared_ptt_reason = str(status.get("shared_ptt_reason") or "").strip()
        shared_ptt_group = str(status.get("shared_ptt_group") or "").strip()
        shared_ptt_owner_name = str(status.get("shared_ptt_owner_name") or "").strip()
        rf_conflict_warning = bool(status.get("rf_conflict_warning"))
        rf_conflict_summary = self._rf_guard_status_reason(status)
        js8_status_stale = bool(status.get("js8_status_stale"))
        varac_status_stale = bool(status.get("varac_status_stale"))
        js8_busy = bool(status.get("js8_busy"))
        fldigi_busy = bool(status.get("fldigi_busy"))
        fldigi_busy_reason = (status.get("fldigi_busy_reason") or "").strip().lower()
        varac_busy = bool(status.get("varac_busy"))
        net_kind = status.get("net_kind")
        flags = status.get("off_schedule_flags") or {}
        fldigi_mode_off = bool(status.get("fldigi_mode_off"))
        fldigi_offset_off = bool(status.get("fldigi_offset_off"))
        sop_contention = bool(status.get("sop_contention"))
        sop_profiles = [str(x).strip() for x in (status.get("sop_contention_profiles") or []) if str(x).strip()]
        sop_selected_profile = str(status.get("sop_selected_profile") or "").strip()
        active_source = str(status.get("source") or "").strip().upper()
        next_change_minutes = None
        sop_next_minutes = self._get_next_sop_action_minutes()
        next_change = getattr(self.scheduler, "next_change_utc", None)
        if next_change is not None:
            try:
                if getattr(next_change, "tzinfo", None) is None:
                    next_change = next_change.replace(tzinfo=datetime.timezone.utc)
                else:
                    next_change = next_change.astimezone(datetime.timezone.utc)
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                delta = (next_change - now_utc).total_seconds()
                if delta > 0:
                    next_change_minutes = int((delta + 59) // 60)
            except Exception:
                next_change_minutes = None

        if (control_mode in {"MANUAL", "NONE"}) or not use_scheduler:
            self.scheduler_status_header.setText("Frequency")
            self._set_scheduler_reasons([freq_label or "--"])
            self.resume_schedule_btn.setVisible(False)
            self.suspend_schedule_btn.setVisible(False)
            self.suspend_duration_label.setVisible(False)
            self.suspend_duration_combo.setVisible(False)
            try:
                self.scheduler_status_container.adjustSize()
            except Exception:
                pass
            self._hide_sidebar_schedule_controls()
            self._auto_collapse_inactive_nav_groups()
            return

        if suspended_until:
            self._apply_active_hold_status_panel(hold_snapshot)
            return

        reasons = []
        busy_sources = []
        if js8_busy:
            busy_sources.append("JS8")
        if varac_busy:
            busy_sources.append("VarAC")
        if fldigi_busy:
            if fldigi_busy_reason == "gibberish":
                busy_sources.append("FLDigi (gibberish)")
            else:
                busy_sources.append("FLDigi")
        busy_line = f"BUSY RX: {'; '.join(busy_sources)}" if busy_sources else ""

        if off_schedule:
            if flags.get("frequency"):
                reasons.append("Frequency")
            if flags.get("offset"):
                reasons.append("JS8 Offset")
            if flags.get("mode") or flags.get("fldigi_offset"):
                if fldigi_offset_off:
                    reasons.append("FLDigi Offset")
                if fldigi_mode_off:
                    reasons.append("FLDigi Mode")
                if not fldigi_mode_off and not fldigi_offset_off:
                    reasons.append("FLDigi Mode/Offset")
            if varac_waiting:
                reasons.append("Waiting to Clear")
            if ptt_active:
                reasons.append("Sending Traffic")
            if shared_ptt_blocked:
                if shared_ptt_owner_name and shared_ptt_group:
                    reasons.append(f"Shared PTT {shared_ptt_group}: {shared_ptt_owner_name}")
                elif shared_ptt_reason:
                    reasons.append(shared_ptt_reason)
                else:
                    reasons.append("Shared PTT")
            if rf_conflict_warning and rf_conflict_summary:
                reasons.append(rf_conflict_summary)
            if js8_status_stale:
                reasons.append("Verify JS8Call")
            if varac_status_stale:
                reasons.append("Verify VarAC")
            if js8_busy or varac_busy:
                reasons.append("QSO")
            if busy_line and not net_kind:
                reasons.append(busy_line)
            if sop_contention and active_source == "SOP":
                contenders = [p for p in sop_profiles if p and p != sop_selected_profile]
                if contenders:
                    reasons.append(f"SOP Contention: {sop_selected_profile or 'Winner'} over {', '.join(contenders[:3])}")
                else:
                    reasons.append("SOP Contention")
            if next_change_minutes is not None and next_change_minutes <= 15:
                reasons.append(f"Freq Change: {next_change_minutes} min")
        else:
            if varac_waiting:
                reasons.append("Waiting to Clear")
            if ptt_active:
                reasons.append("Sending Traffic")
            if shared_ptt_blocked:
                if shared_ptt_owner_name and shared_ptt_group:
                    reasons.append(f"Shared PTT {shared_ptt_group}: {shared_ptt_owner_name}")
                elif shared_ptt_reason:
                    reasons.append(shared_ptt_reason)
                else:
                    reasons.append("Shared PTT")
            if rf_conflict_warning and rf_conflict_summary:
                reasons.append(rf_conflict_summary)
            if js8_status_stale:
                reasons.append("Verify JS8Call")
            if varac_status_stale:
                reasons.append("Verify VarAC")
            if js8_busy or varac_busy:
                reasons.append("QSO")
            if net_kind:
                reasons.append(net_kind)
            if busy_line and not net_kind:
                reasons.append(busy_line)
            if sop_contention and active_source == "SOP":
                contenders = [p for p in sop_profiles if p and p != sop_selected_profile]
                if contenders:
                    reasons.append(f"SOP Contention: {sop_selected_profile or 'Winner'} over {', '.join(contenders[:3])}")
                else:
                    reasons.append("SOP Contention")
            if next_change_minutes is not None and next_change_minutes <= 15:
                reasons.append(f"Freq Change: {next_change_minutes} min")

        if off_schedule:
            self.scheduler_status_header.setText("Off Schedule")
            self.scheduler_status_header.setStyleSheet(
                label_style("danger", resolve_theme(self.settings), weight=700)
            )
            self._set_scheduler_reasons(reasons or [""])
            self.resume_schedule_btn.setVisible(True)
            self.suspend_duration_label.setVisible(True)
            self.suspend_duration_combo.setVisible(True)
            try:
                theme = resolve_theme(self.settings)
                self.resume_schedule_btn.setStyleSheet(button_style("info", theme))
            except Exception:
                pass
            self.suspend_schedule_btn.setVisible(True)
            self.suspend_schedule_btn.setText("Suspend")
            self.suspend_schedule_btn.setToolTip(
                f"Pause schedule control for {self._selected_sidebar_hold_minutes()} minutes."
            )
            try:
                theme = resolve_theme(self.settings)
                self.suspend_schedule_btn.setStyleSheet(button_style("warning", theme))
            except Exception:
                pass
            try:
                theme = resolve_theme(self.settings)
                highlight = theme.get("surface_alt", theme.get("surface", "#FFFFFF"))
                border = theme.get("warning", theme.get("border", "#CCCCCC"))
                self.scheduler_status_container.setStyleSheet(
                    "QGroupBox { background-color: %s; border: 1px solid %s; border-radius: 6px; }"
                    "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 4px; }"
                    % (highlight, border)
                )
            except Exception:
                pass
        else:
            if sop_next_minutes is not None and 0 <= sop_next_minutes <= 180:
                hours = sop_next_minutes // 60
                minutes = sop_next_minutes % 60
                self.scheduler_status_header.setText(f"SOP Action in: {hours}:{minutes:02d}")
            else:
                self.scheduler_status_header.setText("On Schedule")
            self.scheduler_status_header.setStyleSheet("")
            reason_lines: list[str] = []
            if active_source == "SOP" and net_kind:
                reason_lines.append(str(net_kind))
            if sop_contention and active_source == "SOP":
                contenders = [p for p in sop_profiles if p and p != sop_selected_profile]
                if contenders:
                    reason_lines.append(
                        f"SOP Contention: {sop_selected_profile or 'Winner'} over {', '.join(contenders[:3])}"
                    )
                else:
                    reason_lines.append("SOP Contention")
            self._set_scheduler_reasons(reason_lines)
            self.resume_schedule_btn.setVisible(False)
            self.suspend_duration_label.setVisible(True)
            self.suspend_duration_combo.setVisible(True)
            self.suspend_schedule_btn.setVisible(True)
            self.suspend_schedule_btn.setText("Suspend")
            self.suspend_schedule_btn.setToolTip(
                f"Pause schedule control for {self._selected_sidebar_hold_minutes()} minutes."
            )
            try:
                theme = resolve_theme(self.settings)
                self.suspend_schedule_btn.setStyleSheet(button_style("warning", theme))
            except Exception:
                pass
            try:
                self.scheduler_status_container.setStyleSheet(
                    "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 4px; }"
                )
            except Exception:
                pass
        self._update_scheduler_action_button_widths()
        try:
            self.scheduler_status_container.adjustSize()
        except Exception:
            pass
        self._hide_sidebar_schedule_controls()
        self._auto_collapse_inactive_nav_groups()

    def _refresh_sop_awareness(self) -> None:
        cache = getattr(self, "_sop_awareness", None)
        manager = getattr(getattr(self, "sop_tab", None), "manager", None)
        if cache is not None and manager is not None and not getattr(self, "_shutting_down", False):
            cache.refresh(lambda: detached_sop_reader(manager))
            dashboard = getattr(self, "controlfreq_tab", None)
            if dashboard is not None:
                dashboard.set_sop_attention_snapshot(cache.attention_snapshot())

    def _station_command_sop_rows(self, selected=None):
        cache = getattr(self, "_sop_awareness", None)
        rows = cache.rows() if cache is not None else ()
        if selected is None:
            choices = getattr(self, "_station_command_last_choices", [])
            selected = next((item for item in choices if self._station_command_snapshot_id(item) ==
                             int(getattr(self, "_station_command_selected_profile_id", 0) or 0)), None)
        if selected is None:
            return []
        radio_id = self._station_command_snapshot_id(selected)
        profile = self._station_command_health_profile(selected, radio_id) or {}
        refs = self._station_command_assigned_plan_refs_for_radio(radio_id)
        groups = {str(row.get("group_name") or row.get("group") or "").strip().upper()
                  for row in refs if isinstance(row, Mapping)}
        group, _band = self._station_command_assigned_plan_group_band(selected)
        if group:
            groups.add(group.upper())
        softwares = {label for flag, label in (("use_js8call", "JS8CALL"), ("use_fldigi", "FLDIGI"),
                     ("use_varac", "VARAC")) if profile.get(flag)}
        return relevant_sop_actions(rows, radio_id=radio_id,
            operating_profile_id=int(profile.get("assigned_operating_profile_id") or 0),
            groups=groups, softwares=softwares)

    def _get_next_sop_action_minutes(self, selected=None):
        """Read published reminders only; never query SOP data from the shell."""
        try:
            self._sop_next_due_minutes = None
            self._sop_next_action_label = ""
            self._sop_next_action_count = 0
            rows = self._station_command_sop_rows(selected)
            if not rows:
                return None
            self._sop_next_action_label = str(rows[0].get("action_label") or "SOP Action").strip()
            self._sop_next_action_count = len(rows)
            next_due = rows[0].get("next_due_utc")
            if next_due is None:
                return None
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            delta = (next_due - now_utc).total_seconds()
            mins = max(0, int((delta + 59) // 60))
            self._sop_next_due_minutes = mins
            return mins
        except Exception:
            return None

    def _invalidate_sop_status_cache(self) -> None:
        cache = getattr(self, "_sop_awareness", None)
        if cache is not None:
            cache.invalidate()
        self._sop_next_due_cache_ts = 0.0
        self._sop_next_due_minutes = None
        self._sop_next_action_label = ""
        self._sop_next_action_count = 0

    def _on_sop_data_changed(self) -> None:
        self._sop_data_refresh_pending = True
        timer = getattr(self, "_sop_data_refresh_timer", None)
        if isinstance(timer, QTimer):
            timer.start()
            return
        self._flush_sop_data_changed()

    def _flush_sop_data_changed(self) -> None:
        if not bool(getattr(self, "_sop_data_refresh_pending", False)):
            return
        self._sop_data_refresh_pending = False
        self._invalidate_sop_status_cache()
        with perf_span("main.sop_data_changed.flush", settings=self.settings, min_ms=8.0):
            try:
                if hasattr(self, "scheduler"):
                    self.scheduler.force_refresh()
            except Exception:
                pass
            try:
                if hasattr(self, "hf_schedule_tab") and hasattr(self.hf_schedule_tab, "on_sop_data_changed"):
                    self.hf_schedule_tab.on_sop_data_changed()
            except Exception:
                pass
            try:
                if hasattr(self, "controlfreq_tab") and hasattr(self.controlfreq_tab, "on_sop_data_changed"):
                    self.controlfreq_tab.on_sop_data_changed()
            except Exception:
                pass
            try:
                if hasattr(self, "net_tab") and hasattr(self.net_tab, "on_sop_data_changed"):
                    self.net_tab.on_sop_data_changed()
            except Exception:
                pass
            try:
                self._refresh_freq_planner_if_loaded()
            except Exception:
                pass
            try:
                self._refresh_scheduler_status_panel()
            except Exception:
                pass

    def _mesh_runtime_configs(self):
        try:
            return load_mesh_connection_configs(self.settings)
        except Exception as e:
            log.debug("MainWindow: local mesh settings load failed: %s", e)
            return tuple()

    @staticmethod
    def _mesh_runtime_signature_from_configs(configs) -> tuple[tuple[object, ...], ...]:
        signature = []
        for config in configs:
            try:
                signature.append(
                    (
                        mesh_connection_config_key(config),
                        bool(config.enabled),
                        str(config.protocol),
                        str(config.connection_type.value),
                        str(config.endpoint_address),
                        str(config.ble_device_name),
                        int(config.tcp_port),
                        int(config.serial_baud),
                        int(config.ble_scan_timeout_sec),
                        str(config.http_base_url),
                        str(config.mqtt_broker),
                        str(config.mqtt_topic_root),
                        bool(config.send_enabled),
                        bool(config.store_messages_enabled),
                        bool(config.map_positions_enabled),
                        bool(config.bridge_to_reticulum_enabled),
                    )
                )
            except Exception:
                continue
        return tuple(sorted(signature, key=lambda item: str(item[0])))

    def _start_mesh_runtime_if_enabled(self) -> None:
        configs = tuple(self._mesh_runtime_configs())
        publish_snapshot = getattr(self, "_publish_station_command_mesh_snapshot", None)
        if callable(publish_snapshot):
            publish_snapshot(configs=configs)
        signature = self._mesh_runtime_signature_from_configs(configs)
        self._mesh_runtime_signature = signature
        if not configs:
            return
        existing_thread = self._mesh_worker_thread
        if existing_thread is not None:
            if existing_thread.isRunning():
                return
            self._mesh_worker_thread = None
            self._mesh_worker = None
            self._mesh_runtime_stopping = False
        try:
            thread = QThread(self)
            worker = MeshConnectionWorker(configs, db_path=default_mesh_db_path())
            manual_retry_ids = set(
                getattr(self, "_mesh_manual_retry_adapter_ids", set()) or set()
            )
            for adapter_id in manual_retry_ids:
                worker.reset_retry_budget(str(adapter_id))
            worker.moveToThread(thread)
            thread.started.connect(worker.start)
            worker.error_ready.connect(self._on_mesh_runtime_error)
            worker.health_ready.connect(self._on_mesh_runtime_health)
            settings_health_handler = getattr(self.settings_tab, "on_mesh_health_ready", None)
            if callable(settings_health_handler):
                worker.health_ready.connect(settings_health_handler)
            worker.event_ready.connect(self._on_mesh_runtime_event)
            worker.channels_ready.connect(self.settings_tab.on_mesh_channels_ready)
            worker.channel_capabilities_ready.connect(self.settings_tab.on_mesh_channel_capabilities_ready)
            worker.operation_ready.connect(self.settings_tab.on_mesh_operation_ready)
            self._mesh_retry_requested.connect(worker.retry_now, Qt.QueuedConnection)
            try:
                self._mesh_send_requested.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._mesh_send_requested.connect(worker.send_message, Qt.QueuedConnection)
            worker.send_ready.connect(self._on_mesh_send_result)
            self.settings_tab.mesh_channel_refresh_requested.connect(worker.refresh_channels, Qt.QueuedConnection)
            self.settings_tab.mesh_channel_configure_requested.connect(worker.configure_channel, Qt.QueuedConnection)
            self.settings_tab.mesh_channel_remove_device_requested.connect(
                worker.remove_channel_from_device,
                Qt.QueuedConnection,
            )
            worker.stopped.connect(thread.quit)
            worker.stopped.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(
                lambda runtime_thread=thread, runtime_worker=worker: self._on_mesh_runtime_thread_finished(
                    runtime_thread,
                    runtime_worker,
                )
            )
            self._mesh_worker_thread = thread
            self._mesh_worker = worker
            self._mesh_runtime_stopping = False
            thread.start()
            self._mesh_manual_retry_adapter_ids = set()
            log.info("MainWindow: local mesh runtime starting for %s configured source(s).", len(configs))
        except Exception as e:
            self._mesh_worker_thread = None
            self._mesh_worker = None
            log.warning("MainWindow: local mesh runtime failed to start: %s", e)

    def _restart_mesh_runtime_if_needed(self) -> None:
        configs = tuple(self._mesh_runtime_configs())
        publish_snapshot = getattr(self, "_publish_station_command_mesh_snapshot", None)
        if callable(publish_snapshot):
            publish_snapshot(configs=configs)
        signature = self._mesh_runtime_signature_from_configs(configs)
        if signature == getattr(self, "_mesh_runtime_signature", tuple()):
            return
        self._mesh_runtime_signature = signature
        thread = getattr(self, "_mesh_worker_thread", None)
        if thread is not None and thread.isRunning():
            self._mesh_runtime_restart_pending = bool(configs)
            self._stop_mesh_runtime()
            return
        if thread is not None:
            self._mesh_worker_thread = None
            self._mesh_worker = None
            self._mesh_runtime_stopping = False
        self._mesh_runtime_restart_pending = False
        if configs:
            QTimer.singleShot(0, self._start_mesh_runtime_if_enabled)

    def _cancel_mesh_runtime_operation(self, adapter_id: str, request_class: str) -> None:
        worker = getattr(self, "_mesh_worker", None)
        if worker is not None:
            worker.request_cancel(adapter_id, request_class)

    def _restart_mesh_runtime_now(self) -> None:
        configs = tuple(self._mesh_runtime_configs())
        publish_snapshot = getattr(self, "_publish_station_command_mesh_snapshot", None)
        if callable(publish_snapshot):
            publish_snapshot(configs=configs)
        self._mesh_runtime_signature = self._mesh_runtime_signature_from_configs(configs)
        thread = getattr(self, "_mesh_worker_thread", None)
        if thread is not None and thread.isRunning():
            self._mesh_runtime_restart_pending = bool(configs)
            self._stop_mesh_runtime()
            return
        if thread is not None:
            self._mesh_worker_thread = None
            self._mesh_worker = None
            self._mesh_runtime_stopping = False
        if configs:
            QTimer.singleShot(0, self._start_mesh_runtime_if_enabled)

    def _manual_reconnect_mesh_runtime_now(self) -> None:
        """Retry configured adapters without replacing a healthy live worker."""

        worker = getattr(self, "_mesh_worker", None)
        thread = getattr(self, "_mesh_worker_thread", None)
        configs = tuple(self._mesh_runtime_configs())
        if worker is not None and thread is not None and thread.isRunning():
            for config in configs:
                if bool(getattr(config, "enabled", False)):
                    self._mesh_retry_requested.emit(str(config.adapter_id))
            return
        self._mesh_manual_retry_adapter_ids = {
            str(config.adapter_id)
            for config in configs
            if bool(getattr(config, "enabled", False))
        }
        self._restart_mesh_runtime_now()

    def _disconnect_mesh_runtime(self) -> None:
        """Stop the live mesh worker without scheduling an automatic restart."""

        self._mesh_runtime_restart_pending = False
        self._stop_mesh_runtime()

    def _stop_mesh_runtime(self) -> None:
        worker = getattr(self, "_mesh_worker", None)
        thread = getattr(self, "_mesh_worker_thread", None)
        if worker is None and thread is None:
            self._mesh_runtime_stopping = False
            return
        self._mesh_runtime_stopping = True
        stop_requested = False
        if worker is not None:
            try:
                # Set the cancellation flag synchronously. A queued stop cannot
                # run while a BLE connect/channel request is occupying the
                # worker thread's event loop.
                worker.request_stop()
                if thread is not None and thread.isRunning():
                    QMetaObject.invokeMethod(worker, "stop", Qt.QueuedConnection)
                    stop_requested = True
                else:
                    worker.stop()
            except Exception as e:
                log.debug("MainWindow shutdown: local mesh runtime stop failed: %s", e)
        if thread is not None:
            try:
                if not stop_requested:
                    thread.quit()
                if not thread.wait(200):
                    log.debug("MainWindow shutdown: local mesh runtime thread did not stop within 200 ms.")
                    if worker is not None:
                        self._guard_mesh_runtime_shutdown(thread, worker)
            except Exception as e:
                log.debug("MainWindow shutdown: local mesh runtime thread stop failed: %s", e)

    def _guard_mesh_runtime_shutdown(self, thread: QThread, worker: MeshConnectionWorker) -> None:
        try:
            thread.setParent(None)
        except Exception:
            pass
        entry = (thread, worker)
        if entry not in _MESH_RUNTIME_SHUTDOWN_GUARD:
            _MESH_RUNTIME_SHUTDOWN_GUARD.append(entry)

        def _release_guard() -> None:
            try:
                _MESH_RUNTIME_SHUTDOWN_GUARD.remove(entry)
            except ValueError:
                pass

        try:
            thread.finished.connect(_release_guard)
        except Exception:
            pass

    def _on_mesh_runtime_thread_finished(
        self,
        runtime_thread: QThread | None = None,
        runtime_worker: MeshConnectionWorker | None = None,
    ) -> None:
        log.debug("MainWindow: local mesh runtime thread finished.")
        if runtime_thread is not None and self._mesh_worker_thread is not runtime_thread:
            return
        if runtime_worker is None or self._mesh_worker is runtime_worker:
            self._mesh_worker = None
        self._mesh_worker_thread = None
        self._mesh_runtime_stopping = False
        if self._mesh_runtime_restart_pending and not self._shutdown_close_pending:
            self._mesh_runtime_restart_pending = False
            QTimer.singleShot(0, self._start_mesh_runtime_if_enabled)

    def _on_mesh_runtime_error(self, message: str) -> None:
        text = str(message or "").strip()
        if text:
            stamp = time.monotonic()
            previous = getattr(self, "_mesh_runtime_error_signature", None)
            if previous is not None and previous[0] == text and stamp - previous[1] < 30.0:
                return
            self._mesh_runtime_error_signature = (text, stamp)
            log.warning("Local Mesh runtime: %s", text)

    @Slot(object)
    def _request_mesh_send(self, request: object) -> None:
        """Route one operator-confirmed send to the worker that owns the adapter session."""

        if not isinstance(request, MeshSendRequest):
            return
        worker = getattr(self, "_mesh_worker", None)
        thread = getattr(self, "_mesh_worker_thread", None)
        unavailable = bool(
            worker is None
            or thread is None
            or not thread.isRunning()
            or getattr(self, "_mesh_runtime_stopping", False)
        )
        if unavailable:
            self._on_mesh_send_result(
                MeshSendResult(
                    request_id=request.request_id,
                    adapter_id=request.adapter_id,
                    transport="",
                    destination_kind=request.destination_kind,
                    destination_id=request.destination_id,
                    channel_id=request.channel_id,
                    state="failed",
                    requested_at=request.requested_at,
                    completed_at=datetime.datetime.now(datetime.timezone.utc),
                    detail="Local Mesh runtime is not available. Connect the selected device and try again.",
                    retryable=True,
                    evidence="runtime_unavailable",
                )
            )
            return
        self._mesh_send_requested.emit(request)

    @Slot(object)
    def _on_mesh_send_result(self, result: object) -> None:
        viewer = getattr(self, "message_viewer_tab", None)
        if viewer is not None and hasattr(viewer, "on_mesh_send_result"):
            viewer.on_mesh_send_result(result)

    def _on_mesh_runtime_health(self, health) -> None:
        self._publish_station_command_mesh_snapshot(health=health)
        viewer = getattr(self, "message_viewer_tab", None)
        if viewer is not None and hasattr(viewer, "on_mesh_health_ready"):
            viewer.on_mesh_health_ready(health)
        try:
            if hasattr(self, "station_health_tab") and hasattr(self.station_health_tab, "refresh"):
                self.station_health_tab.refresh()
        except Exception:
            pass
        try:
            timer = getattr(self, "_mesh_health_command_refresh_timer", None)
            if isinstance(timer, QTimer):
                timer.start()
            else:
                QTimer.singleShot(350, lambda: self._refresh_station_command_bar(force=False))
        except Exception:
            pass

    def _on_mesh_runtime_event(self, event) -> None:
        tab = getattr(self, "stations_map_tab", None)
        if tab is not None and hasattr(tab, "on_mesh_data_changed"):
            tab.on_mesh_data_changed(event)
        try:
            if hasattr(self, "controlfreq_tab") and hasattr(self.controlfreq_tab, "refresh_view"):
                self.controlfreq_tab.refresh_view()
        except Exception:
            pass
        try:
            if self.message_viewer_tab is not None and hasattr(self.message_viewer_tab, "refresh_messages"):
                self.message_viewer_tab.refresh_messages()
        except Exception:
            pass

    def _register_qt_shutdown_threads(self) -> None:
        """Add currently owned Qt workers to the cooperative shutdown registry."""
        candidates = list(self.findChildren(QThread))
        candidates.extend(entry[0] for entry in tuple(_MESH_RUNTIME_SHUTDOWN_GUARD))
        seen: set[int] = set()
        for thread in candidates:
            marker = id(thread)
            if marker in seen:
                continue
            seen.add(marker)
            try:
                label = str(thread.objectName() or "").strip() or str(marker)
            except RuntimeError:
                continue

            def _thread_stopped(owned: QThread = thread) -> bool:
                try:
                    return not owned.isRunning()
                except RuntimeError:
                    return True

            self._shutdown_registry.register(
                f"qt_thread:{label}",
                request_stop=lambda owned=thread: owned.requestInterruption(),
                is_stopped=_thread_stopped,
            )

    def _on_app_about_to_quit(self):
        preferences = getattr(self, "_window_preferences", None)
        if preferences is not None and not self._shutting_down:
            save_full_window_geometry(self, preferences)
        if self._shutting_down:
            return
        self._shutting_down = True
        dock = getattr(self, "_radio_control_panel_dock_window", None)
        if dock is not None:
            dock.setEnabled(False)
            dock.hide()
        audit_cache = getattr(self, "_condition_audit_read_cache", None)
        if audit_cache is not None:
            audit_cache.close()
        awareness = getattr(self, "_sop_awareness", None)
        if awareness is not None:
            awareness.close()
        awareness_timer = getattr(self, "_sop_awareness_timer", None)
        if awareness_timer is not None:
            awareness_timer.stop()
        self._mesh_runtime_restart_pending = False
        self._close_transient_shutdown_ui()
        map_window = getattr(self, "map_window", None)
        if map_window is not None:
            try:
                map_window.shutdown()
            except Exception as e:
                log.debug("MainWindow shutdown: Map window stop failed: %s", e)
        self._register_qt_shutdown_threads()
        stop_errors = self._shutdown_registry.request_stop_all()
        if stop_errors:
            log.warning(
                "MainWindow shutdown: stop request failed for registered worker(s): %s",
                ", ".join(stop_errors),
            )
        try:
            if hasattr(self, "_ui_watchdog"):
                self._ui_watchdog.stop()
        except Exception:
            pass
        try:
            if hasattr(self, "_cpu_watchdog"):
                self._cpu_watchdog.stop()
        except Exception:
            pass
        self._stop_mesh_runtime()
        try:
            shutdown_dependency_status_service()
        except Exception as e:
            log.debug("MainWindow shutdown: dependency status service stop failed: %s", e)
        try:
            if hasattr(self, "_sop_data_refresh_timer"):
                self._sop_data_refresh_timer.stop()
        except Exception:
            pass
        for timer_name in (
            "_message_projection_followup_timer",
            "_message_projection_reconcile_timer",
        ):
            try:
                timer = getattr(self, timer_name, None)
                if timer is not None:
                    timer.stop()
            except Exception:
                pass
        try:
            if hasattr(self, "_hold_state_timer"):
                self._hold_state_timer.stop()
        except Exception:
            pass
        try:
            if hasattr(self, "scheduler"):
                self.scheduler.stop()
        except Exception as e:
            log.debug("MainWindow shutdown: scheduler stop failed: %s", e)
        try:
            if hasattr(self, "background_ingest"):
                self.background_ingest.stop()
        except Exception as e:
            log.debug("MainWindow shutdown: background ingest stop failed: %s", e)
        try:
            if hasattr(self, "launch_orchestrator"):
                self.launch_orchestrator.stop_sequence()
        except Exception as e:
            log.debug("MainWindow shutdown: launch orchestrator stop failed: %s", e)
        try:
            if self._launch_progress_dialog is not None:
                self._launch_progress_dialog.close()
                self._launch_progress_dialog = None
        except Exception:
            pass
        try:
            if self._log_dialog is not None:
                self._log_dialog.close()
                self._log_dialog = None
        except Exception:
            pass
        try:
            if self.log_tab is not None and hasattr(self.log_tab, "set_tab_active"):
                self.log_tab.set_tab_active(False)
        except Exception:
            pass
        try:
            if hasattr(self, "js8_control"):
                self.js8_control.stop()
        except Exception as e:
            log.debug("MainWindow shutdown: JS8 control stop failed: %s", e)
        for _label, widget in self._screens:
            try:
                if hasattr(widget, "shutdown"):
                    widget.shutdown()
            except Exception as e:
                log.debug("MainWindow shutdown: widget shutdown failed for %r: %s", widget, e)
                continue
        try:
            JS8RxHub.shutdown_all()
        except Exception as e:
            log.debug("MainWindow shutdown: JS8RxHub shutdown failed: %s", e)
        try:
            if hasattr(self, "station_runtime_manager"):
                self.station_runtime_manager.stop()
        except Exception as e:
            log.debug("MainWindow shutdown: station runtime manager stop failed: %s", e)
        try:
            set_scheduler_enabled_override(None)
        except Exception:
            pass

    def _close_transient_shutdown_ui(self) -> None:
        try:
            self._dismiss_off_schedule_prompt()
            self._dismiss_varac_wait_prompt()
        except Exception:
            pass
        app = QApplication.instance()
        if app is None:
            return
        try:
            popup = app.activePopupWidget()
            if popup is not None:
                popup.close()
        except Exception as e:
            log.debug("MainWindow shutdown: active popup close failed: %s", e)
        try:
            modal = app.activeModalWidget()
            if modal is not None and modal is not self:
                if isinstance(modal, QDialog):
                    modal.reject()
                else:
                    modal.close()
        except Exception as e:
            log.debug("MainWindow shutdown: active modal close failed: %s", e)
        try:
            for widget in list(app.topLevelWidgets()):
                if widget is self or not widget.isVisible():
                    continue
                if isinstance(widget, QDialog):
                    widget.reject()
                elif widget.windowModality() != Qt.NonModal or widget.parent() is not None:
                    widget.close()
        except Exception as e:
            log.debug("MainWindow shutdown: transient widget close failed: %s", e)

    def closeEvent(self, event):
        preferences = getattr(self, "_window_preferences", None)
        if preferences is not None and not getattr(self, "_shutdown_close_pending", False):
            save_full_window_geometry(self, preferences)
        if not self._allow_final_close:
            event.ignore()
            if not self._shutdown_close_pending:
                self._shutdown_close_pending = True
                self._shutdown_wait_started = time.monotonic()
                self._shutdown_wait_last_log = self._shutdown_wait_started
                # Keep the Qt event loop alive, but remove the application from
                # view while worker-owned timers are stopped on their owning
                # threads. Accepting the close immediately would let QObject
                # parents destroy a still-running QThread during process exit.
                self.hide()
                self._on_app_about_to_quit()
                QTimer.singleShot(0, self._poll_graceful_close)
            return
        unsubscribe = getattr(self, "_action_feedback_unsubscribe", None)
        if callable(unsubscribe):
            try:
                unsubscribe()
            except Exception:
                pass
            self._action_feedback_unsubscribe = None
        self._on_app_about_to_quit()
        super().closeEvent(event)

    def _poll_graceful_close(self) -> None:
        live_threads: list[QThread] = []
        candidates = list(self.findChildren(QThread))
        candidates.extend(entry[0] for entry in tuple(_MESH_RUNTIME_SHUTDOWN_GUARD))
        seen: set[int] = set()
        for thread in candidates:
            marker = id(thread)
            if marker in seen:
                continue
            seen.add(marker)
            try:
                if thread.isRunning():
                    live_threads.append(thread)
            except RuntimeError:
                continue
        registered_pending = self._shutdown_registry.pending()
        if live_threads or registered_pending:
            now = time.monotonic()
            elapsed = now - self._shutdown_wait_started
            if elapsed >= 3.0 and not self._shutdown_deadline_reported:
                self._shutdown_deadline_reported = True
                emit_span(
                    "shutdown.deadline_exceeded",
                    elapsed * 1000.0,
                    settings=self.settings,
                    meta={
                        "qt_threads": len(live_threads),
                        "workers": list(registered_pending),
                    },
                    level="error",
                )
                log.error(
                    "MainWindow shutdown exceeded 3.0s; pending Qt threads=%s, workers=%s",
                    len(live_threads),
                    ", ".join(registered_pending) or "none",
                )
                self._shutdown_registry.request_stop_all()
                for thread in live_threads:
                    try:
                        thread.requestInterruption()
                        thread.quit()
                    except RuntimeError:
                        continue
            if now - self._shutdown_wait_last_log >= 1.0:
                log.info(
                    "MainWindow shutdown: waiting %.1fs for %s Qt thread(s), registered=%s.",
                    elapsed,
                    len(live_threads),
                    ", ".join(registered_pending) or "none",
                )
                self._shutdown_wait_last_log = now
            QTimer.singleShot(50, self._poll_graceful_close)
            return
        self._allow_final_close = True
        elapsed_ms = max(0.0, (time.monotonic() - self._shutdown_wait_started) * 1000.0)
        emit_span(
            "shutdown.complete",
            elapsed_ms,
            settings=self.settings,
            meta={"deadline_ms": 3000},
            level="warning" if elapsed_ms > 3000.0 else "info",
        )
        log.info("MainWindow shutdown: all Qt worker threads stopped cleanly.")
        self.close()
        # The first close event hides the last visible window while Qt workers
        # drain. Closing an already-hidden window does not reliably emit
        # lastWindowClosed on every platform, so explicitly end the event loop
        # after the final close event has been accepted.
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def resizeEvent(self, event):
        try:
            self._sync_status_box_width()
        except Exception:
            pass
        super().resizeEvent(event)
        try:
            width = int(getattr(self, "station_command_bar", self).width() or 0)
            previous_width = int(getattr(self, "_station_command_last_layout_width", 0) or 0)
            if abs(width - previous_width) >= 24:
                self._station_command_last_layout_width = width
                self._station_command_radio_summary_signature = None
                if bool(getattr(self, "_adaptive_station_shell_enabled", False)):
                    QTimer.singleShot(0, self._reflow_adaptive_station_shell)
                self._schedule_station_command_bar_refresh("resize", force=False)
            self._schedule_station_command_layout(force=False)
            self._schedule_station_command_summary_geometry()
        except Exception:
            pass
        try:
            self._auto_collapse_inactive_nav_groups()
        except Exception:
            pass
        try:
            window_width = int(self.width() or 0)
            if window_width < 1080 and not bool(getattr(self, "_main_nav_collapsed", False)):
                self._main_nav_auto_collapsed = True
                self._set_main_navigation_collapsed(True)
            elif (
                window_width >= 1180
                and bool(getattr(self, "_main_nav_auto_collapsed", False))
                and bool(getattr(self, "_main_nav_collapsed", False))
            ):
                self._main_nav_auto_collapsed = False
                self._set_main_navigation_collapsed(False)
        except Exception:
            pass

    def _sync_status_box_width(self) -> None:
        if not hasattr(self, "scheduler_status_container"):
            return
        width = 0
        if hasattr(self, "status_dock_widget"):
            try:
                width = int(self.status_dock_widget.width())
            except Exception:
                width = 0
        if hasattr(self, "nav_buttons") and self.nav_buttons:
            try:
                nav_width = max(btn.width() for btn in self.nav_buttons)
                if width <= 10:
                    width = nav_width
                if width <= 10:
                    width = max(btn.sizeHint().width() for btn in self.nav_buttons)
            except Exception:
                if width <= 10:
                    width = 0
        if width <= 10 and hasattr(self, "nav_widget"):
            try:
                margins = self.nav_widget.layout().contentsMargins()
                width = int(self.nav_widget.width() - margins.left() - margins.right())
            except Exception:
                width = int(self.nav_widget.width())
        if width <= 10 and hasattr(self, "nav_scroll"):
            try:
                width = int(self.nav_scroll.viewport().width())
            except Exception:
                pass
        if width > 0:
            for container in (
                getattr(self, "scheduler_status_container", None),
                getattr(self, "condition_level_container", None),
            ):
                if container is None:
                    continue
                try:
                    container.setFixedWidth(width)
                except Exception:
                    pass

    def _update_ledge_clock(self) -> None:
        if not hasattr(self, "ledge_local_time_label") or not hasattr(self, "ledge_utc_time_label"):
            return
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        utc_day = now_utc.strftime("%a")
        utc_text = now_utc.strftime(f"UTC {utc_day} %H:%M:%S Z")
        try:
            tz_name = str(self.settings.get("timezone", "UTC") or "UTC")
            tz = get_timezone(tz_name)
            now_local = now_utc.astimezone(tz)
            local_day = now_local.strftime("%a")
            abbr = now_local.tzname() or tz_name
            local_text = now_local.strftime(f"Local {local_day} %H:%M:%S {abbr}")
        except Exception:
            local_text = "Local --"
        self.ledge_local_time_label.setText(local_text)
        self.ledge_utc_time_label.setText(utc_text)
        self.ledge_local_time_label.setToolTip(local_text)
        self.ledge_utc_time_label.setToolTip(utc_text)
        for label, value in (
            (getattr(self, "station_command_local_time_label", None), local_text),
            (getattr(self, "station_command_utc_time_label", None), utc_text),
        ):
            if isinstance(label, QLabel):
                try:
                    display_value = now_utc.strftime("%H:%MZ") if bool(label.property("compactClock")) else value
                    if label.property("clockSeconds") is False:
                        display_value = re.sub(r"(\d{2}:\d{2}):\d{2}", r"\1", display_value)
                    set_widget_value(label, "text", display_value)
                    set_widget_value(label, "toolTip", display_value)
                except RuntimeError:
                    # Responsive shell rebuilds replace these child labels.
                    pass

    def _update_scheduler_action_button_widths(self) -> None:
        buttons = [
            getattr(self, "resume_schedule_btn", None),
            getattr(self, "suspend_schedule_btn", None),
            getattr(self, "logs_active_btn", None),
        ]
        valid_buttons = [btn for btn in buttons if btn is not None]
        if not valid_buttons:
            return
        width = 0
        for btn in valid_buttons:
            try:
                hint_w = int(btn.sizeHint().width())
            except Exception:
                hint_w = 0
            try:
                text_w = int(btn.fontMetrics().horizontalAdvance(btn.text()) + 28)
            except Exception:
                text_w = 0
            width = max(width, hint_w, text_w)
        width = max(140, min(width, 220))
        for btn in valid_buttons:
            try:
                btn.setFixedWidth(width)
            except Exception:
                pass

    def _update_nav_layout_metrics(self) -> None:
        if not hasattr(self, "nav_widget") or not getattr(self, "nav_buttons", None):
            return
        if bool(getattr(self, "_main_nav_collapsed", False)):
            self.nav_widget.setFixedWidth(self._compact_navigation_width())
            return
        content_width = 0
        for btn in self.nav_buttons:
            try:
                hint_w = int(btn.sizeHint().width())
            except Exception:
                hint_w = 0
            try:
                text_w = int(btn.fontMetrics().horizontalAdvance(btn.text()) + 40)
            except Exception:
                text_w = 0
            content_width = max(content_width, hint_w, text_w)
        for header in getattr(self, "_nav_group_headers", {}).values():
            try:
                hint_w = int(header.sizeHint().width())
            except Exception:
                hint_w = 0
            try:
                text_w = int(header.fontMetrics().horizontalAdvance(header.text()) + 48)
            except Exception:
                text_w = 0
            content_width = max(content_width, hint_w, text_w)

        # Accordion child rows are indented; reserve that offset so expanded
        # child buttons do not lose right-edge pixels.
        child_indent_w = 0
        for body_layout in getattr(self, "_nav_group_layouts", {}).values():
            if body_layout is None:
                continue
            try:
                margins = body_layout.contentsMargins()
                child_indent_w = max(child_indent_w, int(margins.left() + margins.right()))
            except Exception:
                continue

        # Status cards should fully fit in the rail without horizontal clipping.
        status_hint_w = 0
        for container in (
            getattr(self, "scheduler_status_container", None),
            getattr(self, "condition_level_container", None),
            getattr(self, "ledge_clock_widget", None),
        ):
            if container is None:
                continue
            try:
                status_hint_w = max(status_hint_w, int(container.sizeHint().width()))
            except Exception:
                continue

        content_width = max(content_width + child_indent_w, status_hint_w)
        content_width = max(150, min(content_width, 300))
        for btn in self.nav_buttons:
            try:
                btn.setMinimumWidth(content_width)
            except Exception:
                pass
        for header in getattr(self, "_nav_group_headers", {}).values():
            try:
                header.setMinimumWidth(content_width)
            except Exception:
                pass
        try:
            layout = self.nav_widget.layout()
            margins = layout.contentsMargins() if layout is not None else None
            margin_w = int((margins.left() + margins.right()) if margins is not None else 8)
        except Exception:
            margin_w = 8

        # Additional reserve avoids clipping from scroll-area internals and borders.
        rail_padding_w = 20
        panel_w = max(180, min(content_width + margin_w + rail_padding_w, 340))
        try:
            self.nav_widget.setMinimumWidth(panel_w)
            self.nav_widget.setMaximumWidth(panel_w)
        except Exception:
            pass
        self._update_scheduler_action_button_widths()
        self._sync_status_box_width()
        self._auto_collapse_inactive_nav_groups()

    def _compact_navigation_width(self) -> int:
        try:
            scale = float(resolve_ui_text_scale(self.settings))
        except Exception:
            scale = 1.0
        return max(74, min(88, int(round(74 * scale))))

    def _compact_navigation_button_width(self) -> int:
        # The button sits inside both the 4px-per-side main navigation margins
        # and the 3px-per-side compact-widget margins. Subtract both so Qt does
        # not clip the button's right border/icon paint area on macOS or Linux.
        return max(56, self._compact_navigation_width() - 14)

    @staticmethod
    def _compact_navigation_specs() -> tuple[tuple[str, str, str, str], ...]:
        """Visible label, accessible label, route/group key, owned icon name."""
        return (
            ("Ops", "Ops Center", "ControlFreq", "ops.svg"),
            ("Map", "Map", "Map", "map.svg"),
            ("Msgs", "Messages", "Messages", "messages.svg"),
            ("BBS", "CommsPlan BBS", "Managed BBS", "bbs.svg"),
            ("Spotter", "CommsPlan Spotter", "CommsPlan Spotter", "spotter.svg"),
            ("Net Ctrl", "Net Control", "NCS", "net-control.svg"),
            ("Calls", "Operators", "Operators", "operators.svg"),
            ("Resources", "Tools and Resources", "Resources", "resources.svg"),
            ("Plans", "Plans", "Plan Builder", "plans.svg"),
            ("Station", "Station", "Station", "station.svg"),
            ("Config", "Configuration", "Configuration", "settings.svg"),
            ("Help", "Help", "Help", "help.svg"),
        )

    def _activate_navigation_item(self, button_label: str, screen_label: str) -> None:
        for spec, button in zip(getattr(self, "_nav_specs", ()), getattr(self, "nav_buttons", ())):
            if tuple(spec) == (button_label, screen_label):
                button.click()
                return

    def _current_screen_label(self) -> str:
        try:
            index = int(self.stack.currentIndex())
            return str(self._screens[index][0])
        except Exception:
            return ""

    def _run_if_screen_current(
        self,
        expected_index: int,
        expected_epoch: int,
        callback: Callable[[], None],
    ) -> bool:
        """Run queued UI work only for the navigation that scheduled it."""

        if bool(getattr(self, "_shutting_down", False)):
            return False
        if int(getattr(self, "_navigation_epoch", -1)) != int(expected_epoch):
            return False
        try:
            if int(self.stack.currentIndex()) != int(expected_index):
                return False
        except Exception:
            return False
        callback()
        return True

    def _show_compact_navigation_menu(self, anchor: QToolButton, group_key: str) -> None:
        menu = QMenu(anchor)
        menu.setObjectName("mainCompactNavFlyout")
        for button_label, screen_label in getattr(self, "_nav_specs", ()):
            if self._nav_group_for_label(button_label, screen_label) != group_key:
                continue
            action = QAction(button_label, menu)
            action.setCheckable(True)
            is_current = self._current_screen_label() == screen_label
            if screen_label == "Messages":
                is_current = is_current and button_label.lower() == str(
                    getattr(self, "_messages_nav_context", "inbox")
                )
            elif screen_label == "Settings":
                expected_context = "main" if button_label == "Main" else "radios"
                is_current = is_current and expected_context == str(
                    getattr(self, "_settings_nav_context", "main")
                )
            elif screen_label == "Resources":
                # Frequencies represents the entire internal Resources browser;
                # its last-open catalog tab is intentionally remembered.
                is_current = is_current and button_label == "Frequencies"
            action.setChecked(is_current)
            action.triggered.connect(
                lambda _checked=False, label=button_label, screen=screen_label: self._activate_navigation_item(label, screen)
            )
            menu.addAction(action)
        self._compact_nav_menu = menu
        menu.aboutToHide.connect(
            lambda: self._sync_compact_navigation_selection(self._current_screen_label())
        )
        menu.popup(anchor.mapToGlobal(anchor.rect().topRight()))

    def _build_compact_navigation_widget(self) -> QWidget:
        widget = QWidget(self.nav_widget)
        widget.setObjectName("mainCompactNavigation")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(3, 0, 3, 0)
        layout.setSpacing(3)
        icon_root = Path(__file__).resolve().parents[2] / "assets" / "icons" / "navigation"
        self.nav_compact_buttons: list[QToolButton] = []
        self.nav_compact_button_group = QButtonGroup(self)
        self.nav_compact_button_group.setExclusive(True)
        grouped_keys = set(getattr(self, "_nav_group_order", ()))
        button_width = self._compact_navigation_button_width()
        try:
            button_height = control_height_for_font(widget, vertical_padding=26, floor=48)
        except Exception:
            button_height = 48
        for label, accessible_label, target, icon_name in self._compact_navigation_specs():
            if target in grouped_keys and not any(
                self._nav_group_for_label(item_label, screen) == target
                for item_label, screen in self._nav_specs
            ):
                continue
            button = QToolButton(widget)
            button.setObjectName("mainCompactNavButton")
            button.setText(label)
            button.setIcon(QIcon(str(icon_root / icon_name)))
            button.setIconSize(QSize(22, 22))
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setCheckable(True)
            button.setFixedSize(button_width, button_height)
            button.setToolTip(
                f"Open {accessible_label} choices." if target in grouped_keys else f"Open {accessible_label}."
            )
            button.setAccessibleName(accessible_label)
            button.setProperty("navTarget", target)
            button.setProperty("navGroup", target in grouped_keys)
            if target in grouped_keys:
                button.clicked.connect(
                    lambda _checked=False, anchor=button, group=target: self._show_compact_navigation_menu(anchor, group)
                )
            else:
                route = next(
                    ((item_label, screen) for item_label, screen in self._nav_specs if screen == target),
                    None,
                )
                if route is not None:
                    button.clicked.connect(
                        lambda _checked=False, item_label=route[0], screen=route[1]: self._activate_navigation_item(item_label, screen)
                    )
            self.nav_compact_button_group.addButton(button)
            layout.addWidget(button, 0, Qt.AlignHCenter)
            self.nav_compact_buttons.append(button)
        if self.nav_compact_buttons:
            self.nav_compact_buttons[0].setChecked(True)
        layout.addStretch(1)
        return widget

    def _sync_compact_navigation_selection(self, screen_label: str) -> None:
        screen = str(screen_label or "").strip()
        target = self._nav_group_for_screen_label(screen) or screen
        group = getattr(self, "nav_compact_button_group", None)
        if group is not None:
            group.setExclusive(False)
        for button in getattr(self, "nav_compact_buttons", []) or []:
            button.setChecked(str(button.property("navTarget") or "").strip() == target)
        if group is not None:
            group.setExclusive(True)

    def _style_compact_navigation(self, theme: Mapping[str, object]) -> None:
        widget = getattr(self, "nav_compact_widget", None)
        if widget is None:
            return
        widget.setStyleSheet(
            "QToolButton#mainCompactNavButton {"
            f"background:{theme.get('surface_alt', '#ECEFF1')}; color:{theme.get('text', '#222222')};"
            f"border:1px solid {theme.get('border', '#CCCCCC')}; border-radius:6px; padding:2px;"
            "}"
            "QToolButton#mainCompactNavButton:checked {"
            f"background:{theme.get('surface', '#FFFFFF')}; color:{theme.get('text', '#222222')};"
            f"border:2px solid {theme.get('accent', '#2A6FD3')}; border-left:4px solid {theme.get('accent', '#2A6FD3')};"
            "}"
            "QToolButton#mainCompactNavButton:focus {"
            f"border:2px solid {theme.get('info', '#1565C0')};"
            "}"
        )

    def _toggle_main_navigation(self) -> None:
        self._main_nav_auto_collapsed = False
        self._set_main_navigation_collapsed(not bool(getattr(self, "_main_nav_collapsed", False)))

    def _set_main_navigation_collapsed(self, collapsed: bool) -> None:
        self._main_nav_collapsed = bool(collapsed)
        if hasattr(self, "nav_collapse_btn"):
            self.nav_collapse_btn.setText("≫" if collapsed else "≪")
            self.nav_collapse_btn.setAccessibleName("Expand navigation" if collapsed else "Collapse navigation")
            self.nav_collapse_btn.setToolTip(
                "Expand full navigation." if collapsed else "Collapse navigation to a compact workflow rail."
            )
        for widget in (
            getattr(self, "logo_label", None),
            getattr(self, "ledge_clock_widget", None),
            getattr(self, "nav_scroll", None),
            getattr(self, "status_dock_widget", None),
        ):
            if widget is not None:
                widget.setVisible(False if collapsed else widget not in {
                    getattr(self, "ledge_clock_widget", None),
                    getattr(self, "condition_level_container", None),
                })
        compact = getattr(self, "nav_compact_widget", None)
        if compact is not None:
            compact.setVisible(collapsed)
        if collapsed:
            compact_width = self._compact_navigation_width()
            self.nav_widget.setMinimumWidth(compact_width)
            self.nav_widget.setMaximumWidth(compact_width)
        else:
            self.nav_widget.setMinimumWidth(150)
            self.nav_widget.setMaximumWidth(300)
            self._update_nav_layout_metrics()
        try:
            central = self.centralWidget()
            if central is not None and central.layout() is not None:
                central.layout().activate()
            self.station_command_bar.updateGeometry()
            self.station_command_radio_summary_scroll.updateGeometry()
            QTimer.singleShot(0, self._reflow_adaptive_station_shell)
        except Exception:
            pass

    def _dismiss_off_schedule_prompt(self) -> None:
        if hasattr(self, "_off_schedule_prompt") and self._off_schedule_prompt is not None:
            try:
                self._off_schedule_prompt.close()
            except Exception:
                pass
            self._off_schedule_prompt = None
            self._off_schedule_prompt_key = None

    def _dismiss_radio_off_schedule_prompt(self, radio_id: int) -> None:
        """Cancel only the named radio's obsolete review, including deferred UI."""
        getattr(self, "_pending_off_schedule_prompts", {}).pop(int(radio_id), None)
        key = getattr(self, "_off_schedule_prompt_key", None)
        if key and key[0] == int(radio_id):
            self._dismiss_off_schedule_prompt()
        getattr(self, "_station_command_off_schedule_by_radio", {}).pop(int(radio_id), None)

    def _show_next_off_schedule_prompt(self) -> None:
        if getattr(self, "_off_schedule_prompt", None) is not None or self._shutting_down:
            return
        pending = getattr(self, "_pending_off_schedule_prompts", {})
        if pending:
            radio_id = next(iter(pending))
            payload = pending.pop(radio_id)
            self._on_off_schedule_detected(payload)
            if pending and getattr(self, "_off_schedule_prompt", None) is None:
                QTimer.singleShot(0, self._show_next_off_schedule_prompt)

    def _on_off_schedule_cleared(self) -> None:
        # This compatibility signal represents only the primary prompt loop.
        radio_id = int(getattr(getattr(self, "station_runtime_manager", None), "_primary_device_id", 0) or 0)
        if radio_id > 0:
            self._dismiss_radio_off_schedule_prompt(radio_id)
        self._station_command_radio_summary_signature = None
        self._refresh_station_command_bar(force=False)

    def _dismiss_varac_wait_prompt(self) -> None:
        if hasattr(self, "_varac_wait_prompt") and self._varac_wait_prompt is not None:
            try:
                self._varac_wait_prompt.close()
            except Exception:
                pass
            self._varac_wait_prompt = None

    def _dismiss_coordination_conflict_prompt(self) -> None:
        if hasattr(self, "_coordination_conflict_prompt") and self._coordination_conflict_prompt is not None:
            try:
                self._coordination_conflict_prompt.close()
            except Exception:
                pass
            self._coordination_conflict_prompt = None

    def _build_prompt_hold_duration_combo(self, parent) -> QComboBox:
        combo = QComboBox(parent)
        combo.setToolTip("How long CommsPlan should leave this radio off schedule.")
        refresh_hold_duration_combo(combo, self.settings, getattr(self, "_active_runtime_profile", None))
        combo.addItem("Indefinite", 0)
        return combo

    def _selected_prompt_hold_duration(self, combo: QComboBox | None) -> int:
        if combo is None:
            return selected_hold_duration(combo, self.settings, getattr(self, "_active_runtime_profile", None))
        try:
            data = combo.currentData()
            if data is not None and int(data) <= 0:
                return 0
        except Exception:
            pass
        return selected_hold_duration(combo, self.settings, getattr(self, "_active_runtime_profile", None))

    def _radio_name_for_device_profile_id(self, device_profile_id: int | None) -> str:
        try:
            ident = int(device_profile_id or 0)
        except Exception:
            ident = 0
        if ident <= 0:
            return "Radio"
        try:
            for snapshot in self._station_command_radio_choices():
                if self._station_command_snapshot_id(snapshot) == ident:
                    return self._station_command_snapshot_name(snapshot)
        except Exception:
            pass
        try:
            store = getattr(self, "multi_radio_store", None)
            profiles = store.list_device_profiles() if store is not None and hasattr(store, "list_device_profiles") else []
            for profile in profiles:
                if int(profile.get("id", 0) or 0) == ident:
                    name = str(profile.get("name") or "").strip()
                    if name:
                        return name
        except Exception:
            pass
        return f"Radio {ident}"

    def _attach_prompt_hold_duration_row(self, msg: QMessageBox, combo: QComboBox) -> None:
        try:
            row = QWidget(msg)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)
            label = QLabel("Suspend")
            label.setMinimumWidth(80)
            layout.addWidget(label)
            layout.addWidget(combo)
            layout.addStretch(1)
            msg.layout().addWidget(row, msg.layout().rowCount(), 0, 1, msg.layout().columnCount())
        except Exception:
            pass

    def _on_off_schedule_detected(self, payload: dict) -> None:
        if self._shutting_down:
            return
        items = payload.get("items") if isinstance(payload, dict) else None
        items = items if isinstance(items, list) else []
        if not items:
            return
        try:
            target_radio_id = int((payload or {}).get("device_profile_id") or 0)
        except Exception:
            target_radio_id = 0
        radio_id = target_radio_id
        entry = payload.get("entry") or {}
        target_radio_id = target_radio_id or (self._station_command_scheduler_entry_radio_id(entry) if isinstance(entry, Mapping) else 0)
        radio_id = target_radio_id
        scheduler = getattr(self, "scheduler", None)
        if target_radio_id <= 0 or scheduler is None:
            return
        source = str(payload.get("source") or entry.get("source_type") or "HF").upper()
        if (scheduler._net_corrections_suppressed(target_radio_id) or source == "NET"
                or not scheduler._automatic_source_enabled(target_radio_id, source)
                or scheduler._radio_manual_control_blocks_off_schedule_prompt(target_radio_id)):
            return
        try:
            if radio_id > 0:
                state = getattr(self, "_station_command_off_schedule_by_radio", None)
                if not isinstance(state, dict):
                    state = {}
                    self._station_command_off_schedule_by_radio = state
                state[radio_id] = {"items": list(items), "entry": dict(entry)}
                self._station_command_radio_summary_signature = None
                self._refresh_station_command_bar(force=False)
        except Exception:
            pass
        prompt_key = (int(target_radio_id or 0), tuple(str(item) for item in items))
        active_key = getattr(self, "_off_schedule_prompt_key", None)
        if getattr(self, "_off_schedule_prompt", None) is not None and active_key == prompt_key:
            return
        if getattr(self, "_off_schedule_prompt", None) is not None:
            pending = getattr(self, "_pending_off_schedule_prompts", {})
            pending[target_radio_id] = dict(payload)
            self._pending_off_schedule_prompts = pending
            return
        msg = QMessageBox(self)
        radio_name = self._radio_name_for_device_profile_id(target_radio_id)
        msg.setWindowTitle(f"{radio_name} Off Schedule")
        if len(items) == 1:
            text = f"{items[0]} is off schedule."
        elif len(items) == 2:
            text = f"{items[0]} and {items[1]} are off schedule."
        else:
            text = f"{', '.join(items[:-1])}, and {items[-1]} are off schedule."
        msg.setText(text)
        try:
            entry = payload.get("entry") if isinstance(payload, Mapping) else {}
            group = str((entry or {}).get("group_name") or (entry or {}).get("group") or "").strip()
            band = str((entry or {}).get("band") or "").strip()
            freq = str((entry or {}).get("frequency") or "").strip()
            scheduled_bits = [part for part in (group, band, freq) if part]
            if scheduled_bits:
                msg.setInformativeText(f"Scheduled target for {radio_name}: {' '.join(scheduled_bits)}")
        except Exception:
            pass
        apply_btn = msg.addButton("Resume Schedule", QMessageBox.AcceptRole)
        ignore_btn = msg.addButton("Skip Once", QMessageBox.RejectRole)
        suspend_btn = msg.addButton("Suspend", QMessageBox.DestructiveRole)
        hold_combo = self._build_prompt_hold_duration_combo(msg)
        self._attach_prompt_hold_duration_row(msg, hold_combo)
        self._off_schedule_prompt = msg
        self._off_schedule_prompt_key = prompt_key
        # A review is never accepted by an unattended timeout. In particular,
        # an old dialog cannot retune a radio after the operator takes control.
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == apply_btn:
            try:
                self.scheduler.resolve_off_schedule("apply", items=items, target_device_profile_id=target_radio_id)
            except Exception:
                pass
        elif clicked == ignore_btn:
            try:
                self.scheduler.resolve_off_schedule("ignore", items=items, target_device_profile_id=target_radio_id)
            except Exception:
                pass
        elif clicked == suspend_btn:
            try:
                mins = self._selected_prompt_hold_duration(hold_combo)
                if mins > 0:
                    set_hold_duration_default(self.settings, mins)
                    self._sync_hold_duration_combos()
                self.scheduler.resolve_off_schedule(
                    "suspend",
                    items=items,
                    minutes=mins,
                    target_device_profile_id=target_radio_id,
                )
                self.on_hold_state_changed(force_reload=True)
            except Exception:
                pass
        if getattr(self, "_off_schedule_prompt", None) is msg:
            self._off_schedule_prompt = None
            self._off_schedule_prompt_key = None
        QTimer.singleShot(0, self._show_next_off_schedule_prompt)

    def _on_varac_wait_detected(self, payload: dict) -> None:
        if self._shutting_down:
            return
        self._dismiss_varac_wait_prompt()
        msg = QMessageBox(self)
        msg.setWindowTitle("Frequency Change Pending")
        msg.setText("VarAC is waiting for frequency to clear.\nChange frequency now?")
        apply_btn = msg.addButton("Resume Sched.", QMessageBox.AcceptRole)
        ignore_btn = msg.addButton("Skip Once", QMessageBox.RejectRole)
        suspend_btn = msg.addButton("Pause Schedule", QMessageBox.DestructiveRole)
        hold_combo = self._build_prompt_hold_duration_combo(msg)
        self._attach_prompt_hold_duration_row(msg, hold_combo)
        self._varac_wait_prompt = msg
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == apply_btn:
            try:
                self.scheduler.resolve_varac_wait("apply")
            except Exception:
                pass
        elif clicked == suspend_btn:
            try:
                mins = selected_hold_duration(hold_combo, self.settings, getattr(self, "_active_runtime_profile", None))
                set_hold_duration_default(self.settings, mins)
                self._sync_hold_duration_combos()
                self.scheduler.resolve_varac_wait("suspend", minutes=mins)
                self.on_hold_state_changed(force_reload=True)
            except Exception:
                pass
        else:
            try:
                self.scheduler.resolve_varac_wait("ignore")
            except Exception:
                pass
        self._varac_wait_prompt = None

    def _on_coordination_conflict_detected(self, payload: dict) -> None:
        if self._shutting_down:
            return
        self._dismiss_coordination_conflict_prompt()
        summary = str((payload or {}).get("summary") or "").strip() or "RF conflict detected."
        detail = str((payload or {}).get("detail") or "").strip()
        msg = QMessageBox(self)
        msg.setWindowTitle("RF Conflict Warning")
        msg.setText(summary)
        if detail:
            msg.setInformativeText(detail)
        apply_btn = msg.addButton("Proceed Once", QMessageBox.AcceptRole)
        ignore_btn = msg.addButton("Skip Once", QMessageBox.RejectRole)
        suspend_btn = msg.addButton("Pause Schedule", QMessageBox.DestructiveRole)
        hold_combo = self._build_prompt_hold_duration_combo(msg)
        self._attach_prompt_hold_duration_row(msg, hold_combo)
        self._coordination_conflict_prompt = msg
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == apply_btn:
            try:
                self.scheduler.resolve_coordination_conflict("apply")
            except Exception:
                pass
        elif clicked == suspend_btn:
            try:
                mins = selected_hold_duration(hold_combo, self.settings, getattr(self, "_active_runtime_profile", None))
                set_hold_duration_default(self.settings, mins)
                self._sync_hold_duration_combos()
                self.scheduler.resolve_coordination_conflict("suspend", minutes=mins)
                self.on_hold_state_changed(force_reload=True)
            except Exception:
                pass
        else:
            try:
                self.scheduler.resolve_coordination_conflict("ignore")
            except Exception:
                pass
        self._coordination_conflict_prompt = None

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _apply_callsign_to_tab_titles(self):
        """
        Append the configured callsign to each navigation label, if available.
        This is a helper so the Settings tab can call back into the main
        window (e.g., after saving a new callsign) by doing:
            self.parent()._apply_callsign_to_tab_titles()
        """
        data = self.settings.all()
        callsign = (data.get("callsign") or "").strip().upper()
        base_labels = getattr(self, "_nav_base_labels", [])
        if not callsign:
            # Reset to base titles if no callsign is set
            for idx, base in enumerate(base_labels):
                if idx < len(self.nav_buttons):
                    self.nav_buttons[idx].setText(base)
            self._update_nav_layout_metrics()
            self._station_health_alert_signature = None
            self._refresh_station_health_alert()
            return

        for idx, base in enumerate(base_labels):
            lbl = f"{base} [{callsign}]"
            if idx < len(self.nav_buttons):
                self.nav_buttons[idx].setText(lbl)
        self._update_nav_layout_metrics()
        self._station_health_alert_signature = None
        self._refresh_station_health_alert()

    def open_help_anchor(self, anchor: str | None, *, title: str | None = None) -> None:
        help_index = next((idx for idx, (label, _) in enumerate(self._screens) if label == "Help"), -1)
        if help_index >= 0:
            self._set_screen(help_index)
        tab = getattr(self, "help_tab", None)
        if tab is None:
            return
        try:
            tab.open_anchor(anchor)
        except Exception:
            pass
        if title:
            try:
                tab.setWindowTitle(str(title))
            except Exception:
                pass

    def open_context_help(self, context_key: str | None) -> None:
        context = get_help_context(context_key)
        if self._context_help_dialog is None:
            self._context_help_dialog = ContextHelpDialog(self.settings, self)
            try:
                self._context_help_dialog.finished.connect(self._on_context_help_finished)
            except Exception:
                pass
        try:
            self._context_help_dialog.apply_theme()
        except Exception:
            pass
        self._context_help_dialog.show_help_for(context.key)

    def _on_context_help_finished(self, *_args) -> None:
        self._help_dialog_settle_until = time.time() + 0.35
        log.info("UI_LIFECYCLE|context_help_closed settle_ms=350")

    def open_settings_section(
        self,
        health_key: str = "freqinout",
        radio_id: int | None = None,
        *,
        settings_nav_context: str | None = None,
    ) -> None:
        idx = self._screen_index_by_label.get("Settings", -1)
        if idx < 0:
            return
        context = str(settings_nav_context or "").strip().lower()
        if not context:
            target = str(health_key or "").strip().lower()
            main_targets = {
                "operator_info",
                "freqinout",
                "operating_groups",
                "local_comms",
                "message_auth",
                "sop_export",
                "logging",
            }
            context = "main" if target in main_targets else "radios"
        if context == "software":
            self._settings_nav_context = "software"
        else:
            self._settings_nav_context = "main" if context == "main" else "radios"
        self._set_screen(idx)
        navigation_epoch = int(getattr(self, "_navigation_epoch", 0))
        if hasattr(self.settings_tab, "show_settings_context"):
            QTimer.singleShot(
                0,
                lambda key=str(health_key or "freqinout"), ident=radio_id, ctx=self._settings_nav_context, expected=idx, epoch=navigation_epoch: self._run_if_screen_current(
                    expected,
                    epoch,
                    lambda: self.settings_tab.show_settings_context(
                        ctx,
                        health_key=key,
                        radio_id=ident,
                    ),
                ),
            )
        elif hasattr(self.settings_tab, "focus_section_by_health_key"):
            QTimer.singleShot(
                0,
                lambda key=str(health_key or "freqinout"), ident=radio_id, expected=idx, epoch=navigation_epoch: self._run_if_screen_current(
                    expected,
                    epoch,
                    lambda: self.settings_tab.focus_section_by_health_key(key, ident),
                ),
            )

    def open_guided_add_radio(self) -> None:
        """Open the existing first-radio workflow from any operator surface."""

        self.open_settings_section("radio_profiles", settings_nav_context="radios")
        if hasattr(self.settings_tab, "start_guided_add_radio"):
            QTimer.singleShot(0, self.settings_tab.start_guided_add_radio)

    def open_station_bbs(self) -> None:
        """Open the top-level, station-owned CommsPlan BBS service."""
        idx = self._screen_index_by_label.get("Managed BBS", -1)
        if idx >= 0:
            self._set_screen(idx)

    def open_fio_spotter(self) -> None:
        """Open the primary CommsPlan Spotter service workspace."""
        idx = self._screen_index_by_label.get("CommsPlan Spotter", -1)
        if idx >= 0:
            self._set_screen(idx)

    def open_fio_spotter_forms(self, *, legacy_form_sources: object | None = None) -> None:
        """Open CommsPlan Spotter directly on its station-owned Forms page."""
        self.open_fio_spotter()
        tab = getattr(self, "fio_spotter_tab", None)
        if tab is not None and legacy_form_sources is not None and hasattr(tab, "set_legacy_form_sources"):
            tab.set_legacy_form_sources(legacy_form_sources)
        if tab is not None and hasattr(tab, "open_forms"):
            tab.open_forms()

    def open_fio_spotter_imports(self) -> None:
        """Open CommsPlan Spotter directly on its External Spotter imports page."""
        self.open_fio_spotter()
        tab = getattr(self, "fio_spotter_tab", None)
        if tab is not None and hasattr(tab, "open_imports"):
            tab.open_imports()

    def open_fio_spotter_expect(self, *, entry_id: int = 0, expect_key: str = "") -> None:
        """Open fresh, authoritative Expect administration for one rule."""
        self.open_fio_spotter()
        tab = getattr(self, "fio_spotter_tab", None)
        if tab is not None and hasattr(tab, "open_expect_entry"):
            tab.open_expect_entry(entry_id=entry_id, expect_key=expect_key)

    def open_resources_section(self, section: str | NavigationIntent = "frequency_catalog") -> None:
        """Open an implemented Resources route without exposing unfinished ones."""
        # ``resources.shortwave`` remains a typed contextual route even when a
        # legacy or failed-startup profile cannot expose its navigation child.
        intent = section if isinstance(section, NavigationIntent) else None
        requested = intent.destination_route.rsplit(".", 1)[-1] if intent else section
        key = str(requested or "frequency_catalog").strip().lower()
        if key in {"last", "frequencies"}:
            remembered = str(getattr(self, "_resources_nav_context", "frequency_catalog") or "frequency_catalog")
            key = remembered if remembered in {"frequency_catalog", "net_directory", "import_export"} else "frequency_catalog"
        if key == "shortwave":
            shortwave_index = self._screen_index_by_label.get("Shortwave", -1)
            if shortwave_index >= 0 and ("Shortwave", "Shortwave") in self._nav_specs:
                self._set_screen(shortwave_index)
                return
            key = "frequency_catalog"
        if key not in {"frequency_catalog", "net_directory", "import_export"}:
            key = "frequency_catalog"
        self._resources_nav_context = key
        idx = self._screen_index_by_label.get("Resources", -1)
        if idx < 0:
            return
        self._set_screen(idx)
        tab = self._get_tab_by_label("Resources")
        if tab is not None:
            if hasattr(tab, "set_navigation_intent"):
                tab.set_navigation_intent(intent)
            if hasattr(tab, "open_section"):
                tab.open_section(key)

    def _open_navigation_intent(self, intent: object) -> None:
        if not isinstance(intent, NavigationIntent):
            return
        if intent.destination_route.startswith("resources."):
            self.open_resources_section(intent)
        elif intent.destination_route == "settings.operating_groups":
            self.open_settings_section("operating_groups", settings_nav_context="main")

    def _return_navigation_intent(self, intent: object) -> None:
        if not isinstance(intent, NavigationIntent):
            return
        if intent.return_route == "plans.local_nets":
            index = self._screen_index_by_label.get("Local Nets", -1)
            if index >= 0:
                self._set_screen(index)
        elif intent.return_route == "ops.schedule_outlook":
            index = self._screen_index_by_label.get("Ops Center", -1)
            if index < 0:
                return
            self._set_screen(index)

            def restore_ops_context() -> None:
                scroll = getattr(self.controlfreq_tab, "controlfreq_scroll", None)
                if scroll is not None:
                    scroll.verticalScrollBar().setValue(max(0, int(intent.return_scroll_y)))
                self.controlfreq_tab.refresh_local_nets_outlook()

            QTimer.singleShot(0, restore_ops_context)

    @staticmethod
    def _local_net_item_value(item: object, name: str) -> object:
        return item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)

    def _build_local_nets_outlook(self, now_utc: datetime.datetime) -> object:
        from freqinout.core.known_operating_groups import net_resources_db_path
        from freqinout.core.local_net_projection import build_local_net_outlook
        from freqinout.core.local_net_store import LocalNetStore
        from freqinout.core.resource_catalog_store import ResourceCatalogStore

        path = net_resources_db_path()
        return build_local_net_outlook(
            LocalNetStore(path),
            ResourceCatalogStore(path),
            now_utc,
            horizon_days=30,
            later_limit=50,
        )

    def _shortwave_receiver_profiles(self) -> list[dict[str, object]]:
        """Read configured device identities for the manual Shortwave label only.

        The Shortwave UI calls this on its worker lane.  The returned identity
        is not a capability grant and is never used to tune/control a device.
        """
        return [dict(row) for row in self.multi_radio_store.list_device_profiles()]

    def _build_shortwave_listening_outlook(self, now_utc: datetime.datetime) -> object:
        from freqinout.core.shortwave_listening import ShortwaveListeningStore, build_shortwave_listening_outlook
        from freqinout.core.known_operating_groups import net_resources_db_path

        return build_shortwave_listening_outlook(
            ShortwaveListeningStore(net_resources_db_path()), now_utc, horizon_days=30, later_limit=50,
        )

    @staticmethod
    def _shortwave_listening_item_value(item: object, name: str) -> object:
        return item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)

    def _open_shortwave_listening_details(self, item: object) -> None:
        reminder_key = self._shortwave_listening_item_value(item, "reminder_key")
        index = self._screen_index_by_label.get("Shortwave", -1)
        if not reminder_key or index < 0:
            return
        self._set_screen(index)
        tab = self._get_tab_by_label("Shortwave")
        if tab is not None and hasattr(tab, "focus_reminder"):
            QTimer.singleShot(0, lambda target=tab, key=str(reminder_key): target.focus_reminder(key))

    def _dismiss_shortwave_listening_occurrence(self, item: object) -> None:
        """Dismiss only the surfaced occurrence; it never changes source/schedule state."""
        from freqinout.core.shortwave_listening import ShortwaveListeningStore
        from freqinout.core.known_operating_groups import net_resources_db_path

        reminder_key = self._shortwave_listening_item_value(item, "reminder_key")
        occurrence_key = self._shortwave_listening_item_value(item, "occurrence_key")
        start_utc = self._shortwave_listening_item_value(item, "start_utc")
        if not reminder_key or not occurrence_key or not isinstance(start_utc, datetime.datetime):
            return
        ShortwaveListeningStore(net_resources_db_path()).dismiss_occurrence(
            str(reminder_key), str(occurrence_key), start_utc, note="Dismissed from Ops Center",
        )

    def _open_local_net_details(self, item: object) -> None:
        schedule_id = self._local_net_item_value(item, "local_net_schedule_id")
        occurrence_id = self._local_net_item_value(item, "occurrence_id")
        index = self._screen_index_by_label.get("Local Nets", -1)
        if index < 0 or not schedule_id:
            return
        self._set_screen(index)
        tab = self._get_tab_by_label("Local Nets")
        if tab is not None and hasattr(tab, "focus_schedule"):
            QTimer.singleShot(
                0,
                lambda target=tab, schedule=schedule_id, occurrence=occurrence_id: target.focus_schedule(
                    schedule, occurrence
                ),
            )

    def _dismiss_local_net_occurrence(self, item: object) -> None:
        from freqinout.core.known_operating_groups import net_resources_db_path
        from freqinout.core.local_net_store import LocalNetStore

        occurrence_id = self._local_net_item_value(item, "occurrence_id")
        schedule_id = self._local_net_item_value(item, "local_net_schedule_id")
        start_utc = self._local_net_item_value(item, "start_utc")
        if not occurrence_id or not schedule_id or not isinstance(start_utc, datetime.datetime):
            return
        try:
            LocalNetStore(net_resources_db_path()).dismiss_occurrence_reference(
                str(occurrence_id),
                str(schedule_id),
                start_utc,
                note="Dismissed from Ops Center",
            )
        except Exception as exc:
            log.debug("Ops Center Local Net dismissal failed: %s", exc)
            return
        self.controlfreq_tab.refresh_local_nets_outlook()

    def _open_local_net_sop(self, item: object) -> None:
        from freqinout.core.local_net_projection import build_local_net_sop_intent

        schedule_id = self._local_net_item_value(item, "local_net_schedule_id")
        occurrence_id = self._local_net_item_value(item, "occurrence_id")
        session_id = self._local_net_item_value(item, "net_session_id")
        sop_id = self._local_net_item_value(item, "sop_id")
        if not schedule_id or not occurrence_id or not sop_id:
            return
        intent = build_local_net_sop_intent(
            local_net_schedule_key=str(schedule_id),
            occurrence_key=str(occurrence_id),
            net_session_key=str(session_id) if session_id else None,
            sop_id=int(sop_id),
            return_scroll_y=self.controlfreq_tab.controlfreq_scroll.verticalScrollBar().value(),
        )
        index = self._screen_index_by_label.get("SOP", -1)
        if index < 0:
            return
        self._set_screen(index)
        if hasattr(self.sop_tab, "open_local_net_context"):
            QTimer.singleShot(0, lambda target=self.sop_tab, context=intent: target.open_local_net_context(context))

    def open_hf_net_subscription(self, session_keys: object) -> None:
        """Hand canonical directory sessions to the existing HF Nets editor."""
        keys = tuple(str(key).strip() for key in (session_keys or ()) if str(key).strip())
        if not keys:
            return
        idx = self._screen_index_by_label.get("Net Schedule", -1)
        if idx < 0:
            return
        self._set_screen(idx)
        tab = self._get_tab_by_label("Net Schedule")
        if tab is not None and hasattr(tab, "open_directory_subscription"):
            QTimer.singleShot(0, lambda target=tab, selected=keys: target.open_directory_subscription(selected))

    def open_hf_net_schedule_for_session(self, net_session_key: object) -> None:
        """Open the named HF schedule that already follows a directory session."""
        key = str(net_session_key or "").strip()
        if not key:
            return
        idx = self._screen_index_by_label.get("Net Schedule", -1)
        if idx < 0:
            return
        self._set_screen(idx)
        tab = self._get_tab_by_label("Net Schedule")
        if tab is not None and hasattr(tab, "open_directory_schedule"):
            QTimer.singleShot(0, lambda target=tab, selected=key: target.open_directory_schedule(selected))

    def _open_station_health_runtime_source_related_view(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        health_key = str(payload.get("health_key", "") or "radio_profiles").strip().lower()
        context = str(payload.get("settings_nav_context", "") or "radios").strip().lower()
        radio_id = self._station_health_runtime_payload_radio_id(payload)
        self.open_settings_section(
            health_key=health_key,
            radio_id=radio_id,
            settings_nav_context="main" if context in {"main", "global"} else "radios",
        )

    def _station_health_runtime_payload_radio_id(self, payload: Mapping[str, object]) -> int | None:
        raw_radio_id = payload.get("radio_id")
        try:
            if raw_radio_id not in (None, ""):
                ident = int(raw_radio_id)
                if ident > 0:
                    return ident
        except Exception:
            pass
        label = str(raw_radio_id or "").strip().lower()
        if not label:
            return None
        try:
            profiles = list(self.multi_radio_store.list_device_profiles())
        except Exception:
            profiles = []
        for profile in profiles:
            if not isinstance(profile, Mapping):
                continue
            try:
                ident = int(profile.get("id", 0) or 0)
            except Exception:
                ident = 0
            name = str(profile.get("name", "") or "").strip().lower()
            if ident > 0 and name and name == label:
                return ident
        return None

    def open_messages_section(
        self,
        mode: str = "inbox",
        *,
        group_filter: str = "",
        topic_filter: str = "",
        query_filter: str = "",
        source_family: str = "",
        age_filter_seconds: object = 0,
        concern_only: object = False,
        state_filter: str = "",
        grid_filter: str = "",
        fema_region_filter: str = "",
        action_filter: str = "",
        traffic_message_ids: Sequence[str] | None = None,
        traffic_count_at_click: int = 0,
        compose_intent: Mapping[str, object] | None = None,
    ) -> None:
        idx = self._screen_index_by_label.get("Messages", -1)
        if idx < 0:
            return
        mode_key = str(mode or "inbox").strip().lower()
        self._messages_nav_context = "compose" if mode_key == "compose" else "inbox"
        map_context = map_context_from_mapping(
            {
                "group_filter": group_filter,
                "topic_filter": topic_filter,
                "query_filter": query_filter,
                "source_family": source_family,
                "age_filter_seconds": age_filter_seconds,
                "concern_only": concern_only,
                "state_filter": state_filter,
                "grid_filter": grid_filter,
                "fema_region_filter": fema_region_filter,
            }
        )
        normalized_intent = compose_intent_from_mapping(compose_intent).as_dict() if compose_intent else {}
        self._messages_nav_filter_context = {
            **map_context.as_messages_kwargs(),
            "action_filter": str(action_filter or "").strip().lower(),
            "compose_intent": normalized_intent,
        }
        if traffic_message_ids is not None:
            self._messages_nav_filter_context.update(
                traffic_message_ids=tuple(dict.fromkeys(str(value) for value in traffic_message_ids))[:200],
                traffic_count_at_click=max(0, int(traffic_count_at_click)),
            )
        self._set_screen(idx)

    def open_hf_operator(self, callsign: str = "") -> None:
        """Open HF Operators and apply an optional cached reader identity."""

        idx = self._screen_index_by_label.get("HF Operators", -1)
        if idx < 0:
            return
        requested = str(callsign or "").strip().upper()
        self._set_screen(idx)

        def apply_focus() -> None:
            tab = getattr(self, "operator_history_tab", None)
            search = getattr(tab, "search_edit", None)
            if tab is None or search is None or not requested:
                return
            search.setText(requested)
            apply_filter = getattr(tab, "_apply_filter", None)
            if callable(apply_filter):
                apply_filter()
            search.setFocus(Qt.OtherFocusReason)

        QTimer.singleShot(0, apply_focus)

    def open_fio_spotter_watch(self, candidate: Mapping[str, object]) -> None:
        """Open CommsPlan Spotter and stage one unsaved source-neutral watch."""

        idx = self._screen_index_by_label.get("CommsPlan Spotter", -1)
        if idx < 0:
            return
        payload = dict(candidate or {})
        self._set_screen(idx)

        def apply_draft() -> None:
            tab = getattr(self, "fio_spotter_tab", None)
            callback = getattr(tab, "open_watch_draft", None)
            if callable(callback):
                callback(payload)

        QTimer.singleShot(0, apply_draft)

    @Slot()
    def present_main_window(self) -> None:
        """Bring the existing CommsPlan workspace forward without changing its placement.

        Map is a persistent peer window, so returning to CommsPlan must activate this
        window in place rather than reconstructing it or forcing a normal/maximized
        transition.  Clearing only a minimized state preserves the operator's
        chosen normal, maximized, or full-screen presentation.
        """
        try:
            if self.isMinimized():
                state = self.windowState() & ~Qt.WindowMinimized
                self.setWindowState(state)
            if not self.isVisible():
                self.show()
            self.raise_()
            self.activateWindow()
        except Exception as exc:
            # Window activation is a best-effort platform request. Navigation
            # remains complete even if a compositor declines foreground focus.
            log.debug("MainWindow: failed bringing CommsPlan workspace to front: %s", exc)

    def _apply_pending_map_focus(self) -> None:
        pending = getattr(self, "_pending_map_focus", None)
        tab = getattr(self, "stations_map_tab", None)
        if not pending or tab is None:
            return
        kind, context = pending
        focus = None
        if kind == "spotter":
            focus = getattr(tab, "focus_hf_reports", None) or getattr(tab, "focus_spotter_reports", None)
        elif kind == "local":
            focus = getattr(tab, "focus_local_reports", None)
        if not callable(focus):
            return
        self._pending_map_focus = None
        try:
            focus(**context)
        except Exception as exc:
            log.debug("MainWindow: deferred map focus failed: %s", exc)
        QTimer.singleShot(0, self._sync_map_filters_from_tab)

    def open_spotter_map(
        self,
        *,
        group_filter: str = "",
        topic_filter: str = "",
        query_filter: str = "",
        state_filter: str = "",
        grid_filter: str = "",
    ) -> None:
        idx = self._screen_index_by_label.get("Map", -1)
        if idx < 0 or self._screen_is_runtime_suppressed("Map"):
            return
        self._pending_map_focus = (
            "spotter",
            {
                "group_filter": str(group_filter or ""),
                "topic_filter": str(topic_filter or ""),
                "query_filter": str(query_filter or ""),
                "state_filter": str(state_filter or ""),
                "grid_filter": str(grid_filter or ""),
            },
        )
        self._set_screen(idx)
        self._apply_pending_map_focus()

    # Legacy source-contract marker: def open_local_reports_map(self) -> None:
    def open_local_reports_map(self, **context: object) -> None:
        self._open_local_reports_map(**context)

    def open_local_reports_map_context(
        self,
        *,
        group_filter: str = "",
        topic_filter: str = "",
        query_filter: str = "",
        state_filter: str = "",
        grid_filter: str = "",
    ) -> None:
        self._open_local_reports_map(
            group_filter=group_filter,
            topic_filter=topic_filter,
            query_filter=query_filter,
            state_filter=state_filter,
            grid_filter=grid_filter,
        )

    def _open_local_reports_map(
        self,
        *,
        group_filter: str = "",
        topic_filter: str = "",
        query_filter: str = "",
        state_filter: str = "",
        grid_filter: str = "",
    ) -> None:
        idx = self._screen_index_by_label.get("Map", -1)
        if idx < 0 or self._screen_is_runtime_suppressed("Map"):
            return
        self._pending_map_focus = (
            "local",
            {
                "group_filter": str(group_filter or ""),
                "topic_filter": str(topic_filter or ""),
                "query_filter": str(query_filter or ""),
                "state_filter": str(state_filter or ""),
                "grid_filter": str(grid_filter or ""),
            },
        )
        self._set_screen(idx)
        self._apply_pending_map_focus()

    def open_local_reports(self, callsign: str = "", *, topic_filter: str = "", query: str = "") -> None:
        idx = self._screen_index_by_label.get("Local Reports", -1)
        if idx < 0:
            return
        self._set_screen(idx)
        tab = getattr(self, "local_report_history_tab", None)
        if tab is not None and hasattr(tab, "show_context"):
            QTimer.singleShot(
                0,
                lambda: tab.show_context(
                    callsign=str(callsign or "").strip().upper(),
                    topic=str(topic_filter or "").strip(),
                    query=str(query or "").strip(),
                ),
            )
        elif tab is not None and hasattr(tab, "show_callsign"):
            QTimer.singleShot(0, lambda cs=str(callsign or "").strip().upper(): tab.show_callsign(cs))

    def _apply_messages_nav_context(self) -> None:
        tab = getattr(self, "message_viewer_tab", None)
        if tab is None:
            try:
                current = self.stack.currentWidget()
                if isinstance(current, MessageViewerTab):
                    tab = current
            except Exception:
                tab = None
        if tab is None:
            return
        mode = str(getattr(self, "_messages_nav_context", "inbox") or "inbox").strip().lower()
        try:
            if mode == "compose" and hasattr(tab, "show_compose_from_navigation"):
                tab.show_compose_from_navigation()
                context = dict(getattr(self, "_messages_nav_filter_context", {}) or {})
                intent = context.get("compose_intent")
                if isinstance(intent, dict) and intent and hasattr(tab, "prefill_compose_intent"):
                    tab.prefill_compose_intent(intent)
            elif hasattr(tab, "show_inbox_with_context"):
                context = dict(getattr(self, "_messages_nav_filter_context", {}) or {})
                context.pop("compose_intent", None)
                has_context = context.get("traffic_message_ids") is not None
                for key, value in context.items():
                    if key == "source_family" and str(value or "").strip().lower() in {"", "message"}:
                        continue
                    if key == "age_filter_seconds":
                        try:
                            if int(value or 0) in {0, 7 * 24 * 60 * 60}:
                                continue
                        except Exception:
                            continue
                    active = (
                        bool(value) if isinstance(value, bool)
                        else bool(value) if isinstance(value, (int, float))
                        else bool(str(value or "").strip())
                    )
                    if active:
                        has_context = True
                        break
                if has_context:
                    tab.show_inbox_with_context(**context)
                elif hasattr(tab, "show_inbox_from_navigation"):
                    tab.show_inbox_from_navigation()
            elif hasattr(tab, "show_inbox_from_navigation"):
                tab.show_inbox_from_navigation()
        except Exception as exc:
            log.exception("MainWindow: failed applying Messages navigation context: %s", exc)
            set_status = getattr(tab, "_set_compose_status", None)
            if mode == "compose" and callable(set_status):
                try:
                    set_status(
                        "The requested working copy could not be loaded completely. "
                        "Compose remains available; retry View or start a new draft.",
                        role="warning",
                    )
                except Exception:
                    pass

    def _open_station_health_detail(self, device_profile_id: int = 0, scope_name: str = "") -> None:
        idx = self._screen_index_by_label.get("Station Health", -1)
        if idx >= 0:
            self._set_screen(idx)
        tab = getattr(self, "station_health_tab", None)
        if tab is None:
            return
        try:
            tab.focus_scope(
                device_profile_id=int(device_profile_id or 0),
                scope_name=str(scope_name or "").strip(),
            )
        except Exception:
            pass

    def _on_station_command_health_clicked(self, event=None, *, anchor: QWidget | None = None) -> None:
        try:
            ident = int(getattr(self, "_station_command_selected_profile_id", 0) or 0)
        except Exception:
            ident = 0
        self._show_station_command_health_menu(device_profile_id=ident, anchor=anchor)
        try:
            if event is not None:
                event.accept()
        except Exception:
            pass

    @staticmethod
    def _quick_search_blob(record: Mapping[str, object]) -> str:
        return " ".join(
            str(record.get(key, "") or "")
            for key in ("title", "subtitle", "category", "keywords")
        ).upper()

    def _quick_search_add_record(
        self,
        records: list[dict[str, object]],
        *,
        category: str,
        title: str,
        subtitle: str = "",
        screen: str = "",
        action: str = "screen",
        keywords: str = "",
        section_key: str = "",
        message_mode: str = "",
        radio_id: int = 0,
        detail_text: str = "",
    ) -> None:
        clean_title = str(title or "").strip()
        if not clean_title:
            return
        records.append(
            {
                "category": str(category or "Result"),
                "title": clean_title,
                "subtitle": str(subtitle or "").strip(),
                "screen": str(screen or "").strip(),
                "action": str(action or "screen").strip(),
                "keywords": str(keywords or "").strip(),
                "section_key": str(section_key or "").strip(),
                "message_mode": str(message_mode or "").strip(),
                "radio_id": int(radio_id or 0),
                "detail_text": str(detail_text or "").strip(),
            }
        )

    def _build_quick_search_records(self) -> list[dict[str, object]]:
        now = time.time()
        cached_ts, cached_records = getattr(self, "_quick_search_cache", (0.0, []))
        if cached_records and (now - float(cached_ts or 0.0)) < 5.0:
            return [dict(row) for row in cached_records]
        records: list[dict[str, object]] = []
        for button_label, screen_label in getattr(self, "_nav_specs", []) or []:
            label = str(button_label or screen_label or "").strip()
            screen = str(screen_label or label).strip()
            if not label or self._screen_is_runtime_suppressed(screen):
                continue
            action = "screen"
            message_mode = ""
            section_key = ""
            if screen == "Settings":
                action = "settings"
                section_key = "radio_profiles" if label == "Radios" else "operator_info"
            elif screen == "Messages":
                action = "messages"
                message_mode = "compose" if label == "Compose" else "inbox"
            self._quick_search_add_record(
                records,
                category="Go To",
                title=label,
                subtitle=screen,
                screen=screen,
                action=action,
                section_key=section_key,
                message_mode=message_mode,
                keywords=f"{label} {screen} tab view navigation",
            )
        for title, section, keywords in (
            ("RF Guard", "radio_profiles", "safety antenna band overlap advanced frequency guard radio settings"),
            ("Operating Groups", "hf_operating_groups", "hf magnet ghostnet js8call wefax groups frequencies"),
            ("Local Groups", "local_comms_groups", "repeaters local comms groups"),
            ("Schedule Assignment", "radio_profiles", "assign frequency plan schedule radio rf guard"),
            ("Message Compose", "", "compose message outbound inbox js8 flmsg varac"),
        ):
            self._quick_search_add_record(
                records,
                category="Action" if title == "Message Compose" else "Settings",
                title=title,
                subtitle="Messages" if title == "Message Compose" else "Open related settings",
                screen="Messages" if title == "Message Compose" else "Settings",
                action="messages" if title == "Message Compose" else "settings",
                section_key=section,
                message_mode="compose" if title == "Message Compose" else "",
                keywords=keywords,
            )
        try:
            for profile in self.multi_radio_store.list_device_profiles():
                ident = int(profile.get("id", 0) or 0)
                backend = str(profile.get("control_backend", "") or "").strip()
                device_class = str(profile.get("device_class", "") or "").strip()
                assignment = str(profile.get("assigned_operating_profile_name", "") or "").strip()
                subtitle = " ".join(part for part in (device_class, backend, assignment) if part)
                self._quick_search_add_record(
                    records,
                    category="Radio",
                    title=str(profile.get("name") or f"Radio {ident}"),
                    subtitle=subtitle or "Radio profile",
                    screen="Settings",
                    action="settings",
                    section_key="radio_profiles",
                    radio_id=ident,
                    keywords=f"{backend} {device_class} {assignment} flrig rigctld js8call sdr",
                )
        except Exception:
            pass
        try:
            for plan in self.multi_radio_store.list_frequency_plans():
                category = str(plan.get("category", "") or "normal").strip()
                target_screen = "FreqPlanner"
                if category == "hf_daily_schedule":
                    target_screen = "HF Schedule"
                elif category == "hf_net_schedule":
                    target_screen = "Net Schedule"
                self._quick_search_add_record(
                    records,
                    category="Schedule",
                    title=str(plan.get("name") or "Frequency Plan"),
                    subtitle=category.replace("_", " "),
                    screen=target_screen,
                    action="screen",
                    keywords=f"{plan.get('source_refs_json', '')} {plan.get('group_refs_json', '')} {plan.get('frequency_refs_json', '')}",
                )
        except Exception:
            pass
        try:
            for row in load_operating_groups(self.settings):
                group = str(row.get("group") or row.get("group_name") or "").strip()
                band = str(row.get("band") or "").strip()
                freq = str(row.get("frequency") or row.get("freq") or "").strip()
                mode = str(row.get("mode") or "").strip()
                self._quick_search_add_record(
                    records,
                    category="Frequency",
                    title=" ".join(part for part in (group, band) if part),
                    subtitle=" ".join(part for part in (freq, mode) if part),
                    screen="ControlFreq",
                    action="screen",
                    keywords=f"{group} {band} {freq} {mode} operating group qsy schedule",
                )
        except Exception:
            pass
        try:
            for archive in load_js8spotter_archive_records(limit_per_table=12)[:72]:
                table = str(archive.source_table or "").strip().lower()
                if table in {"grid", "signal", "activity", "csstatrep"}:
                    screen = "HF Operators"
                    keywords = f"{archive.keywords} js8spotter spotter history traffic map operator callsign grid"
                else:
                    screen = "Messages"
                    keywords = f"{archive.keywords} js8spotter spotter history messages expect alert profile search"
                source_label = archive.source_db
                try:
                    if source_label:
                        source_label = Path(source_label).name
                except Exception:
                    pass
                detail_lines = [
                    "JS8Spotter History",
                    "",
                    f"Type: {archive.source_table}",
                    f"Source ID: {archive.source_id}",
                    f"Source DB: {archive.source_db or 'Unknown'}",
                    f"Imported: {datetime.datetime.fromtimestamp(archive.imported_ts).strftime('%Y-%m-%d %H:%M') if archive.imported_ts else 'Unknown'}",
                    "",
                    "Summary:",
                    archive.subtitle or archive.title,
                    "",
                    "Archived Payload:",
                    json.dumps(archive.payload, indent=2, sort_keys=True, default=str),
                ]
                self._quick_search_add_record(
                    records,
                    category="Spotter History",
                    title=archive.title,
                    subtitle=" | ".join(part for part in (f"{table}: {archive.subtitle}" if archive.subtitle else table, source_label) if part),
                    screen=screen,
                    action="spotter_archive_detail",
                    keywords=keywords,
                    detail_text="\n".join(detail_lines),
                )
        except Exception:
            pass
        try:
            try:
                assigned_schedule_status = self._station_health_assigned_schedule_status_rows()
            except Exception:
                assigned_schedule_status = []
            report = build_station_readiness_report(
                self.settings.all(),
                device_profiles=self.multi_radio_store.list_device_profiles(),
                operating_groups=load_operating_groups(self.settings),
                assigned_schedule_status=assigned_schedule_status,
            )
            for issue in list(getattr(report, "issues", ()) or ())[:20]:
                self._quick_search_add_record(
                    records,
                    category="Issue",
                    title=format_readiness_issue(issue),
                    subtitle="Station Health / Settings",
                    screen="Station Health",
                    action="health",
                    section_key=str(getattr(issue, "section_key", "") or ""),
                    radio_id=int(getattr(issue, "radio_id", 0) or 0),
                    keywords=f"{getattr(issue, 'severity', '')} {getattr(issue, 'scope', '')}",
                )
        except Exception:
            pass
        self._quick_search_cache = (now, [dict(row) for row in records])
        return records

    def quick_search(self, query: str, *, limit: int = 16) -> list[dict[str, object]]:
        terms = [part.strip().upper() for part in str(query or "").split() if part.strip()]
        if not terms:
            return []
        matches: list[tuple[int, int, dict[str, object]]] = []
        for idx, record in enumerate(self._build_quick_search_records()):
            blob = self._quick_search_blob(record)
            if not all(term in blob for term in terms):
                continue
            title = str(record.get("title", "") or "").upper()
            category = str(record.get("category", "") or "")
            score = 100 if title == " ".join(terms) else 80 if title.startswith(terms[0]) else 50
            if category in {"Issue", "Action", "Radio"}:
                score += 10
            matches.append((score, idx, record))
        matches.sort(key=lambda item: (-item[0], item[1]))
        return [dict(record) for _score, _idx, record in matches[: max(1, int(limit or 16))]]

    def _activate_quick_search_result(self, record: Mapping[str, object]) -> None:
        action = str(record.get("action", "screen") or "screen").strip()
        screen = str(record.get("screen", "") or "").strip()
        if action == "settings":
            self.open_settings_section(
                str(record.get("section_key", "") or "operator_info"),
                radio_id=int(record.get("radio_id", 0) or 0),
            )
            return
        if action == "messages":
            self.open_messages_mode(str(record.get("message_mode", "") or "inbox"))
            return
        if action == "health":
            self._open_station_health_detail(
                device_profile_id=int(record.get("radio_id", 0) or 0),
                scope_name=str(record.get("section_key", "") or ""),
            )
            return
        if action == "spotter_archive_detail":
            detail = str(record.get("detail_text", "") or "").strip()
            if detail:
                QMessageBox.information(self, str(record.get("title", "") or "Spotter History"), detail)
                return
        idx = self._screen_index_by_label.get(screen, -1)
        if idx >= 0:
            self._set_screen(idx)

    def show_quick_search_results(self, query: str, anchor: QWidget | None = None) -> bool:
        results = self.quick_search(query)
        if not results:
            try:
                service = getattr(self, "action_feedback_service", None)
                if service is not None:
                    service.publish(
                        scope="global",
                        status="info",
                        summary=f"No CommsPlan results for '{str(query or '').strip()}'.",
                        detail="Try a radio name, schedule, group, frequency, setting, message action, or setup issue.",
                        source_surface="quick_search",
                    )
            except Exception:
                pass
            return False
        anchor_widget = anchor or self
        menu = QMenu(anchor_widget)
        last_category = ""
        for record in results:
            category = str(record.get("category", "Result") or "Result")
            if category != last_category:
                header = QAction(category, menu)
                header.setEnabled(False)
                menu.addAction(header)
                last_category = category
            title = str(record.get("title", "") or "")
            subtitle = str(record.get("subtitle", "") or "")
            action = QAction(f"{title}  -  {subtitle}" if subtitle else title, menu)
            action.setData(dict(record))
            menu.addAction(action)
        selected = menu.exec(anchor_widget.mapToGlobal(anchor_widget.rect().bottomLeft()))
        if selected is None or not selected.isEnabled():
            return True
        data = selected.data()
        if isinstance(data, Mapping):
            self._activate_quick_search_result(data)
        return True

    def _open_command_palette(self) -> None:
        query, accepted = QInputDialog.getText(
            self,
            "Find in CommsPlan",
            "Screen, setting, radio, schedule, action, or issue:",
        )
        if accepted and str(query or "").strip():
            self.show_quick_search_results(str(query).strip(), self)

    def _style_station_command_bar(self, theme: dict) -> None:
        if not hasattr(self, "station_command_bar"):
            return
        border = theme.get("station_control_border", theme.get("accent", "#2E6F9E"))
        surface = theme.get("station_control_surface", theme.get("surface_alt", "#F6F8FA"))
        text = theme.get("station_control_text", theme.get("text", "#222222"))
        muted = theme.get("station_control_muted", theme.get("text_muted", "#6A737D"))
        tile_surface = theme.get("station_control_tile_surface", theme.get("surface", "#FFFFFF"))
        tile_selected_surface = theme.get("station_control_tile_selected_surface", theme.get("surface", "#FFFFFF"))
        tile_border = theme.get("station_control_tile_border", theme.get("border", "#D3D7DD"))
        tile_selected_border = theme.get("station_control_tile_selected_border", border)
        chevron_path = (Path(__file__).resolve().parents[2] / "assets" / "dropdown-chevron.svg").as_posix()
        station_command_bar_style = (
            "QFrame#stationCommandBar {"
            f"background: {surface}; border: 1px solid {border}; border-bottom: 2px solid {border}; border-radius: 6px;"
            "}"
            "QFrame#stationCommandBar QLabel {"
            f"background: transparent; color: {text};"
            "}"
            "QFrame#stationCommandRadioTile {"
            f"background: {tile_surface}; border: 1px solid {tile_border}; border-radius: 6px;"
            "}"
            "QFrame#stationCommandRadioTile[selected=\"true\"] {"
            f"background: {tile_selected_surface}; border: 2px solid {tile_selected_border};"
            "}"
            "QFrame#stationCommandPrimaryContext {"
            f"background: {tile_surface}; border: 1px solid {tile_border}; border-radius: 6px;"
            "}"
            "QLabel#stationCommandPrimaryNow {"
            f"color: {text}; font-weight: 800; padding: 2px 5px;"
            "}"
            "QLabel#stationCommandPrimaryState {"
            f"color: {muted}; font-weight: 600;"
            "}"
            "QFrame#stationCommandClock {"
            f"border-left: 1px solid {tile_border};"
            "}"
            "QLabel#stationCommandLocalTime {"
            f"color: {text}; font-weight: 700;"
            "}"
            "QLabel#stationCommandUtcTime, QLabel#stationCommandControlsDrawerLabel {"
            f"color: {muted}; font-weight: 600;"
            "}"
            "QLabel#stationCommandRadioTileNow {"
            f"color: {text}; font-weight: 800;"
            "}"
            "QLabel#stationCommandRadioTileState, QLabel#stationCommandRadioTileNext {"
            f"color: {muted};"
            "}"
            "QPushButton#stationCommandRadioTileHealth {"
            "padding-left: 8px; padding-right: 8px;"
            "}"
            "QComboBox#stationCommandRadioTileFrequency {"
            f"background: {theme.get('surface', '#FFFFFF')}; color: {text};"
            f"border: 1px solid {tile_border}; border-radius: 5px; padding: 4px 34px 4px 8px; font-weight: 800;"
            "}"
            "QComboBox#stationCommandRadioTileFrequency::drop-down {"
            f"border-left: 1px solid {tile_border}; width: 28px; subcontrol-origin: padding; subcontrol-position: top right;"
            "}"
            "QComboBox#stationCommandRadioTileFrequency::down-arrow {"
            f"image: url({chevron_path}); width: 12px; height: 8px;"
            "}"
            "QComboBox#stationCommandRadioTileDuration {"
            f"background: {theme.get('surface', '#FFFFFF')}; color: {muted};"
            f"border: 1px solid {tile_border}; border-radius: 5px; padding: 4px 24px 4px 8px;"
            "}"
            "QComboBox#stationCommandRadioTileDuration::drop-down {"
            "border: none; width: 22px;"
            "}"
            "QToolButton#stationCommandRadioTileTimedSuspend::menu-button,"
            "QToolButton#stationCommandRadioTileSchedulerSuspend::menu-button {"
            f"border-left: 1px solid {tile_border}; width: 16px;"
            "}"
            "QToolButton#stationCommandRadioTileTimedSuspend::menu-indicator,"
            "QToolButton#stationCommandRadioTileSchedulerSuspend::menu-indicator {"
            f"image: url({chevron_path}); width: 8px; height: 6px; right: 4px;"
            "}"
        )
        if station_command_bar_style != getattr(self, "_station_command_bar_style_signature", ""):
            self.station_command_bar.setStyleSheet(station_command_bar_style)
            self._station_command_bar_style_signature = station_command_bar_style
        for label in (
            getattr(self, "station_command_radio_label", None),
            getattr(self, "station_command_now_caption", None),
            getattr(self, "station_command_action_label", None),
            getattr(self, "station_command_state_label", None),
            getattr(self, "station_command_next_label", None),
            getattr(self, "station_command_health_label", None),
            getattr(self, "station_command_radio_summary_label", None),
        ):
            if label is not None:
                label.setStyleSheet(f"background: transparent; color: {text}; font-weight: 600;")
        for label in getattr(self, "station_command_health_text_labels", {}).values():
            try:
                label.setStyleSheet(f"background: transparent; color: {text};")
            except Exception:
                pass
        if getattr(self, "station_command_now_label", None) is not None:
            self.station_command_now_label.setStyleSheet(
                "QLabel#stationCommandNow {"
                f"background: {theme.get('surface', '#FFFFFF')}; color: {text};"
                f"border: 1px solid {border}; border-radius: 5px; padding: 4px 10px; font-weight: 800;"
                "}"
            )
        if getattr(self, "station_command_radio_combo", None) is not None:
            self.station_command_radio_combo.setStyleSheet(
                "QComboBox#stationCommandRadioSelector {"
                f"background: {theme.get('surface', '#FFFFFF')}; color: {text};"
                f"border: 1px solid {border}; border-radius: 5px; padding: 4px 28px 4px 10px; font-weight: 800;"
                "}"
                "QComboBox#stationCommandRadioSelector::drop-down {"
                "border: none; width: 24px;"
                "}"
                "QComboBox#stationCommandRadioSelector QAbstractItemView {"
                f"background: {theme.get('surface', '#FFFFFF')}; color: {text}; selection-background-color: {border};"
                "}"
            )
        if getattr(self, "station_command_duration_combo", None) is not None:
            self.station_command_duration_combo.setStyleSheet(f"color: {muted};")
        if getattr(self, "station_command_radio_summary_scroll", None) is not None:
            self.station_command_radio_summary_scroll.setStyleSheet(
                "QScrollArea#stationCommandRadioSummaryScroll {background: transparent; border: none;}"
                "QWidget#stationCommandRadioSummary {background: transparent;}"
            )
        if getattr(self, "station_command_radio_admin_panel", None) is not None:
            self.station_command_radio_admin_panel.setStyleSheet("QWidget#stationCommandRadioAdminPanel {background: transparent;}")
        manual_qsy_active = self._station_command_scheduler_manual_qsy_active()
        scheduler_suspended_manual = self._station_command_scheduler_suspended_manually()
        try:
            timed_hold_active = bool((getattr(self, "_hold_state_snapshot", None) or {}).get("active"))
        except Exception:
            timed_hold_active = False
        button_roles = (
            (getattr(self, "station_command_qsy_btn", None), "warning" if manual_qsy_active else "info"),
            (getattr(self, "station_command_hold_btn", None), "warning" if timed_hold_active else "info"),
            (getattr(self, "station_command_suspend_btn", None), "warning" if scheduler_suspended_manual else "muted"),
            (
                getattr(self, "station_command_resume_btn", None),
                "warning" if manual_qsy_active or scheduler_suspended_manual or timed_hold_active else "muted",
            ),
            (
                getattr(self, "station_command_radio_admin_btn", None),
                "info" if bool(getattr(self, "_station_command_radio_admin_expanded", False)) else "muted",
            ),
            (getattr(self, "station_command_radio_prev_btn", None), "muted"),
            (getattr(self, "station_command_radio_next_btn", None), "muted"),
        )
        for btn, role in button_roles:
            if btn is not None:
                btn.setStyleSheet(button_style(role, theme) if btn.isEnabled() else button_style("muted", theme) + f" color: {muted};")
        for sep in (
            getattr(self, "station_command_radio_separator", None),
            getattr(self, "station_command_now_separator", None),
        ):
            if sep is not None:
                sep.setStyleSheet(f"color: {border}; background: transparent;")

    def _station_command_layout_mode_for_width(self, width: int) -> str:
        try:
            return "compact" if int(width) < 1100 else "wide"
        except Exception:
            return "wide"

    def _apply_station_command_bar_layout(self, *, force: bool = False) -> None:
        if not hasattr(self, "station_command_layout"):
            return
        width = int(getattr(self.station_command_bar, "width", lambda: 0)() or self.width() or 0)
        mode = self._station_command_layout_mode_for_width(width)
        card_mode = bool(getattr(self, "_station_command_multi_mode_active", False))
        layout_signature = (mode, card_mode)
        if not force and layout_signature == getattr(self, "_station_command_layout_signature", None):
            return
        self._station_command_layout_mode = mode
        self._station_command_layout_signature = layout_signature
        layout = self.station_command_layout
        for widget in (
            self.station_command_radio_label,
            self.station_command_radio_combo,
            getattr(self, "station_command_radio_separator", None),
            getattr(self, "station_command_now_caption", None),
            self.station_command_now_label,
            getattr(self, "station_command_freq_combo", None),
            self.station_command_state_label,
            getattr(self, "station_command_now_separator", None),
            getattr(self, "station_command_action_label", None),
            self.station_command_next_label,
            getattr(self, "station_command_health_label", None),
            getattr(self, "station_command_health_widget", None),
            self.station_command_duration_combo,
            self.station_command_qsy_btn,
            self.station_command_hold_btn,
            self.station_command_suspend_btn,
            self.station_command_resume_btn,
            getattr(self, "station_command_radio_summary_label", None),
            getattr(self, "station_command_radio_prev_btn", None),
            getattr(self, "station_command_radio_summary_scroll", None),
            getattr(self, "station_command_radio_next_btn", None),
            getattr(self, "station_command_radio_admin_btn", None),
            getattr(self, "station_command_radio_admin_panel", None),
        ):
            if widget is not None:
                layout.removeWidget(widget)
        for col in range(16):
            layout.setColumnStretch(col, 0)

        if card_mode and hasattr(self, "station_command_radio_summary_scroll"):
            for widget in (
                self.station_command_radio_label,
                self.station_command_radio_combo,
                getattr(self, "station_command_radio_separator", None),
                getattr(self, "station_command_now_caption", None),
                self.station_command_now_label,
                getattr(self, "station_command_freq_combo", None),
                self.station_command_state_label,
                getattr(self, "station_command_now_separator", None),
                getattr(self, "station_command_action_label", None),
                self.station_command_next_label,
                getattr(self, "station_command_health_label", None),
                getattr(self, "station_command_health_widget", None),
                self.station_command_duration_combo,
                self.station_command_qsy_btn,
                self.station_command_hold_btn,
                self.station_command_suspend_btn,
                self.station_command_resume_btn,
                getattr(self, "station_command_radio_summary_label", None),
                getattr(self, "station_command_radio_prev_btn", None),
                getattr(self, "station_command_radio_next_btn", None),
                getattr(self, "station_command_radio_admin_btn", None),
                getattr(self, "station_command_radio_admin_panel", None),
            ):
                if widget is not None and widget is not self.station_command_radio_summary_scroll:
                    widget.setVisible(False)
            self.station_command_radio_summary_scroll.setVisible(True)
            layout.addWidget(self.station_command_radio_summary_scroll, 0, 0, 1, 16)
            layout.setColumnStretch(15, 1)
            return

        if mode == "compact":
            self.station_command_now_label.setWordWrap(False)
            self.station_command_state_label.setWordWrap(False)
            self.station_command_next_label.setWordWrap(False)
            self.station_command_radio_separator.setVisible(False)
            self.station_command_now_separator.setVisible(False)
            layout.addWidget(self.station_command_radio_label, 0, 0)
            layout.addWidget(self.station_command_radio_combo, 0, 1)
            layout.addWidget(self.station_command_now_caption, 0, 2)
            layout.addWidget(self.station_command_now_label, 0, 3)
            layout.addWidget(self.station_command_state_label, 0, 4)
            layout.addWidget(self.station_command_action_label, 1, 0)
            layout.addWidget(self.station_command_freq_combo, 1, 1, 1, 2)
            layout.addWidget(self.station_command_qsy_btn, 1, 3)
            layout.addWidget(self.station_command_suspend_btn, 1, 4)
            layout.addWidget(self.station_command_duration_combo, 2, 1)
            layout.addWidget(self.station_command_hold_btn, 2, 3)
            layout.addWidget(self.station_command_resume_btn, 2, 4)
            if hasattr(self, "station_command_health_label") and hasattr(self, "station_command_health_widget"):
                layout.addWidget(self.station_command_health_label, 3, 0)
                layout.addWidget(self.station_command_health_widget, 3, 1)
            layout.addWidget(self.station_command_next_label, 3, 2, 1, 3)
            if hasattr(self, "station_command_radio_summary_label") and hasattr(self, "station_command_radio_summary_scroll"):
                layout.addWidget(self.station_command_radio_summary_label, 4, 0)
                if hasattr(self, "station_command_radio_prev_btn"):
                    layout.addWidget(self.station_command_radio_prev_btn, 4, 1)
                layout.addWidget(self.station_command_radio_summary_scroll, 4, 2, 1, 2)
                if hasattr(self, "station_command_radio_next_btn"):
                    layout.addWidget(self.station_command_radio_next_btn, 4, 4)
            if hasattr(self, "station_command_radio_admin_btn"):
                layout.addWidget(self.station_command_radio_admin_btn, 4, 5)
            if hasattr(self, "station_command_radio_admin_panel"):
                layout.addWidget(self.station_command_radio_admin_panel, 5, 0, 1, 5)
            layout.setColumnStretch(4, 1)
        else:
            self.station_command_radio_separator.setVisible(True)
            self.station_command_now_separator.setVisible(True)
            layout.addWidget(self.station_command_radio_label, 0, 0)
            layout.addWidget(self.station_command_radio_combo, 0, 1)
            layout.addWidget(self.station_command_radio_separator, 0, 2, 1, 1)
            layout.addWidget(self.station_command_now_caption, 0, 3)
            layout.addWidget(self.station_command_now_label, 0, 4)
            layout.addWidget(self.station_command_state_label, 0, 5)
            layout.addWidget(self.station_command_now_separator, 0, 6, 1, 1)
            layout.addWidget(self.station_command_action_label, 0, 7)
            layout.addWidget(self.station_command_freq_combo, 0, 8, 1, 2)
            layout.addWidget(self.station_command_qsy_btn, 0, 10)
            layout.addWidget(self.station_command_suspend_btn, 0, 12)
            layout.addWidget(self.station_command_duration_combo, 1, 8, 1, 2)
            layout.addWidget(self.station_command_hold_btn, 1, 10)
            layout.addWidget(self.station_command_resume_btn, 1, 12)
            if hasattr(self, "station_command_health_label") and hasattr(self, "station_command_health_widget"):
                layout.addWidget(self.station_command_health_label, 1, 0)
                layout.addWidget(self.station_command_health_widget, 1, 1)
            layout.addWidget(self.station_command_next_label, 1, 3, 1, 7)
            if hasattr(self, "station_command_radio_summary_label") and hasattr(self, "station_command_radio_summary_scroll"):
                layout.addWidget(self.station_command_radio_summary_label, 2, 0)
                if hasattr(self, "station_command_radio_prev_btn"):
                    layout.addWidget(self.station_command_radio_prev_btn, 2, 1)
                layout.addWidget(self.station_command_radio_summary_scroll, 2, 2, 1, 9)
                if hasattr(self, "station_command_radio_next_btn"):
                    layout.addWidget(self.station_command_radio_next_btn, 2, 11)
            if hasattr(self, "station_command_radio_admin_btn"):
                layout.addWidget(self.station_command_radio_admin_btn, 2, 12)
            if hasattr(self, "station_command_radio_admin_panel"):
                layout.addWidget(self.station_command_radio_admin_panel, 3, 0, 1, 13)
            layout.setColumnStretch(9, 1)
            layout.setColumnStretch(15, 2)

    def _apply_app_theme(self, *, force: bool = False):
        app = QApplication.instance()
        try:
            self.settings.reload()
        except Exception:
            pass
        theme = resolve_theme(self.settings)
        ui_text_scale = resolve_ui_text_scale(self.settings)
        appearance_signature = (
            tuple(sorted((str(key), repr(value)) for key, value in theme.items())),
            round(float(ui_text_scale), 4),
        )
        if not force and appearance_signature == getattr(self, "_applied_appearance_signature", None):
            log.debug("UI_THEME|unchanged application theme refresh skipped")
            return
        apply_app_theme(app, theme, ui_text_scale=ui_text_scale)
        self._applied_appearance_signature = appearance_signature
        fit_child_combo_boxes(self)
        self._set_logo_pixmap()
        self._update_log_indicator()
        if self._context_help_dialog is not None:
            try:
                self._context_help_dialog.apply_theme()
            except Exception:
                pass
        try:
            if hasattr(self, "condition_levels_edit_btn"):
                self._style_condition_levels_edit_action(theme)
        except Exception:
            pass
        try:
            self._style_ledge_clock(theme)
        except Exception:
            pass
        try:
            self._style_station_command_bar(theme)
        except Exception:
            pass
        try:
            self._style_compact_navigation(theme)
        except Exception:
            pass
        if hasattr(self, "map_prop_badge"):
            try:
                self.map_prop_badge.setStyleSheet(
                    f"font-weight: bold; color: {theme.get('info', theme.get('accent', '#1E88E5'))};"
                )
            except Exception:
                pass
        for widget in (
            self.freq_planner_tab,
            self.sop_tab,
            self.hf_schedule_tab,
            self.net_tab,
            self.fldigi_tab,
            self.js8_tab,
            self.message_viewer_tab,
            self.fio_spotter_tab,
            self.log_tab,
            self.operator_history_tab,
            getattr(self, "call_finder_tab", None),
            self.local_operator_tab,
            self.local_report_history_tab,
            self.local_ncs_tab,
            self.peer_sched_tab,
            self.settings_tab,
            self.controlfreq_tab,
            self.station_overview_tab,
            self.station_health_tab,
            self.help_tab,
        ):
            if widget is None:
                continue
            if hasattr(widget, "apply_theme"):
                try:
                    if widget is self.settings_tab:
                        widget.apply_theme(theme)
                    else:
                        widget.apply_theme()
                except Exception:
                    pass
        map_window = getattr(self, "map_window", None)
        if isinstance(map_window, PersistentMapWindow):
            try:
                map_window.apply_theme(theme)
            except Exception:
                log.debug("MainWindow: failed forwarding theme to Map window", exc_info=True)
        elif self.stations_map_tab is not None:
            try:
                self.stations_map_tab.apply_theme(theme)
            except Exception:
                log.debug("MainWindow: failed applying theme to Map workspace", exc_info=True)
        self._update_ncs_nav_button_styles()
        self._update_nav_layout_metrics()
        self._refresh_condition_level_panel()

    def _placeholder_widget(self, label: str) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(QLabel(f"Loading {label}..."))
        self._lazy_placeholders[label] = w
        return w

    def _start_lazy_prewarm(self) -> None:
        if self._shutting_down:
            return
        self._prewarm_next_lazy_tab()

    @staticmethod
    def _truthy_flag(value: object, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        try:
            raw = str(value).strip().lower()
        except Exception:
            return default
        if raw == "":
            return default
        return raw in {"1", "true", "yes", "on"}

    def _should_prewarm_deferred_screens_at_startup(self) -> bool:
        """Keep deferred screens deferred unless an operator explicitly opts in."""
        try:
            raw = self.settings.get("startup_deferred_screen_prewarm", None)
        except Exception:
            raw = None
        return MainWindow._truthy_flag(raw, False)

    def _show_tab_loading_notice(self, text: str) -> None:
        try:
            self.statusBar().showMessage(str(text or "Preparing..."), 2500)
        except Exception:
            pass

    def _hide_tab_loading_notice(self) -> None:
        try:
            self.statusBar().clearMessage()
        except Exception:
            pass

    def _restore_nav_selection_to_active_tab(self) -> None:
        try:
            idx = self._active_tab_index
            if idx is None:
                return
            label = self._screens[idx][0] if 0 <= idx < len(self._screens) else ""
            if label == "Settings":
                nav_idx = self._settings_nav_button_indices.get(
                    str(getattr(self, "_settings_nav_context", "main") or "main")
                )
            elif label == "Resources":
                nav_idx = self._resources_nav_button_indices.get(
                    str(getattr(self, "_resources_nav_context", "frequency_catalog") or "frequency_catalog")
                )
            else:
                nav_idx = self._nav_screen_index_map.get(idx)
            if nav_idx is None or not (0 <= nav_idx < len(self.nav_buttons)):
                return
            btn = self.nav_buttons[nav_idx]
            if not btn.isChecked():
                btn.setChecked(True)
        except Exception:
            pass

    def _prewarm_next_lazy_tab(self) -> None:
        if self._shutting_down:
            return
        if self._lazy_prewarm_index >= len(self._lazy_prewarm_labels):
            return
        label = self._lazy_prewarm_labels[self._lazy_prewarm_index]
        self._lazy_prewarm_index += 1
        try:
            idx = next((i for i, (name, _w) in enumerate(self._screens) if name == label), -1)
            if idx >= 0:
                self._ensure_lazy_tab_loaded(label, idx)
        except Exception:
            pass
        QTimer.singleShot(1500, self._prewarm_next_lazy_tab)

    def _create_freq_planner_tab(self) -> QWidget:
        with perf_span(
            "main_window.create_freq_planner_tab",
            settings=self.settings,
            min_ms=5.0,
        ):
            self.freq_planner_tab = FreqPlannerTab(self, plan_context_service=self.plan_context_service)
            try:
                self.settings_tab.settings_saved.connect(self.freq_planner_tab.on_settings_saved)
            except Exception:
                pass
            self._connect_lazy_screen_signal(
                "freq_planner.scheduler",
                self.freq_planner_tab.schedule_saved,
                self.scheduler.force_refresh,
            )
            return self.freq_planner_tab

    def _create_message_viewer_tab(self) -> QWidget:
        with perf_span(
            "main_window.create_message_viewer_tab",
            settings=self.settings,
            min_ms=5.0,
        ):
            self.message_viewer_tab = MessageViewerTab(self, plan_context_service=self.plan_context_service)
            try:
                self.settings_tab.settings_saved.connect(self.message_viewer_tab.on_settings_saved)
            except Exception:
                pass
            try:
                self.message_viewer_tab.busyStateChanged.connect(self._set_heavy_content_refresh_active)
            except Exception:
                pass
            try:
                self.message_viewer_tab.meshSendRequested.connect(self._request_mesh_send)
            except Exception:
                pass
            return self.message_viewer_tab

    def _connect_lazy_screen_signal(self, key: str, signal: object, slot: object) -> None:
        """Connect a deferred-screen signal once, after both endpoints exist."""
        connected = getattr(self, "_lazy_screen_signal_keys", None)
        if connected is None:
            connected = set()
            self._lazy_screen_signal_keys = connected
        if key in connected or signal is None or not callable(slot):
            return
        try:
            signal.connect(slot)
            connected.add(key)
        except Exception as exc:
            log.debug("MainWindow deferred screen signal wiring failed: %s: %s", key, exc)

    def _create_hf_schedule_tab(self) -> QWidget:
        with perf_span("main_window.create_hf_schedule_tab", settings=self.settings, min_ms=5.0):
            tab = DailyScheduleTab(self, plan_context_service=self.plan_context_service)
            self.hf_schedule_tab = tab
            self._connect_lazy_screen_signal(
                "settings_saved.hf_schedule",
                self.settings_tab.settings_saved,
                tab.on_settings_saved,
            )
            self._connect_lazy_screen_signal(
                "hf_schedule.freq_planner",
                tab.schedule_saved,
                self._refresh_freq_planner_if_loaded,
            )
            self._connect_lazy_screen_signal(
                "hf_schedule.scheduler",
                tab.schedule_saved,
                self.scheduler.force_refresh,
            )
            self._connect_lazy_screen_signal(
                "hf_schedule.sop",
                tab.schedule_saved,
                getattr(self.sop_tab, "on_hf_schedule_saved", None),
            )
            return tab

    def _create_net_schedule_tab(self) -> QWidget:
        with perf_span("main_window.create_net_schedule_tab", settings=self.settings, min_ms=5.0):
            tab = NetScheduleTab(self, plan_context_service=self.plan_context_service)
            self.net_tab = tab
            self._connect_lazy_screen_signal(
                "settings_saved.net_schedule",
                self.settings_tab.settings_saved,
                tab.on_settings_saved,
            )
            self._connect_lazy_screen_signal(
                "net_schedule.freq_planner",
                tab.schedule_saved,
                self._refresh_freq_planner_if_loaded,
            )
            self._connect_lazy_screen_signal(
                "net_schedule.scheduler",
                tab.schedule_saved,
                self.scheduler.force_refresh,
            )
            return tab

    def _create_local_nets_tab(self) -> QWidget:
        """Construct the reminder-only Local Nets workspace on first visit."""
        from freqinout.gui.local_nets_tab import LocalNetsTab

        with perf_span("main_window.create_local_nets_tab", settings=self.settings, min_ms=5.0):
            tab = LocalNetsTab(self, settings=self.settings)
            self.local_nets_tab = tab
            self._connect_lazy_screen_signal(
                "local_nets.resources",
                getattr(tab, "open_resources_requested", None),
                lambda section="frequency_catalog": self.open_resources_section(str(section)),
            )
            self._connect_lazy_screen_signal(
                "local_nets.settings",
                getattr(tab, "open_settings_requested", None),
                lambda _section=None: self.open_settings_section(
                    "operating_groups", settings_nav_context="main"
                ),
            )
            self._connect_lazy_screen_signal(
                "local_nets.navigation",
                getattr(tab, "navigation_requested", None),
                self._open_navigation_intent,
            )
            return tab

    def _create_fldigi_ncs_tab(self) -> QWidget:
        with perf_span("main_window.create_fldigi_ncs_tab", settings=self.settings, min_ms=5.0):
            tab = FldigiNetControlTab(self)
            self.fldigi_tab = tab
            self._connect_lazy_screen_signal(
                "settings_saved.fldigi_ncs",
                self.settings_tab.settings_saved,
                tab.on_settings_saved,
            )
            self._connect_lazy_screen_signal(
                "fldigi_ncs.net_status",
                getattr(tab, "net_status_changed", None),
                self._on_ncs_net_status_changed,
            )
            self._connect_lazy_screen_signal(
                "fldigi_ncs.net_session_status",
                getattr(tab, "net_session_status_changed", None),
                self._on_ncs_scoped_net_status_changed,
            )
            return tab

    def _create_js8_ncs_tab(self) -> QWidget:
        with perf_span("main_window.create_js8_ncs_tab", settings=self.settings, min_ms=5.0):
            tab = JS8CallNetControlTab(self)
            self.js8_tab = tab
            self._connect_lazy_screen_signal(
                "settings_saved.js8_ncs",
                self.settings_tab.settings_saved,
                tab.on_settings_saved,
            )
            self._connect_lazy_screen_signal(
                "js8_ncs.net_status",
                getattr(tab, "net_status_changed", None),
                self._on_ncs_net_status_changed,
            )
            self._connect_lazy_screen_signal(
                "js8_ncs.net_session_status",
                getattr(tab, "net_session_status_changed", None),
                self._on_ncs_scoped_net_status_changed,
            )
            return tab

    def _create_local_ncs_tab(self) -> QWidget:
        with perf_span("main_window.create_local_ncs_tab", settings=self.settings, min_ms=5.0):
            tab = LocalNCSTab(self)
            self.local_ncs_tab = tab
            self._connect_lazy_screen_signal(
                "settings_saved.local_ncs",
                self.settings_tab.settings_saved,
                tab.on_settings_saved,
            )
            self._connect_lazy_screen_signal(
                "local_ncs.net_status",
                getattr(tab, "net_status_changed", None),
                self._on_ncs_net_status_changed,
            )
            self._wire_lazy_local_data_links()
            return tab

    def _create_station_overview_tab(self) -> QWidget:
        with perf_span("main_window.create_station_overview_tab", settings=self.settings, min_ms=5.0):
            tab = StationOverviewTab(self)
            tab.set_runtime_manager(self.station_runtime_manager)
            scheduler = getattr(self, "scheduler", None)
            if scheduler is not None and hasattr(scheduler, "get_endpoint_operational_summaries"):
                tab.set_endpoint_summary_provider(scheduler.get_endpoint_operational_summaries)
            self.station_overview_tab = tab
            try:
                self.station_overview_tab.health_details_requested.connect(self._open_station_health_detail)
            except Exception as exc:
                log.debug("MainWindow deferred screen signal wiring failed: station overview health details: %s", exc)
            return tab

    def _create_station_health_tab(self) -> QWidget:
        with perf_span("main_window.create_station_health_tab", settings=self.settings, min_ms=5.0):
            tab = StationHealthTab(self)
            tab.set_scope_resolver(self._station_health_scope_resolver)
            tab.set_runtime_item_provider(self._station_health_runtime_items)
            tab.set_runtime_source_provider(self._station_health_runtime_source_rows)
            self._connect_lazy_screen_signal(
                "station_health.related_view_requested",
                getattr(tab, "related_view_requested", None),
                self._open_station_health_runtime_source_related_view,
            )
            self.station_health_tab = tab
            return tab

    def _create_station_bbs_tab(self) -> QWidget:
        with perf_span("main_window.create_station_bbs_tab", settings=self.settings, min_ms=5.0):
            tab = StationBbsTab(self, settings=self.settings)
            tab.catalog_published.connect(self._on_station_bbs_catalog_published)
            self.station_bbs_tab = tab
            return tab

    def _on_station_bbs_catalog_published(self, snapshot) -> None:
        """Publish to the separate Settings view without a synchronous reload."""
        tab = getattr(self, 'settings_tab', None)
        publish = getattr(getattr(tab, 'settings', None), 'publish_station_bbs_catalog', None)
        if callable(publish):
            publish(snapshot)
            tab._last_station_readiness_report = None
            tab._refresh_section_titles()

    def _create_resources_tab(self) -> QWidget:
        from freqinout.gui.resources_tab import ResourcesTab

        with perf_span("main_window.create_resources_tab", settings=self.settings, min_ms=5.0):
            tab = ResourcesTab(self)
            tab.add_to_hf_nets_requested.connect(self.open_hf_net_subscription)
            tab.open_hf_schedule_requested.connect(self.open_hf_net_schedule_for_session)
            tab.return_requested.connect(self._return_navigation_intent)
            self.resources_tab = tab
            return tab

    def _create_shortwave_tab(self) -> QWidget:
        from freqinout.gui.shortwave_tab import ShortwaveWorkspace

        with perf_span("main_window.create_shortwave_tab", settings=self.settings, min_ms=5.0):
            tab = ShortwaveWorkspace(
                self,
                db_path=get_config_dir() / "config" / "freqinout_nets.db",
                receiver_profiles_provider=self._shortwave_receiver_profiles,
            )
            self.shortwave_tab = tab
            return tab

    def _create_fio_spotter_tab(self) -> QWidget:
        with perf_span("main_window.create_fio_spotter_tab", settings=self.settings, min_ms=5.0):
            tab = FioSpotterTab(
                self,
                settings=self.settings,
                open_compose=lambda intent=None: self.open_messages_section(
                    "compose", compose_intent=intent
                ),
                open_inbox=lambda row: self.open_messages_section(
                    "inbox",
                    query_filter=str(row.get("from_call") or row.get("group_name") or ""),
                    source_family=str(row.get("source_family") or ""),
                ),
                open_map=lambda row: self.open_spotter_map(
                    group_filter=str(row.get("group_name") or ""),
                    query_filter=str(row.get("from_call") or ""),
                    state_filter=str(row.get("state_code") or ""),
                    grid_filter=str(row.get("grid") or ""),
                ),
                open_operator=lambda row: self.open_hf_operator(
                    str(row.get("from_call") or "")
                ),
            )
            self.fio_spotter_tab = tab
            return tab

    def _create_operator_history_tab(self) -> QWidget:
        with perf_span("main_window.create_operator_history_tab", settings=self.settings, min_ms=5.0):
            tab = OperatorHistoryTab(self)
            self.operator_history_tab = tab
            self._connect_lazy_screen_signal(
                "settings_saved.operator_history",
                getattr(self.settings_tab, "settings_saved", None),
                getattr(tab, "on_settings_saved", None),
            )
            self._connect_lazy_screen_signal(
                "operator_history.updated",
                getattr(tab, "operator_history_updated", None),
                self._on_operator_history_local_update,
            )
            return tab

    def _create_local_operator_tab(self) -> QWidget:
        with perf_span("main_window.create_local_operator_tab", settings=self.settings, min_ms=5.0):
            tab = LocalOperatorTab(self)
            self.local_operator_tab = tab
            self._connect_lazy_screen_signal(
                "settings_saved.local_operator",
                getattr(self.settings_tab, "settings_saved", None),
                tab.on_settings_saved,
            )
            self._connect_lazy_screen_signal(
                "local_operator.reports_requested",
                getattr(tab, "local_reports_requested", None),
                self.open_local_reports,
            )
            self._wire_lazy_local_data_links()
            return tab

    def _create_call_finder_tab(self) -> QWidget:
        from freqinout.gui.call_finder_tab import CallFinderTab
        tab = CallFinderTab(self)
        self.call_finder_tab = tab
        tab.add_operator_requested.connect(self._review_fcc_operator)
        return tab

    def _review_fcc_operator(self, record) -> None:
        destination, accepted = QInputDialog.getItem(
            self, "Add callsign reference to Operators", "Review in:",
            ["HF Callsigns", "Local Callsigns"], 0 if record.service == "Amateur" else 1, False,
        )
        if not accepted:
            return
        screen = "HF Operators" if destination == "HF Callsigns" else "Local Operators"
        index = self._screen_index_by_label[screen]
        self._ensure_lazy_tab_loaded(screen, index)
        tab = self.stack.widget(index)
        tab.review_fcc_license(record)

    def _create_local_report_history_tab(self) -> QWidget:
        with perf_span("main_window.create_local_report_history_tab", settings=self.settings, min_ms=5.0):
            tab = LocalReportHistoryTab(self)
            self.local_report_history_tab = tab
            self._connect_lazy_screen_signal(
                "settings_saved.local_report_history",
                getattr(self.settings_tab, "settings_saved", None),
                tab.on_settings_saved,
            )
            self._connect_lazy_screen_signal(
                "local_report_history.map_requested",
                getattr(tab, "local_reports_map_requested", None),
                self.open_local_reports_map,
            )
            self._wire_lazy_local_data_links()
            return tab

    def _wire_lazy_local_data_links(self) -> None:
        """Wire only loaded local-data screens; unloaded screens load fresh state."""
        local_ncs = getattr(self, "local_ncs_tab", None)
        if local_ncs is None:
            return
        signal = getattr(local_ncs, "local_data_updated", None)
        self._connect_lazy_screen_signal(
            "local_ncs.reload_lookup",
            signal,
            getattr(local_ncs, "reload_operator_lookup", None),
        )
        local_operators = getattr(self, "local_operator_tab", None)
        if local_operators is not None:
            self._connect_lazy_screen_signal(
                "local_operator.reload_ncs_lookup",
                getattr(local_operators, "local_operator_updated", None),
                getattr(local_ncs, "reload_operator_lookup", None),
            )
            self._connect_lazy_screen_signal(
                "local_ncs.refresh_local_operators",
                signal,
                getattr(local_operators, "_load_data", None),
            )
        local_reports = getattr(self, "local_report_history_tab", None)
        if local_reports is not None:
            self._connect_lazy_screen_signal(
                "local_ncs.refresh_local_reports",
                signal,
                getattr(local_reports, "refresh_reports", None),
            )

    def _create_peer_sched_tab(self) -> QWidget:
        with perf_span("main_window.create_peer_sched_tab", settings=self.settings, min_ms=5.0):
            self.peer_sched_tab = PeerSchedTab(self)
            return self.peer_sched_tab

    def _create_help_tab(self) -> QWidget:
        with perf_span("main_window.create_help_tab", settings=self.settings, min_ms=5.0):
            self.help_tab = HelpTab(self)
            return self.help_tab

    def _create_stations_map_tab(self, parent: QWidget) -> QWidget:
        with perf_span(
            "main_window.create_stations_map_tab",
            settings=self.settings,
            min_ms=5.0,
        ):
            self.stations_map_tab = StationsMapTab(
                parent,
                plan_context_service=self.plan_context_service,
                application_host=self,
            )
            QTimer.singleShot(0, self._sync_map_filters_from_tab)
            return self.stations_map_tab

    def _ensure_map_window(self) -> PersistentMapWindow:
        existing = getattr(self, "map_window", None)
        if isinstance(existing, PersistentMapWindow):
            return existing
        window = PersistentMapWindow(
            self,
            self.settings,
            self._create_stations_map_tab,
        )
        window.content_ready.connect(self._on_map_window_content_ready)
        window.content_failed.connect(self._on_map_window_content_failed)
        window.work_visibility_changed.connect(self._on_map_window_work_visibility_changed)
        window.destroyed.connect(self._on_map_window_destroyed)
        self.map_window = window
        window.apply_theme(resolve_theme(self.settings))
        self._update_map_navigation_state(False)
        return window

    def _open_map_window(self) -> None:
        if self._shutting_down or self._screen_is_runtime_suppressed("Map"):
            return
        try:
            window = self._ensure_map_window()
            window.present()
            self._update_map_navigation_state(window.is_available_for_work())
        except Exception as exc:
            log.exception("MainWindow: failed to open persistent Map window")
            self._update_map_navigation_state(False, error=str(exc))

    def _on_map_window_content_ready(self, tab: object) -> None:
        if self._shutting_down:
            return
        if isinstance(tab, StationsMapTab):
            self.stations_map_tab = tab
        try:
            if hasattr(tab, "set_app_active"):
                tab.set_app_active(self._ui_refresh_allowed())
            if hasattr(tab, "set_map_visible"):
                window = getattr(self, "map_window", None)
                tab.set_map_visible(bool(window is not None and window.is_available_for_work()))
        except Exception:
            log.debug("MainWindow: failed publishing Map window lifecycle", exc_info=True)
        self._apply_pending_map_focus()
        window = getattr(self, "map_window", None)
        self._update_map_navigation_state(bool(window is not None and window.is_available_for_work()))

    def _on_map_window_content_failed(self, error: str) -> None:
        if self._shutting_down:
            return
        self._update_map_navigation_state(False, error=str(error or ""))

    def _on_map_window_work_visibility_changed(self, visible: bool) -> None:
        tab = getattr(self, "stations_map_tab", None)
        if tab is not None and hasattr(tab, "set_map_visible"):
            try:
                tab.set_map_visible(bool(visible))
            except Exception:
                log.debug("MainWindow: failed updating Map work visibility", exc_info=True)
        if not self._shutting_down:
            self._update_map_navigation_state(bool(visible))

    def _on_map_window_destroyed(self, _obj: object | None = None) -> None:
        self.map_window = None
        self.stations_map_tab = None
        if not self._shutting_down:
            self._update_map_navigation_state(False)

    def _update_map_navigation_state(self, visible: bool, *, error: str = "") -> None:
        if error:
            tooltip = "Map unavailable — click to retry opening it in a separate window."
        elif visible:
            tooltip = "Map open — click to bring the separate Map window to front."
        else:
            tooltip = "Open Map in a separate window and keep working in CommsPlan."
        for spec, button in zip(getattr(self, "_nav_specs", ()), getattr(self, "nav_buttons", ())):
            if tuple(spec) == ("Map", "Map"):
                button.setToolTip(tooltip)
                button.setAccessibleName("Map")
                button.setAccessibleDescription(tooltip)
        for button in getattr(self, "nav_compact_buttons", ()):
            try:
                if str(button.accessibleName() or "") == "Map":
                    button.setToolTip(tooltip)
                    button.setAccessibleDescription(tooltip)
            except Exception:
                continue

    def _ensure_lazy_tab_loaded(self, label: str, index: int) -> None:
        with perf_span(
            "main_window.ensure_lazy_tab_loaded",
            settings=self.settings,
            meta={"label": label, "index": index},
            min_ms=5.0,
        ):
            if label not in self._lazy_factories:
                return
            existing = self._get_tab_by_label(label)
            if existing is not None and existing is not self._lazy_placeholders.get(label):
                return
            factory = self._lazy_factories[label]
            try:
                new_widget = factory()
            except Exception:
                log.exception("MainWindow: failed to create deferred screen %r", label)
                return
            placeholder = self._lazy_placeholders.get(label)
            current_widget = self.stack.currentWidget()
            placeholder_index = self.stack.indexOf(placeholder) if placeholder is not None else -1
            insert_index = placeholder_index if placeholder_index >= 0 else index
            # Insert the real page before removing its placeholder.  Removing a
            # current QStackedWidget page first can briefly expose an adjacent
            # page, which users perceive as the recurring "swipe and vanish".
            self.stack.insertWidget(insert_index, new_widget)
            # Finish theme and base layout while the real page is still
            # hidden behind its stable placeholder.  Publishing the page
            # first and styling it afterward can expose two native geometry
            # profiles during a screen's first frame.
            try:
                if hasattr(new_widget, "apply_theme"):
                    new_widget.apply_theme()
                new_widget.ensurePolished()
                new_layout = new_widget.layout()
                if new_layout is not None:
                    new_layout.invalidate()
                    new_layout.activate()
            except Exception:
                pass
            if current_widget is placeholder:
                self.stack.setCurrentWidget(new_widget)
            if placeholder is not None and placeholder_index >= 0:
                self.stack.removeWidget(placeholder)
                placeholder.deleteLater()
            elif current_widget is not None and current_widget is not placeholder:
                self.stack.setCurrentWidget(current_widget)
            self._screens[index] = (label, new_widget)

    def _settle_active_screen_layout(self, index: int, navigation_epoch: int) -> None:
        """Finish one first-visible layout pass without resizing the top-level window."""
        if bool(getattr(self, "_shutting_down", False)):
            return
        if int(getattr(self, "_navigation_epoch", -1)) != int(navigation_epoch):
            return
        if int(self.stack.currentIndex()) != int(index):
            return
        widget = self.stack.widget(index)
        if widget is None or widget is not self.stack.currentWidget():
            return
        try:
            widget.ensurePolished()
            page_layout = widget.layout()
            if page_layout is not None:
                page_layout.invalidate()
                page_layout.activate()
            stack_layout = self.stack.layout()
            if stack_layout is not None:
                stack_layout.invalidate()
                stack_layout.activate()
            widget.updateGeometry()
            widget.update()
            self.stack.update()
        except Exception:
            pass
        try:
            hook = getattr(widget, "on_first_visible_layout_ready", None)
            if callable(hook):
                hook()
        except Exception:
            log.debug("MainWindow: first-visible layout hook failed for %s", self._screens[index][0], exc_info=True)

    def _get_tab_by_label(self, label: str) -> QWidget | None:
        for name, widget in self._screens:
            if name == label:
                return widget
        return None

    def _queue_condition_levels_changed(self) -> None:
        self._condition_levels_refresh_pending = True
        timer = getattr(self, "_condition_levels_refresh_timer", None)
        if isinstance(timer, QTimer):
            timer.start()
            return
        # Fallback path if timer initialization failed for any reason.
        self._apply_condition_levels_changed()

    def notify_condition_levels_changed(self) -> None:
        self._queue_condition_levels_changed()

    def _apply_condition_levels_changed(self) -> None:
        if not bool(getattr(self, "_condition_levels_refresh_pending", False)):
            return
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty("condition_levels_changed")
            return
        self._condition_levels_refresh_pending = False
        try:
            self.settings.reload()
        except Exception:
            pass
        try:
            self._refresh_condition_level_panel()
        except Exception:
            pass
        try:
            self._invalidate_sop_status_cache()
        except Exception:
            pass
        try:
            if hasattr(self, "scheduler"):
                self.scheduler.force_refresh()
        except Exception:
            pass
        try:
            if hasattr(self, "sop_tab") and self.sop_tab is not None:
                if hasattr(self.sop_tab, "on_condition_levels_changed"):
                    self.sop_tab.on_condition_levels_changed()
                elif hasattr(self.sop_tab, "on_settings_saved"):
                    self.sop_tab.on_settings_saved()
        except Exception:
            pass
        try:
            if hasattr(self, "controlfreq_tab") and self.controlfreq_tab is not None:
                if hasattr(self.controlfreq_tab, "on_condition_levels_changed"):
                    self.controlfreq_tab.on_condition_levels_changed()
                elif hasattr(self.controlfreq_tab, "on_sop_data_changed"):
                    self.controlfreq_tab.on_sop_data_changed()
        except Exception:
            pass
        try:
            if self.freq_planner_tab is not None:
                if hasattr(self.freq_planner_tab, "on_condition_levels_changed"):
                    self.freq_planner_tab.on_condition_levels_changed()
                elif self.freq_planner_tab.isVisible() and hasattr(self.freq_planner_tab, "on_settings_saved"):
                    self.freq_planner_tab.on_settings_saved()
        except Exception:
            pass
        try:
            if hasattr(self, "station_health_tab") and self.station_health_tab is not None:
                self.station_health_tab.refresh_from_registry()
        except Exception:
            pass
        try:
            self._refresh_scheduler_status_panel()
        except Exception:
            pass

    def _on_settings_saved_for_lazy_tabs(self) -> None:
        if getattr(self, "_settings_saved_refresh_pending", False):
            return
        self._settings_saved_refresh_pending = True
        QTimer.singleShot(120, self._flush_settings_saved_for_lazy_tabs)

    def _flush_settings_saved_for_lazy_tabs(self) -> None:
        self._settings_saved_refresh_pending = False
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty("settings_saved")
            return
        self._refresh_plan_context_labels("settings_saved")
        try:
            if self.freq_planner_tab is not None:
                self._run_timed_ui_refresh("settings_saved.freq_planner", self.freq_planner_tab.on_settings_saved)
        except Exception:
            pass
        try:
            if self.message_viewer_tab is not None:
                self._run_timed_ui_refresh("settings_saved.message_viewer", self.message_viewer_tab.on_settings_saved)
        except Exception:
            pass
        try:
            if self.controlfreq_tab is not None:
                self._run_timed_ui_refresh("settings_saved.controlfreq", self.controlfreq_tab.on_settings_saved)
        except Exception:
            pass
        try:
            self._run_timed_ui_refresh("settings_saved.condition_level_panel", self._refresh_condition_level_panel)
        except Exception:
            pass
        try:
            self._refresh_map_prop_target_controls()
        except Exception:
            pass
        sop_tab = getattr(self, "sop_tab", None)
        try:
            active_widget = self.stack.currentWidget() if hasattr(self, "stack") else None
        except Exception:
            active_widget = None
        if sop_tab is not None and active_widget is sop_tab:
            self._sop_settings_refresh_pending = False
            try:
                self._run_timed_ui_refresh("settings_saved.sop", sop_tab.on_settings_saved)
            except Exception:
                pass
        elif sop_tab is not None:
            self._sop_settings_refresh_pending = True

    def _on_operating_groups_changed(self) -> None:
        """Refresh only consumers of HF operating-group configuration."""

        self._station_command_group_qsy_lookup_cache = None
        self._station_command_group_qsy_rows_cache = None
        self._on_settings_saved_for_lazy_tabs()
        for label, tab in (
            ("operating_groups.hf_schedule", getattr(self, "hf_schedule_tab", None)),
            ("operating_groups.net_schedule", getattr(self, "net_tab", None)),
            ("operating_groups.fldigi_ncs", getattr(self, "fldigi_tab", None)),
            ("operating_groups.js8_ncs", getattr(self, "js8_tab", None)),
        ):
            callback = getattr(tab, "on_settings_saved", None)
            if callable(callback):
                self._run_timed_ui_refresh(label, callback)
        try:
            scheduler = getattr(self, "scheduler", None)
            if scheduler is not None:
                scheduler.force_refresh()
        except Exception:
            log.debug("MainWindow: operating-group scheduler refresh failed", exc_info=True)

    def _plan_context_consumer_widgets(self) -> tuple[object | None, ...]:
        return (
            getattr(self, "hf_schedule_tab", None),
            getattr(self, "net_tab", None),
            getattr(self, "freq_planner_tab", None),
            getattr(self, "sop_tab", None),
            getattr(self, "message_viewer_tab", None),
            getattr(self, "controlfreq_tab", None),
            getattr(self, "stations_map_tab", None),
        )

    def _refresh_plan_context_labels(self, reason: str = "") -> None:
        try:
            self.plan_context_service.invalidate()
        except Exception as e:
            log.debug("MainWindow: plan context invalidation failed for %s: %s", reason or "refresh", e)
        for widget in self._plan_context_consumer_widgets():
            label = getattr(widget, "plan_context_label", None)
            if label is None or not hasattr(label, "refresh_context"):
                continue
            try:
                label.refresh_context(refresh=True)
            except Exception as e:
                log.debug("MainWindow: plan context label refresh failed for %s: %s", reason or "refresh", e)

    def _load_runtime_active_device_profile(self) -> dict[str, object]:
        manager = getattr(self, "station_runtime_manager", None)
        if manager is not None and hasattr(manager, "get_runtime_primary_device_profile"):
            try:
                profile = manager.get_runtime_primary_device_profile()
            except Exception as e:
                log.debug("MainWindow: failed to load runtime-primary device profile: %s", e)
                profile = None
            if isinstance(profile, dict):
                return dict(profile)
        try:
            profile = self.multi_radio_store.get_runtime_primary_device_profile()
        except Exception as e:
            log.debug("MainWindow: failed to load runtime-primary device profile from store: %s", e)
            return {}
        return dict(profile) if isinstance(profile, dict) else {}

    @staticmethod
    def _runtime_profile_deployment_mode(profile: object) -> str:
        if not isinstance(profile, dict):
            return "full"
        mode = str(profile.get("deployment_mode", "full") or "full").strip().lower()
        return mode if mode in {"full", "minimal"} else "full"

    @staticmethod
    def _runtime_policy_enabled(policy: object, key: str, default: bool = True) -> bool:
        if not isinstance(policy, dict):
            return bool(default)
        value = policy.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return int(value) != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(default)

    @staticmethod
    def _runtime_state_signature_for(profile: object, policy: object) -> tuple[object, ...]:
        if not isinstance(profile, dict):
            profile = {}
        if not isinstance(policy, dict):
            policy = {}
        return (
            int(profile.get("id", 0) or 0),
            str(profile.get("name", "") or "").strip(),
            str(profile.get("control_backend", "") or "").strip().lower(),
            MainWindow._runtime_profile_deployment_mode(profile),
            str(policy.get("operating_profile_name", "") or "").strip(),
            str(policy.get("assignment_state", "") or "").strip().lower(),
            MainWindow._runtime_policy_enabled(policy, "scheduler_enabled", True),
            str(policy.get("scheduler_mode", "full") or "full").strip().lower(),
            MainWindow._runtime_policy_enabled(policy, "use_messages", True),
            MainWindow._runtime_policy_enabled(policy, "use_map", True),
            MainWindow._runtime_policy_enabled(policy, "use_background_ingest", True),
            MainWindow._runtime_policy_enabled(policy, "use_launch_control", True),
            MainWindow._runtime_policy_enabled(policy, "use_net_control_tabs", True),
            bool(policy.get("swap_active", False)),
            str(policy.get("swap_mode", "") or "").strip().lower(),
            str(policy.get("swap_summary", "") or "").strip(),
            str(profile.get("freq_enforcement_mode", "") or "").strip(),
            str(profile.get("freq_prompt_interval", "") or "").strip(),
            str(profile.get("fldigi_enforcement_mode", "") or "").strip(),
            str(profile.get("fldigi_prompt_interval", "") or "").strip(),
            str(profile.get("js8_enforcement_mode", "") or "").strip(),
            str(profile.get("js8_prompt_interval", "") or "").strip(),
            int(profile.get("schedule_hold_minutes_default", 0) or 0),
        )

    @staticmethod
    def _runtime_timer_policy_for(profile: object) -> dict[str, object]:
        if not isinstance(profile, dict):
            return {}
        keys = (
            "freq_enforcement_mode",
            "freq_prompt_interval",
            "fldigi_enforcement_mode",
            "fldigi_prompt_interval",
            "js8_enforcement_mode",
            "js8_prompt_interval",
        )
        return {key: profile.get(key) for key in keys if str(profile.get(key, "") or "").strip()}

    def _primary_runtime_policy(self) -> dict[str, object]:
        manager = getattr(self, "station_runtime_manager", None)
        policy: object = {}
        if manager is not None and hasattr(manager, "primary_runtime_policy"):
            try:
                policy = manager.primary_runtime_policy()
            except Exception:
                policy = {}
        data = dict(policy) if isinstance(policy, dict) else {}
        return {
            "operating_profile_name": str(data.get("operating_profile_name", "") or "").strip(),
            "assignment_state": str(data.get("assignment_state", "unassigned") or "unassigned").strip().lower(),
            "scheduler_enabled": self._runtime_policy_enabled(data, "scheduler_enabled", True),
            "scheduler_mode": str(data.get("scheduler_mode", "full") or "full").strip().lower() or "full",
            "use_messages": self._runtime_policy_enabled(data, "use_messages", True),
            "use_map": self._runtime_policy_enabled(data, "use_map", True),
            "use_background_ingest": self._runtime_policy_enabled(data, "use_background_ingest", True),
            "use_launch_control": self._runtime_policy_enabled(data, "use_launch_control", True),
            "use_net_control_tabs": self._runtime_policy_enabled(data, "use_net_control_tabs", True),
            "swap_active": bool(data.get("swap_active", False)),
            "swap_mode": str(data.get("swap_mode", "") or "").strip().lower(),
            "swap_summary": str(data.get("swap_summary", "") or "").strip(),
            "swap_source_name": str(data.get("swap_source_name", "") or "").strip(),
            "swap_target_name": str(data.get("swap_target_name", "") or "").strip(),
        }

    def start_post_shell_services(self) -> None:
        """Start non-critical listeners only after the usable shell is painted.

        This lifecycle boundary is intentionally public so ``main`` can queue
        it after recording the first-shell metric.  It is idempotent because
        tests, embedded launchers, and runtime-profile changes may call it more
        than once.
        """

        if self._shutting_down or self._post_shell_services_started:
            return
        self._post_shell_services_started = True
        # Connecting optional radios/mesh transports is runtime work, not shell
        # construction.  Some BLE stacks take tens of seconds to time out; even
        # though the adapter owns a worker, starting it during MainWindow.__init__
        # creates enough callback and discovery pressure to delay the first
        # painted frame on slower systems.
        self._start_mesh_runtime_if_enabled()
        background = getattr(self, "background_ingest", None)
        if self._background_ingest_start_pending and background is not None:
            try:
                if not hasattr(background, "is_running") or not background.is_running():
                    background.start()
                self._background_ingest_start_pending = False
                log.info("MainWindow: post-shell background ingest started")
            except Exception as exc:
                # Leave the request pending so a later explicit lifecycle call
                # can retry without blocking or failing application startup.
                log.warning("MainWindow: post-shell background ingest start failed: %s", exc)
        self.request_message_projection_catchup(reason="post_shell")
        self._schedule_message_projection_reconcile()
        self._publish_watchdog_diagnostic_snapshot()

    def present_first_run_onboarding(self) -> None:
        """Present fresh-station guidance after the usable shell is painted."""

        if self._shutting_down or self._first_run_onboarding_presented:
            return
        if self._first_run_onboarding_dialog is not None:
            return
        try:
            status = self.station_runtime_manager.runtime_status()
            settings_values = dict(self.settings.all())
            has_device_profiles = bool(self.multi_radio_store.list_device_profiles())
            eligible = should_present_first_run_onboarding(
                status,
                settings_values=settings_values,
                has_device_profiles=has_device_profiles,
            )
        except Exception as exc:
            log.warning("MainWindow: first-run onboarding eligibility check failed: %s", exc)
            return
        if not eligible:
            return

        self._first_run_onboarding_presented = True
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Welcome to CommsPlan")
        dialog.setAccessibleName("Welcome to CommsPlan station setup")
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        dialog.setIcon(QMessageBox.Information)
        dialog.setText("No existing CommsPlan station was found.")
        dialog.setInformativeText(
            "Choose your operator information, groups, radios, applications, and operating behavior. Scheduling stays off until you review and enable it."
        )
        setup_button = dialog.addButton("Set Up Station…", QMessageBox.AcceptRole)
        later_button = dialog.addButton("Set Up Later", QMessageBox.RejectRole)
        setup_button.setAccessibleName("Set up station")
        later_button.setAccessibleName("Set up the first radio later")
        dialog.setDefaultButton(setup_button)
        theme = resolve_theme(self.settings)
        setup_button.setStyleSheet(button_style("primary", theme))
        later_button.setStyleSheet(button_style("secondary", theme))
        dialog.finished.connect(
            lambda _result, owned=dialog, primary=setup_button: self._finish_first_run_onboarding(
                owned,
                primary,
            )
        )
        dialog.destroyed.connect(lambda *_args: setattr(self, "_first_run_onboarding_dialog", None))
        self._first_run_onboarding_dialog = dialog
        log.info("MainWindow: presenting fresh-station onboarding after shell startup")
        dialog.open()

    def _finish_first_run_onboarding(self, dialog: QMessageBox, setup_button: object) -> None:
        if self._shutting_down:
            return
        start_setup = dialog.clickedButton() is setup_button
        try:
            self.settings.set(FIRST_RUN_ONBOARDING_ACK_KEY, True)
        except Exception as exc:
            log.warning("MainWindow: could not persist first-run onboarding choice: %s", exc)
        log.info(
            "MainWindow: fresh-station onboarding choice=%s",
            "set_up_first_radio" if start_setup else "set_up_later",
        )
        if start_setup:
            QTimer.singleShot(0, lambda: self.open_settings_section("operator_station_setup", settings_nav_context="main"))

    def request_message_projection_catchup(self, *, reason: str = "source_change"):
        """Coalesce message projection work onto the application-owned lane."""

        if self._shutting_down:
            return None
        service = getattr(self, "message_projection_maintenance", None)
        if service is None:
            return None
        background = getattr(self, "background_ingest", None)
        if background is not None and hasattr(background, "has_inflight_jobs"):
            try:
                if background.has_inflight_jobs("messages"):
                    self._message_projection_catchup_pending = True
                    self._schedule_message_projection_followup(reason=f"after:{reason}")
                    return None
            except Exception:
                pass
        try:
            future = service.start_post_shell_catchup()
        except Exception as exc:
            log.debug("MainWindow: projection catch-up request failed (%s): %s", reason, exc)
            return None
        if future is self._message_projection_future and not future.done():
            self._message_projection_catchup_pending = True
        if future is not self._message_projection_future:
            self._message_projection_future = future
            self._message_projection_catchup_pending = False

            def _done(done_future, request_reason=str(reason or "source_change")) -> None:
                try:
                    result = done_future.result()
                    payload = {
                        "reason": request_reason,
                        "state": str(getattr(result, "state", "complete") or "complete"),
                        "committed": int(getattr(result, "committed", 0) or 0),
                        "deleted": int(getattr(result, "deleted", 0) or 0),
                        "deferred": int(getattr(result, "deferred", 0) or 0),
                    }
                except Exception as exc:
                    payload = {
                        "reason": request_reason,
                        "state": "failed",
                        "error": type(exc).__name__,
                    }
                self._message_projection_cycle_finished.emit(payload)

            future.add_done_callback(_done)
        return future

    def _on_background_ingest_projection_work_ready(self, job_name: str) -> None:
        name = str(job_name or "").strip().lower()
        if name in {"messages", "varac", "sitreps", "dynamic_flamp_projection"}:
            self.request_message_projection_catchup(reason=f"ingest:{name}")

    def _schedule_message_projection_reconcile(self) -> None:
        timer = getattr(self, "_message_projection_reconcile_timer", None)
        if timer is None or self._shutting_down or not self._post_shell_services_started:
            return
        # Low-cost safety reconciliation runs at a deterministic process-local
        # jitter in the specified 30-60 second window, avoiding cadence lockstep
        # with Mesh, BBS, and scheduler timers.
        self._message_projection_refresh_sequence += 1
        interval_ms = 30_000 + (
            (int(time.monotonic() * 1000.0) + self._message_projection_refresh_sequence * 7919)
            % 30_001
        )
        timer.start(interval_ms)

    def _on_message_projection_reconcile_timer(self) -> None:
        self.request_message_projection_catchup(reason="idle_reconcile")
        self._schedule_message_projection_reconcile()

    def _schedule_message_projection_followup(
        self, *, reason: str = "paced_followup", delay_ms: int = 1000
    ) -> None:
        timer = getattr(self, "_message_projection_followup_timer", None)
        if timer is None or self._shutting_down:
            return
        self._message_projection_followup_reason = str(reason or "paced_followup")
        if not timer.isActive():
            timer.start(max(250, int(delay_ms)))

    def _on_message_projection_followup_timer(self) -> None:
        if self._shutting_down:
            return
        reason = self._message_projection_followup_reason or "paced_followup"
        self._message_projection_followup_reason = ""
        self._message_projection_catchup_pending = False
        self.request_message_projection_catchup(reason=reason)

    def _on_message_projection_cycle_finished(self, payload: object) -> None:
        if self._shutting_down:
            return
        data = payload if isinstance(payload, Mapping) else {}
        self._publish_watchdog_diagnostic_snapshot()
        state = str(data.get("state", "") or "").strip().lower()
        if self._message_projection_catchup_pending or state in {"sliced", "deferred"}:
            self._message_projection_catchup_pending = False
            self._schedule_message_projection_followup(reason="coalesced_followup")

    def _on_message_projection_progressed(self, progress: object) -> None:
        """Coalesce committed batch progress into bounded visible Inbox reads."""

        if self._shutting_down:
            return
        # An explicit Message Index rebuild has its own progress dialog and
        # requests exactly one indexed Inbox refresh on terminal completion.
        # Refreshing the full visible Inbox after every tiny rebuild cycle can
        # keep the UI busy for the duration of a large historical replay.
        if str(getattr(progress, "rebuild_id", "") or ""):
            return
        changed = int(getattr(progress, "committed", 0) or 0) + int(
            getattr(progress, "deleted", 0) or 0
        )
        viewer = getattr(self, "message_viewer_tab", None)
        if changed and viewer is not None and hasattr(
            viewer, "_request_projected_message_query"
        ):
            viewer._request_projected_message_query(
                force=False,
                delay_ms=1000,
            )

    def _publish_watchdog_diagnostic_snapshot(self) -> None:
        watchdogs = tuple(
            watchdog
            for watchdog in (
                getattr(self, "_ui_watchdog", None),
                getattr(self, "_cpu_watchdog", None),
            )
            if watchdog is not None and hasattr(watchdog, "publish_diagnostic_snapshot")
        )
        if not watchdogs:
            return
        scheduler_snapshot: Mapping[str, object] = {}
        projection_snapshot: Mapping[str, object] = {}
        try:
            scheduler = getattr(self, "scheduler", None)
            if scheduler is not None and hasattr(scheduler, "get_multi_endpoint_diagnostics"):
                scheduler_snapshot = scheduler.get_multi_endpoint_diagnostics()
        except Exception:
            scheduler_snapshot = {"state": "unavailable"}
        try:
            service = getattr(self, "message_projection_maintenance", None)
            if service is not None and hasattr(service, "diagnostic_snapshot"):
                projection_snapshot = service.diagnostic_snapshot()
        except Exception:
            projection_snapshot = {"state": "unavailable"}
        snapshot = {
            "scheduler": scheduler_snapshot,
            "message_projection": projection_snapshot,
        }
        for watchdog in watchdogs:
            watchdog.publish_diagnostic_snapshot(snapshot)

    @staticmethod
    def _suppressed_screens_for_runtime(profile: object, policy: object) -> set[str]:
        suppressed: set[str] = set()
        if MainWindow._runtime_profile_deployment_mode(profile) == "minimal":
            suppressed.update({"Map", "Messages", "FreqPlanner"})
        if not MainWindow._runtime_policy_enabled(policy, "use_map", True):
            suppressed.add("Map")
        if not MainWindow._runtime_policy_enabled(policy, "use_messages", True):
            suppressed.add("Messages")
        if not MainWindow._runtime_policy_enabled(policy, "use_net_control_tabs", True):
            suppressed.update({"NCS-FLDigi/SSB", "NCS-JS8", "NCS-Local"})
        return suppressed

    @staticmethod
    def _runtime_background_ingest_enabled(profile: object, policy: object) -> bool:
        if MainWindow._runtime_profile_deployment_mode(profile) == "minimal":
            return False
        return MainWindow._runtime_policy_enabled(policy, "use_background_ingest", True)

    @staticmethod
    def _runtime_launch_enabled(profile: object, policy: object) -> bool:
        if MainWindow._runtime_profile_deployment_mode(profile) == "minimal":
            return False
        return MainWindow._runtime_policy_enabled(policy, "use_launch_control", True)

    @staticmethod
    def _runtime_lazy_prewarm_labels(suppressed_labels: set[str]) -> list[str]:
        return [label for label in ("Messages", "FreqPlanner") if label not in suppressed_labels]

    @staticmethod
    def _runtime_banner_text(profile: object, policy: object) -> str:
        profile_name = str(profile.get("name", "") or "").strip() if isinstance(profile, dict) else ""
        backend = str(profile.get("control_backend", "") or "").strip().upper() if isinstance(profile, dict) else ""
        operating_name = str(policy.get("operating_profile_name", "") or "").strip() if isinstance(policy, dict) else ""
        assignment_state = str(policy.get("assignment_state", "") or "").strip().lower() if isinstance(policy, dict) else ""
        swap_summary = str(policy.get("swap_summary", "") or "").strip() if isinstance(policy, dict) else ""
        profile_label = profile_name or "Primary device"
        backend_txt = f" via {backend}" if backend else ""
        if MainWindow._runtime_profile_deployment_mode(profile) == "minimal":
            return (
                f"{profile_label}{backend_txt} is running in Minimal mode. "
                "Map, Messages, FreqPlanner, startup launch, and background ingest are suppressed."
            )

        restrictions: list[str] = []
        if not MainWindow._runtime_policy_enabled(policy, "scheduler_enabled", True):
            restrictions.append("scheduler automation off")
        if not MainWindow._runtime_policy_enabled(policy, "use_map", True):
            restrictions.append("Map hidden")
        if not MainWindow._runtime_policy_enabled(policy, "use_messages", True):
            restrictions.append("Messages hidden")
        if not MainWindow._runtime_policy_enabled(policy, "use_net_control_tabs", True):
            restrictions.append("net control tabs hidden")
        if not MainWindow._runtime_policy_enabled(policy, "use_background_ingest", True):
            restrictions.append("background ingest off")
        if not MainWindow._runtime_policy_enabled(policy, "use_launch_control", True):
            restrictions.append("launch control off")
        if not swap_summary and restrictions == ["launch control off"]:
            return ""
        if not restrictions:
            return swap_summary
        operating_txt = operating_name or "assigned operating model"
        state_txt = "temporary override" if assignment_state == "temporary_override" else "active policy"
        detail = f"{profile_label}{backend_txt} is running under {operating_txt} ({state_txt}): {'; '.join(restrictions)}."
        if swap_summary:
            return f"{swap_summary} {detail}"
        return detail

    def _style_runtime_mode_banner(self, theme: dict) -> None:
        if not hasattr(self, "runtime_mode_banner") or not hasattr(self, "runtime_mode_label"):
            return
        border = theme.get("warning", theme.get("accent", "#d97706"))
        surface = theme.get("surface_alt", theme.get("surface", "#f4f4f5"))
        text = theme.get("text", "#222222")
        try:
            self.runtime_mode_banner.setStyleSheet(
                f"QFrame {{ border: 1px solid {border}; border-radius: 6px; background: {surface}; }}"
            )
            self.runtime_mode_label.setStyleSheet(f"color: {text}; font-weight: 600;")
        except Exception:
            pass

    def _set_nav_visibility_for_screen(self, screen_label: str, visible: bool) -> None:
        label = str(screen_label or "").strip()
        if label == "Settings":
            indices = list(getattr(self, "_settings_nav_button_indices", {}).values())
        elif label == "Messages":
            indices = list(getattr(self, "_messages_nav_button_indices", {}).values())
        else:
            indices = []
        if indices:
            for nav_idx in indices:
                if 0 <= nav_idx < len(self.nav_buttons):
                    try:
                        self.nav_buttons[nav_idx].setVisible(bool(visible))
                    except Exception:
                        pass
            return
        screen_idx = self._screen_index_by_label.get(label)
        if screen_idx is None:
            return
        nav_idx = self._nav_screen_index_map.get(screen_idx)
        if nav_idx is None or not (0 <= nav_idx < len(self.nav_buttons)):
            return
        try:
            self.nav_buttons[nav_idx].setVisible(bool(visible))
        except Exception:
            pass

    def _runtime_fallback_screen_index(self) -> int:
        for label in ("ControlFreq", "Settings"):
            idx = self._screen_index_by_label.get(label)
            if idx is not None:
                return int(idx)
        return 0

    def _screen_is_runtime_suppressed(self, screen_label: str) -> bool:
        return str(screen_label or "").strip() in self._suppressed_screen_labels

    def _apply_runtime_profile_state(self, *, force: bool = False) -> None:
        profile = self._load_runtime_active_device_profile()
        policy = self._primary_runtime_policy()
        signature = self._runtime_state_signature_for(profile, policy)
        if not force and signature == self._runtime_profile_signature:
            return
        if signature != self._runtime_profile_signature:
            self._station_command_qsy_requests = {}
            self._station_command_qsy_dispositions = {}
        self._active_runtime_profile = profile
        self._active_runtime_policy = policy
        self._runtime_profile_signature = signature
        self._suppressed_screen_labels = self._suppressed_screens_for_runtime(profile, policy)
        for label in ("Map", "Messages", "FreqPlanner", "NCS-FLDigi/SSB", "NCS-JS8", "NCS-Local"):
            self._set_nav_visibility_for_screen(label, label not in self._suppressed_screen_labels)
        if "Map" in self._suppressed_screen_labels:
            map_window = getattr(self, "map_window", None)
            if map_window is not None:
                map_window.hide()
        self._launch_startup_suppressed = not self._runtime_launch_enabled(profile, policy)
        try:
            if hasattr(self.launch_orchestrator, "set_runtime_launch_enabled"):
                self.launch_orchestrator.set_runtime_launch_enabled(
                    not self._launch_startup_suppressed,
                    reason="Launch Control is disabled by the primary operating model.",
                )
        except Exception:
            pass
        if self._launch_startup_suppressed:
            try:
                self.launch_orchestrator.stop_sequence()
            except Exception:
                pass
        try:
            if hasattr(self, "settings_tab") and hasattr(self.settings_tab, "_update_launch_control_buttons"):
                self.settings_tab._update_launch_control_buttons()
        except Exception:
            pass
        try:
            if hasattr(self, "scheduler") and self.scheduler is not None and hasattr(self.scheduler, "set_runtime_scheduler_enabled"):
                self.scheduler.set_runtime_scheduler_enabled(bool(policy.get("scheduler_enabled", True)))
        except Exception:
            pass
        try:
            if hasattr(self, "scheduler") and self.scheduler is not None and hasattr(self.scheduler, "set_runtime_timer_policy"):
                self.scheduler.set_runtime_timer_policy(self._runtime_timer_policy_for(profile))
        except Exception as e:
            log.debug("MainWindow: failed to apply runtime timer policy: %s", e)
        try:
            self._sync_hold_duration_combos()
        except Exception:
            pass
        try:
            set_scheduler_enabled_override(bool(policy.get("scheduler_enabled", True)))
        except Exception:
            pass
        self._lazy_prewarm_labels = self._runtime_lazy_prewarm_labels(self._suppressed_screen_labels)
        if not self._runtime_background_ingest_enabled(profile, policy):
            self._background_ingest_start_pending = False
            if hasattr(self, "background_ingest") and self.background_ingest is not None:
                try:
                    self.background_ingest.stop()
                except Exception:
                    pass
        else:
            if hasattr(self, "background_ingest") and self.background_ingest is not None:
                try:
                    if not self._post_shell_services_started:
                        self._background_ingest_start_pending = True
                    elif hasattr(self.background_ingest, "is_running"):
                        if not self.background_ingest.is_running():
                            self.background_ingest.start()
                    else:
                        self.background_ingest.start()
                except Exception:
                    pass
            if bool(getattr(self, "_startup_deferred_prewarm_enabled", False)):
                QTimer.singleShot(3000, self._start_lazy_prewarm)

        banner_text = self._runtime_banner_text(profile, policy)
        if hasattr(self, "runtime_mode_label"):
            self.runtime_mode_label.setText(banner_text)
        if hasattr(self, "runtime_mode_banner"):
            self.runtime_mode_banner.setVisible(bool(banner_text))
        try:
            self._style_runtime_mode_banner(resolve_theme(self.settings))
        except Exception:
            pass
        try:
            current_index = self.stack.currentIndex() if hasattr(self, "stack") else -1
            if 0 <= current_index < len(self._screens):
                current_label = self._screens[current_index][0]
                if self._screen_is_runtime_suppressed(current_label):
                    self._set_screen(self._runtime_fallback_screen_index())
        except Exception:
            pass
        try:
            self._update_nav_layout_metrics()
        except Exception:
            pass
        self._sync_settings_runtime_status(
            refresh_store=False,
            include_varac_sync_status=False,
        )

    def _refresh_station_overview(self, *, force: bool = False) -> None:
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty("station_overview")
            return
        try:
            if hasattr(self, "station_overview_tab") and self.station_overview_tab is not None:
                self.station_overview_tab.refresh_from_manager(force=force)
        except Exception:
            pass
        self._schedule_station_command_bar_refresh("station_overview", force=False)

    @staticmethod
    def _station_command_value(source: object, key: str, default: object = "") -> object:
        if isinstance(source, Mapping):
            return source.get(key, default)
        return getattr(source, key, default)

    @staticmethod
    def _station_command_bool(value: object, default: bool = False) -> bool:
        if value in (None, ""):
            return default
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _station_command_frequency_controls_available(snapshot: object | None) -> bool:
        if snapshot is None:
            return False
        if str(MainWindow._station_command_value(snapshot, "device_class", "")).lower() == "observer":
            return False
        active = MainWindow._station_command_value(snapshot, "runtime_active", None)
        if active is not None and not MainWindow._station_command_bool(active, default=False):
            return False
        profile = dict(snapshot) if isinstance(snapshot, Mapping) else {}
        for key in (
            "control_backend",
            "use_varac",
            "uses_varac",
            "use_flrig",
            "uses_flrig",
            "use_js8call",
            "uses_js8call",
            "use_fldigi",
            "uses_fldigi",
        ):
            value = MainWindow._station_command_value(snapshot, key, None)
            if value is not None:
                profile[key] = value
        if not profile:
            return True
        has_explicit_control_shape = any(
            str(profile.get(key, "") or "").strip()
            for key in (
                "control_backend",
                "use_varac",
                "uses_varac",
                "use_flrig",
                "uses_flrig",
                "use_js8call",
                "uses_js8call",
                "use_fldigi",
                "uses_fldigi",
            )
        )
        if not has_explicit_control_shape:
            return True
        return frequency_controls_available(profile)

    @staticmethod
    def _station_command_snapshot_name(snapshot: object) -> str:
        try:
            if isinstance(snapshot, Mapping):
                projected = station_command_radio_from_mapping(snapshot)
                if projected.short_name:
                    return projected.card_title
            name = str(MainWindow._station_command_value(snapshot, "name", "") or "").strip()
            if name:
                return name
            ident = int(
                MainWindow._station_command_value(
                    snapshot,
                    "device_profile_id",
                    MainWindow._station_command_value(snapshot, "id", 0),
                )
                or 0
            )
            return f"Radio {ident}" if ident > 0 else "Radio"
        except Exception:
            return "Radio"

    @staticmethod
    def _station_command_snapshot_id(snapshot: object) -> int:
        try:
            return int(
                MainWindow._station_command_value(
                    snapshot,
                    "device_profile_id",
                    MainWindow._station_command_value(snapshot, "id", 0),
                )
                or 0
            )
        except Exception:
            return 0

    @staticmethod
    def _station_command_frequency_text(snapshot: object) -> str:
        try:
            freq = MainWindow._station_command_format_frequency(
                MainWindow._station_command_value(snapshot, "current_frequency_label", ""),
                suffix="",
            )
            parts = [freq]
            band = str(MainWindow._station_command_value(snapshot, "current_band", "") or "").strip()
            if band:
                parts.append(band)
            text = " ".join(part for part in parts if part).strip()
            return text or "unavailable"
        except Exception:
            return "unavailable"

    @staticmethod
    def _station_command_parse_frequency(value: object) -> float | None:
        freq = parse_frequency_mhz(value)
        if freq is not None:
            return freq
        text = str(value or "").replace("MHz", "").strip()
        return parse_frequency_mhz(text)

    @staticmethod
    def _station_command_current_frequency_key(snapshot: object | None) -> str:
        if snapshot is None:
            return ""
        for field in ("current_frequency_label", "current_frequency_mhz", "current_frequency"):
            value = MainWindow._station_command_value(snapshot, field, "")
            frequency = MainWindow._station_command_parse_frequency(value)
            if frequency is not None:
                if float(frequency) > 1_000:
                    frequency = float(frequency) / 1_000_000
                return f"{float(frequency):.6f}"
        try:
            frequency_hz = float(MainWindow._station_command_value(snapshot, "current_frequency_hz", 0) or 0)
        except Exception:
            frequency_hz = 0
        return f"{frequency_hz / 1_000_000:.6f}" if frequency_hz > 0 else ""

    @staticmethod
    def _station_command_alternate_qsy_options(
        options: Mapping[str, Mapping[str, object]],
        snapshot: object | None,
    ) -> dict[str, dict[str, object]]:
        current_key = MainWindow._station_command_current_frequency_key(snapshot)
        filtered: dict[str, dict[str, object]] = {}
        for option_key, meta in options.items():
            try:
                frequency_key = f"{float(meta.get('freq')):.6f}"
            except Exception:
                frequency_key = ""
            if current_key and frequency_key == current_key:
                continue
            filtered[str(option_key)] = dict(meta)
        return filtered

    @staticmethod
    def _station_command_group_display_name(group: object) -> str:
        text = str(group or "").strip().upper()
        if text == "S2 UNDERGROUND":
            return "S2/GHOSTNET"
        return text

    def _station_command_schedule_group_band(self, snapshot: object | None = None) -> tuple[str, str]:
        try:
            if snapshot is not None:
                lane_group, lane_band = self._station_command_lane_current_group_band(
                    self._station_command_snapshot_id(snapshot)
                )
                if lane_group:
                    return lane_group, lane_band
                snapshot_group = self._station_command_group_display_name(
                    self._station_command_value(snapshot, "schedule_group", "")
                )
                snapshot_band = str(
                    self._station_command_value(snapshot, "schedule_band", "")
                    or ""
                ).strip().upper()
                if snapshot_group:
                    return snapshot_group, snapshot_band
                snapshot_group = self._station_command_group_display_name(
                    self._station_command_value(snapshot, "current_group", "")
                    or self._station_command_value(snapshot, "group", "")
                    or self._station_command_value(snapshot, "group_name", "")
                )
                snapshot_band = str(
                    self._station_command_value(snapshot, "current_band", "")
                    or self._station_command_value(snapshot, "band", "")
                    or ""
                ).strip().upper()
                if snapshot_group:
                    return snapshot_group, snapshot_band
            sched = getattr(self, "scheduler", None)
            entry = getattr(sched, "current_schedule_entry", {}) if sched is not None else {}
            if isinstance(entry, Mapping):
                source = str(getattr(sched, "current_source", "") or "").strip().upper()
                selected_radio_id = self._station_command_snapshot_id(snapshot) if snapshot is not None else 0
                if selected_radio_id > 0:
                    scheduler_radio_id = self._station_command_scheduler_entry_radio_id(entry)
                    if scheduler_radio_id <= 0 and sched is not None:
                        target_getter = getattr(sched, "_primary_manual_control_radio_id", None)
                        if callable(target_getter):
                            try:
                                scheduler_radio_id = int(target_getter() or 0)
                            except Exception:
                                scheduler_radio_id = 0
                    if scheduler_radio_id <= 0:
                        return "", ""
                    if scheduler_radio_id != selected_radio_id:
                        return "", ""
                current_raw = self._station_command_value(snapshot, "current_frequency_label", "") if snapshot is not None else ""
                entry_freq = self._station_command_parse_frequency(entry.get("frequency"))
                current_freq = (
                    self._station_command_parse_frequency(current_raw)
                    if snapshot is not None
                    else None
                )
                if source != "QSY" and snapshot is not None and not str(current_raw or "").strip():
                    return "", ""
                if (
                    source != "QSY"
                    and entry_freq is not None
                    and current_freq is not None
                    and abs(float(entry_freq) - float(current_freq)) > 0.0005
                ):
                    return "", ""
                group = self._station_command_group_display_name(entry.get("group"))
                band = str(entry.get("band") or "").strip().upper()
                if group or band:
                    return group, band
        except Exception:
            pass
        return "", ""

    @staticmethod
    def _station_command_parse_json_list(value: object) -> list[dict[str, object]]:
        if isinstance(value, list):
            raw = value
        else:
            try:
                raw = json.loads(str(value or "[]"))
            except Exception:
                raw = []
        return [dict(item) for item in raw if isinstance(item, Mapping)]

    @staticmethod
    def _station_command_parse_json_ref_items(value: object) -> list[object]:
        if isinstance(value, list):
            raw = value
        else:
            try:
                raw = json.loads(str(value or "[]"))
            except Exception:
                raw = []
        refs: list[object] = []
        for item in raw:
            if isinstance(item, Mapping):
                refs.append(dict(item))
            elif isinstance(item, str):
                text = item.strip()
                if text:
                    refs.append(text)
        return refs

    @staticmethod
    def _station_command_hhmm_to_minutes(value: object) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if ":" in text:
                hour_text, minute_text = text.split(":", 1)
            elif len(text) in {3, 4} and text.isdigit():
                hour_text, minute_text = text[:-2], text[-2:]
            else:
                return None
            hour = int(hour_text)
            minute = int(minute_text)
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                return None
            return hour * 60 + minute
        except Exception:
            return None

    @staticmethod
    def _station_command_day_name(value: object) -> str:
        text = str(value or "").strip().upper()
        aliases = {
            "SUN": "SUNDAY",
            "MON": "MONDAY",
            "TUE": "TUESDAY",
            "TUES": "TUESDAY",
            "WED": "WEDNESDAY",
            "THU": "THURSDAY",
            "THUR": "THURSDAY",
            "THURS": "THURSDAY",
            "FRI": "FRIDAY",
            "SAT": "SATURDAY",
        }
        return aliases.get(text, text)

    @staticmethod
    def _station_command_next_day_name(day: str) -> str:
        days = ("SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")
        try:
            return days[(days.index(day) + 1) % len(days)]
        except ValueError:
            return "SUNDAY"

    @staticmethod
    def _station_command_ref_mapping(ref: object) -> Mapping[str, object] | None:
        return ref if isinstance(ref, Mapping) else None

    @classmethod
    def _station_command_ref_active_now(cls, ref: object, now_utc: datetime.datetime) -> bool:
        ref = cls._station_command_ref_mapping(ref)
        if ref is None:
            return False
        start = cls._station_command_hhmm_to_minutes(
            ref.get("start_utc") or ref.get("start") or ref.get("start_local")
        )
        end = cls._station_command_hhmm_to_minutes(
            ref.get("end_utc") or ref.get("end") or ref.get("end_local")
        )
        if start is None or end is None:
            return False
        today = now_utc.strftime("%A").upper()
        row_day = cls._station_command_day_name(ref.get("day_utc") or ref.get("day") or "ALL")
        minute = now_utc.hour * 60 + now_utc.minute
        if row_day == "ALL":
            return start <= minute < end if start <= end else (minute >= start or minute < end)
        if start <= end:
            return row_day == today and start <= minute < end
        return (row_day == today and minute >= start) or (
            cls._station_command_next_day_name(row_day) == today and minute < end
        )

    def _station_command_assigned_plan_group_band(self, snapshot: object) -> tuple[str, str]:
        ident = self._station_command_snapshot_id(snapshot)
        if ident <= 0:
            return "", ""
        lane = self._station_command_lane_for_radio(ident)
        if isinstance(lane, Mapping):
            lane_group, lane_band = self._station_command_entry_group_band(
                lane.get("current_entry") if isinstance(lane.get("current_entry"), Mapping) else None
            )
            # A worker lane is authoritative even when its current entry is
            # empty.  Do not let saved recurrence rows replace or reinterpret
            # the worker's current state.
            return lane_group, lane_band
        refs = self._station_command_assigned_plan_refs_for_radio(ident)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for ref in refs:
            ref = self._station_command_ref_mapping(ref)
            if ref is None or not self._station_command_ref_active_now(ref, now_utc):
                continue
            group = self._station_command_group_display_name(ref.get("group_name") or ref.get("group"))
            band = str(ref.get("band") or "").strip().upper()
            if group:
                return group, band
        if refs:
            for ref in refs:
                ref = self._station_command_ref_mapping(ref)
                if ref is None:
                    continue
                group = self._station_command_group_display_name(ref.get("group_name") or ref.get("group"))
                band = str(ref.get("band") or "").strip().upper()
                if group:
                    return group, band
            return "", ""
        lane_group, lane_band = self._station_command_lane_current_group_band(ident)
        if lane_group:
            return lane_group, lane_band
        return "", ""

    def _station_command_next_minutes(self, snapshot: object) -> int | None:
        """Return the next meaningful plan transition for one radio."""
        transition = self._station_command_next_transition(snapshot)
        if not isinstance(transition, Mapping) or transition.get("state") != "ready":
            return None
        next_start = transition.get("start_utc")
        if not isinstance(next_start, datetime.datetime):
            return None
        seconds = (next_start - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return max(0, int((seconds + 59) // 60)) if seconds >= 0 else -1

    def _station_command_plan_cache(self) -> tuple[dict[int, dict[str, object]], dict[int, dict[str, object]]]:
        now = time.monotonic()
        cache = getattr(self, "_station_command_plan_cache_data", None)
        expires = float(getattr(self, "_station_command_plan_cache_expires", 0.0) or 0.0)
        scheduler = getattr(self, "scheduler", None)
        key_getter = getattr(scheduler, "schedule_display_cache_key", None)
        revision = key_getter() if callable(key_getter) else None
        if isinstance(cache, tuple) and ((revision is not None and revision == getattr(self, "_station_command_plan_cache_revision", None)) or (revision is None and now < expires)):
            return cache
        # The command bar is a render path.  Its former fallback opened the
        # settings database every 15 seconds (and after each forced refresh),
        # so a busy message writer could freeze the entire Qt thread.  The
        # scheduler already publishes the same plan/profile projection from a
        # worker; derive the display cache exclusively from that immutable
        # snapshot.
        assignments_by_radio: dict[int, dict[str, object]] = {}
        plans_by_id: dict[int, dict[str, object]] = {}
        try:
            lanes = self._station_command_active_schedule_lanes(force=False)
        except Exception:
            lanes = {}
        for radio_id, lane in lanes.items():
            if not isinstance(lane, Mapping):
                continue
            try:
                plan_id = int(lane.get("frequency_plan_id") or 0)
            except Exception:
                plan_id = 0
            if plan_id <= 0:
                continue
            assignments_by_radio[int(radio_id)] = {
                "device_profile_id": int(radio_id),
                "frequency_plan_id": plan_id,
                "frequency_plan_name": str(lane.get("frequency_plan_name") or ""),
            }
            schedule_refs = [
                dict(row)
                for key in ("hf_rows", "net_rows")
                for row in (lane.get(key) or ())
                if isinstance(row, Mapping)
            ]
            plans_by_id[plan_id] = {
                "id": plan_id,
                "name": str(lane.get("frequency_plan_name") or ""),
                "schedule_refs_json": schedule_refs,
                "frequency_refs_json": "[]",
            }
        cache = (assignments_by_radio, plans_by_id)
        self._station_command_plan_cache_data = cache
        self._station_command_plan_cache_revision = revision
        self._station_command_plan_cache_expires = now + 15.0
        return cache

    def _invalidate_station_command_plan_cache(self) -> None:
        self._station_command_plan_cache_data = None
        self._station_command_plan_cache_expires = 0.0
        self._station_command_group_qsy_lookup_cache = None
        self._station_command_group_qsy_rows_cache = None

    def _station_command_assigned_plan_for_radio(self, device_profile_id: int) -> dict[str, object] | None:
        try:
            assignments_by_radio, plans_by_id = self._station_command_plan_cache()
            assignment = assignments_by_radio.get(int(device_profile_id or 0))
            if not assignment:
                return None
            plan_id = int(assignment.get("frequency_plan_id") or 0)
            if plan_id <= 0:
                return None
            return plans_by_id.get(plan_id)
        except Exception:
            return None

    def _station_command_assigned_plan_refs_for_radio(self, device_profile_id: int) -> list[object]:
        plan = self._station_command_assigned_plan_for_radio(device_profile_id)
        if not isinstance(plan, Mapping):
            return []
        refs: list[object] = []
        for key in ("schedule_refs_json", "frequency_refs_json"):
            refs.extend(self._station_command_parse_json_ref_items(plan.get(key, "[]")))
        return refs

    def _station_command_assigned_plan_name_for_radio(self, device_profile_id: int) -> str:
        plan = self._station_command_assigned_plan_for_radio(device_profile_id)
        if isinstance(plan, Mapping):
            name = str(plan.get("name") or "").strip()
            if name:
                return name
        try:
            assignments_by_radio, _plans_by_id = self._station_command_plan_cache()
            assignment = assignments_by_radio.get(int(device_profile_id or 0), {})
            for key in ("frequency_plan_name", "plan_name", "name"):
                name = str(assignment.get(key) or "").strip() if isinstance(assignment, Mapping) else ""
                if name:
                    return name
        except Exception:
            pass
        lane = self._station_command_lane_for_radio(device_profile_id)
        if isinstance(lane, Mapping):
            name = str(lane.get("frequency_plan_name") or "").strip()
            if name:
                return name
        return ""

    def _station_command_active_schedule_lanes(self, *, force: bool = False) -> dict[int, dict[str, object]]:
        now = time.monotonic()
        cache = getattr(self, "_station_command_lane_cache_data", None)
        expires = float(getattr(self, "_station_command_lane_cache_expires", 0.0) or 0.0)
        scheduler = getattr(self, "scheduler", None)
        key_getter = getattr(scheduler, "schedule_display_cache_key", None)
        revision = key_getter() if callable(key_getter) else None
        if isinstance(cache, dict) and not force and ((revision is not None and revision == getattr(self, "_station_command_lane_cache_revision", None)) or (revision is None and now < expires)):
            return cache
        lanes: dict[int, dict[str, object]] = {}
        if scheduler is not None and hasattr(scheduler, "active_schedule_lanes"):
            try:
                display_getter = getattr(scheduler, "schedule_lane_display_snapshot", None)
                rows = (display_getter() if callable(display_getter) else
                        scheduler.active_schedule_lanes(force=force, request_refresh=False))
                for lane in rows:
                    if not isinstance(lane, Mapping):
                        continue
                    try:
                        radio_id = int(lane.get("device_profile_id") or 0)
                    except Exception:
                        radio_id = 0
                    if radio_id > 0:
                        lanes[radio_id] = dict(lane)
            except Exception as exc:
                log.debug("MainWindow: failed to load station command schedule lanes: %s", exc)
                lanes = {}
        self._station_command_lane_cache_data = lanes
        self._station_command_lane_cache_revision = revision
        self._station_command_lane_cache_expires = now + 15.0
        return lanes

    def _invalidate_station_command_lane_cache(self) -> None:
        self._station_command_lane_cache_data = None
        self._station_command_lane_cache_expires = 0.0

    def _station_command_lane_for_radio(self, device_profile_id: int) -> dict[str, object] | None:
        try:
            radio_id = int(device_profile_id or 0)
        except Exception:
            radio_id = 0
        if radio_id <= 0:
            return None
        lane = self._station_command_active_schedule_lanes().get(radio_id)
        return dict(lane) if isinstance(lane, Mapping) else None

    def _station_command_entry_group_band(self, entry: Mapping[str, object] | None) -> tuple[str, str]:
        if not isinstance(entry, Mapping):
            return "", ""
        group = self._station_command_group_display_name(
            entry.get("group_name") or entry.get("group") or entry.get("net_name") or entry.get("label")
        )
        band = str(entry.get("band") or "").strip().upper()
        return group, band

    def _station_command_lane_current_group_band(self, device_profile_id: int) -> tuple[str, str]:
        lane = self._station_command_lane_for_radio(device_profile_id)
        entry = lane.get("current_entry") if isinstance(lane, Mapping) else None
        return self._station_command_entry_group_band(entry if isinstance(entry, Mapping) else None)

    def _station_command_next_transition(self, snapshot: object) -> dict[str, object]:
        """Return one worker-published next transition for this radio.

        The command bar consumes the worker's entry and boundary as one
        transition record.  It must not replay schedule recurrence to choose a
        destination independently from the countdown.
        """
        ident = MainWindow._station_command_snapshot_id(snapshot)
        lane_getter = getattr(self, "_station_command_lane_for_radio", None)
        lane = lane_getter(ident) if callable(lane_getter) and ident > 0 else None
        if not isinstance(lane, Mapping):
            return {"state": "no_next", "entry": {}, "start_utc": None}
        if lane.get("schedule_projection_pending"):
            return {"state": "pending", "entry": {}, "start_utc": None}

        has_start = "next_entry_start_utc" in lane
        raw_start = lane.get("next_entry_start_utc")
        next_entry = lane.get("next_entry")
        entry = dict(next_entry) if isinstance(next_entry, Mapping) else {}
        if not has_start and not entry:
            return {"state": "no_next", "entry": {}, "start_utc": None}
        if not has_start:
            # Compatibility for older lane providers that exposed only the
            # entry.  The label remains available, but no time is invented.
            return {"state": "legacy_entry_only", "entry": entry, "start_utc": None}
        if raw_start is None or not isinstance(raw_start, datetime.datetime):
            return {"state": "no_next", "entry": {}, "start_utc": None}
        start_utc = raw_start
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=datetime.timezone.utc)
        else:
            start_utc = start_utc.astimezone(datetime.timezone.utc)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if start_utc < now_utc:
            return {"state": "expired", "entry": entry, "start_utc": start_utc}
        if not entry:
            return {"state": "no_next", "entry": {}, "start_utc": start_utc}
        return {"state": "ready", "entry": entry, "start_utc": start_utc}

    def _station_command_lane_schedule_rows(self, device_profile_id: int) -> list[dict[str, object]]:
        lane = self._station_command_lane_for_radio(device_profile_id)
        if not isinstance(lane, Mapping):
            return []
        rows: list[dict[str, object]] = []
        for key in ("hf_rows", "net_rows", "sop_rows"):
            raw_rows = lane.get(key)
            if not isinstance(raw_rows, list):
                continue
            rows.extend(dict(row) for row in raw_rows if isinstance(row, Mapping))
        return rows

    @staticmethod
    def _station_command_scheduler_entry_radio_id(entry: Mapping[str, object]) -> int:
        return station_command_scheduler_entry_radio_id(entry)

    def _station_command_manual_qsy_meta_for_radio(self, device_profile_id: int) -> dict[str, object] | None:
        meta = getattr(self, "_station_command_manual_qsy_meta", None)
        return station_command_manual_qsy_meta_for_radio(
            meta=meta if isinstance(meta, Mapping) else None,
            meta_profile_id=getattr(self, "_station_command_manual_qsy_profile_id", 0),
            device_profile_id=int(device_profile_id or 0),
        )

    def _station_command_manual_control_state_for_radio(self, device_profile_id: int) -> object | None:
        try:
            radio_id = int(device_profile_id or 0)
        except Exception:
            radio_id = 0
        if radio_id <= 0:
            return None
        try:
            scheduler = getattr(self, "scheduler", None)
            snapshot = getattr(scheduler, "manual_control_state_snapshot", None)
            return snapshot(radio_id) if callable(snapshot) else None
        except Exception:
            return None

    def _station_command_manual_control_service_available(self) -> bool:
        snapshot = getattr(
            getattr(self, "scheduler", None), "manual_control_state_snapshot", None
        )
        return callable(snapshot)

    @staticmethod
    def _station_command_manual_state_has_qsy_target(state: object | None) -> bool:
        target = getattr(state, "manual_target", None)
        if target is None:
            return False
        try:
            return int(getattr(target, "frequency_hz", 0) or 0) > 0
        except Exception:
            return False

    def _station_command_scheduler_manual_qsy_active_for_radio(self, device_profile_id: int) -> bool:
        try:
            ident = int(device_profile_id or 0)
        except Exception:
            return False
        if ident <= 0:
            return False
        state = self._station_command_manual_control_state_for_radio(ident)
        if state is not None:
            state_name = str(getattr(state, "state", "") or "").strip()
            if state_name == "manual_qsy":
                return True
            if state_name == "manual_hold" and self._station_command_manual_state_has_qsy_target(state):
                return True
            return False
        if self._station_command_manual_control_service_available():
            return False
        if self._station_command_manual_qsy_meta_for_radio(ident):
            return True
        try:
            sched = getattr(self, "scheduler", None)
            entry = getattr(sched, "current_schedule_entry", {}) if sched is not None else {}
            target_getter = getattr(sched, "_primary_manual_control_radio_id", None)
            primary_manual_radio_id = target_getter() if callable(target_getter) else 0
            return scheduler_manual_qsy_active_for_radio(
                device_profile_id=int(device_profile_id or 0),
                manual_meta=None,
                scheduler_source=getattr(sched, "current_source", "") if sched is not None else "",
                scheduler_manual_active=getattr(sched, "_manual_qsy_active", False) if sched is not None else False,
                scheduler_entry=entry if isinstance(entry, Mapping) else None,
                primary_manual_radio_id=primary_manual_radio_id,
            )
        except Exception:
            return False

    def _station_command_scheduler_manual_qsy_active(self) -> bool:
        try:
            active_id = int(getattr(self, "_station_command_selected_profile_id", 0) or 0)
        except Exception:
            active_id = 0
        return self._station_command_scheduler_manual_qsy_active_for_radio(active_id)

    @staticmethod
    def _station_command_countdown_text(remaining_sec: object) -> str:
        return station_command_countdown_text(remaining_sec)

    def _update_station_command_hold_button_labels(self, hold_snapshot: Mapping[str, object]) -> None:
        hold_btn = getattr(self, "station_command_hold_btn", None)
        suspend_btn = getattr(self, "station_command_suspend_btn", None)
        qsy_btn = getattr(self, "station_command_qsy_btn", None)
        if hold_btn is None or suspend_btn is None:
            return
        base_qsy = str(getattr(self, "_station_command_qsy_suspend_base_text", "QSY Suspend") or "QSY Suspend")
        base_suspend = str(getattr(self, "_station_command_suspend_base_text", "Suspend Scheduler") or "Suspend Scheduler")
        manual_qsy_active = self._station_command_scheduler_manual_qsy_active()
        scheduler_suspended_manual = self._station_command_scheduler_suspended_manually()
        if qsy_btn is not None:
            qsy_btn.setText("QSY Now")
        suspend_btn.setText("Scheduler Suspended" if scheduler_suspended_manual else base_suspend)
        if isinstance(hold_snapshot, Mapping) and bool(hold_snapshot.get("active")):
            countdown = self._station_command_countdown_text(hold_snapshot.get("remaining_sec"))
            if countdown:
                hold_btn.setText(f"Suspended {countdown}")
                until = hold_snapshot.get("until")
                if isinstance(until, datetime.datetime):
                    local_dt = until.astimezone()
                    tip = f"Scheduler resumes at {local_dt:%H:%M:%S} local."
                    hold_btn.setToolTip(tip)
                return
        hold_btn.setText(base_qsy)

    def _station_command_manual_qsy_meta_for_selected(self) -> dict[str, object] | None:
        try:
            active_id = int(getattr(self, "_station_command_selected_profile_id", 0) or 0)
        except Exception:
            return None
        return self._station_command_manual_qsy_meta_for_radio(active_id)

    def _station_command_set_manual_qsy_meta(self, meta: Mapping[str, object] | None) -> None:
        if not isinstance(meta, Mapping):
            self._station_command_manual_qsy_meta = None
            self._station_command_manual_qsy_profile_id = None
            return
        self._station_command_manual_qsy_meta = dict(meta)
        try:
            self._station_command_manual_qsy_profile_id = int(
                meta.get("target_device_profile_id")
                or getattr(self, "_station_command_selected_profile_id", 0)
                or 0
            )
        except Exception:
            self._station_command_manual_qsy_profile_id = None

    def _station_command_clear_manual_qsy_meta(self, target_device_profile_id: int | None = None) -> None:
        if target_device_profile_id is not None:
            try:
                target_id = int(target_device_profile_id or 0)
                stored_id = int(getattr(self, "_station_command_manual_qsy_profile_id", 0) or 0)
            except Exception:
                target_id = 0
                stored_id = 0
            if target_id > 0 and stored_id not in {0, target_id}:
                return
        self._station_command_manual_qsy_meta = None
        self._station_command_manual_qsy_profile_id = None

    def _station_command_clear_pending_qsy_for_radio(self, device_profile_id: int | None) -> None:
        try:
            radio_id = int(device_profile_id or 0)
        except Exception:
            radio_id = 0
        if radio_id <= 0:
            return
        pending = getattr(self, "_station_command_card_qsy_pending_keys", None)
        if isinstance(pending, dict):
            pending.pop(radio_id, None)

    def _station_command_scheduler_suspended_manually_for_radio(self, device_profile_id: int) -> bool:
        try:
            state = self._station_command_manual_control_state_for_radio(int(device_profile_id or 0))
            if state is not None and getattr(state, "state", "") == "manual_suspend":
                return True
            if state is not None:
                return False
            if self._station_command_manual_control_service_available():
                return False
            return scheduler_suspended_manually_for_radio(
                device_profile_id=int(device_profile_id or 0),
                suspended_manual=getattr(self, "_station_command_scheduler_suspended_manual", False),
                suspended_profile_id=getattr(self, "_station_command_scheduler_suspended_manual_profile_id", 0),
                runtime_scheduler_enabled_override=None,
                selected_profile_id=getattr(self, "_station_command_selected_profile_id", 0),
            )
        except Exception:
            return False

    def _station_command_scheduler_suspended_manually(self) -> bool:
        try:
            active_id = int(getattr(self, "_station_command_selected_profile_id", 0) or 0)
        except Exception:
            active_id = 0
        return self._station_command_scheduler_suspended_manually_for_radio(active_id)

    def _station_command_timed_suspend_active_for_radio(self, device_profile_id: int) -> bool:
        if self._station_command_scheduler_manual_qsy_active_for_radio(device_profile_id):
            return False
        hold_snapshot = self._station_command_hold_snapshot_for_radio(device_profile_id)
        return timed_suspend_active_for_radio(
            device_profile_id=int(device_profile_id or 0),
            timed_suspend_profile_id=getattr(self, "_station_command_timed_suspend_profile_id", 0),
            hold_active=bool(hold_snapshot.get("active")),
        )

    def _station_command_hold_snapshot_for_radio(self, device_profile_id: int) -> dict[str, object]:
        try:
            radio_id = int(device_profile_id or 0)
        except Exception:
            radio_id = 0
        if radio_id <= 0:
            return {"active": False, "until": None, "remaining_sec": None}
        try:
            state = self._station_command_manual_control_state_for_radio(radio_id)
        except Exception:
            state = None
        if state is not None and getattr(state, "state", "") in {"manual_hold", "manual_qsy"}:
            text = str(getattr(state, "hold_until_utc", "") or "").strip()
            if text:
                try:
                    until = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=datetime.timezone.utc)
                    until = until.astimezone(datetime.timezone.utc)
                    remaining = (until - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                    if remaining > 0:
                        return {"active": True, "until": until, "remaining_sec": remaining}
                except Exception:
                    pass
        return {"active": False, "until": None, "remaining_sec": None}

    def _station_command_set_scheduler_suspended_manual(
        self,
        suspended: bool,
        *,
        target_device_profile_id: int | None = None,
    ) -> None:
        value = bool(suspended)
        try:
            target_id = int(target_device_profile_id or getattr(self, "_station_command_selected_profile_id", 0) or 0)
        except Exception:
            target_id = 0
        if target_id > 0:
            try:
                scheduler = getattr(self, "scheduler", None)
                if scheduler is not None:
                    if value:
                        scheduler._record_manual_suspend_state(
                            target_device_profile_id=target_id,
                            operator_source="main_control_center",
                        )
                    else:
                        scheduler._record_manual_resume_state(target_id)
            except Exception as exc:
                log.debug("MainWindow: failed to persist radio-scoped scheduler suspend: %s", exc)
            self._station_command_scheduler_suspended_manual = value
            self._station_command_scheduler_suspended_manual_profile_id = target_id if value else 0
            try:
                set_scheduler_enabled_override(None)
            except Exception:
                pass
            return

        self._station_command_scheduler_suspended_manual = value
        if value:
            try:
                self._station_command_scheduler_suspended_manual_profile_id = int(
                    getattr(self, "_station_command_selected_profile_id", 0) or 0
                )
            except Exception:
                self._station_command_scheduler_suspended_manual_profile_id = 0
        else:
            self._station_command_scheduler_suspended_manual_profile_id = 0
        try:
            scheduler = getattr(self, "scheduler", None)
            if scheduler is not None and hasattr(scheduler, "set_runtime_scheduler_enabled"):
                scheduler.set_runtime_scheduler_enabled(False if value else True)
        except Exception as exc:
            log.debug("MainWindow: failed to update runtime scheduler enabled state: %s", exc)
        try:
            set_scheduler_enabled_override(False if value else True)
        except Exception:
            pass
        if value:
            try:
                set_suspend_until(self.settings, None)
            except Exception:
                pass

    def _station_command_group_band_for_frequency(self, snapshot: object) -> tuple[str, str]:
        try:
            snapshot_group = self._station_command_group_display_name(
                self._station_command_value(snapshot, "current_group", "")
                or self._station_command_value(snapshot, "schedule_group", "")
                or self._station_command_value(snapshot, "group", "")
                or self._station_command_value(snapshot, "group_name", "")
            )
            snapshot_band = str(self._station_command_value(snapshot, "current_band", "") or "").strip().upper()
            if snapshot_group:
                return snapshot_group, snapshot_band
            current_freq = self._station_command_parse_frequency(self._station_command_value(snapshot, "current_frequency_label", ""))
            current_band = snapshot_band
            if current_freq is None:
                return "", current_band
            meta_map = build_qsy_options(load_operating_groups(self.settings))
            for meta in meta_map.values():
                try:
                    meta_freq = parse_frequency_mhz(meta.get("freq"))
                except Exception:
                    meta_freq = None
                if meta_freq is None or abs(float(meta_freq) - float(current_freq)) > 0.0005:
                    continue
                band = str(meta.get("band") or "").strip().upper()
                if current_band and band and band != current_band:
                    continue
                return self._station_command_group_display_name(meta.get("group")), band or current_band
            return "", current_band
        except Exception:
            return "", ""

    def _station_command_now_text(self, snapshot: object) -> str:
        snapshot_id = self._station_command_snapshot_id(snapshot)
        manual_meta = (
            self._station_command_manual_qsy_meta_for_radio(snapshot_id)
            if snapshot_id > 0
            else self._station_command_manual_qsy_meta_for_selected()
        )
        if manual_meta:
            return self._station_command_qsy_label(manual_meta)
        group, band = self._station_command_assigned_plan_group_band(snapshot)
        if not group:
            group, band = self._station_command_schedule_group_band(snapshot)
        if not group:
            group, band = self._station_command_group_band_for_frequency(snapshot)
        if group:
            return " ".join(part for part in (group, band) if part).strip()
        return self._station_command_frequency_text(snapshot)

    def _station_command_reported_dial(self, snapshot: object) -> tuple[str, bool, Optional[int]]:
        """Format only already-published evidence; never probe a radio here."""
        radio_id = self._station_command_snapshot_id(snapshot)
        evidence = getattr(self, "_station_command_endpoint_status_cache", {}).get(radio_id, {})
        fresh = bool(evidence.get("generation") and not evidence.get("stale", True)
                     and not evidence.get("invalidated") and not evidence.get("closed"))
        backend = str(self._station_command_value(snapshot, "control_backend", "")).lower()
        value = evidence.get("js8_frequency_hz") if backend == "js8call" else evidence.get("frequency_hz")
        if value is None:
            fresh = False
            value = self._station_command_value(snapshot, "current_frequency_hz", None)
        try:
            hz = int(value)
            if hz <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            fresh = False
            key = self._station_command_current_frequency_key(snapshot)
            try:
                hz = round(float(key) * 1_000_000)
            except (ValueError, TypeError):
                hz = None
        if hz:
            dial = f"{hz / 1_000_000:.6f}".rstrip("0")
            if dial.endswith("."):
                dial += "000"
            elif len(dial.partition(".")[2]) < 3:
                dial += "0" * (3 - len(dial.partition(".")[2]))
            return f"{dial} MHz" + (" · stale" if not fresh else ""), fresh, hz
        # Legacy detached snapshots can supply a preformatted readback only.
        label = self._station_command_frequency_text(snapshot)
        return (f"{label} · unverified" if label and "mhz" in label.lower()
                else "Frequency unknown"), False, None

    def _station_command_selected_context_text(self, snapshot: object) -> str:
        dial, _fresh, _hz = self._station_command_reported_dial(snapshot)
        name = self._station_command_snapshot_name(snapshot)
        parent = getattr(self, "station_command_radio_summary_widget", None)
        if parent is not None:
            metrics = parent.fontMetrics()
            name = metrics.elidedText(name, Qt.ElideRight, metrics.horizontalAdvance("M") * 18)
        destination = self._station_command_now_text_for_summary(snapshot, self._station_command_snapshot_id(snapshot))
        if self._station_command_manual_qsy_meta_for_radio(self._station_command_snapshot_id(snapshot)):
            destination = f"Target {destination}" if destination else ""
        # Keep the dial ahead of secondary group/band when space is limited.
        return primary_context_text(name, dial) + (f" · {destination}" if destination and destination != dial else "")

    def _station_command_display_evidence(self, snapshot: object) -> dict:
        radio_id = self._station_command_snapshot_id(snapshot)
        evidence = getattr(self, "_station_command_endpoint_status_cache", {}).get(radio_id, {})
        return project_endpoint_display(evidence)

    def _station_command_display_dial(self, snapshot: object) -> tuple[str, bool, Optional[int]]:
        """Last-confirmed presentation only; never used to verify a command."""
        evidence = self._station_command_display_evidence(snapshot)
        backend = str(self._station_command_value(snapshot, "control_backend", "")).lower()
        key = "js8_frequency_hz" if backend == "js8call" else "frequency_hz"
        value = evidence.get(key)
        try:
            hz = int(value)
            if hz <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            return "Frequency unavailable", False, None
        current = evidence.get("display_field_states", {}).get(key) == "current"
        dial = f"{hz / 1_000_000:.6f}".rstrip("0")
        dial += "0" * max(0, 3 - len(dial.partition(".")[2]))
        return f"{dial} MHz", current, hz

    def _station_command_authority_text(self, snapshot: object) -> str:
        radio_id = self._station_command_snapshot_id(snapshot)
        state = self._station_command_manual_control_state_for_radio(radio_id)
        kind = str(getattr(state, "state", "") or "")
        hold = self._station_command_hold_snapshot_for_radio(radio_id)
        if kind == "manual_hold" and getattr(getattr(state, "manual_target", None), "source_action", "") == "qsy":
            kind = "manual_qsy"
        if not kind:
            if self._station_command_scheduler_manual_qsy_active_for_radio(radio_id):
                kind = "manual_qsy"
            elif self._station_command_scheduler_suspended_manually_for_radio(radio_id):
                kind = "manual_suspend"
            elif hold.get("active"):
                kind = "manual_hold"
        fallback = self._station_command_compact_state_text(self._station_command_state_text(snapshot))
        scheduler = getattr(self, 'scheduler', None)
        if getattr(scheduler, '_require_operator_schedule_approval', False):
            review = next((lane.get('schedule_review') or {} for lane in scheduler._cached_active_schedule_lane_rows()
                           if int(lane.get('device_profile_id') or 0) == int(radio_id or 0)), {})
            if not review.get('enabled'):
                fallback = str(review.get('label') or 'Scheduling off — review required')
        return authority_text(kind, hold.get("remaining_sec"), fallback)

    def _station_command_is_held(self, snapshot: object) -> bool:
        radio_id = self._station_command_snapshot_id(snapshot)
        state = self._station_command_manual_control_state_for_radio(radio_id)
        return (str(getattr(state, "state", "")) in {"manual_qsy", "manual_hold", "manual_suspend"}
                or self._station_command_scheduler_manual_qsy_active_for_radio(radio_id)
                or self._station_command_scheduler_suspended_manually_for_radio(radio_id)
                or bool(self._station_command_hold_snapshot_for_radio(radio_id).get("active"))
                or not self._station_command_bool(self._station_command_value(snapshot, "scheduler_enabled", True), default=True))

    def _station_command_qsy_progress_text(self, snapshot: object) -> str:
        radio_id = self._station_command_snapshot_id(snapshot)
        request = getattr(self, "_station_command_qsy_requests", {}).get(radio_id)
        dial, fresh, observed = self._station_command_reported_dial(snapshot)
        evidence = getattr(self, "_station_command_endpoint_status_cache", {}).get(radio_id, {})
        display_state = self._station_command_display_evidence(snapshot).get("display_state")
        readback_notice = ("" if display_state == "current" else
                           "Readback delayed · last confirmed values" if display_state == "delayed" else
                           "Radio readback unavailable")
        if not request:
            return readback_notice
        disposition = str(request.get("disposition") or "queued")
        target_hz = int(request.get("frequency_hz") or 0)
        target = f"{target_hz / 1_000_000:.6f}".rstrip("0").rstrip(".")
        requested = f"Requested {target} MHz"
        if disposition == "manual":
            return f"{requested} · tune manually"
        if disposition == "blocked":
            return "QSY blocked · review radio status"
        scheduler = getattr(self, "scheduler", None)
        getter = getattr(scheduler, "command_result_snapshot", None)
        result = getter(radio_id) if callable(getter) else {}
        if (result and result.get("source") == "QSY"
                and result.get("frequency_hz") == request.get("frequency_hz")
                and float(result.get("completed_monotonic") or 0) >= float(request.get("requested_monotonic") or 0)):
            status = str(result.get("status") or "")
            if status in {"rejected", "failed", "timed_out"}:
                reason = str(result.get("reason_code") or "control unavailable").replace("execution_", "").replace("_", " ")
                return f"QSY blocked · {reason}"
        confirmed_at = request.get("dial_confirmed_monotonic")
        if confirmed_at is not None:
            # This is a brief historical acknowledgement of fresh raw proof,
            # never a current readiness claim or input to a command guard.
            # Ordinary inflight polls cannot reopen a completed dial request.
            if readback_notice:
                return readback_notice
            if fresh and observed is not None and abs(observed - target_hz) > 10:
                return ""
            return "Dial confirmed" if time.monotonic() - confirmed_at <= 5.0 else ""
        # Match the endpoint verification tolerance without granting mode/VFO
        # verification or triggering a probe from this display path.
        if (fresh and observed is not None and abs(observed - target_hz) <= 10
                and float(evidence.get("collected_monotonic") or 0) >= float(request.get("requested_monotonic") or 0)):
            request["dial_confirmed_monotonic"] = time.monotonic()
            return "Dial confirmed"
        if disposition == "pending_verification":
            return f"{requested} · checking radio safety"
        evidence_state = (" · readback delayed" if display_state == "delayed" else
                          " · readback unavailable" if display_state in {"unavailable", "invalidated"} else "")
        return f"{requested} · awaiting readback{evidence_state}"

    def _station_command_remember_qsy_request(self, meta: Mapping[str, object], accepted: bool) -> None:
        try:
            radio_id = int(meta.get("target_device_profile_id") or getattr(self, "_station_command_selected_profile_id", 0) or 0)
            target = round(float(meta.get("freq")) * 1_000_000)
        except (TypeError, ValueError, OverflowError):
            return
        if target <= 0:
            return
        requests = getattr(self, "_station_command_qsy_requests", None)
        if not isinstance(requests, dict):
            requests = self._station_command_qsy_requests = {}
        requests[radio_id] = {
            "frequency_hz": target,
            "requested_monotonic": float(getattr(self, "_station_command_qsy_submit_monotonic", time.monotonic())),
            "disposition": (getattr(self, "_station_command_qsy_dispositions", {}).get(radio_id, "queued")
                            if accepted else "blocked"),
        }

    def _station_command_now_tooltip(self, snapshot: object) -> str:
        exact = self._station_command_frequency_text(snapshot)
        snapshot_id = self._station_command_snapshot_id(snapshot)
        manual_meta = (
            self._station_command_manual_qsy_meta_for_radio(snapshot_id)
            if snapshot_id > 0
            else self._station_command_manual_qsy_meta_for_selected()
        )
        if manual_meta:
            target = self._station_command_qsy_label(manual_meta)
            return f"QSY target: {target}; radio reports: {exact}" if exact else f"QSY target: {target}"
        group, band = self._station_command_schedule_group_band(snapshot)
        if not group:
            group, band = self._station_command_group_band_for_frequency(snapshot)
        target = " ".join(part for part in (group, band) if part).strip()
        try:
            source = str(getattr(getattr(self, "scheduler", None), "current_source", "") or "").strip().upper()
        except Exception:
            source = ""
        if source == "QSY" and target and exact and target != exact:
            return f"QSY target: {target}; radio reports: {exact}"
        return f"{target}: {exact}" if target and exact and target != exact else exact

    @staticmethod
    def _station_command_format_frequency(value: object, *, suffix: str = "MHz") -> str:
        freq = parse_frequency_mhz(value)
        if freq is None:
            text = str(value or "").replace("MHz", "").strip()
            freq = parse_frequency_mhz(text)
        if freq is None:
            return ""
        total_hz = max(0, int(round(float(freq) * 1_000_000)))
        mhz = total_hz // 1_000_000
        khz = (total_hz % 1_000_000) // 1_000
        hz = total_hz % 1_000
        label = f"{mhz}.{khz:03d}.{hz:03d}"
        return f"{label} {suffix}".strip() if suffix else label

    @staticmethod
    def _station_command_state_text(snapshot: object) -> str:
        try:
            if MainWindow._station_command_bool(MainWindow._station_command_value(snapshot, "ptt_active", False)) or MainWindow._station_command_bool(
                MainWindow._station_command_value(snapshot, "shared_ptt_blocked", False)
            ):
                return "Busy: PTT"
            if str(MainWindow._station_command_value(snapshot, "device_class", "") or "").strip().lower() == "observer":
                return "Monitor"
            if not MainWindow._station_command_bool(MainWindow._station_command_value(snapshot, "runtime_active", True), default=True):
                return "Configured inactive"
            if not MainWindow._station_command_bool(
                MainWindow._station_command_value(snapshot, "scheduler_enabled", True),
                default=True,
            ):
                return "Scheduler Off"
            summary = str(MainWindow._station_command_value(snapshot, "status_summary", "") or "").strip()
            if summary and "xml-rpc" not in summary.lower():
                return summary
            state = str(MainWindow._station_command_value(snapshot, "overall_state", "") or "").strip()
            return state.title() if state else "On Schedule"
        except Exception:
            return "unknown"

    def _station_command_next_text(self, snapshot: object) -> str:
        try:
            if str(self._station_command_value(snapshot, "device_class", "") or "").strip().lower() == "observer":
                return (
                    str(self._station_command_value(snapshot, "observer_follow_summary", "") or "").strip()
                    or "Receive-only monitor"
                )
            if not MainWindow._station_command_bool(
                MainWindow._station_command_value(snapshot, "scheduler_enabled", True),
                default=True,
            ):
                return "Scheduler disabled"
            transition_getter = getattr(self, "_station_command_next_transition", None)
            transition = (
                transition_getter(snapshot)
                if callable(transition_getter)
                else MainWindow._station_command_next_transition(self, snapshot)
            )
            if transition is not None:
                state = str(transition.get("state") or "").strip().lower()
                if state == "pending":
                    return "Schedule updating"
                if state == "expired":
                    return "Next transition expired"
                if state == "no_next":
                    return "No next transition"
                entry = transition.get("entry")
                if isinstance(entry, Mapping):
                    next_group, next_band = self._station_command_entry_group_band(entry)
                    next_label = " ".join(part for part in (next_group, next_band) if part).strip()
                    if next_label:
                        return next_label
                if state == "legacy_entry_only":
                    return "Next transition unavailable"
                return "No next transition"
            return "No next transition"
        except Exception:
            return "none"

    @staticmethod
    def _station_command_qsy_frequency_text(meta: Mapping[str, object]) -> str:
        raw = meta.get("frequency", meta.get("freq"))
        return format_frequency_mhz(raw)

    @staticmethod
    def _station_command_qsy_label(meta: Mapping[str, object]) -> str:
        group = MainWindow._station_command_group_display_name(meta.get("group"))
        band = str(meta.get("band") or "").strip().upper()
        parts = []
        if group:
            parts.append(group)
        if band:
            parts.append(band)
        freq = MainWindow._station_command_qsy_frequency_text(meta)
        if freq:
            parts.append(freq)
        if not parts:
            freq = MainWindow._station_command_qsy_frequency_text(meta)
            if freq:
                parts.append(freq)
        return " ".join(part for part in parts if part).strip() or "Frequency"

    @staticmethod
    def _station_command_qsy_tooltip(meta: Mapping[str, object]) -> str:
        label = MainWindow._station_command_qsy_label(meta)
        freq = MainWindow._station_command_qsy_frequency_text(meta)
        mode = str(meta.get("mode") or "").strip()
        details = " ".join(part for part in (freq, mode) if part).strip()
        return f"{label}: {details}" if details else label

    def _station_command_preferred_qsy_key(self, selected: object | None) -> str:
        selected_id = self._station_command_snapshot_id(selected) if selected is not None else 0
        has_assigned_refs = False
        manual_meta = (
            self._station_command_manual_qsy_meta_for_radio(selected_id)
            if selected_id > 0
            else self._station_command_manual_qsy_meta_for_selected()
        )
        if manual_meta:
            key = self._station_command_qsy_option_key(manual_meta)
            if key:
                return key
        if selected_id > 0:
            refs = self._station_command_assigned_plan_refs_for_radio(selected_id)
            has_assigned_refs = bool(refs)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            for ref in refs:
                ref = self._station_command_ref_mapping(ref)
                if ref is None or not self._station_command_ref_active_now(ref, now_utc):
                    continue
                try:
                    freq = self._station_command_parse_frequency(
                        ref.get("frequency") or ref.get("freq") or ref.get("frequency_mhz")
                    )
                    if freq is not None:
                        resolved_meta = self._station_command_qsy_meta_from_plan_ref(
                            ref, self._station_command_operating_group_qsy_lookup()
                        )
                        key = self._station_command_qsy_option_key(resolved_meta) if resolved_meta else ""
                        return key or self._station_command_qsy_option_key({"freq": freq})
                except Exception:
                    pass
            for ref in refs:
                ref = self._station_command_ref_mapping(ref)
                if ref is None:
                    continue
                try:
                    freq = self._station_command_parse_frequency(
                        ref.get("frequency") or ref.get("freq") or ref.get("frequency_mhz")
                    )
                    if freq is not None:
                        resolved_meta = self._station_command_qsy_meta_from_plan_ref(
                            ref, self._station_command_operating_group_qsy_lookup()
                        )
                        key = self._station_command_qsy_option_key(resolved_meta) if resolved_meta else ""
                        return key or self._station_command_qsy_option_key({"freq": freq})
                except Exception:
                    pass
            if not refs:
                lane = self._station_command_lane_for_radio(selected_id)
                entry = lane.get("current_entry") if isinstance(lane, Mapping) else None
                if isinstance(entry, Mapping):
                    try:
                        freq = self._station_command_parse_frequency(entry.get("frequency"))
                        if freq is not None:
                            resolved_meta = self._station_command_qsy_meta_from_plan_ref(
                                entry, self._station_command_operating_group_qsy_lookup()
                            )
                            return self._station_command_qsy_option_key(resolved_meta) if resolved_meta else self._station_command_qsy_option_key({"freq": freq})
                    except Exception:
                        pass
        try:
            sched = getattr(self, "scheduler", None)
            entry = getattr(sched, "current_schedule_entry", {}) if sched is not None else {}
            if isinstance(entry, Mapping):
                if selected_id > 0:
                    entry_id = self._station_command_scheduler_entry_radio_id(entry)
                    if entry_id <= 0 or entry_id != selected_id:
                        raise ValueError("scheduler entry belongs to another radio")
                freq = self._station_command_parse_frequency(entry.get("frequency"))
                if freq is not None:
                    resolved_meta = self._station_command_qsy_meta_from_plan_ref(
                        entry, self._station_command_operating_group_qsy_lookup()
                    )
                    return self._station_command_qsy_option_key(resolved_meta) if resolved_meta else self._station_command_qsy_option_key({"freq": freq})
        except Exception:
            pass
        if selected is not None and (selected_id <= 0 or not has_assigned_refs):
            try:
                freq = self._station_command_parse_frequency(
                    self._station_command_value(selected, "current_frequency_label", "")
                )
                if freq is not None:
                    return self._station_command_qsy_option_key({
                        "freq": freq,
                        "group": self._station_command_value(selected, "schedule_group", ""),
                        "band": self._station_command_value(selected, "schedule_band", ""),
                    })
            except Exception:
                pass
        return ""

    @staticmethod
    def _station_command_qsy_option_key(meta: object) -> str:
        if not isinstance(meta, Mapping):
            return ""
        row_key = str(meta.get("operating_group_frequency_key") or "").strip()
        if row_key:
            return "row:" + row_key
        frequency = meta.get("freq", meta.get("frequency"))
        try:
            hz = normalize_frequency_hz(frequency)
        except Exception:
            hz = None
        if hz is None:
            parsed = parse_frequency_mhz(frequency)
            hz = normalize_frequency_hz(parsed) if parsed is not None else None
        if hz is None:
            return ""
        return "|".join((
            str(meta.get("group") or meta.get("group_name") or "").strip().upper(),
            str(meta.get("band") or "").strip().upper(),
            str(meta.get("mode") or "").strip().upper(),
            str(hz),
            str(meta.get("vfo") or "").strip().upper(),
            str(meta.get("fldigi_mode") or "").strip().upper(),
            str(meta.get("fldigi_offset") or "").strip(),
        ))

    def _refresh_station_command_frequency_combo(self, selected: object | None = None) -> bool:
        combo = getattr(self, "station_command_freq_combo", None)
        if not isinstance(combo, QComboBox):
            return False
        if combo.view().isVisible() or combo.hasFocus():
            return selected_qsy_meta(combo) is not None
        current = selected_qsy_meta(combo)
        current_key = self._station_command_qsy_option_key(current)
        preferred_key = self._station_command_preferred_qsy_key(selected)
        try:
            catalog_meta_map = build_qsy_options(load_operating_groups(self.settings))
        except Exception:
            catalog_meta_map = {}
        meta_map = self._station_command_alternate_qsy_options(catalog_meta_map, selected)
        if isinstance(current, Mapping) and not current.get("operating_group_frequency_key"):
            current_group = self._station_command_group_display_name(current.get("group") or current.get("group_name"))
            current_band = str(current.get("band") or "").strip().upper()
            current_hz = normalize_frequency_hz(current.get("frequency") or current.get("freq"))
            current_mode = str(current.get("mode") or "").strip().upper()
            candidates = [
                key for key, meta in meta_map.items()
                if self._station_command_group_display_name(meta.get("group")).upper() == current_group.upper()
                and str(meta.get("band") or "").strip().upper() == current_band
                and normalize_frequency_hz(meta.get("frequency") or meta.get("freq")) == current_hz
                and (not current_mode or str(meta.get("mode") or "").strip().upper() == current_mode)
            ]
            if len(candidates) == 1:
                current_key = candidates[0]
        previous_block = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("Manual QSY target", None)
            for key, meta in sorted(
                meta_map.items(),
                key=lambda item: (
                    str(item[1].get("group") or "").upper(),
                    str(item[1].get("band") or "").upper(),
                    float(item[1].get("freq") or 0.0),
                ),
            ):
                combo.addItem(self._station_command_qsy_label(meta), meta)
                combo.setItemData(combo.count() - 1, self._station_command_qsy_tooltip(meta), Qt.ToolTipRole)
            select_key = current_key or preferred_key
            if select_key:
                for index in range(combo.count()):
                    data = combo.itemData(index)
                    if isinstance(data, Mapping) and self._station_command_qsy_option_key(data) == select_key:
                        combo.setCurrentIndex(index)
                        break
            if selected_qsy_meta(combo) is None and len(meta_map) == 1 and not preferred_key:
                for index in range(combo.count()):
                    if isinstance(combo.itemData(index), Mapping):
                        combo.setCurrentIndex(index)
                        break
            if selected_qsy_meta(combo) is None:
                combo.setCurrentIndex(-1)
        finally:
            combo.blockSignals(previous_block)
        return selected_qsy_meta(combo) is not None

    def _selected_station_command_hold_minutes(self) -> int:
        return selected_hold_duration(
            getattr(self, "station_command_duration_combo", None),
            self.settings,
            getattr(self, "_active_runtime_profile", None),
        )

    def _publish_station_command_feedback(
        self,
        *,
        action_type: str,
        status: str,
        summary: str,
        detail: str = "",
    ) -> None:
        try:
            service = getattr(self, "action_feedback_service", None)
            if service is None or not hasattr(service, "publish"):
                return
            radio_id = getattr(self, "_station_command_selected_profile_id", None)
            target_label = self.station_command_radio_combo.currentText() if hasattr(self, "station_command_radio_combo") else ""
            service.publish(
                scope="radio",
                action_type=action_type,
                status=status,
                summary=summary,
                radio_profile_id=str(radio_id) if radio_id not in (None, 0, "") else None,
                target_label=str(target_label or "").strip(),
                detail=str(detail or "").strip(),
                source_surface="station_command_bar",
            )
            if str(status or "").strip().lower() in {"blocked", "failed", "error"} and hasattr(self, "statusBar"):
                self.statusBar().showMessage(str(summary or detail or "Station command needs attention."), 6000)
        except Exception as e:
            log.debug("MainWindow: station command feedback failed: %s", e)

    def _on_station_command_hold_duration_changed(self) -> None:
        mins = self._selected_station_command_hold_minutes()
        set_hold_duration_default(self.settings, mins)
        self.on_hold_duration_default_changed()

    def _save_station_command_js8_offset(
        self,
        radio_profile_id: int,
        offset_hz: int,
        *,
        expected_instance_id: int | None = None,
    ) -> bool:
        """Persist a per-radio JS8 default; runtime applies it through safety gates."""

        try:
            saved = self.multi_radio_store.update_radio_js8_offset(
                int(radio_profile_id),
                int(offset_hz),
                expected_instance_id=expected_instance_id,
            )
            manager = getattr(self, "station_runtime_manager", None)
            if manager is not None and hasattr(manager, "update_runtime_js8_offset"):
                manager.update_runtime_js8_offset(int(radio_profile_id), int(offset_hz))
            cache = list(getattr(self, "_station_command_profile_cache", []) or [])
            self._station_command_profile_cache = [
                dict(saved)
                if self._station_command_snapshot_id(item) == int(radio_profile_id)
                else item
                for item in cache
            ]
            if int(getattr(self, "_station_command_selected_profile_id", 0) or 0) == int(radio_profile_id):
                self._active_runtime_profile = dict(saved)
            scheduler = getattr(self, "scheduler", None)
            if scheduler is not None and hasattr(scheduler, "force_refresh"):
                scheduler.force_refresh()
            self._publish_station_command_feedback(
                action_type="js8_offset",
                status="succeeded",
                summary=f"JS8 offset saved: {int(offset_hz)} Hz.",
                detail="CommsPlan will apply the per-radio default only when radio and JS8 activity safety checks permit it.",
            )
            self._refresh_station_command_bar(force=True)
            return True
        except Exception as exc:
            self._publish_station_command_feedback(
                action_type="js8_offset",
                status="failed",
                summary="JS8 offset was not changed.",
                detail=str(exc),
            )
            return False

    def _station_command_selected_qsy_meta(self) -> dict | None:
        combo = getattr(self, "station_command_freq_combo", None)
        if not isinstance(combo, QComboBox):
            return None
        meta = selected_qsy_meta(combo)
        if not isinstance(meta, Mapping):
            return None
        out = dict(meta)
        try:
            profile_id = int(getattr(self, "_station_command_selected_profile_id", 0) or 0)
        except Exception:
            profile_id = 0
        if profile_id > 0:
            out["target_device_profile_id"] = profile_id
        return out

    @staticmethod
    def _station_command_display_plan_name(plan_name: object) -> str:
        text = str(plan_name or "").strip()
        if len(text) > 5 and text.lower().endswith(" plan"):
            return text[:-5].strip()
        return text

    def _on_station_command_qsy_now_clicked(self, meta: Mapping[str, object] | None = None) -> None:
        meta = dict(meta) if isinstance(meta, Mapping) else self._station_command_selected_qsy_meta()
        if not meta:
            self._publish_station_command_feedback(
                action_type="qsy",
                status="blocked",
                summary="QSY blocked: select a manual target.",
                detail="Choose a frequency in the top command bar before using QSY Now.",
            )
            return
        self._station_command_qsy_submit_monotonic = time.monotonic()
        ok = perform_qsy(self, meta)
        self._station_command_remember_qsy_request(meta, ok)
        if ok:
            self._station_command_set_manual_qsy_meta(meta)
            try:
                pending = getattr(self, "_station_command_card_qsy_pending_keys", None)
                if isinstance(pending, dict):
                    pending.pop(int(meta.get("target_device_profile_id") or 0), None)
            except Exception:
                pass
        freq_label = self._station_command_qsy_label(meta)
        self._publish_station_command_feedback(
            action_type="qsy",
            status="partial" if ok else "blocked",
            summary=f"QSY requested: {freq_label}" if ok else f"QSY blocked: {freq_label}",
            detail="Scheduler is suspended in manual QSY state until Resume Schedule or the configured timed hold expires." if ok else "",
        )
        self._refresh_station_command_controls_after_state_change()

    def _on_station_command_qsy_hold_clicked(
        self,
        meta: Mapping[str, object] | None = None,
        hold_minutes: int | None = None,
    ) -> None:
        meta = dict(meta) if isinstance(meta, Mapping) else self._station_command_selected_qsy_meta()
        if not meta:
            self._publish_station_command_feedback(
                action_type="qsy",
                status="blocked",
                summary="QSY Suspend blocked: select a manual target.",
                detail="Choose a frequency in the top command bar before using QSY Suspend.",
            )
            return
        self._station_command_qsy_submit_monotonic = time.monotonic()
        mins = perform_qsy_with_hold(
            self,
            self.settings,
            meta,
            int(hold_minutes or self._selected_station_command_hold_minutes()),
        )
        self._station_command_remember_qsy_request(meta, mins > 0)
        if mins > 0:
            self._station_command_set_manual_qsy_meta(meta)
            try:
                pending = getattr(self, "_station_command_card_qsy_pending_keys", None)
                if isinstance(pending, dict):
                    pending.pop(int(meta.get("target_device_profile_id") or 0), None)
            except Exception:
                pass
        freq_label = self._station_command_qsy_label(meta)
        self._publish_station_command_feedback(
            action_type="qsy",
            status="partial" if mins > 0 else "blocked",
            summary=f"Timed QSY requested: {freq_label}" if mins > 0 else f"Timed QSY blocked: {freq_label}",
            detail=f"Scheduler suspended for {mins} minutes." if mins > 0 else "",
        )
        self._refresh_station_command_controls_after_state_change()

    def _on_station_command_timed_suspend_clicked(self, target_device_profile_id: int | None = None, *, minutes: int | None = None) -> None:
        try:
            target_id = int(target_device_profile_id or getattr(self, "_station_command_selected_profile_id", 0) or 0) or None
        except Exception:
            target_id = None
        try:
            mins = suspend_schedule_hold(
                self,
                self.settings,
                minutes=int(minutes) if minutes is not None else self._selected_station_command_hold_minutes(),
                warn_rf_conflict=True,
                target_device_profile_id=target_id,
            )
            if mins > 0:
                self._station_command_clear_manual_qsy_meta(target_id)
            try:
                self._station_command_timed_suspend_profile_id = int(target_id or 0)
            except Exception:
                self._station_command_timed_suspend_profile_id = 0
            self._publish_station_command_feedback(
                action_type="timed_suspend_schedule",
                status="succeeded" if mins > 0 else "blocked",
                summary=f"Scheduler suspended for {mins} minutes." if mins > 0 else "Timed Suspend blocked.",
                detail="Scheduled frequency changes are paused temporarily. Manual radio changes remain available." if mins > 0 else "",
            )
        except Exception as e:
            self._publish_station_command_feedback(
                action_type="timed_suspend_schedule",
                status="failed",
                summary="Timed Suspend failed.",
                detail=str(e),
            )
        self._refresh_station_command_controls_after_state_change()

    def _on_station_command_pause_clicked(self, target_device_profile_id: int | None = None) -> None:
        try:
            target_id = int(target_device_profile_id or getattr(self, "_station_command_selected_profile_id", 0) or 0) or None
        except Exception:
            target_id = None
        try:
            self._station_command_set_scheduler_suspended_manual(True, target_device_profile_id=target_id)
            self._station_command_timed_suspend_profile_id = 0
            self._publish_station_command_feedback(
                action_type="suspend_schedule",
                status="succeeded",
                summary="Scheduler suspended.",
                detail="Scheduled frequency changes are suspended manually until Resume Schedule.",
            )
        except Exception as e:
            self._publish_station_command_feedback(
                action_type="suspend_schedule",
                status="failed",
                summary="Suspend Scheduler failed.",
                detail=str(e),
            )
        self._refresh_station_command_controls_after_state_change()

    def _on_station_command_resume_clicked(self, target_device_profile_id: int | None = None) -> None:
        try:
            target_profile_id = int(target_device_profile_id or getattr(self, "_station_command_selected_profile_id", 0) or 0) or None
        except Exception:
            target_profile_id = None
        try:
            ok = resume_schedule_hold(
                self,
                self.settings,
                target_device_profile_id=target_profile_id,
            )
        except TypeError:
            ok = False if target_profile_id is not None else resume_schedule_hold(self, self.settings)
        if ok:
            self._station_command_clear_manual_qsy_meta(target_profile_id)
            self._station_command_clear_pending_qsy_for_radio(target_profile_id)
            self._invalidate_station_command_lane_cache()
            getattr(self, "_station_command_qsy_requests", {}).pop(int(target_profile_id or 0), None)
            self._station_command_set_scheduler_suspended_manual(False, target_device_profile_id=target_profile_id)
            self._station_command_timed_suspend_profile_id = 0
        self._publish_station_command_feedback(
            action_type="resume_schedule",
            status="succeeded" if ok else "blocked",
            summary="Schedule resumed." if ok else "Resume blocked.",
            detail="" if ok else "RF Guard or scheduler state prevented resume.",
        )
        self._refresh_station_command_bar(force=True)

    def _refresh_station_command_controls_after_state_change(self) -> None:
        try:
            choices = getattr(self, "_station_command_last_choices", []) or []
        except Exception:
            choices = []
        if isinstance(choices, list) and choices:
            try:
                snapshot = suspend_snapshot(self.settings, allow_reload=False)
            except Exception:
                snapshot = {}
            self._update_station_command_radio_tile_hold_controls(snapshot if isinstance(snapshot, Mapping) else {})
            return
        self._refresh_station_command_bar(force=False)

    def _activate_station_command_radio(self, device_profile_id: int) -> bool:
        ident = int(device_profile_id or 0)
        if ident <= 0:
            return False
        choices = self._station_command_active_radio_choices(
            getattr(self, "_station_command_last_choices", []) or self._station_command_configured_profiles()
        )
        profile = next(
            (
                choice
                for choice in choices
                if self._station_command_snapshot_id(choice) == ident
            ),
            None,
        )
        if profile is None or (
            str(self._station_command_value(profile, "device_class", "")).lower() == "observer"
            or (self._station_command_value(profile, "runtime_active", None) is not None
                and not self._station_command_bool(self._station_command_value(profile, "runtime_active", None), default=False))
        ):
            self._publish_station_command_feedback(
                action_type="select_radio",
                status="failed",
                summary="Radio selection failed.",
                detail="Only active, controllable radios can be selected in the control bar.",
            )
            return False
        try:
            self._active_runtime_profile = dict(profile) if isinstance(profile, Mapping) else profile
        except Exception as exc:
            log.debug("MainWindow: station command radio selection cache failed: %s", exc)
            return False
        return True

    @staticmethod
    def _clear_station_command_health_layout(layout: QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _station_command_health_profile(self, selected: object | None, selected_id: int) -> dict | None:
        # Health summaries are part of the command-bar repaint path.  The
        # profile cache is refreshed by the runtime/settings lifecycle; never
        # reopen the device-profile database from a timer-driven paint/update.
        for profile in (
            list(getattr(self, "_station_command_profile_cache", []) or [])
            + list(getattr(self, "_station_command_last_choices", []) or [])
        ):
            if self._station_command_snapshot_id(profile) == int(selected_id or 0):
                if isinstance(profile, Mapping):
                    return dict(profile)
        return dict(selected) if isinstance(selected, Mapping) else None

    def _station_command_health_items(self, profile: object | None) -> list[tuple[str, str]]:
        profile_map = dict(profile) if isinstance(profile, Mapping) else None
        if profile_map is None:
            ident = self._station_command_snapshot_id(profile) if profile is not None else 0
            if ident > 0:
                profile_map = self._station_command_health_profile(profile, ident)
        if not isinstance(profile_map, dict):
            return []
        try:
            items = visible_status_programs(dict(self.settings.all()), device_profiles=[profile_map])
            return self._station_command_health_monitored_items(
                items,
                radio_profile_id=int(profile_map.get("id", 0) or 0),
            )
        except Exception:
            return []

    @staticmethod
    def _station_command_launch_health_key(name: str) -> str:
        app_name = str(name or "").strip()
        return "JS8Call_API" if app_name == "JS8Call" else app_name

    def _station_command_health_monitored_items(
        self,
        items: list[tuple[str, str]],
        *,
        radio_profile_id: int = 0,
    ) -> list[tuple[str, str]]:
        monitored_by_key: dict[str, bool] = {}
        if radio_profile_id > 0:
            # Production command-bar rendering consumes only the lifecycle
            # published radio projection.  In particular, it must not call
            # get_radio_launch_bundle(), which opens the settings database.
            cache = getattr(self, "_station_command_launch_monitor_cache", {})
            if isinstance(cache, Mapping):
                for key, monitor in tuple(cache.get(int(radio_profile_id), ()) or ()):
                    monitored_by_key[str(key)] = bool(monitor)
        else:
            # Compatibility fallback is station-scoped and is intentionally
            # unavailable to radio-scoped command-bar rendering.  It supports
            # legacy/small isolated callers that have no radio identity.
            try:
                raw_items = self.settings.get("launch_control_items", [])
            except Exception:
                raw_items = []
            if isinstance(raw_items, list):
                builtin_names = set(LAUNCH_APP_ORDER)
                for item in raw_items:
                    if not isinstance(item, Mapping):
                        continue
                    name = str(item.get("name", "") or "").strip()
                    if name not in builtin_names:
                        continue
                    monitored_by_key[self._station_command_launch_health_key(name)] = bool(
                        item.get("monitor_health", item.get("enabled", True))
                    )
        if not monitored_by_key:
            return items
        return [(key, label) for key, label in items if monitored_by_key.get(key, True)]

    def _refresh_station_command_launch_monitor_cache(
        self,
        profiles: Sequence[Mapping[str, object]] | None = None,
    ) -> None:
        """Publish each radio's health-monitor choice outside the paint path."""
        if profiles is None:
            profiles = tuple(getattr(self, "_station_command_profile_cache", ()) or ())
        orchestrator = getattr(self, "launch_orchestrator", None)
        getter = getattr(orchestrator, "get_radio_launch_bundle", None)
        if not callable(getter):
            return
        cache: dict[int, tuple[tuple[str, bool], ...]] = {}
        scopes = {}
        for profile in profiles:
            if not isinstance(profile, Mapping):
                continue
            try:
                radio_profile_id = int(profile.get("id", 0) or 0)
            except Exception:
                radio_profile_id = 0
            if radio_profile_id <= 0:
                continue
            try:
                bundle = getter(radio_profile_id)
            except Exception:
                # Retain a prior published projection rather than making a
                # transient settings read failure look like configuration loss.
                prior = getattr(self, "_station_command_launch_monitor_cache", {})
                if isinstance(prior, Mapping) and radio_profile_id in prior:
                    cache[radio_profile_id] = tuple(prior[radio_profile_id])
                continue
            raw_items = bundle.get("items", []) if isinstance(bundle, Mapping) else []
            monitored: list[tuple[str, bool]] = []
            if isinstance(raw_items, list):
                for item in raw_items:
                    if not isinstance(item, Mapping):
                        continue
                    name = str(item.get("name", "") or "").strip()
                    if name not in LAUNCH_APP_ORDER:
                        continue
                    monitored.append(
                        (
                            self._station_command_launch_health_key(name),
                            bool(item.get("monitor_health", item.get("enabled", True))),
                        )
                    )
            cache[radio_profile_id] = tuple(monitored)
            identities = {}
            for item in raw_items:
                if not isinstance(item, Mapping):
                    continue
                readiness = item.get('readiness_policy') or {}
                if not isinstance(readiness, Mapping):
                    continue
                name = str(item.get('name') or item.get('app_name') or '')
                if not name or (name not in LAUNCH_APP_ORDER and not (item.get('launch_path_override') or item.get('launch_command_override') or readiness.get('executable'))):
                    continue
                environment = readiness.get('environment')
                environment = environment if isinstance(environment, Mapping) else {}
                identities[name] = dict(target=str(item.get('launch_path_override') or item.get('launch_command_override') or readiness.get('executable') or ''),
                    arguments=tuple(item.get('launch_arguments') or readiness.get('launch_arguments') or ()),
                    process_executable=str(readiness.get('process_executable') or ''),
                    process_ini=str(readiness.get('process_ini') or readiness.get('profile_selector') or ''),
                    wine_prefix=str(readiness.get('wine_prefix') or environment.get('WINEPREFIX') or ''),
                    local_expected=str(readiness.get('local_expected','true')).lower() not in {'0','false'})
            scopes[radio_profile_id] = dict(host_override=profile.get('js8_host'),port_override=profile.get('js8_port'),
                flrig_host_override=profile.get('flrig_host'),flrig_port_override=profile.get('flrig_port'),
                fldigi_host_override=profile.get('fldigi_host'),fldigi_port_override=profile.get('fldigi_port'),
                instance_identities=identities)
        self._station_command_launch_monitor_cache = cache
        self._application_status_scope_cache = scopes
        self._refresh_application_status_scopes()

    def _refresh_application_status_scopes(self, snapshot=None) -> None:
        """Queue bounded probes from lifecycle/process publications, never paint."""
        if snapshot is not None and str(getattr(snapshot, 'scope', '')) != 'legacy_primary':
            return
        service = getattr(self, 'dependency_status_service', None)
        if service is None:
            return
        for radio_id, values in tuple(getattr(self, '_application_status_scope_cache', {}).items()):
            service.status_snapshot(radio_id=radio_id, **dict(values))

    def _station_command_health_status_snapshot(self, profile: object | None) -> Mapping[str, object]:
        service = getattr(self, 'dependency_status_service', None)
        radio_id = self._station_command_snapshot_id(profile) if profile is not None else 0
        if service is not None and radio_id:
            published = service.software_status_snapshot(radio_id=radio_id)
            if any(info.get('checked_at') for info in published.values()):
                return published
        # An explicit (even empty) radio-scoped cache is authoritative.  Do
        # not substitute the station/global process cache, which can describe
        # a different radio endpoint and produce cross-radio false warnings.
        if isinstance(profile, Mapping) and "service_states" in profile:
            service_states = profile.get("service_states")
            return service_states if isinstance(service_states, Mapping) else {}
        if profile is not None and hasattr(profile, "service_states"):
            service_states = getattr(profile, "service_states", {})
            return service_states if isinstance(service_states, Mapping) else {}
        try:
            return self.dependency_status_service.software_status_snapshot()
        except Exception:
            return {}

    def _station_command_control_health_issue(self, profile: object | None) -> tuple[str, str, str, str] | None:
        if profile is None:
            return None
        backend = str(self._station_command_value(profile, "control_backend", "") or "").strip().lower()
        if backend in {"", "manual"}:
            return None
        control_ready = self._station_command_value(profile, "control_ready", None)
        if control_ready is None or self._station_command_bool(control_ready, default=False):
            return None
        label = {
            "flrig": "FLRig",
            "rigctld": "RigCtlD",
            "js8call": "JS8",
        }.get(backend, backend.upper())
        tooltip = str(self._station_command_value(profile, "status_summary", "") or "").strip()
        if not tooltip:
            tooltip = f"{label} control is not reachable."
        return ("__control_ready__", label, "warn", tooltip)

    @staticmethod
    def _station_command_health_state(info: Mapping[str, object]) -> str:
        state = str(info.get("state", "idle") or "idle").strip().lower()
        if state == "ok":
            return "ok"
        if state == "error":
            return "error"
        return "warn"

    @staticmethod
    def _station_command_health_is_live_dependency(key: str, profile: object | None = None) -> bool:
        normalized = str(key or "").strip()
        if normalized in {"FLRig", "RigCtlD", "JS8Call_API", "Observer", "VarAC Cluster"}:
            return True
        backend = str(MainWindow._station_command_value(profile, "control_backend", "") or "").strip().lower()
        return normalized == {
            "flrig": "FLRig",
            "rigctld": "RigCtlD",
            "js8call": "JS8Call_API",
        }.get(backend, "")

    @staticmethod
    def _station_command_health_issue_state(
        key: str,
        info: Mapping[str, object],
        profile: object | None = None,
    ) -> str:
        state = str(info.get("state", "idle") or "idle").strip().lower()
        if state == "error":
            return "error"
        if state == "warn":
            return "warn"
        if state == "ok":
            return "ok"
        if MainWindow._station_command_health_is_live_dependency(key, profile):
            return "warn"
        return "ok"

    @staticmethod
    def _station_command_health_ready_tooltip(
        key: str,
        label_text: str,
        info: Mapping[str, object],
        profile: object | None = None,
    ) -> str:
        tooltip = str(info.get("tooltip", "") or "").strip()
        state = str(info.get("state", "idle") or "idle").strip().lower()
        if state == "ok":
            return tooltip or f"{label_text} is ready."
        if MainWindow._station_command_health_is_live_dependency(key, profile):
            return tooltip or f"{label_text} is not reachable."
        return f"{label_text} is available when needed."

    @staticmethod
    def _station_command_health_summary_state(issue_states: list[str]) -> str:
        if not issue_states:
            return "ok"
        if any(state == "error" for state in issue_states):
            return "error"
        return "warn"

    def _station_command_off_schedule_payload_for_profile(self, profile: dict | None) -> dict[str, object] | None:
        radio_id = self._station_command_snapshot_id(profile) if profile is not None else 0
        if radio_id <= 0:
            return None
        state = getattr(self, "_station_command_off_schedule_by_radio", {})
        if not isinstance(state, Mapping):
            return None
        payload = state.get(radio_id)
        return dict(payload) if isinstance(payload, Mapping) else None

    def _station_command_assignment_rf_guard_issues(self, profile: dict | None) -> list[tuple[str, str]]:
        radio_id = self._station_command_snapshot_id(profile) if profile is not None else 0
        if radio_id <= 0:
            return []
        lane = self._station_command_lane_for_radio(radio_id)
        if not isinstance(lane, Mapping):
            # Compatibility for isolated presenters/tests constructed without
            # the production scheduler. A real MainWindow never performs this
            # fallback during command-bar rendering.
            if hasattr(self, "scheduler"):
                return []
            try:
                assignment = self.multi_radio_store.get_effective_assigned_plan_for_device(radio_id)
            except Exception:
                assignment = None
            if not isinstance(assignment, Mapping):
                return []
            lane = {
                "assignment_validation_status_json": assignment.get(
                    "validation_status_json", ""
                )
            }
        try:
            validation = json.loads(
                str(lane.get("assignment_validation_status_json", "") or "{}")
            )
        except Exception:
            validation = {}
        if not isinstance(validation, Mapping):
            return []
        state = str(validation.get("state", "") or "").strip().lower()
        if state not in {"blocked", "warning"}:
            return []
        messages = [
            str(item).strip()
            for key in ("blocked", "warnings", "messages")
            for item in (validation.get(key) or [])
            if str(item or "").strip()
        ]
        if not messages:
            messages = ["RF Guard detected an assigned schedule issue."]
        severity = "error" if state == "blocked" else "warn"
        return [(severity, message) for message in messages]

    def _station_command_health_summary_for_profile(self, profile: object | None) -> dict[str, object]:
        items = self._station_command_health_items(profile)
        off_schedule_payload = self._station_command_off_schedule_payload_for_profile(profile)
        assignment_guard_issues = self._station_command_assignment_rf_guard_issues(profile)
        off_schedule_items = []
        if isinstance(off_schedule_payload, Mapping):
            raw_items = off_schedule_payload.get("items")
            if isinstance(raw_items, list):
                off_schedule_items = [str(item).strip() for item in raw_items if str(item).strip()]
        snapshot = self._station_command_health_status_snapshot(profile)
        operational_issues: list[tuple[str, str, str, str]] = []
        advisories: list[tuple[str, str, str, str]] = []
        if off_schedule_items:
            operational_issues.append(
                (
                    "__off_schedule__",
                    "Off Schedule",
                    "warn",
                    "Review " + ", ".join(off_schedule_items) + " before trusting scheduler position.",
                )
            )
        for severity, message in assignment_guard_issues:
            operational_issues.append(
                (
                    "__schedule_assignment_rf_guard__",
                    "RF Guard",
                    severity,
                    message,
                )
            )
        healthy_count = 0
        for key, label_text in items:
            info = snapshot.get(key, {})
            state = self._station_command_health_issue_state(key, info, profile)
            tooltip = self._station_command_health_ready_tooltip(key, label_text, info, profile)
            if state == "ok":
                healthy_count += 1
            else:
                # Process/application imperfections remain visible in Health
                # Details, but do not color the radio unless cached endpoint
                # evidence says they currently impair operation.
                advisories.append((key, label_text, state, tooltip))
        control_issue = self._station_command_control_health_issue(profile)
        if control_issue is not None:
            _control_key, control_label, _control_state, _control_tooltip = control_issue
            if not any(label == control_label for _key, label, _state, _tooltip in operational_issues):
                operational_issues.append(control_issue)

        operational_state = str(
            self._station_command_value(profile, "operational_state", "") or ""
        ).strip().lower()
        operational_detail = str(
            self._station_command_value(profile, "operational_detail", "")
            or self._station_command_value(profile, "status_summary", "")
            or ""
        ).strip()
        if operational_state in {"control_stalled", "endpoint_unavailable", "receiver_unavailable"}:
            if not any(key == "__control_ready__" for key, _label, _state, _tooltip in operational_issues):
                operational_issues.append(
                    (
                        "__operational__",
                        "Radio control",
                        "warn",
                        operational_detail or "The selected radio endpoint is unavailable.",
                    )
                )
        elif operational_state == "readback_mismatch":
            operational_issues.append(
                (
                    "__operational__",
                    "Schedule readback",
                    "warn",
                    operational_detail or "The radio readback does not match the active schedule.",
                )
            )

        issue_items = operational_issues + advisories
        operational_issue_states = [state for _key, _label, state, _tooltip in operational_issues]
        summary_state = self._station_command_health_summary_state(operational_issue_states)
        if not operational_issue_states:
            control_ready = self._station_command_value(profile, "control_ready", None)
            if operational_state in {"on_schedule_verified", "js8_verification_unavailable"}:
                summary_state = "ok"
            elif operational_state or control_ready is None:
                summary_state = "idle"
            elif self._station_command_bool(control_ready, default=False):
                summary_state = "ok"
        if off_schedule_items:
            summary_label = "Off Schedule"
            summary_tooltip = "Off Schedule: " + ", ".join(off_schedule_items)
        elif assignment_guard_issues:
            summary_label = "RF Guard"
            summary_tooltip = "; ".join(message for _severity, message in assignment_guard_issues)
        elif summary_state == "error":
            summary_label = "Setup"
            summary_tooltip = "; ".join(
                f"{label}: {tooltip}" for _key, label, _state, tooltip in operational_issues
            )
        elif summary_state == "warn":
            summary_label = "Review"
            summary_tooltip = "; ".join(
                f"{label}: {tooltip}" for _key, label, _state, tooltip in operational_issues
            )
        elif summary_state == "ok":
            summary_label = "Operational"
            summary_tooltip = operational_detail or "Radio control is operational."
            if advisories:
                summary_tooltip += f" {len(advisories)} advisory item(s) are available in Health Details."
        elif not items and not operational_state:
            summary_label = "No checks"
            summary_tooltip = "No configured software health items for this radio."
        else:
            summary_label = "Checking"
            summary_tooltip = operational_detail or "Waiting for current cached radio-control evidence."
            if advisories:
                summary_tooltip += f" {len(advisories)} advisory item(s) are available in Health Details."
        return {
            "items": items,
            "healthy_count": healthy_count,
            "issues": issue_items,
            "operational_issues": operational_issues,
            "advisories": advisories,
            "state": summary_state,
            "label": summary_label,
            "tooltip": summary_tooltip,
        }

    def _show_station_command_health_menu(
        self,
        *,
        device_profile_id: int = 0,
        snapshot: object | None = None,
        anchor: QWidget | None = None,
    ) -> None:
        ident = int(device_profile_id or 0)
        selected = snapshot
        if selected is None and ident > 0:
            try:
                for candidate in getattr(self, "_station_command_last_choices", []) or []:
                    if self._station_command_snapshot_id(candidate) == ident:
                        selected = candidate
                        break
            except Exception:
                selected = None
        if ident <= 0 and selected is not None:
            ident = self._station_command_snapshot_id(selected)
        profile = self._station_command_health_profile(selected, ident)
        subject = selected if selected is not None else profile
        summary = self._station_command_health_summary_for_profile(subject)
        radio_name = self._station_command_snapshot_name(profile or selected or {"id": ident})
        anchor_widget = anchor or getattr(self, "station_command_health_widget", None) or self
        try:
            theme = resolve_theme(self.settings)
        except Exception:
            theme = {}
        menu_surface = theme.get("station_control_tile_selected_surface", theme.get("surface", "#FFFFFF"))
        menu_border = theme.get("station_control_tile_selected_border", theme.get("border", "#D3D7DD"))
        menu_text = theme.get("station_control_text", theme.get("text", "#222222"))
        menu_muted = theme.get("station_control_muted", theme.get("text_muted", "#6A737D"))
        menu = QMenu(anchor_widget)
        menu.setObjectName("stationCommandHealthMenu")
        menu.setStyleSheet(
            "QMenu#stationCommandHealthMenu {"
            f"background: {menu_surface}; color: {menu_text}; border: 1px solid {menu_border};"
            "padding: 5px;"
            "}"
            "QMenu#stationCommandHealthMenu::item {"
            "padding: 5px 22px 5px 10px;"
            "}"
            "QMenu#stationCommandHealthMenu::item:disabled {"
            f"color: {menu_muted};"
            "}"
            "QMenu#stationCommandHealthMenu::item:selected {"
            f"background: {theme.get('accent', menu_border)}; color: {theme.get('surface', '#FFFFFF')};"
            "}"
        )
        title = QAction(f"{radio_name} Health: {summary.get('label')}", menu)
        title.setEnabled(False)
        menu.addAction(title)
        issues = [tuple(item) for item in summary.get("issues", []) if isinstance(item, tuple)]
        operational_keys = {
            str(item[0])
            for item in summary.get("operational_issues", [])
            if isinstance(item, tuple) and item
        }
        if issues:
            for issue_key, label, state, tooltip in issues[:6]:
                if state == "error":
                    prefix = "Fix"
                elif str(issue_key) in operational_keys:
                    prefix = "Affects operation"
                else:
                    prefix = "Advisory"
                action = QAction(f"{prefix}: {label} - {tooltip}", menu)
                action.setEnabled(False)
                menu.addAction(action)
            if len(issues) > 6:
                more = QAction(f"+{len(issues) - 6} more item(s)", menu)
                more.setEnabled(False)
                menu.addAction(more)
        else:
            detail = str(summary.get("tooltip", "") or "No health issues for this radio.")
            action = QAction(detail, menu)
            action.setEnabled(False)
            menu.addAction(action)
        menu.addSeparator()
        open_action = QAction("Open Health Details", menu)
        open_action.triggered.connect(lambda _checked=False, profile_id=ident: self._open_station_health_detail(device_profile_id=profile_id))
        menu.addAction(open_action)
        if ident > 0 and issues:
            settings_action = QAction("Open Radio Settings", menu)
            settings_action.triggered.connect(
                lambda _checked=False, profile_id=ident: self.open_settings_section(
                    "radio_profiles",
                    radio_id=profile_id,
                    settings_nav_context="radios",
                )
            )
            menu.addAction(settings_action)
        self._station_command_health_menu = menu
        try:
            menu.popup(anchor_widget.mapToGlobal(anchor_widget.rect().bottomLeft()))
        except Exception:
            self._open_station_health_detail(device_profile_id=ident)

    def _station_command_attention_key(self, snapshot: object) -> tuple[str, object]:
        """Return a stable display key without resolving any external state."""
        ident = self._station_command_snapshot_id(snapshot)
        if ident > 0:
            return ("id", ident)
        name = self._station_command_snapshot_name(snapshot).strip().casefold()
        return ("name", name or id(snapshot))

    def _station_command_unique_attention_snapshots(
        self,
        choices: Sequence[object],
    ) -> list[object]:
        """Deduplicate already-classified snapshots while preserving order."""
        unique: list[object] = []
        seen: set[tuple[str, object]] = set()
        for snapshot in choices:
            key = self._station_command_attention_key(snapshot)
            if key in seen:
                continue
            seen.add(key)
            unique.append(snapshot)
        return unique

    def _station_command_cached_attention_reason(self, snapshot: object) -> str:
        """Describe cached attention state without health, scheduler, or endpoint calls."""
        if self._station_command_bool(self._station_command_value(snapshot, "ptt_active", False)):
            return "PTT active"
        if self._station_command_bool(self._station_command_value(snapshot, "shared_ptt_blocked", False)):
            return (
                str(self._station_command_value(snapshot, "shared_ptt_status_text", "") or "").strip()
                or "Shared PTT blocked"
            )

        off_schedule = self._station_command_off_schedule_payload_for_profile(snapshot)
        if isinstance(off_schedule, Mapping):
            return "Off Schedule"

        # Read a previously rendered lane cache directly. Calling the lane
        # accessor here could build a schedule projection, which is forbidden
        # while opening this lightweight disclosure.
        ident = self._station_command_snapshot_id(snapshot)
        lane_cache = getattr(self, "_station_command_lane_cache_data", None)
        lane = lane_cache.get(ident) if isinstance(lane_cache, Mapping) else None
        if isinstance(lane, Mapping):
            try:
                validation = json.loads(str(lane.get("assignment_validation_status_json", "") or "{}"))
            except Exception:
                validation = {}
            if isinstance(validation, Mapping) and str(validation.get("state", "") or "").strip().lower() in {
                "blocked",
                "warning",
            }:
                return "RF Guard"

        service_states = self._station_command_value(snapshot, "service_states", {})
        service_candidates: list[tuple[int, str]] = []
        if isinstance(service_states, Mapping):
            for key, raw_info in service_states.items():
                if not isinstance(raw_info, Mapping):
                    continue
                state = str(raw_info.get("state", "idle") or "idle").strip().lower()
                is_live_idle = state not in {"ok", "error", "warn", "warning", "attention"} and self._station_command_health_is_live_dependency(
                    str(key), snapshot
                )
                if state not in {"error", "warn", "warning", "attention"} and not is_live_idle:
                    continue
                label = str(key or "Service").replace("_API", "").strip() or "Service"
                tooltip = str(raw_info.get("tooltip", "") or "").strip()
                if tooltip and len(tooltip) <= 64:
                    reason = tooltip.rstrip(".")
                elif state == "error":
                    reason = f"{label} error"
                elif is_live_idle:
                    reason = f"{label} unavailable"
                else:
                    reason = f"{label} needs review"
                service_candidates.append((2 if state == "error" else 1, reason))
        if service_candidates:
            service_candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
            return service_candidates[0][1]

        warning = str(self._station_command_value(snapshot, "warning_text", "") or "").strip()
        if warning:
            return warning if len(warning) <= 64 else "Configuration warning"

        backend = str(self._station_command_value(snapshot, "control_backend", "") or "").strip()
        control_ready = self._station_command_value(snapshot, "control_ready", None)
        if backend.lower() not in {"", "manual"} and control_ready is not None and not self._station_command_bool(control_ready):
            return f"{backend.upper()} unavailable"

        state = self._station_command_compact_state_text(self._station_command_state_text(snapshot)).strip()
        if state and state.lower() not in {"clear", "healthy", "on schedule", "unknown"}:
            return state
        return "Review status"

    def _station_command_cached_attention_rank(self, snapshot: object, selected_id: int) -> int:
        """Rank already-classified snapshots using immutable/cached fields only."""
        ident = self._station_command_snapshot_id(snapshot)
        state = str(self._station_command_value(snapshot, "overall_state", "") or "").strip().lower()
        services = self._station_command_value(snapshot, "service_states", {})
        service_error = isinstance(services, Mapping) and any(
            isinstance(info, Mapping) and str(info.get("state", "") or "").strip().lower() == "error"
            for info in services.values()
        )
        if (
            self._station_command_bool(self._station_command_value(snapshot, "ptt_active", False))
            or self._station_command_bool(self._station_command_value(snapshot, "shared_ptt_blocked", False))
            or service_error
            or state in {"error", "failed", "blocked"}
        ):
            score = 1000
        else:
            score = 700
        if self._station_command_bool(self._station_command_value(snapshot, "runtime_primary", False)):
            score += 80
        if self._station_command_bool(self._station_command_value(snapshot, "runtime_active", False)):
            score += 40
        if ident > 0 and ident == int(selected_id or 0):
            score += 250
        return score

    def _station_command_attention_summary_entries(
        self,
        choices: Sequence[object],
        selected_id: int,
        *,
        limit: int = 3,
    ) -> list[tuple[object, int, str, str, str]]:
        """Build a bounded, cache-only attention summary for the control bar."""
        entries: list[tuple[object, int, str, str, str]] = []
        for snapshot in self._station_command_unique_attention_snapshots(choices):
            ident = self._station_command_snapshot_id(snapshot)
            reason = self._station_command_cached_attention_reason(snapshot)
            name = self._station_command_snapshot_name(snapshot)
            entries.append((snapshot, ident, name, reason, reason))
        entries.sort(
            key=lambda entry: (
                -self._station_command_cached_attention_rank(entry[0], selected_id),
                entry[2].casefold(),
            )
        )
        return entries[: max(1, int(limit or 3))]

    def _show_station_command_attention_menu(
        self,
        *,
        choices: Sequence[object],
        selected_id: int,
        anchor: QWidget,
        theme: Mapping[str, object] | None = None,
    ) -> None:
        """Show cached attention by radio without probing endpoints or storage."""
        affected = self._station_command_unique_attention_snapshots(choices)
        entries = self._station_command_attention_summary_entries(affected, selected_id)
        affected_count = len(affected)
        if not affected_count:
            return
        menu_theme = dict(theme or {})
        menu = QMenu(anchor)
        menu.setObjectName("stationCommandAttentionMenu")
        menu.setToolTipsVisible(True)
        menu.setStyleSheet(
            "QMenu#stationCommandAttentionMenu {"
            f"background: {menu_theme.get('surface', '#FFFFFF')}; color: {menu_theme.get('text', '#222222')}; "
            f"border: 1px solid {menu_theme.get('border', '#D3D7DD')}; padding: 5px;"
            "}"
            "QMenu#stationCommandAttentionMenu::item { padding: 5px 22px 5px 10px; }"
        )
        noun = "radio" if affected_count == 1 else "radios"
        verb = "needs" if affected_count == 1 else "need"
        title = QAction(f"{affected_count} {noun} {verb} attention", menu)
        title.setEnabled(False)
        menu.addAction(title)
        for _snapshot, ident, name, reason, status in entries:
            action = QAction(f"Review {name} — {reason}", menu)
            action.setToolTip(f"{name}: {status}. Open Station Health focused on this source.")
            action.setEnabled(ident > 0)
            action.triggered.connect(
                lambda _checked=False, profile_id=ident: self._open_station_health_detail(device_profile_id=profile_id)
            )
            menu.addAction(action)
        remaining = affected_count - len(entries)
        if remaining > 0:
            more = QAction(f"+{remaining} more — open Station Health", menu)
            more.setToolTip("Open Station Health to review every affected radio.")
            more.triggered.connect(lambda _checked=False: self._open_station_health_detail())
            menu.addAction(more)
        menu.addSeparator()
        open_action = QAction("Open Station Health", menu)
        open_action.setToolTip("Open Station Health with the complete issue list.")
        open_action.triggered.connect(lambda _checked=False: self._open_station_health_detail())
        menu.addAction(open_action)
        self._station_command_attention_menu = menu
        try:
            menu.popup(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        except Exception:
            self._open_station_health_detail()

    def _add_station_command_health_item(
        self,
        *,
        key: str,
        label_text: str,
        state: str,
        tooltip: str,
        theme: dict,
    ) -> None:
        led = self.station_command_health_leds.get(key)
        text_label = self.station_command_health_text_labels.get(key)
        if led is None or text_label is None:
            led = QLabel(self.station_command_health_widget)
            led.setFixedSize(14, 14)
            led.setCursor(Qt.PointingHandCursor)
            led.mousePressEvent = lambda event, widget=led: self._on_station_command_health_clicked(event, anchor=widget)
            text_label = QLabel(self.station_command_health_widget)
            text_label.setCursor(Qt.PointingHandCursor)
            text_label.mousePressEvent = lambda event, widget=text_label: self._on_station_command_health_clicked(event, anchor=widget)
            self.station_command_health_leds[key] = led
            self.station_command_health_text_labels[key] = text_label
            self.station_command_health_layout.addWidget(led)
            self.station_command_health_layout.addWidget(text_label)
            self.station_command_health_layout.addStretch(1)

        set_widget_value(led, "styleSheet", led_style(state, theme))
        set_widget_value(led, "toolTip", tooltip)
        set_widget_value(text_label, "text", label_text)
        set_widget_value(text_label, "toolTip", tooltip)
        set_widget_value(
            text_label,
            "styleSheet",
            f"background: transparent; color: {theme.get('station_control_text', theme.get('text', '#222222'))};",
        )

    def _refresh_station_command_health(self, selected: object | None, selected_id: int) -> None:
        if not hasattr(self, "station_command_health_layout"):
            return
        if not isinstance(getattr(self, "station_command_health_leds", None), dict):
            self.station_command_health_leds = {}
        if not isinstance(getattr(self, "station_command_health_text_labels", None), dict):
            self.station_command_health_text_labels = {}
        profile = self._station_command_health_profile(selected, selected_id)
        subject = selected if selected is not None else profile
        items = self._station_command_health_items(subject)
        theme = resolve_theme(self.settings)
        summary = self._station_command_health_summary_for_profile(subject)
        label_text = str(summary.get("label", "Checking") or "Checking")
        state = str(summary.get("state", "idle") or "idle")
        tooltip = str(summary.get("tooltip", "") or "Waiting for current cached radio-control evidence.")
        has_items = bool(items)
        widget = self.station_command_health_widget
        font = widget.font()
        font_signature = (font.toString(), int(widget.fontMetrics().height()))
        theme_signature = tuple(sorted((str(key), repr(value)) for key, value in theme.items()))
        # The rendered summary carries cached endpoint freshness through its
        # state and tooltip. Include the selected radio, visibility, palette,
        # and font so a presentation change cannot be hidden by this fast path.
        signature = (
            int(selected_id or 0),
            has_items,
            label_text,
            state,
            tooltip,
            tuple((str(key), str(label)) for key, label in items),
            theme_signature,
            font_signature,
        )
        if signature == getattr(self, "_station_command_health_widget_signature", None):
            return
        self._add_station_command_health_item(
            key="__summary__",
            label_text=label_text,
            state=state,
            tooltip=tooltip,
            theme=theme,
        )
        self.station_command_health_label.setVisible(has_items)
        if widget.isHidden() == has_items:
            widget.setVisible(has_items)
        self._station_command_health_widget_signature = signature

    @staticmethod
    def _station_command_is_controllable_profile(profile: object) -> bool:
        device_class = str(MainWindow._station_command_value(profile, "device_class", "tx_rx") or "tx_rx").strip().lower()
        if device_class == "observer":
            return False
        backend = str(MainWindow._station_command_value(profile, "control_backend", "manual") or "manual").strip().lower()
        return backend in SUPPORTED_RUNTIME_CONTROL_BACKENDS

    def _station_command_configured_profiles(self) -> list[object]:
        # This method is called from the command-bar repaint path. Never open
        # SQLite here: the runtime manager owns endpoint refresh and publishes
        # immutable snapshots, while the last rendered choices cover its
        # brief startup/restart gaps.
        profiles = list(getattr(self, "_station_command_profile_cache", []) or [])
        if not profiles:
            profiles = list(getattr(self, "_station_command_last_choices", []) or [])
        if not profiles:
            active = getattr(self, "_active_runtime_profile", None)
            if isinstance(active, Mapping) and active:
                profiles = [dict(active)]
        return [
            dict(profile) if isinstance(profile, Mapping) else profile
            for profile in profiles
            if self._station_command_is_controllable_profile(profile)
            or str(self._station_command_value(profile, "device_class", "")).lower() == "observer"
        ]

    def _station_command_selected_snapshot(self, choices: list[object]) -> object | None:
        selected_id = getattr(self, "_station_command_selected_profile_id", None)
        if selected_id not in (None, 0):
            for snapshot in choices:
                if self._station_command_snapshot_id(snapshot) == int(selected_id):
                    return snapshot
        primary = next(
            (
                snapshot
                for snapshot in choices
                if self._station_command_bool(self._station_command_value(snapshot, "runtime_primary", False))
            ),
            None,
        )
        if primary is not None:
            return primary
        active = next(
            (
                snapshot
                for snapshot in choices
                if self._station_command_bool(self._station_command_value(snapshot, "runtime_active", False))
            ),
            None,
        )
        if active is not None:
            return active
        return choices[0] if choices else None

    def _station_command_now_text_for_summary(self, snapshot: object, selected_id: int) -> str:
        ident = self._station_command_snapshot_id(snapshot)
        if ident > 0 and ident == int(selected_id or 0):
            return self._station_command_now_text(snapshot)
        group, band = self._station_command_assigned_plan_group_band(snapshot)
        if not group:
            group, band = self._station_command_schedule_group_band(snapshot)
        if not group:
            group, band = self._station_command_group_band_for_frequency(snapshot)
        if group:
            return " ".join(part for part in (group, band) if part).strip()
        return self._station_command_frequency_text(snapshot)

    @staticmethod
    def _station_command_compact_state_text(state_text: object) -> str:
        text = str(state_text or "").strip()
        if not text:
            return "Ready"
        if text.lower() in {"ok", "ready"}:
            return "Ready"
        if text.lower().startswith("configured"):
            return "Inactive"
        return text

    def _station_command_radio_summary_text(self, snapshot: object, selected_id: int) -> str:
        name = self._station_command_snapshot_name(snapshot)
        now = self._station_command_now_text_for_summary(snapshot, selected_id)
        state = self._station_command_compact_state_text(self._station_command_state_text(snapshot))
        parts = [name]
        if now:
            parts.append(now)
        if state and state.lower() not in {"on schedule"}:
            parts.append(state)
        return " | ".join(parts)

    def _station_command_radio_summary_tooltip(self, snapshot: object, selected_id: int) -> str:
        name = self._station_command_snapshot_name(snapshot)
        now = self._station_command_now_text_for_summary(snapshot, selected_id)
        exact = self._station_command_frequency_text(snapshot)
        state = self._station_command_compact_state_text(self._station_command_state_text(snapshot))
        next_text = self._station_command_next_text(snapshot)
        return f"{name}\nNow: {now}\nFrequency: {exact}\nState: {state}\nNext: {next_text}"

    def _station_command_active_radio_choices(self, choices: list[object]) -> list[object]:
        active = [
            snapshot
            for snapshot in choices
            if self._station_command_value(snapshot, "runtime_active", None) is None
            or self._station_command_bool(self._station_command_value(snapshot, "runtime_active", False), default=False)
        ]
        return active if active else choices

    def _station_command_radio_cards_per_page(self, total: int) -> int:
        total = max(1, int(total or 1))
        return total

    def _station_command_radio_summary_available_width(self) -> int:
        scroll_width = 0
        viewport_width = 0
        try:
            scroll = getattr(self, "station_command_radio_summary_scroll", None)
            viewport = getattr(scroll, "viewport", lambda: None)()
            viewport_width = int(getattr(viewport, "width", lambda: 0)() or 0)
            scroll_width = int(getattr(scroll, "width", lambda: 0)() or 0)
        except Exception:
            viewport_width = 0
        try:
            bar_width = int(getattr(self.station_command_bar, "width", lambda: 0)() or self.width() or 0)
        except Exception:
            bar_width = 0
        fallback_width = viewport_width or scroll_width or bar_width or 660
        width = max(fallback_width, viewport_width, scroll_width, bar_width) - 20
        return max(1, width)

    def _station_command_dual_radio_cards_fit(self, choices: Sequence[object]) -> bool:
        if len(choices) != 2:
            return False
        width = self._station_command_radio_summary_available_width()
        return width >= (380 * 2 + 6)

    def _station_command_card_choices_for_layout(self, choices: Sequence[object], selected_id: int) -> list[object]:
        card_choices = list(choices)
        if not card_choices:
            return []
        if self._station_command_dual_radio_cards_fit(card_choices):
            return card_choices
        focused_snapshot = next(
            (
                snapshot
                for snapshot in card_choices
                if self._station_command_snapshot_id(snapshot) == int(selected_id or 0)
            ),
            card_choices[0],
        )
        return [focused_snapshot]

    def _station_command_radio_card_width(self, count: int) -> int:
        count = max(1, int(count or 1))
        width = self._station_command_radio_summary_available_width()
        if count <= 1:
            return max(280, min(900, width))
        gaps = max(0, count - 1) * 6
        available = max(300, width - gaps)
        minimum_card = 380 if count == 2 else 280
        if count * minimum_card + gaps <= width:
            return max(minimum_card, min(480, available // count))
        return minimum_card

    def _station_command_radio_page_slice(self, choices: list[object]) -> tuple[list[object], int, int, int]:
        total = len(choices)
        per_page = self._station_command_radio_cards_per_page(total)
        page_count = max(1, (total + per_page - 1) // per_page)
        try:
            page = int(getattr(self, "_station_command_radio_page", 0) or 0)
        except Exception:
            page = 0
        page = max(0, min(page, page_count - 1))
        self._station_command_radio_page = page
        start = page * per_page
        return choices[start : start + per_page], page, page_count, per_page

    def _change_station_command_radio_page(self, delta: int) -> None:
        try:
            current = int(getattr(self, "_station_command_radio_page", 0) or 0)
        except Exception:
            current = 0
        self._station_command_radio_page = max(0, current + int(delta or 0))
        self._station_command_radio_summary_signature = None
        self._refresh_station_command_bar(force=True)

    def _style_station_command_radio_summary_button(self, button: QPushButton, *, selected: bool, state_text: str) -> None:
        try:
            theme = resolve_theme(self.settings)
        except Exception:
            theme = {}
        state = str(state_text or "").strip().lower()
        if state in {"inactive", "configured inactive", "not enabled"}:
            role = "muted"
        elif state in {"ptt active", "rf guard blocked", "blocked", "error", "failed"}:
            role = "danger"
        elif state in {"manual hold", "manual qsy", "scheduler suspended", "needs review", "warning", "warn"}:
            role = "warning"
        else:
            role = "success" if selected else "info"
        font = button.font()
        font.setBold(True)
        button.setFont(font)
        button.setStyleSheet(button_style(role, theme))

    def _station_command_attention_role_for_snapshot(self, snapshot: object) -> tuple[str, str]:
        ident = self._station_command_snapshot_id(snapshot)
        state = self._station_command_compact_state_text(self._station_command_state_text(snapshot)).strip().lower()
        try:
            health_state = str(self._station_command_health_summary_for_profile(snapshot).get("state", "") or "").strip().lower()
        except Exception:
            health_state = ""
        if health_state == "error" or state in {"ptt active", "rf guard blocked", "blocked", "error", "failed"}:
            return "danger", "Needs action"
        if (
            health_state in {"warn", "warning", "review"}
            or state in {"manual hold", "manual qsy", "scheduler suspended", "needs review", "warning", "warn"}
            or self._station_command_scheduler_manual_qsy_active_for_radio(ident)
            or self._station_command_scheduler_suspended_manually_for_radio(ident)
            or self._station_command_timed_suspend_active_for_radio(ident)
        ):
            return "warning", "Watch"
        if state in {"inactive", "configured inactive", "not enabled"}:
            return "muted", "Inactive"
        return "success", "Clear"

    def _station_command_snapshot_needs_operator_attention(self, snapshot: object) -> bool:
        role, _label = self._station_command_attention_role_for_snapshot(snapshot)
        if role == "danger":
            return True
        if role != "warning":
            return False
        ident = self._station_command_snapshot_id(snapshot)
        operator_control_state = (
            self._station_command_scheduler_manual_qsy_active_for_radio(ident)
            or self._station_command_scheduler_suspended_manually_for_radio(ident)
            or self._station_command_timed_suspend_active_for_radio(ident)
        )
        if operator_control_state:
            return False
        return True

    def _station_command_focus_score(self, snapshot: object, selected_id: int) -> int:
        ident = self._station_command_snapshot_id(snapshot)
        role, _label = self._station_command_attention_role_for_snapshot(snapshot)
        if role == "danger":
            score = 1000
        elif role == "warning":
            score = 700
        else:
            score = 100
        if self._station_command_bool(self._station_command_value(snapshot, "runtime_primary", False)):
            score += 80
        if self._station_command_bool(self._station_command_value(snapshot, "runtime_active", False)):
            score += 40
        if ident > 0 and ident == int(selected_id or 0):
            score += 250
        return score

    def _station_command_promoted_snapshot(self, choices: list[object], selected: object | None) -> object | None:
        if not choices:
            return None
        selected_id = self._station_command_snapshot_id(selected) if selected is not None else 0
        explicit_id = int(getattr(self, "_station_command_selected_profile_id", 0) or 0)
        if selected is not None and explicit_id > 0 and selected_id == explicit_id:
            # Attention may choose the initial focus, but must never override a
            # radio the operator explicitly selected in this control surface.
            return selected
        selected_score = self._station_command_focus_score(selected, selected_id) if selected is not None else -1
        best = max(
            choices,
            key=lambda snapshot: (
                self._station_command_focus_score(snapshot, selected_id),
                -choices.index(snapshot),
            ),
        )
        best_score = self._station_command_focus_score(best, selected_id)
        if selected is not None and selected_score >= best_score:
            return selected
        return best

    def _station_command_source_chip_tooltip(self, snapshot: object, selected_id: int) -> str:
        name = self._station_command_snapshot_name(snapshot)
        role, label = self._station_command_attention_role_for_snapshot(snapshot)
        now = self._station_command_now_text_for_summary(snapshot, selected_id)
        next_text = self._station_command_next_text(snapshot)
        plan_text = self._station_command_display_plan_name(self._station_command_plan_name_for_snapshot(snapshot))
        return f"{name}: {label}\nNow: {now or '--'}\nNext: {next_text or '--'}\nPlan: {plan_text or '--'}"

    def _station_command_mesh_source_chips(self) -> list[dict[str, str]]:
        configs = tuple(getattr(self, "_station_command_mesh_configs", ()) or ())
        rows = self._station_command_cached_mesh_health_rows()
        out: list[dict[str, str]] = []
        saved = list(configs)
        for config in sorted(saved, key=lambda item: (str(item.protocol or ""), self._station_command_mesh_config_chip_label(item).lower())):
            row = self._station_command_best_mesh_health_for_config(config, rows)
            name = self._station_command_mesh_config_chip_label(config)
            if not name:
                continue
            connected = bool(row and row.get("connected"))
            lifecycle_state = str(row.get("lifecycle_state") or "").strip().lower() if row else ""
            last_error = str(row.get("last_error") or "").strip() if row else ""
            if connected or lifecycle_state == "connected":
                role = "eligible_success"
            elif lifecycle_state in {"reconnecting", "away", "disabled"}:
                role = "muted"
            else:
                role = "warning" if last_error or lifecycle_state == "config_error" else "muted"
            status = lifecycle_state.replace("_", " ") if lifecycle_state else ("connected" if connected else "not connected")
            guidance = str(row.get("guidance") or "").strip() if row else ""
            out.append(
                {
                    "name": name,
                    "role": role,
                    "tooltip": (
                        f"Mesh source: {name}\nStatus: {status}"
                        + (f"\n{guidance}" if guidance else "")
                        + (f"\nIssue: {last_error}" if last_error and role == "warning" else "")
                    ),
                }
            )
        if out:
            return out
        # Legacy health rows may exist before a saved-device config is available.
        # Only expose connected, named rows so raw BLE scan IDs do not pollute the
        # source rail or create duplicate device chips.
        best_by_key: dict[str, Mapping[str, object]] = {}
        for row in rows:
            if not isinstance(row, Mapping) or not bool(row.get("connected")):
                continue
            if self._station_command_mesh_row_is_raw_only(row):
                continue
            key = self._station_command_mesh_chip_key(row)
            if not key:
                continue
            prior = best_by_key.get(key)
            if prior is None or self._station_command_mesh_chip_rank(row) > self._station_command_mesh_chip_rank(prior):
                best_by_key[key] = row
        for row in sorted(best_by_key.values(), key=lambda item: self._station_command_mesh_chip_label(item).lower()):
            name = self._station_command_mesh_chip_label(row)
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "role": "eligible_success",
                    "tooltip": f"Mesh source: {name}\nStatus: connected",
                }
            )
        return out

    @staticmethod
    def _station_command_mesh_config_chip_label(config: object) -> str:
        name = str(getattr(config, "ble_device_name", "") or "").strip()
        adapter = str(getattr(config, "adapter_id", "") or "").strip()
        endpoint = str(getattr(config, "endpoint_address", "") or "").strip()
        if MainWindow._station_command_mesh_identifier_looks_raw(name):
            name = ""
        if not name and not MainWindow._station_command_mesh_identifier_looks_raw(adapter):
            name = adapter
        if not name and not MainWindow._station_command_mesh_identifier_looks_raw(endpoint):
            name = endpoint
        for prefix in ("MeshCore-", "meshcore-", "Meshtastic-", "meshtastic-", "Mesh ", "mesh "):
            if name.startswith(prefix):
                name = name[len(prefix) :].strip()
        return name or "Mesh"

    @staticmethod
    def _station_command_best_mesh_health_for_config(
        config: object,
        rows: Sequence[Mapping[str, object]],
    ) -> Mapping[str, object] | None:
        if not isinstance(config, MeshConnectionConfig):
            return None
        matches = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if mesh_health_matches_config(config, row):
                matches.append(row)
        if not matches:
            return None
        return max(matches, key=MainWindow._station_command_mesh_chip_rank)

    @staticmethod
    def _station_command_normalized_mesh_identity(value: object) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"^(meshcore|meshtastic|mesh)[\s:_-]+", "", text)
        return re.sub(r"[^a-z0-9]+", "", text)

    @staticmethod
    def _station_command_mesh_chip_label(row: Mapping[str, object]) -> str:
        name = str(row.get("device_name") or "").strip()
        adapter = str(row.get("adapter_id") or "").strip()
        if MainWindow._station_command_mesh_identifier_looks_raw(name):
            name = ""
        if not name and not MainWindow._station_command_mesh_identifier_looks_raw(adapter):
            name = adapter
        for prefix in ("MeshCore-", "meshcore-", "Mesh ", "mesh "):
            if name.startswith(prefix):
                name = name[len(prefix) :].strip()
        return name or "Mesh"

    @staticmethod
    def _station_command_mesh_chip_key(row: Mapping[str, object]) -> str:
        device_name = str(row.get("device_name") or "").strip()
        adapter_id = str(row.get("adapter_id") or "").strip()
        candidates = []
        if not MainWindow._station_command_mesh_identifier_looks_raw(device_name):
            candidates.append(device_name)
        if not MainWindow._station_command_mesh_identifier_looks_raw(adapter_id):
            candidates.append(adapter_id)
        if not any(candidates) and (adapter_id or device_name):
            return f"mesh:{str(row.get('transport') or 'mesh').strip().lower() or 'mesh'}"
        for candidate in candidates:
            if not candidate:
                continue
            normalized = candidate.lower()
            normalized = re.sub(r"^(meshcore|mesh)[\s:_-]+", "", normalized)
            normalized = re.sub(r"[^a-z0-9]+", "", normalized)
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _station_command_mesh_identifier_looks_raw(value: object) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        text = re.sub(r"^(meshcore|mesh)[\s:_-]+", "", text, flags=re.IGNORECASE).strip()
        if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", text):
            return True
        if re.fullmatch(r"(?:ble|scan)[:_-][0-9a-fA-F:-]{6,}", text):
            return True
        if re.fullmatch(r"[0-9a-fA-F:-]{12,}", text):
            return True
        return False

    @staticmethod
    def _station_command_mesh_row_is_raw_only(row: Mapping[str, object]) -> bool:
        device = str(row.get("device_name") or "").strip()
        adapter = str(row.get("adapter_id") or "").strip()
        device_is_raw = not device or MainWindow._station_command_mesh_identifier_looks_raw(device)
        adapter_is_raw = not adapter or MainWindow._station_command_mesh_identifier_looks_raw(adapter)
        return device_is_raw and adapter_is_raw

    @staticmethod
    def _station_command_mesh_chip_rank(row: Mapping[str, object]) -> tuple[str, int, int]:
        # A retained success from an older adapter identity must not override a
        # newer failed/disconnected snapshot for the same physical device.
        updated = str(row.get("updated_utc") or "").strip()
        connected = 1 if bool(row.get("connected")) else 0
        healthy = 1 if not str(row.get("last_error") or "").strip() else 0
        return (updated, connected, healthy)

    @staticmethod
    def _station_command_mesh_chip_signature(chips: Sequence[Mapping[str, object]]) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (
                str(chip.get("name") or "").strip(),
                str(chip.get("role") or "").strip(),
                str(chip.get("tooltip") or "").strip(),
            )
            for chip in chips
        )

    @staticmethod
    def _station_command_source_control_signature(item: SourceControlItem | None) -> tuple[object, ...]:
        if item is None:
            return ("none",)
        return (
            item.key,
            item.kind,
            item.label,
            item.role,
            item.tooltip,
            tuple((action.key, action.label, action.enabled, action.role, action.tooltip) for action in item.actions),
        )

    def _station_command_source_control_items_signature(self, items: Sequence[SourceControlItem]) -> tuple[object, ...]:
        return tuple(self._station_command_source_control_signature(mesh_item) for mesh_item in items)

    def _station_command_saved_mesh_control_item(self) -> SourceControlItem | None:
        items = self._station_command_saved_mesh_control_items()
        return items[0] if items else None

    def _station_command_saved_mesh_control_items(self) -> tuple[SourceControlItem, ...]:
        configs = tuple(getattr(self, "_station_command_mesh_configs", ()) or ())
        rows = self._station_command_cached_mesh_health_rows()
        return source_control_mesh_items_from_configs(configs, rows)

    def _station_command_cached_mesh_health_rows(self) -> tuple[Mapping[str, object], ...]:
        """Return health only while the worker-published cache is fresh.

        This inexpensive monotonic check is safe in periodic rendering and
        prevents an old connected snapshot from being presented as current
        after a worker or transport failure.
        """
        rows = tuple(getattr(self, "_station_command_mesh_health_rows", ()) or ())
        published_by_adapter = dict(
            getattr(self, "_station_command_mesh_health_published_monotonic_by_adapter", {}) or {}
        )
        if not published_by_adapter:
            # Compatibility with small view-model tests and pre-publisher
            # windows: caller-provided rows carry no live-worker timestamp.
            return rows
        now = time.monotonic()
        return tuple(
            row
            for row in rows
            if not (published := float(published_by_adapter.get(str(row.get("adapter_id") or ""), 0.0) or 0.0))
            or now - published <= MainWindow._STATION_COMMAND_MESH_HEALTH_TTL_SECONDS
        )

    def _publish_station_command_mesh_snapshot(
        self,
        *,
        configs: Sequence[MeshConnectionConfig] | None = None,
        health: object | None = None,
    ) -> None:
        """Publish a UI-only Mesh snapshot from a lifecycle boundary.

        The Mesh worker owns connection polling and persistence.  The command
        bar only consumes this immutable projection.  Configurations are
        refreshed when runtime settings are loaded or saved; health replaces
        the row for one adapter when the worker publishes an update.
        """
        if configs is not None:
            self._station_command_mesh_configs = tuple(configs)
        if health is None:
            return
        adapter_id = str(getattr(health, "adapter_id", "") or "").strip()
        if not adapter_id:
            return
        row = MappingProxyType(
            {
                "adapter_id": adapter_id,
                "transport": str(getattr(health, "transport", "") or ""),
                "enabled": bool(getattr(health, "enabled", False)),
                "connected": bool(getattr(health, "connected", False)),
                "connection_type": str(getattr(health, "connection_type", "") or ""),
                "device_name": str(getattr(health, "device_name", "") or ""),
                "firmware_version": str(getattr(health, "firmware_version", "") or ""),
                "battery_percent": getattr(health, "battery_percent", None),
                "battery_voltage": getattr(health, "battery_voltage", None),
                "last_error": str(getattr(health, "last_error", "") or ""),
                "lifecycle_state": str(getattr(health, "lifecycle_state", "") or ""),
                "required": bool(getattr(health, "required", False)),
                "guidance": str(getattr(health, "guidance", "") or ""),
                "updated_utc": str(getattr(health, "updated_utc", "") or ""),
            }
        )
        prior = tuple(getattr(self, "_station_command_mesh_health_rows", ()) or ())
        rows = [item for item in prior if str(item.get("adapter_id") or "") != adapter_id]
        rows.append(row)
        self._station_command_mesh_health_rows = tuple(rows)
        published_by_adapter = dict(
            getattr(self, "_station_command_mesh_health_published_monotonic_by_adapter", {}) or {}
        )
        published_by_adapter[adapter_id] = time.monotonic()
        self._station_command_mesh_health_published_monotonic_by_adapter = published_by_adapter

    def _open_mesh_settings_from_station_command(self) -> None:
        try:
            self.open_settings_section("local_mesh", settings_nav_context="settings")
        except Exception:
            try:
                self.open_settings_section("operator_info", settings_nav_context="settings")
            except Exception:
                pass

    def _open_mesh_manager_from_station_command(self) -> None:
        existing = getattr(self, "_mesh_manager_dialog", None)
        if isinstance(existing, QDialog) and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        dialog = QDialog(self)
        self._mesh_manager_dialog = dialog
        dialog.setWindowTitle("Local Mesh")
        dialog.setModal(False)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        dialog.finished.connect(lambda *_args: setattr(self, "_mesh_manager_dialog", None))
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("<b>Local Mesh</b>", dialog)
        title.setTextFormat(Qt.RichText)
        layout.addWidget(title)

        rows = self._station_command_cached_mesh_health_rows()
        chips = self._station_command_mesh_source_chips()
        if chips:
            for chip in chips:
                name = str(chip.get("name") or "Mesh").strip()
                role = str(chip.get("role") or "muted").strip()
                tooltip = str(chip.get("tooltip") or "").strip()
                status = "Connected" if role == "eligible_success" else ("Needs attention" if role == "warning" else "Not connected")
                label = QLabel(f"<b>{name}</b><br>{status}", dialog)
                label.setTextFormat(Qt.RichText)
                label.setWordWrap(True)
                if tooltip:
                    label.setToolTip(tooltip)
                layout.addWidget(label)
        else:
            empty = QLabel("No local mesh connection is configured for CommsPlan.", dialog)
            empty.setWordWrap(True)
            layout.addWidget(empty)

        if rows:
            detail_text = (
                f"{len(rows)} retained mesh health row{'s' if len(rows) != 1 else ''}. "
                "Open Local Mesh settings to review BLE scan results, channel policies, and pairing details."
            )
        else:
            detail_text = "Use Local Mesh settings to add or scan a MeshCore, Meshtastic, or future mesh source."
        detail = QLabel(detail_text, dialog)
        detail.setWordWrap(True)
        layout.addWidget(detail)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        restart_btn = QPushButton("Reconnect", dialog)
        restart_btn.setToolTip("Restart the local mesh worker for the configured connection.")
        restart_btn.clicked.connect(
            lambda _checked=False: (
                self._manual_reconnect_mesh_runtime_now(),
                self._refresh_station_command_bar(force=True),
            )
        )
        buttons.addWidget(restart_btn)

        disconnect_btn = QPushButton("Disconnect", dialog)
        disconnect_btn.setToolTip("Stop the local mesh worker. The saved configuration remains unchanged.")
        disconnect_btn.clicked.connect(
            lambda _checked=False: (
                self._disconnect_mesh_runtime(),
                self._refresh_station_command_bar(force=True),
            )
        )
        buttons.addWidget(disconnect_btn)

        settings_btn = QPushButton("Local Mesh Settings", dialog)
        settings_btn.setToolTip("Open the full Local Mesh setup and channel policy screen.")
        settings_btn.clicked.connect(lambda _checked=False, dlg=dialog: (dlg.close(), self._open_mesh_settings_from_station_command()))
        buttons.addWidget(settings_btn)

        close_btn = QPushButton("Close", dialog)
        close_btn.clicked.connect(dialog.close)
        buttons.addWidget(close_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        dialog.resize(560, max(220, dialog.sizeHint().height()))
        dialog.show()

    def _connect_saved_mesh_from_station_command(self, _source_key: str = "") -> None:
        selected_key = str(_source_key or "").strip()
        if selected_key.startswith("connect:"):
            selected_key = selected_key.split("connect:", 1)[1]
        # SettingsTab owns a separate SQLite-backed SettingsManager.  A Scan /
        # Use Device action persists there immediately and then emits this
        # signal.  Reload before resolving the stable endpoint key so the
        # newly saved device cannot be rejected by this window's stale cache.
        try:
            self.settings.reload()
        except Exception as exc:
            log.debug("MainWindow: local mesh settings reload before connect failed: %s", exc)
        try:
            payload = activate_mesh_connection_config(
                self.settings.all(),
                selected_key,
                prefix=self._station_command_mesh_protocol_prefix(selected_key),
            )
        except Exception as exc:
            log.warning("MainWindow: failed to activate saved mesh connection %s: %s", selected_key, exc)
            payload = None
        if not payload:
            log.warning("MainWindow: saved mesh connection %s was not found.", selected_key)
            self._refresh_station_command_bar(force=True)
            return
        try:
            self.settings.set_many(payload, save=True)
        except Exception as exc:
            log.warning("MainWindow: failed to save activated mesh connection %s: %s", selected_key, exc)
            return
        # This is an explicit operator Connect action. Reset only the selected
        # adapter's bounded retry series when the live worker still owns the
        # same configuration; otherwise replace the runtime normally.
        configs = tuple(self._mesh_runtime_configs())
        publish_snapshot = getattr(self, "_publish_station_command_mesh_snapshot", None)
        if callable(publish_snapshot):
            publish_snapshot(configs=configs)
        new_signature = self._mesh_runtime_signature_from_configs(configs)
        selected_config = next(
            (
                config
                for config in configs
                if mesh_connection_config_key(config) == selected_key
                or str(config.adapter_id) == selected_key
            ),
            None,
        )
        worker = getattr(self, "_mesh_worker", None)
        thread = getattr(self, "_mesh_worker_thread", None)
        live_same_runtime = bool(
            selected_config is not None
            and worker is not None
            and thread is not None
            and thread.isRunning()
            and new_signature == getattr(self, "_mesh_runtime_signature", tuple())
        )
        if live_same_runtime:
            self._mesh_retry_requested.emit(str(selected_config.adapter_id))
        else:
            if selected_config is not None:
                pending_manual = set(
                    getattr(self, "_mesh_manual_retry_adapter_ids", set()) or set()
                )
                pending_manual.add(str(selected_config.adapter_id))
                self._mesh_manual_retry_adapter_ids = pending_manual
            self._restart_mesh_runtime_now()
        self._refresh_station_command_bar(force=True)
        settings_tab = getattr(self, "settings_tab", None)
        if settings_tab is not None and hasattr(settings_tab, "_load_mesh_settings_from_data"):
            try:
                settings_tab._load_mesh_settings_from_data(self.settings.all())
            except Exception:
                pass

    @staticmethod
    def _station_command_mesh_protocol_prefix(source_key: str) -> str:
        protocol = str(source_key or "").strip().split(":", 1)[0].strip().lower()
        for known_protocol in ("meshcore", "meshtastic"):
            if protocol == known_protocol or protocol.startswith(f"{known_protocol}-"):
                return known_protocol
        return "meshtastic"

    def _show_mesh_source_menu(self, button: QPushButton, item: SourceControlItem) -> None:
        menu = QMenu(button)
        menu.setObjectName("stationCommandMeshMenu")
        menu.setToolTipsVisible(True)
        for action_item in item.actions:
            is_connected = action_item.role == "eligible_success"
            label = action_item.label
            if is_connected and label.startswith("Connect: "):
                # Keep connected saved devices visible as known configurations,
                # but never present an enabled Connect action for one already
                # owned by the runtime.
                label = f"Connected: {label.split('Connect: ', 1)[1]}"
            action = QAction(label, menu)
            action.setEnabled(bool(action_item.enabled) and not is_connected)
            if action_item.tooltip:
                tooltip = action_item.tooltip
                if is_connected:
                    tooltip = tooltip.replace("Connect to", "Already connected to", 1)
                action.setToolTip(tooltip)
            key = str(action_item.key or "")
            if key.startswith("connect:") and not is_connected:
                action.triggered.connect(lambda _checked=False, source_key=key: self._connect_saved_mesh_from_station_command(source_key))
            menu.addAction(action)
        if item.actions:
            menu.addSeparator()
        connected_actions = [
            action_item
            for action_item in item.actions
            if action_item.role == "eligible_success"
        ]
        if connected_actions:
            connected_names = []
            for action_item in connected_actions:
                name = str(action_item.label or "").strip()
                if name.startswith("Connect: "):
                    name = name.split("Connect: ", 1)[1].strip()
                if name and name not in connected_names:
                    connected_names.append(name)
            active_label = ", ".join(connected_names) or item.label
            disconnect_action = QAction(f"Disconnect {active_label}", menu)
            disconnect_action.setToolTip("Stop the active local mesh worker. Saved device settings remain available.")
            disconnect_action.triggered.connect(
                lambda _checked=False: (
                    self._disconnect_mesh_runtime(),
                    self._refresh_station_command_bar(force=True),
                )
            )
            menu.addAction(disconnect_action)
        scan_action = QAction("Scan for Device…", menu)
        scan_action.setToolTip("Open Local Mesh settings to scan for and use a nearby device.")
        scan_action.triggered.connect(lambda _checked=False: self._open_mesh_settings_from_station_command())
        menu.addAction(scan_action)
        menu.addSeparator()
        manage_action = QAction("Manage Channels", menu)
        manage_action.setToolTip("Open Local Mesh settings and channel/feed review.")
        manage_action.triggered.connect(lambda _checked=False: self._open_mesh_settings_from_station_command())
        menu.addAction(manage_action)
        settings_action = QAction("Mesh Settings", menu)
        settings_action.triggered.connect(lambda _checked=False: self._open_mesh_settings_from_station_command())
        menu.addAction(settings_action)
        self._station_command_mesh_menu = menu
        menu.popup(button.mapToGlobal(button.rect().bottomLeft()))

    def _add_station_command_source_rail(
        self,
        layout: QLayout,
        parent: QWidget,
        choices: list[object],
        selected_id: int,
        theme: Mapping[str, object],
        *,
        mesh_chips: Sequence[Mapping[str, object]] | None = None,
    ) -> int:
        rail = QFrame(parent)
        rail.setObjectName("stationCommandSourceRail")
        rail.setFrameShape(QFrame.NoFrame)
        rail_layout = QHBoxLayout(rail)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        rail_layout.setSpacing(6)
        total_width = 0
        attention = self._station_command_unique_attention_snapshots(
            [
                snapshot
                for snapshot in choices
                if self._station_command_snapshot_needs_operator_attention(snapshot)
            ]
        )
        if attention:
            attention_btn = QPushButton(f"ATTN: {len(attention)}", rail)
            attention_btn.setObjectName("stationCommandAttentionChip")
            attention_noun = "radio or source" if len(attention) == 1 else "radios or sources"
            attention_verb = "needs" if len(attention) == 1 else "need"
            attention_btn.setAccessibleName(f"{len(attention)} {attention_noun} {attention_verb} attention")
            attention_btn.setToolTip("Review radios or sources that need operator attention.")
            attention_btn.setStyleSheet(button_style("warning", theme))
            attention_btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            attention_btn.clicked.connect(
                lambda _checked=False, snapshots=tuple(attention), focus_id=selected_id, anchor=attention_btn, menu_theme=dict(theme): self._show_station_command_attention_menu(
                    choices=snapshots,
                    selected_id=focus_id,
                    anchor=anchor,
                    theme=menu_theme,
                )
            )
            rail_layout.addWidget(attention_btn)
            total_width += int(attention_btn.sizeHint().width() or 0) + 6
        for mesh_item in self._station_command_saved_mesh_control_items():
            chip = QPushButton(f"{mesh_item.label} \u25be", rail)
            chip.setObjectName("stationCommandSourceChip")
            chip.setToolTip(mesh_item.tooltip or "Manage saved local mesh connections.")
            chip.setStyleSheet(button_style(mesh_item.role, theme))
            chip.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            chip.clicked.connect(lambda _checked=False, button=chip, item=mesh_item: self._show_mesh_source_menu(button, item))
            rail_layout.addWidget(chip)
            total_width += int(chip.sizeHint().width() or 0) + 6
        for snapshot in choices:
            ident = self._station_command_snapshot_id(snapshot)
            selected = ident > 0 and ident == int(selected_id or 0)
            role, _label = self._station_command_attention_role_for_snapshot(snapshot)
            chip = QPushButton(self._station_command_snapshot_name(snapshot), rail)
            chip.setObjectName("stationCommandSourceChip")
            chip.setCheckable(True)
            chip.setChecked(selected)
            chip.setToolTip(self._station_command_source_chip_tooltip(snapshot, selected_id))
            chip_role = role if role in {"danger", "warning", "muted"} else "success" if selected else "info"
            chip.setStyleSheet(button_style(chip_role, theme))
            chip.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            if ident > 0:
                chip.clicked.connect(
                    lambda _checked=False, profile_id=ident: self._on_station_command_summary_radio_clicked(profile_id)
                )
            rail_layout.addWidget(chip)
            total_width += int(chip.sizeHint().width() or 0) + 6
        rail_layout.addStretch(1)
        layout.addWidget(rail)
        return max(0, total_width)

    def _toggle_station_command_minimized(self) -> None:
        self._station_command_minimized = not bool(getattr(self, "_station_command_minimized", False))
        if self._station_command_minimized:
            self._station_command_controls_expanded = False
        self._station_command_radio_summary_signature = None
        self._refresh_station_command_bar(force=True)

    def _toggle_adaptive_station_controls(self) -> None:
        self._station_command_minimized = False
        self._station_command_controls_expanded = not bool(
            getattr(self, "_station_command_controls_expanded", False)
        )
        self._station_command_radio_summary_signature = None
        self._refresh_station_command_bar(force=True)

    def _reflow_adaptive_station_shell(self) -> None:
        if not bool(getattr(self, "_adaptive_station_shell_enabled", False)):
            return
        choices = list(getattr(self, "_station_command_last_choices", []) or [])
        try:
            selected_id = int(getattr(self, "_station_command_selected_profile_id", 0) or 0)
        except Exception:
            selected_id = 0
        self._refresh_adaptive_station_command_shell(choices, selected_id)

    def _open_sop_from_station_command(self) -> None:
        index = getattr(self, "_screen_index_by_label", {}).get("SOP")
        if index is not None:
            self._set_screen(int(index))

    def _run_station_quick_hold(self, radio_id: int) -> None:
        resume = (self._station_command_scheduler_manual_qsy_active_for_radio(radio_id)
                  or self._station_command_timed_suspend_active_for_radio(radio_id)
                  or self._station_command_scheduler_suspended_manually_for_radio(radio_id))
        if resume:
            self._on_station_command_resume_clicked(radio_id)
        else:
            self._on_station_command_timed_suspend_clicked(radio_id)

    def _run_adaptive_qsy_action(
        self,
        radio_id: int,
        combo: QComboBox,
        index: int,
        *,
        timed: bool,
    ) -> None:
        combo.setCurrentIndex(index)
        combo.setProperty("stationCommandSelectionDirty", True)
        callback = self._on_station_command_qsy_hold_clicked if timed else self._on_station_command_qsy_now_clicked
        duration_combo = getattr(self, "station_command_duration_combo", None) if timed else None
        self._station_command_for_radio_qsy(radio_id, combo, callback, duration_combo)()

    def _build_adaptive_qsy_button(
        self,
        parent: QWidget,
        snapshot: object,
        *,
        timed: bool,
        theme: Mapping[str, object],
    ) -> QToolButton:
        ident = self._station_command_snapshot_id(snapshot)
        button = QToolButton(parent)
        button.setObjectName("stationCommandQuickHold" if timed else "stationCommandQuickQsy")
        button.setText("Timed QSY" if timed else "QSY")
        button.setPopupMode(QToolButton.InstantPopup)
        button.setToolTip(
            "Choose a plan destination and temporarily hold the schedule after QSY."
            if timed
            else "Choose a plan destination and QSY now."
        )
        combo = QComboBox(parent)
        combo.setVisible(False)
        has_options = self._station_command_populate_card_frequency_combo(combo, snapshot)
        menu = QMenu(button)
        menu.setObjectName("stationCommandQuickQsyMenu")
        preferred_key = self._station_command_preferred_qsy_key(snapshot)
        for index in range(combo.count()):
            meta = combo.itemData(index)
            action = QAction(combo.itemText(index), menu)
            action.setToolTip(str(combo.itemData(index, Qt.ToolTipRole) or ""))
            enabled = has_options and isinstance(meta, Mapping)
            if enabled and not timed:
                enabled = self._station_command_qsy_option_key(meta) != preferred_key
            action.setEnabled(enabled)
            action.triggered.connect(
                lambda _checked=False, idx=index, rid=ident, target_combo=combo, is_timed=timed: self._run_adaptive_qsy_action(
                    rid,
                    target_combo,
                    idx,
                    timed=is_timed,
                )
            )
            menu.addAction(action)
        button.setMenu(menu)
        controls_available = self._station_command_frequency_controls_available(snapshot)
        button.setEnabled(bool(ident > 0 and has_options and controls_available))
        button.setStyleSheet(button_style("info" if not timed else "muted", theme))
        return button

    def _build_adaptive_station_controls_tray(
        self,
        parent: QWidget,
        snapshot: object,
        *,
        density: str,
        theme: Mapping[str, object],
    ) -> QFrame:
        """Build the non-redundant advanced controls for the selected radio."""
        ident = self._station_command_snapshot_id(snapshot)
        frequency_control_available = self._station_command_frequency_controls_available(snapshot)
        manual_qsy_active = self._station_command_scheduler_manual_qsy_active_for_radio(ident)
        hold_snapshot = self._station_command_hold_snapshot_for_radio(ident)
        timed_qsy_active = bool(hold_snapshot.get("active") and manual_qsy_active)
        timed_suspend_active = self._station_command_timed_suspend_active_for_radio(ident)
        scheduler_suspended_manual = self._station_command_scheduler_suspended_manually_for_radio(ident)
        state = self._station_command_compact_state_text(self._station_command_state_text(snapshot))

        tray = QFrame(parent)
        tray.setObjectName("stationCommandControlsTray")
        tray.setAccessibleName(
            f"Advanced radio controls for {self._station_command_snapshot_name(snapshot)}"
        )
        tray.setFrameShape(QFrame.StyledPanel)
        grid = QGridLayout(tray)
        grid.setContentsMargins(7, 5, 7, 5)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(5)

        target_label = QLabel("TARGET", tray)
        target_label.setObjectName("stationCommandControlsTargetLabel")
        target_label.setAccessibleName("Plan target")
        target_label.setStyleSheet(
            f"color:{theme.get('text_muted', theme.get('text', '#555555'))}; font-weight:700;"
        )
        freq_combo = QComboBox(tray)
        freq_combo.setObjectName("stationCommandControlsTarget")
        freq_combo.setAccessibleName("Selected radio plan target")
        freq_combo.setToolTip(
            "Choose an assigned-plan operating destination for this radio."
            if frequency_control_available
            else "This radio's frequency is controlled outside CommsPlan."
        )
        freq_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        freq_combo.setMinimumWidth(170)
        freq_combo.setEnabled(frequency_control_available)
        self._station_command_populate_card_frequency_combo(freq_combo, snapshot)
        preferred_key = self._station_command_preferred_qsy_key(snapshot)
        pending_qsy_keys = getattr(self, "_station_command_card_qsy_pending_keys", None)
        if not isinstance(pending_qsy_keys, dict):
            pending_qsy_keys = {}
            self._station_command_card_qsy_pending_keys = pending_qsy_keys
        pending_key = str(pending_qsy_keys.get(ident, "") or "")
        if pending_key:
            for index in range(freq_combo.count()):
                data = freq_combo.itemData(index)
                try:
                    if isinstance(data, Mapping) and f"{float(data.get('freq')):.6f}" == pending_key:
                        freq_combo.setCurrentIndex(index)
                        break
                except Exception:
                    continue
        freq_combo.setProperty("stationCommandPreferredKey", preferred_key)
        freq_combo.setProperty(
            "stationCommandSelectionDirty",
            bool(pending_key and pending_key != preferred_key),
        )
        combo_font = freq_combo.font()
        combo_font.setBold(True)
        freq_combo.setFont(combo_font)

        duration_combo = QComboBox(tray)
        duration_combo.setObjectName("stationCommandControlsDuration")
        refresh_hold_duration_combo(
            duration_combo,
            self.settings,
            getattr(self, "_active_runtime_profile", None),
        )
        duration_combo.setVisible(False)

        qsy_btn = QPushButton("QSY Now", tray)
        qsy_btn.setObjectName("stationCommandControlsQsyNow")
        qsy_btn.setToolTip("QSY the selected radio to the chosen target now.")
        qsy_btn.clicked.connect(
            self._station_command_for_radio_qsy(
                ident,
                freq_combo,
                self._on_station_command_qsy_now_clicked,
            )
        )

        timer_btn = QToolButton(tray)
        timer_btn.setObjectName("stationCommandControlsTimedQsy")
        timer_btn.setText(timed_qsy_text(timed_qsy_active=timed_qsy_active))
        timer_btn.setPopupMode(QToolButton.MenuButtonPopup)
        timer_btn.setToolTip("QSY, then hold scheduler control for a selected duration.")
        timer_btn.clicked.connect(
            self._station_command_for_radio_qsy(
                ident,
                freq_combo,
                self._on_station_command_qsy_hold_clicked,
                duration_combo,
            )
        )
        timed_menu = QMenu(timer_btn)
        timed_menu.setObjectName("stationCommandControlsTimedQsyMenu")
        indefinite_qsy = QAction("Indefinite", timed_menu)
        indefinite_qsy.setToolTip("QSY and keep scheduler control suspended until Resume.")
        indefinite_qsy.triggered.connect(
            self._station_command_for_radio_qsy(
                ident,
                freq_combo,
                self._on_station_command_qsy_now_clicked,
            )
        )
        timed_menu.addAction(indefinite_qsy)
        timed_menu.addSeparator()
        for duration_index in range(duration_combo.count()):
            duration_value = duration_combo.itemData(duration_index)
            action = QAction(duration_combo.itemText(duration_index), timed_menu)
            action.triggered.connect(
                lambda _checked=False, value=duration_value, combo=duration_combo, button=timer_btn: (
                    combo.setCurrentIndex(
                        next(
                            (idx for idx in range(combo.count()) if combo.itemData(idx) == value),
                            combo.currentIndex(),
                        )
                    ),
                    button.click(),
                )
            )
            timed_menu.addAction(action)
        timer_btn.setMenu(timed_menu)

        scheduler_actions = scheduler_action_state(
            manual_qsy_active=manual_qsy_active,
            timed_qsy_active=timed_qsy_active,
            timed_suspend_active=timed_suspend_active,
            scheduler_suspended_manual=scheduler_suspended_manual,
            scheduler_state_text=state,
        )
        suspend_btn = QToolButton(tray)
        suspend_btn.setObjectName("stationCommandControlsScheduleSuspend")
        suspend_btn.setText(scheduler_actions.timed_suspend_text)
        suspend_btn.setPopupMode(QToolButton.MenuButtonPopup)
        suspend_btn.setToolTip("Suspend scheduled changes without changing frequency.")
        suspend_btn.clicked.connect(
            lambda _checked=False, radio_id=ident: self._on_station_command_timed_suspend_clicked(radio_id)
        )
        suspend_menu = QMenu(suspend_btn)
        suspend_menu.setObjectName("stationCommandControlsScheduleMenu")
        indefinite_suspend = QAction("Indefinite", suspend_menu)
        indefinite_suspend.setToolTip("Suspend scheduler control until Resume.")
        indefinite_suspend.triggered.connect(
            lambda _checked=False, radio_id=ident: self._on_station_command_pause_clicked(radio_id)
        )
        suspend_menu.addAction(indefinite_suspend)
        suspend_menu.addSeparator()
        for duration_index in range(duration_combo.count()):
            duration_value = duration_combo.itemData(duration_index)
            action = QAction(duration_combo.itemText(duration_index), suspend_menu)
            action.triggered.connect(
                lambda _checked=False, value=duration_value, combo=duration_combo, button=suspend_btn: (
                    combo.setCurrentIndex(
                        next(
                            (idx for idx in range(combo.count()) if combo.itemData(idx) == value),
                            combo.currentIndex(),
                        )
                    ),
                    button.click(),
                )
            )
            suspend_menu.addAction(action)
        suspend_btn.setMenu(suspend_menu)

        resume_btn = QPushButton("Resume schedule", tray)
        resume_btn.setObjectName("stationCommandControlsResume")
        resume_btn.setToolTip("Return this radio to scheduled control.")
        resume_active = bool(
            manual_qsy_active
            or timed_qsy_active
            or timed_suspend_active
            or scheduler_suspended_manual
        )
        resume_btn.setEnabled(ident > 0 and frequency_control_available and resume_active)
        resume_btn.clicked.connect(
            lambda _checked=False, radio_id=ident: self._on_station_command_resume_clicked(radio_id)
        )

        health_summary = self._station_command_health_summary_for_profile(snapshot)
        health_state = str(health_summary.get("state", "warn") or "warn").strip().lower()
        health_btn = QToolButton(tray)
        health_btn.setObjectName("stationCommandControlsHealth")
        health_btn.setText("Health…")
        health_btn.setAccessibleName("Radio health")
        health_btn.setToolTip(
            f"Health: {health_summary.get('label', 'Review')}. Open radio health summary."
        )
        health_btn.setEnabled(ident > 0)
        health_btn.clicked.connect(
            lambda _checked=False, radio_id=ident, snap=snapshot, anchor=health_btn: self._show_station_command_health_menu(
                device_profile_id=radio_id,
                snapshot=self._station_command_current_snapshot(radio_id) or snap,
                anchor=anchor,
            )
        )
        plan_btn = QToolButton(tray)
        plan_btn.setObjectName("stationCommandControlsPlan")
        plan_btn.setText("Plan…")
        plan_btn.setAccessibleName("Change radio plan")
        plan_btn.setToolTip("Assign or change the Frequency Plan for the selected radio.")
        plan_btn.setEnabled(ident > 0)
        plan_btn.clicked.connect(
            lambda _checked=False, radio_id=ident: self._open_schedule_assignment_for_radio(radio_id)
        )
        utility_size = control_height_for_font(tray, vertical_padding=9, floor=34)
        for button in (health_btn, plan_btn):
            button.setMinimumHeight(utility_size)
            button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        def update_qsy_buttons(_index: int = -1) -> None:
            manual_qsy_active = self._station_command_scheduler_manual_qsy_active_for_radio(ident)
            hold = self._station_command_hold_snapshot_for_radio(ident)
            timed_qsy_active = bool(hold.get("active") and manual_qsy_active)
            selected_key = self._station_command_combo_selected_key(freq_combo)
            action_state = qsy_action_state(
                selected_meta=selected_qsy_meta(freq_combo),
                preferred_key=str(freq_combo.property("stationCommandPreferredKey") or ""),
                radio_id=ident,
                selection_changed=bool(
                    selected_key
                    and (
                        (preferred_key and selected_key != preferred_key)
                        or (not preferred_key and freq_combo.property("stationCommandSelectionDirty"))
                    )
                ),
                manual_qsy_active=manual_qsy_active,
                timed_qsy_active=timed_qsy_active,
            )
            qsy_btn.setEnabled(bool(frequency_control_available and action_state.qsy_enabled))
            timer_btn.setEnabled(bool(frequency_control_available and action_state.timed_qsy_enabled))
            qsy_btn.setStyleSheet(button_style(action_state.qsy_role, theme))
            timer_btn.setStyleSheet(button_style(action_state.timed_qsy_role, theme))

        def on_target_changed(index: int) -> None:
            selected_key = self._station_command_combo_selected_key(freq_combo)
            if selected_key:
                pending_qsy_keys[ident] = selected_key
            else:
                pending_qsy_keys.pop(ident, None)
            freq_combo.setProperty("stationCommandSelectionDirty", True)
            update_qsy_buttons(index)

        freq_combo.currentIndexChanged.connect(on_target_changed)
        suspend_btn.setEnabled(ident > 0 and frequency_control_available)
        suspend_btn.setStyleSheet(button_style(scheduler_actions.timed_suspend_role, theme))
        resume_btn.setStyleSheet(button_style("warning" if resume_active else "muted", theme))
        health_role = (
            "danger"
            if health_state == "error"
            else "warning"
            if health_state in {"warn", "warning", "review"}
            else "success"
        )
        health_btn.setStyleSheet(
            "QToolButton {"
            f"background:{theme.get('surface_alt', '#ECEFF1')};"
            f"border:2px solid {theme.get(health_role, theme.get('border', '#CCCCCC'))};"
            "border-radius:5px; padding:3px;"
            "}"
            "QToolButton:focus {"
            f"border:2px solid {theme.get('info', '#1565C0')};"
            "}"
        )
        plan_btn.setStyleSheet(button_style("muted", theme))
        update_qsy_buttons()

        controls = (qsy_btn, timer_btn, suspend_btn, resume_btn)
        for button in controls:
            button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        if density == "roomy":
            grid.addWidget(target_label, 0, 0)
            grid.addWidget(freq_combo, 0, 1)
            grid.addWidget(qsy_btn, 0, 2)
            grid.addWidget(timer_btn, 0, 3)
            grid.addWidget(resume_btn, 0, 4)
            grid.setColumnStretch(1, 1)
            detail_row = 1
        else:
            grid.addWidget(target_label, 0, 0)
            grid.addWidget(freq_combo, 0, 1, 1, 3)
            grid.addWidget(qsy_btn, 1, 1)
            grid.addWidget(timer_btn, 1, 2)
            grid.addWidget(resume_btn, 1, 3)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(2, 1)
            grid.setColumnStretch(3, 1)
            detail_row = 2
        tools = QWidget(tray)
        tools_layout = QHBoxLayout(tools)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        tools_layout.setSpacing(6)
        for button in (suspend_btn, health_btn, plan_btn):
            tools_layout.addWidget(button)
        tools_layout.addStretch(1)
        detail_scroll = QScrollArea(tray)
        detail_scroll.setObjectName("stationCommandAdvancedToolsScroll")
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        detail_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        detail_scroll.setWidget(tools)
        detail_scroll.setMinimumHeight(utility_size + 2 * detail_scroll.frameWidth())
        detail_scroll.setMaximumHeight(utility_size + 2 * detail_scroll.frameWidth() + 8)
        grid.addWidget(detail_scroll, detail_row, 1, 1, 4 if density == "roomy" else 3)

        self._station_command_radio_tile_controls = {
            ident: {
                "qsy_btn": qsy_btn,
                "timer_btn": timer_btn,
                "suspend_btn": suspend_btn,
                "resume_btn": resume_btn,
                "freq_combo": freq_combo,
                "frequency_controls_available": frequency_control_available,
                "compact_card": density != "roomy",
            }
        }
        return tray

    def _build_adaptive_station_awareness_rail(
        self,
        parent: QWidget,
        choices: list[object],
        selected_id: int,
        *,
        density: str,
        theme: Mapping[str, object],
    ) -> QFrame:
        rail = ResponsiveAwarenessRail(parent)
        rail.setAccessibleName("Radio, condition and time awareness")
        # The rail owns responsive wrapping.  It exposes the two small
        # QHBoxLayout-compatible methods used below so status publication can
        # keep treating this as a cache-only presentation surface.
        row = rail
        self._station_command_source_widgets = {}
        self._station_command_mesh_widgets = {}
        self.station_command_local_time_label = None
        self.station_command_utc_time_label = None
        attention = self._station_command_unique_attention_snapshots(
            [item for item in choices if self._station_command_snapshot_needs_operator_attention(item)]
        )
        compact_attention = density == "condensed"
        button = QPushButton(
            f"! {len(attention)}" if compact_attention else f"ATTN: {len(attention)}",
            rail,
        )
        button.setObjectName("stationCommandAttentionChip")
        attention_noun = "radio or source" if len(attention) == 1 else "radios or sources"
        attention_verb = "needs" if len(attention) == 1 else "need"
        button.setAccessibleName(f"{len(attention)} {attention_noun} {attention_verb} attention")
        button.setToolTip("Review radios or sources that need operator attention.")
        button.setStyleSheet(button_style("warning", theme))
        button.clicked.connect(
            lambda _checked=False, focus_id=selected_id, anchor=button, menu_theme=dict(theme): self._show_station_command_attention_menu(
                choices=tuple(self._station_command_unique_attention_snapshots([item for item in getattr(self, "_station_command_last_choices", ()) if self._station_command_snapshot_needs_operator_attention(item)])),
                selected_id=focus_id,
                anchor=anchor,
                theme=menu_theme,
            )
        )
        row.addWidget(button)
        button.setVisible(bool(attention))
        self._station_command_attention_widget = button
        for item in self._station_command_saved_mesh_control_items():
            button = QPushButton(f"{item.label} ▾", rail)
            button.setObjectName("stationCommandSourceChip")
            button.setToolTip(item.tooltip or "Manage this mesh source.")
            button.setAccessibleName(f"Mesh source {item.label}")
            button.setStyleSheet(button_style(item.role, theme))
            button.clicked.connect(
                lambda _checked=False, anchor=button, key=getattr(item, "key", item.label): self._show_current_mesh_source_menu(anchor, key)
            )
            self._station_command_mesh_widgets[getattr(item, "key", item.label)] = button
            row.addWidget(button)
        for snapshot in choices:
            ident = self._station_command_snapshot_id(snapshot)
            if ident > 0 and ident == int(selected_id or 0):
                # The selected radio owns the primary context row below. Keeping
                # it in this rail would duplicate identity and weaken the visual
                # relationship between radio and current destination.
                continue
            selected = ident > 0 and ident == int(selected_id or 0)
            role, status_label = self._station_command_attention_role_for_snapshot(snapshot)
            now = self._station_command_now_text_for_summary(snapshot, selected_id)
            prefix = "! " if role == "danger" else "△ " if role == "warning" else "● "
            button = QPushButton(
                prefix
                + source_chip_text(
                    self._station_command_snapshot_name(snapshot),
                    now,
                    density=density if density in {"condensed", "compact", "roomy"} else "compact",
                ),
                rail,
            )
            button.setObjectName("stationCommandSourceChip")
            button.setCheckable(True)
            button.setChecked(selected)
            button.setAccessibleName(
                f"{self._station_command_snapshot_name(snapshot)}, {status_label}, {now or 'frequency unavailable'}"
            )
            button.setToolTip(self._station_command_source_chip_tooltip(snapshot, selected_id))
            chip_role = role if role in {"danger", "warning", "muted"} else "success" if selected else "info"
            button.setStyleSheet(button_style(chip_role, theme))
            button.clicked.connect(
                lambda _checked=False, profile_id=ident: self._on_station_command_summary_radio_clicked(profile_id)
            )
            self._station_command_source_widgets[ident] = button
            row.addWidget(button)
        condition_levels = self._collect_condition_levels()
        if density != "condensed":
            for group, level in condition_levels:
                condition = QPushButton(f"{group}  L{level}", rail)
                condition.setObjectName("stationCommandConditionChip")
                condition.setAccessibleName(f"{group} condition level {level}")
                condition.setToolTip(f"{group} is at condition level {level}. Open condition level controls.")
                bg, fg = self._condition_level_palette(level)
                condition.setStyleSheet(
                    "QPushButton {"
                    f"background:{bg}; color:{fg}; border:1px solid {bg}; border-radius:5px; padding:3px 8px; font-weight:700;"
                    "}"
                )
                condition.clicked.connect(self._open_condition_levels_editor)
                row.addWidget(condition)
        elif condition_levels:
            condition = QPushButton(
                f"Groups {len(condition_levels)}" if len(condition_levels) > 1 else f"{condition_levels[0][0]} L{condition_levels[0][1]}",
                rail,
            )
            condition.setObjectName("stationCommandConditionChip")
            condition.setAccessibleName(
                ", ".join(f"{group} condition level {level}" for group, level in condition_levels)
            )
            condition.setToolTip(
                "Condition levels\n" + "\n".join(f"{group}: Level {level}" for group, level in condition_levels)
            )
            condition.setStyleSheet(button_style("warning", theme))
            condition.clicked.connect(self._open_condition_levels_editor)
            row.addWidget(condition)
        minimize = QToolButton(rail)
        minimize.setObjectName("stationCommandMinimizeToggle")
        minimized = bool(getattr(self, "_station_command_minimized", False))
        minimize.setText("More" if minimized else "Less")
        minimize.setAccessibleName("Restore control bar details" if minimized else "Minimize control bar details")
        minimize.setToolTip("Keep dial, authority, activity and alerts visible; collapse healthy readbacks." if not minimized else "Restore application readbacks and control bar details.")
        minimize.setStyleSheet(button_style("muted", theme))
        minimize.clicked.connect(self._toggle_station_command_minimized)
        # The retained card panel owns one disclosure control. Keep the legacy
        # rail toggle only when this rail is used outside that panel.
        if not isinstance(parent, QWidget) or parent.objectName() != "radioControlGlobalContext":
            row.addWidget(minimize)
        else:
            minimize.hide()
        row.addStretch(1)
        local_text = self.ledge_local_time_label.text() if hasattr(self, "ledge_local_time_label") else "Local --"
        utc_text = self.ledge_utc_time_label.text() if hasattr(self, "ledge_utc_time_label") else "UTC --"
        clock = QFrame(rail)
        clock.setObjectName("stationCommandClock")
        clock_layout = QVBoxLayout(clock)
        clock_layout.setContentsMargins(5, 0, 5, 0)
        clock_layout.setSpacing(0)
        if density != "condensed":
            self.station_command_local_time_label = QLabel(local_text, clock)
            self.station_command_local_time_label.setObjectName("stationCommandLocalTime")
            self.station_command_utc_time_label = QLabel(utc_text, clock)
            self.station_command_utc_time_label.setObjectName("stationCommandUtcTime")
            for label in (self.station_command_local_time_label, self.station_command_utc_time_label):
                label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                label.setProperty("clockSeconds", density == "roomy")
                if density != "roomy":
                    label.setText(re.sub(r"(\d{2}:\d{2}):\d{2}", r"\1", label.text()))
            clock_layout.addWidget(self.station_command_local_time_label)
            clock_layout.addWidget(self.station_command_utc_time_label)
        else:
            compact_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%MZ")
            self.station_command_utc_time_label = QLabel(compact_utc, clock)
            self.station_command_utc_time_label.setObjectName("stationCommandUtcTime")
            self.station_command_utc_time_label.setProperty("compactClock", True)
            self.station_command_utc_time_label.setToolTip(utc_text)
            self.station_command_utc_time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            clock_layout.addWidget(self.station_command_utc_time_label)
        row.addWidget(clock)
        return rail

    def _show_current_mesh_source_menu(self, anchor, key) -> None:
        item = next((item for item in self._station_command_saved_mesh_control_items()
                     if getattr(item, "key", item.label) == key), None)
        if item is not None:
            self._show_mesh_source_menu(anchor, item)

    def _station_command_current_snapshot(self, radio_id):
        return next((item for item in getattr(self, "_station_command_last_choices", ())
                     if self._station_command_snapshot_id(item) == radio_id), None)

    def _station_command_sop_button_text(self, button, selected) -> str:
        label = str(getattr(self, "_sop_next_action_label", "") or "").strip()
        minutes = self._get_next_sop_action_minutes(selected)
        count = int(getattr(self, "_sop_next_action_count", 0) or 0)
        full = sop_action_text(label, minutes, count)
        if not label:
            return "SOP actions"
        tail = full[len("SOP: " + label):]
        width = max(0, button.maximumWidth() - button.fontMetrics().horizontalAdvance("SOP: " + tail) - 28)
        return "SOP: " + button.fontMetrics().elidedText(label, Qt.ElideRight, width) + tail

    def _station_command_readback_visibility(self, name: str, text: str) -> bool:
        if not getattr(self, "_station_command_minimized", False) or name == "Activity":
            return True
        return "unknown" in text.lower() or "→" in text or "target" in text.lower()

    def _update_adaptive_station_values(self, selected, selected_id, application_summary, shell_state, theme) -> None:
        """Publish semantic changes in place; never rebuild controls for polling."""
        values = getattr(self, "_station_command_adaptive_values", {})
        attention = self._station_command_unique_attention_snapshots(
            [item for item in getattr(self, "_station_command_last_choices", ())
             if self._station_command_snapshot_needs_operator_attention(item)]
        )
        button = getattr(self, "_station_command_attention_widget", None)
        if button is not None:
            set_widget_value(button, "text", f"! {len(attention)}" if shell_state.density == "condensed" else f"ATTN: {len(attention)}")
            button.setVisible(bool(attention))
            noun = "radio or source needs" if len(attention) == 1 else "radios or sources need"
            set_widget_value(button, "accessibleName", f"{len(attention)} {noun} attention")
        for item in self._station_command_saved_mesh_control_items():
            button = getattr(self, "_station_command_mesh_widgets", {}).get(getattr(item, "key", item.label))
            if button is not None:
                set_widget_value(button, "toolTip", item.tooltip or "Manage this mesh source.")
                set_widget_value(button, "styleSheet", button_style(item.role, theme))
        for snapshot in getattr(self, "_station_command_last_choices", ()):
            ident = self._station_command_snapshot_id(snapshot)
            button = getattr(self, "_station_command_source_widgets", {}).get(ident)
            if button is None:
                continue
            role, status_label = self._station_command_attention_role_for_snapshot(snapshot)
            now = self._station_command_now_text_for_summary(snapshot, selected_id)
            prefix = "! " if role == "danger" else "△ " if role == "warning" else "● "
            name = self._station_command_snapshot_name(snapshot)
            set_widget_value(button, "text", prefix + source_chip_text(name, now, density=shell_state.density))
            set_widget_value(button, "toolTip", self._station_command_source_chip_tooltip(snapshot, selected_id))
            set_widget_value(button, "accessibleName", f"{name}, {status_label}, {now or 'frequency unavailable'}")
            set_widget_value(button, "styleSheet", button_style(role if role in {"danger", "warning", "muted"} else "info", theme))
        if selected is None or not values:
            return
        ident = self._station_command_snapshot_id(selected)
        resume = (self._station_command_scheduler_manual_qsy_active_for_radio(ident)
                  or self._station_command_timed_suspend_active_for_radio(ident)
                  or self._station_command_scheduler_suspended_manually_for_radio(ident))
        set_widget_value(values["hold"], "text", "Resume schedule" if resume else "Hold schedule")
        set_widget_value(values["hold"], "styleSheet", button_style("warning" if resume else "muted", theme))
        set_widget_value(values["hold"], "toolTip", "Resume scheduled control for this radio." if resume else "Temporarily hold scheduled changes for this radio.")
        self._update_station_command_radio_tile_hold_controls({})
        name = self._station_command_snapshot_name(selected)
        now = self._station_command_selected_context_text(selected)
        state = self._station_command_authority_text(selected)
        role, _label = self._station_command_attention_role_for_snapshot(selected)
        prefix = "! " if role == "danger" else "△ " if role == "warning" else ""
        set_widget_value(values["now"], "text", now)
        set_widget_value(values["now"], "toolTip", self._station_command_frequency_text(selected))
        set_widget_value(values["now"], "accessibleName", f"{name} now operating at {now}")
        set_widget_value(values["state"], "text", prefix + state)
        set_widget_value(values["state"], "toolTip", self._station_command_radio_summary_tooltip(selected, selected_id))
        set_widget_value(values["state"], "styleSheet",
                         f"color:{theme.get(role, theme.get('warning', '#D1A000'))}; font-weight:800;"
                         if role in {"danger", "warning"} else "")
        next_state = planned_transition_text(self._station_command_next_text(selected), self._station_command_next_minutes(selected), self._station_command_is_held(selected))
        set_widget_value(values["next"], "text", next_state.text)
        set_widget_value(values["next"], "accessibleName", next_state.text)
        set_widget_value(values["next"], "styleSheet", label_style(next_state.role, theme, weight=600))
        if values.get("progress") is not None:
            progress = self._station_command_qsy_progress_text(selected)
            progress_role = "danger" if "blocked" in progress else "warning" if "stale" in progress or "unknown" in progress else "muted"
            set_widget_value(values["progress"], "text", progress)
            set_widget_value(values["progress"], "toolTip", progress + ". A requested target is not an observed frequency.")
            set_widget_value(values["progress"], "accessibleName", progress)
            set_widget_value(values["progress"], "styleSheet", label_style(progress_role, theme, weight=400))
        if values["next"].property("prominence") != next_state.prominence:
            values["next"].setProperty("prominence", next_state.prominence)
        minutes = self._get_next_sop_action_minutes(selected)
        label = str(getattr(self, "_sop_next_action_label", "") or "").strip()
        count = int(getattr(self, "_sop_next_action_count", 0) or 0)
        sop = values["sop"]
        text = self._station_command_sop_button_text(sop, selected)
        set_widget_value(sop, "text", text)
        set_widget_value(sop, "accessibleName", sop_action_text(label, minutes, count) + ". Open SOP actions.")
        set_widget_value(sop, "toolTip", (f"Primary SOP action: {label}. " if label else "")
                         + (f"{count} upcoming action{'s' if count != 1 else ''}. " if count else "")
                         + "Open SOP actions and their operational rationale.")
        set_widget_value(sop, "styleSheet", button_style(next_action_state("SOP", minutes).role if minutes is not None else "muted", theme))
        for key, text, tooltip in application_summary:
            widget = values["apps"].get(key)
            if widget is not None:
                widget.setVisible(self._station_command_readback_visibility(key, text))
                set_widget_value(widget, "text", text)
                set_widget_value(widget, "toolTip", tooltip)
                set_widget_value(widget, "accessibleName", text + ". " + tooltip)
        health = self.station_command_radio_summary_widget.findChild(QToolButton, "stationCommandControlsHealth")
        if health is not None:
            summary = self._station_command_health_summary_for_profile(selected)
            state = str(summary.get("state", "warn") or "warn").strip().lower()
            role = "danger" if state == "error" else "warning" if state in {"warn", "warning", "review"} else "success"
            set_widget_value(health, "toolTip", f"Health: {summary.get('label', 'Review')}. Open radio health summary.")
            set_widget_value(health, "styleSheet",
                             "QToolButton {"
                             f"background:{theme.get('surface_alt', '#ECEFF1')};"
                             f"border:2px solid {theme.get(role, theme.get('border', '#CCCCCC'))};"
                             "border-radius:5px; padding:3px;"
                             "}QToolButton:focus {"
                             f"border:2px solid {theme.get('info', '#1565C0')};"
                             "}")

    def _station_command_application_summary(self, selected):
        """Format only worker-retained observations; never probe applications."""
        radio_id = self._station_command_snapshot_id(selected)
        profile = self._station_command_health_profile(selected, radio_id) or {}
        evidence = self._station_command_display_evidence(selected)
        expected = evidence.get("expected_state") or {}
        states = evidence.get("display_field_states") or {}
        summaries = []
        if profile.get("use_fldigi"):
            target = expected.get("fldigi_offset_hz")
            if target is None:
                group, band = self._station_command_assigned_plan_group_band(selected)
                target = self._station_command_operating_group_qsy_lookup().get((group.upper(), band), {}).get("fldigi_offset")
            observed = evidence.get("fldigi_offset_hz")
            mode = evidence.get("fldigi_mode")
            field_state = states.get("fldigi_offset_hz", states.get("fldigi_mode", "unobserved"))
            if mode and states.get("fldigi_mode") != "current":
                field_state = "delayed" if field_state == "current" else field_state
            text = application_display_text("FLDigi", observed, target=target, mode=mode, state=field_state)
            if field_state == "current" and fldigi_receive_active(evidence) and not display_activity_text(evidence):
                text += " · RX"
            summaries.append(("FLDigi", text,
                "Actual FLDigi modem and audio carrier from this radio's endpoint. Last-confirmed values are presentation only, not fresh safety evidence."))
        if profile.get("use_js8call"):
            target = expected.get("js8_offset_hz")
            if target is None:
                target = profile.get("js8_offset_hz")
            text = application_display_text("JS8", evidence.get("js8_offset_hz"), target=target,
                state=states.get("js8_offset_hz", "unobserved"))
            if states.get("js8_busy") == "current" and evidence.get("js8_busy"):
                text += " · Busy"
            summaries.append(("JS8", text,
                "Actual JS8Call offset. Busy is not proof of RX. Click JS8 to edit and save the default; scheduler locks and fresh activity safety still apply."))
        if profile.get("use_varac"):
            text = varac_display_text(evidence.get("varac_status"), current=states.get("varac_status") == "current")
            summaries.append(("VarAC", text,
                "Connected means CommsPlan can read this configured VarAC instance’s status while its process is running. It does not confirm VARA modem or RF readiness. QSO-BBS requires explicit BBS-session evidence."))
        lane = self._station_command_lane_for_radio(radio_id) or {}
        activity = display_activity_text(evidence, net=str(lane.get("current_source") or "").upper() == "NET")
        summaries.append(("Activity", activity,
            "TX requires fresh positive transmit evidence. NET is operating context. Unreported activity remains neutral; busy alone is not RX or QSO."))
        return tuple(summaries)

    def _show_station_js8_offset_editor(self, selected) -> None:
        radio_id = self._station_command_snapshot_id(selected)
        profile = self._station_command_health_profile(selected, radio_id) or {}
        if not profile.get("use_js8call"):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{self._station_command_snapshot_name(selected)} — JS8 offset")
        body = QVBoxLayout(dialog)
        summary = QLabel("Change this radio's default JS8 offset. Scheduled offsets retain authority; changes wait for scheduler safety checks.", dialog)
        summary.setWordWrap(True)
        body.addWidget(summary)
        spin = QSpinBox(dialog)
        spin.setRange(1800, 2700)
        spin.setSingleStep(50)
        spin.setSuffix(" Hz")
        spin.setValue(int(profile.get("js8_offset_hz") or 2050))
        spin.setAccessibleName("Default JS8 offset")
        body.addWidget(spin)
        buttons = QHBoxLayout()
        save = QPushButton("Save Default", dialog)
        cancel = QPushButton("Cancel", dialog)
        theme = resolve_theme(self.settings)
        save.setStyleSheet(button_style("success", theme))
        cancel.setStyleSheet(button_style("muted", theme))
        def commit():
            if self._save_station_command_js8_offset(radio_id, spin.value(),
                    expected_instance_id=int(profile.get("js8_instance_id") or 0) or None):
                dialog.accept()
        save.clicked.connect(commit)
        cancel.clicked.connect(dialog.reject)
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        body.addLayout(buttons)
        dialog.exec()

    def _refresh_adaptive_station_command_shell(self, choices: list[object], selected_id: int) -> None:
        # The approved radio-card surface owns the normal station-control
        # presentation.  Keep the older selected-radio builder available for
        # lightweight compatibility harnesses that do not construct the panel.
        if getattr(self, "radio_control_panel", None) is not None:
            self._refresh_radio_control_panel(choices, selected_id)
            return
        layout = self.station_command_radio_summary_layout
        parent = self.station_command_radio_summary_widget
        selected = next(
            (item for item in choices if self._station_command_snapshot_id(item) == int(selected_id or 0)),
            choices[0] if choices else None,
        )
        if selected is not None:
            selected_id = self._station_command_snapshot_id(selected)
        try:
            width = self._station_command_radio_summary_available_width()
            large_text = float(resolve_ui_text_scale(self.settings)) > 1.05
        except Exception:
            width, large_text = 900, False
        shell_state = shell_layout_state(width, source_count=len(choices), large_text=large_text)
        self._adaptive_station_shell_density = shell_state.density
        selected_signature = ()
        if selected is not None:
            selected_signature = (
                self._station_command_snapshot_id(selected),
                self._station_command_selected_context_text(selected),
                self._station_command_authority_text(selected),
                self._station_command_qsy_progress_text(selected),
                self._station_command_next_text(selected),
                self._station_command_next_minutes(selected),
                str(self._station_command_health_summary_for_profile(selected).get("state", "")),
                self._station_command_scheduler_manual_qsy_active_for_radio(selected_id),
                self._station_command_scheduler_suspended_manually_for_radio(selected_id),
                self._station_command_timed_suspend_active_for_radio(selected_id),
            )
        # A single context row must fit at the actual configured font size.
        # Use a stable role budget, rather than changing layout as live text varies.
        if shell_state.density == "compact" and parent.width() < parent.fontMetrics().horizontalAdvance("M") * 110:
            shell_state = replace(shell_state, stack_primary_context=True)
        application_summary = self._station_command_application_summary(selected) if selected is not None else ()
        mesh_items = self._station_command_saved_mesh_control_items()
        source_values = tuple(
            (self._station_command_snapshot_id(item), self._station_command_snapshot_name(item),
             self._station_command_now_text_for_summary(item, selected_id),
             self._station_command_attention_role_for_snapshot(item),
             self._station_command_source_chip_tooltip(item, selected_id))
            for item in choices
        )
        signature = (
            "adaptive-shell",
            shell_state.density,
            shell_state.stack_primary_context,
            bool(getattr(self, "_station_command_controls_expanded", False)),
            bool(getattr(self, "_station_command_minimized", False)),
            tuple(self._station_command_snapshot_id(item) for item in choices),
            selected_signature,
            tuple(self._collect_condition_levels()),
            self._station_command_source_control_items_signature(self._station_command_saved_mesh_control_items()),
            application_summary,
            self._get_next_sop_action_minutes(selected),
            str(getattr(self, "_sop_next_action_label", "") or ""),
            int(getattr(self, "_sop_next_action_count", 0) or 0),
            source_values,
        )
        previous_signature = getattr(self, "_station_command_radio_summary_signature", None)
        self._station_command_radio_summary_signature = signature
        try:
            theme = resolve_theme(self.settings)
        except Exception:
            theme = {}
        structure = (
            shell_state.density,
            shell_state.stack_primary_context,
            bool(getattr(self, "_station_command_controls_expanded", False)),
            bool(getattr(self, "_station_command_minimized", False)),
            tuple((self._station_command_snapshot_id(item), self._station_command_snapshot_name(item)) for item in choices),
            selected_id,
            tuple(self._collect_condition_levels()),
            tuple((getattr(item, "key", item.label), item.label) for item in mesh_items),
            tuple(name for name, _text, _tip in application_summary),
            tuple(sorted(theme.items())),
            parent.font().toString(),
            # These inputs own menu destinations and captured command authority.
            # Readbacks, countdowns and health wording do not own widget lifetime.
            repr(sorted(self._station_command_plan_qsy_options(selected).items())) if selected is not None else "",
            self._station_command_preferred_qsy_key(selected) if selected is not None else "",
            self._station_command_frequency_controls_available(selected) if selected is not None else False,
        )
        if structure == getattr(self, "_station_command_shell_structure_signature", None):
            if signature != previous_signature:
                self._update_adaptive_station_values(selected, selected_id, application_summary, shell_state, theme)
            return
        self._station_command_shell_structure_signature = structure
        self._station_command_adaptive_values = {}
        self.station_command_local_time_label = None
        self.station_command_utc_time_label = None
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Removing a visible widget's parent briefly promotes it to a
                # native top-level window on Windows. Hide it before retaining
                # the existing unparent-and-delete lifecycle.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._station_command_radio_tile_controls = {}
        layout.addWidget(
            self._build_adaptive_station_awareness_rail(
                parent,
                choices,
                selected_id,
                density=shell_state.density,
                theme=theme,
            )
        )
        if selected is None:
            empty = QLabel("No configured radios · add a radio in Configuration", parent)
            empty.setObjectName("stationCommandRadioSummaryEmpty")
            layout.addWidget(empty)
            parent.setMinimumWidth(0)
            self._sync_station_command_radio_summary_height(card_mode=True)
            return

        ident = self._station_command_snapshot_id(selected)
        context = QFrame(parent)
        context.setObjectName("stationCommandPrimaryContext")
        context.setAccessibleName(f"Selected radio {self._station_command_snapshot_name(selected)}")
        context_layout = QGridLayout(context)
        context_layout.setContentsMargins(7, 4, 7, 4)
        context_layout.setHorizontalSpacing(8)
        context_layout.setVerticalSpacing(3)
        now = self._station_command_selected_context_text(selected)
        state = self._station_command_authority_text(selected)
        attention_role, _attention_label = self._station_command_attention_role_for_snapshot(selected)
        radio_name = self._station_command_snapshot_name(selected)
        now_label = ElidedLabel(now, context)
        now_label.setObjectName("stationCommandPrimaryNow")
        now_label.setStyleSheet(label_style("text", theme, weight=700))
        now_label.setToolTip(self._station_command_frequency_text(selected))
        now_label.setAccessibleName(f"{radio_name} now operating at {now}")
        try:
            context_text_width = now_label.fontMetrics().horizontalAdvance(now_label.text()) + 14
            now_label.setMinimumWidth(min(270, max(150, context_text_width)))
        except Exception:
            pass
        state_prefix = "! " if attention_role == "danger" else "△ " if attention_role == "warning" else ""
        state_label = QLabel(f"{state_prefix}{state}", context)
        state_label.setWordWrap(True)
        state_label.setMinimumWidth(state_label.fontMetrics().horizontalAdvance("M") * 14)
        state_label.setFixedHeight(state_label.fontMetrics().lineSpacing() * 2 + 4)
        state_label.setObjectName("stationCommandPrimaryState")
        state_label.setToolTip(self._station_command_radio_summary_tooltip(selected, selected_id))
        if attention_role in {"danger", "warning"}:
            state_label.setStyleSheet(
                f"color:{theme.get(attention_role, theme.get('warning', '#D1A000'))}; font-weight:800;"
            )
        next_state = planned_transition_text(
            self._station_command_next_text(selected), self._station_command_next_minutes(selected),
            self._station_command_is_held(selected),
        )
        next_label = ElidedLabel(next_state.text, context)
        next_label.setMinimumWidth(0)
        next_label.setObjectName("stationCommandPrimaryNext")
        next_label.setProperty("prominence", next_state.prominence)
        next_label.setAccessibleName(f"Next scheduled action: {next_state.text}")
        next_label.setToolTip(
            f"Next scheduled destination for {self._station_command_snapshot_name(selected)}. "
            "The workspace explains the associated action and rationale."
        )
        next_label.setStyleSheet(label_style(next_state.role, theme, weight=600))
        progress = self._station_command_qsy_progress_text(selected)
        progress_label = ElidedLabel(progress, context)
        progress_label.setObjectName("stationCommandQsyProgress")
        progress_role = "danger" if "blocked" in progress else "warning" if "stale" in progress or "unknown" in progress else "muted"
        progress_label.setStyleSheet(label_style(progress_role, theme, weight=400))
        progress_label.setAccessibleName(progress)
        progress_label.setToolTip(progress + ". A requested target is not an observed frequency.")

        actions = QWidget(context)
        actions.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        actions_layout = QGridLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setHorizontalSpacing(5)
        actions_layout.setVerticalSpacing(3)
        controls_expanded = bool(getattr(self, "_station_command_controls_expanded", False))
        qsy_button = self._build_adaptive_qsy_button(context, selected, timed=False, theme=theme)
        manual_qsy = self._station_command_scheduler_manual_qsy_active_for_radio(ident)
        timed_suspend = self._station_command_timed_suspend_active_for_radio(ident)
        manual_suspend = self._station_command_scheduler_suspended_manually_for_radio(ident)
        hold = QPushButton("Resume schedule" if manual_qsy or timed_suspend or manual_suspend else "Hold schedule", actions)
        hold.setObjectName("stationCommandQuickSchedule")
        hold.setToolTip(
            "Resume scheduled control for this radio."
            if manual_qsy or timed_suspend or manual_suspend
            else "Temporarily hold scheduled changes for this radio."
        )
        hold.clicked.connect(
            lambda _checked=False, radio_id=ident: self._run_station_quick_hold(radio_id)
        )
        hold.setStyleSheet(button_style("warning" if manual_qsy or timed_suspend or manual_suspend else "muted", theme))
        if controls_expanded:
            # These quick controls yield to the advanced tray. They retain the
            # same construction path for collapsed-mode consistency but must
            # not float unlaid-out over the selected-radio context.
            qsy_button.setVisible(False)
            hold.setVisible(False)
        sop_minutes = self._get_next_sop_action_minutes(selected)
        sop_action_label = str(getattr(self, "_sop_next_action_label", "") or "").strip()
        sop_count = int(getattr(self, "_sop_next_action_count", 0) or 0)
        sop = QPushButton(sop_action_text(sop_action_label, sop_minutes, sop_count), actions)
        sop.setObjectName("stationCommandQuickSop")
        sop.setAccessibleName(sop_action_text(sop_action_label, sop_minutes, sop_count) + ". Open SOP actions.")
        sop.setToolTip(
            (f"Primary SOP action: {sop_action_label}. " if sop_action_label else "")
            + (f"{sop_count} upcoming action{'s' if sop_count != 1 else ''}. " if sop_count else "")
            + "Open SOP actions and their operational rationale."
        )
        sop.clicked.connect(self._open_sop_from_station_command)
        sop_role = next_action_state("SOP", sop_minutes).role if sop_minutes is not None else "muted"
        sop.setStyleSheet(button_style(sop_role, theme))
        controls = QPushButton("Close controls" if controls_expanded else "Controls…", actions)
        controls.setObjectName("stationCommandControlsToggle")
        controls.setAccessibleName("Close advanced radio controls" if controls_expanded else "Open advanced radio controls")
        controls.setToolTip(
            "Close advanced radio controls."
            if controls_expanded
            else "Show target, QSY timing, schedule, health and plan controls for the selected radio."
        )
        controls.clicked.connect(self._toggle_adaptive_station_controls)
        controls.setStyleSheet(
            button_style("info" if controls_expanded else "muted", theme)
        )
        minimized = bool(getattr(self, "_station_command_minimized", False))
        column = 0
        if not controls_expanded:
            if not minimized:
                actions_layout.addWidget(qsy_button, 0, column)
                column += 1
            else:
                qsy_button.hide()
            actions_layout.addWidget(hold, 0, column)
            column += 1
        wrap_sop = shell_state.stack_primary_context and bool(sop_action_label) and not controls_expanded and not minimized
        if wrap_sop:
            actions_layout.addWidget(controls, 0, column)
            actions_layout.addWidget(sop, 1, 0, 1, column + 1)
        else:
            actions_layout.addWidget(sop, 0, column)
            actions_layout.addWidget(controls, 0, column + 1)
        hold.setMinimumWidth(hold.fontMetrics().horizontalAdvance("Resume schedule") + 28)
        sop.setMaximumWidth(parent.fontMetrics().horizontalAdvance("M") * (32 if shell_state.density == "roomy" else 30))
        sop.setText(self._station_command_sop_button_text(sop, selected))

        if shell_state.stack_primary_context:
            context_layout.addWidget(now_label, 0, 0)
            context_layout.addWidget(state_label, 0, 1)
            context_layout.addWidget(next_label, 1, 0)
            context_layout.addWidget(progress_label, 2, 0, 1, 2)
            context_layout.addWidget(actions, 3, 0, 1, 2)
            context_layout.setColumnStretch(0, 1)
        else:
            context_layout.addWidget(now_label, 0, 0)
            context_layout.addWidget(state_label, 0, 1)
            context_layout.addWidget(actions, 0, 2)
            context_layout.addWidget(next_label, 1, 0, 1, 2)
            context_layout.addWidget(progress_label, 1, 2)
            context_layout.setColumnStretch(0, 1)
        # A reserved one-line progress slot prevents polling from growing the shell.
        progress_label.setMinimumHeight(progress_label.fontMetrics().lineSpacing() + 4)
        layout.addWidget(context)

        if application_summary:
            app_row = QWidget(parent)
            app_row.setObjectName("stationCommandApplicationAwareness")
            app_layout = QHBoxLayout(app_row)
            app_layout.setContentsMargins(4, 0, 4, 0)
            app_layout.setSpacing(6)
            for name, text, tooltip in application_summary:
                if name == "JS8":
                    value = QPushButton(text, app_row)
                    value.setStyleSheet(button_style("muted", theme))
                    value.clicked.connect(lambda _checked=False, target=selected: self._show_station_js8_offset_editor(target))
                else:
                    value = ReadbackLabel(text, app_row)
                    value.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
                    value.setMinimumWidth(0)
                value.setVisible(self._station_command_readback_visibility(name, text))
                value.setObjectName("stationCommand" + name + "Awareness")
                value.setToolTip(tooltip)
                value.setAccessibleName(text + ". " + tooltip)
                app_layout.addWidget(value)
            app_layout.addStretch(1)
            layout.addWidget(app_row)

        self._station_command_adaptive_values = {
            "now": now_label, "state": state_label, "next": next_label, "sop": sop, "hold": hold,
            "progress": progress_label,
            "apps": {name: app_row.findChild(QWidget, "stationCommand" + name + "Awareness")
                     for name, _text, _tip in application_summary} if application_summary else {},
        }

        if controls_expanded:
            layout.addWidget(
                self._build_adaptive_station_controls_tray(
                    parent,
                    selected,
                    density=shell_state.density,
                    theme=theme,
                )
            )
        else:
            self._station_command_radio_tile_controls = {}
        parent.setMinimumWidth(0)
        parent.setMaximumWidth(16777215)
        try:
            parent.adjustSize()
        except Exception:
            pass
        self._sync_station_command_radio_summary_height(card_mode=True)
        self._schedule_station_command_summary_geometry()

    def _station_command_card_plan_options(self, snapshot: object) -> tuple[dict, ...]:
        """Resolve destinations from worker-published plan rows and memory settings."""
        raw = self._station_command_value(snapshot, "plan_options", ())
        if isinstance(raw, Mapping):
            raw = tuple(raw.values())
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and raw:
            options = [dict(item) for item in raw if isinstance(item, Mapping)]
        else:
            ident = self._station_command_snapshot_id(snapshot)
            lookup = self._station_command_operating_group_qsy_lookup()
            options = []
            seen = set()
            for ref in self._station_command_assigned_plan_refs_for_radio(ident):
                meta = self._station_command_qsy_meta_from_plan_ref(ref, lookup)
                if not meta:
                    continue
                key = (meta.get("group"), meta.get("band"), meta.get("freq"))
                if key not in seen:
                    seen.add(key)
                    options.append(meta)
        catalog = []
        for row in self._station_command_operating_group_qsy_rows():
            meta = dict(row)
            meta["freq"] = parse_frequency_mhz(meta.get("frequency") or meta.get("freq"))
            if meta["freq"] is not None:
                catalog.append(meta)
        unique = {}
        for meta in (*options, *catalog):
            key = self._station_command_qsy_option_key(meta)
            if key:
                unique[key] = meta
        options = list(unique.values())
        # Next is a shortcut to an existing assigned destination, never a new
        # command target fabricated by the UI from a countdown or label.
        ident = self._station_command_snapshot_id(snapshot)
        lane = self._station_command_lane_for_radio(ident) or {}
        next_entry = lane.get("next_entry") or {}
        next_hz = None
        if isinstance(next_entry, Mapping):
            frequency = parse_frequency_mhz(next_entry.get("frequency") or next_entry.get("freq"))
            if frequency is not None:
                next_hz = round(frequency * 1_000_000)
        minutes = self._station_command_next_minutes(snapshot)
        for option in options:
            option.pop("is_next", None)
            option.pop("next_minutes", None)
            try:
                if (next_hz and minutes is not None and 0 <= minutes <= 15
                        and abs(round(float(option.get("freq")) * 1_000_000) - next_hz) <= 10):
                    option["is_next"] = True
                    option["next_minutes"] = minutes
            except (TypeError, ValueError):
                pass
        return tuple(options)

    def _radio_card_display(self, snapshot: object, *, plan_options=None) -> RadioCardDisplay:
        """Translate the parent display cache into card presentation fields."""

        ident = self._station_command_snapshot_id(snapshot)
        evidence: Mapping[str, object] = {}
        getter = getattr(self, "_station_command_display_evidence", None)
        if callable(getter):
            try:
                raw = getter(snapshot)
                if isinstance(raw, Mapping):
                    evidence = raw
            except Exception:
                evidence = {}
        dial_text = "Frequency unavailable"
        dial_hz = None
        dial_current = False
        dial_getter = getattr(self, "_station_command_display_dial", None)
        if callable(dial_getter):
            try:
                raw_dial = dial_getter(snapshot)
                if isinstance(raw_dial, Sequence) and len(raw_dial) >= 3:
                    dial_text = str(raw_dial[0] or dial_text)
                    dial_current = bool(raw_dial[1])
                    dial_hz = int(raw_dial[2]) if raw_dial[2] not in (None, "") else None
            except Exception:
                pass
        observed_meta = evidence.get("observed_meta")
        if not isinstance(observed_meta, Mapping) and dial_hz:
            observed_meta = {"freq": float(dial_hz) / 1_000_000.0}
        group = str(evidence.get("current_group") or evidence.get("group") or "").strip()
        band = str(evidence.get("current_band") or evidence.get("band") or "").strip()
        if not group or not band:
            try:
                group = group or str((observed_meta or {}).get("group") or "").strip()
                band = band or str((observed_meta or {}).get("band") or "").strip()
            except Exception:
                pass
        # Worker-published plan options carry the resolved group/band.  Use
        # them only to explain an observed dial; a requested destination never
        # becomes the current group in the card.
        if observed_meta and (not group or not band):
            try:
                observed_freq = float(observed_meta.get("freq") or observed_meta.get("frequency"))
            except (TypeError, ValueError):
                observed_freq = 0.0
            raw_plan = plan_options if plan_options is not None else self._station_command_card_plan_options(snapshot)
            if isinstance(raw_plan, Mapping):
                raw_plan = tuple(raw_plan.values())
            matching_groups, matching_bands = set(), set()
            for option in raw_plan if isinstance(raw_plan, Sequence) and not isinstance(raw_plan, (str, bytes)) else ():
                if not isinstance(option, Mapping):
                    continue
                try:
                    option_freq = float(option.get("freq", option.get("frequency")))
                except (TypeError, ValueError):
                    continue
                if observed_freq and abs(round(option_freq * 1_000_000) - round(observed_freq * 1_000_000)) <= 10:
                    matching_groups.add(str(option.get("group") or option.get("group_name") or "").strip())
                    matching_bands.add(str(option.get("band") or "").strip())
            # One dial can belong to multiple groups. A first-row choice is
            # not evidence that this radio is operating in that group.
            if not group and len(matching_groups) == 1:
                group = next(iter(matching_groups))
            if not band and len(matching_bands) == 1:
                band = next(iter(matching_bands))
        authority = evidence.get("authority")
        try:
            authority = authority or self._station_command_authority_text(snapshot)
        except Exception:
            authority = authority or "Control state unavailable"
        state = str(evidence.get("display_state") or "current").strip().lower()
        notice = str(evidence.get("notice") or "").strip()
        if not notice and state in {"delayed", "unavailable", "invalidated"}:
            notice = ("CAT delayed · last confirmed frequency" if state == "delayed" else
                      "CAT unavailable · last confirmed frequency" if dial_hz else
                      "CAT unavailable")
        next_text = str(evidence.get("next_text") or "").strip()
        if not next_text:
            try:
                next_text = self._station_command_next_text(snapshot)
            except Exception:
                next_text = "No next destination"
        try:
            next_state = planned_transition_text(
                next_text,
                None if next_text == "Schedule updating" else self._station_command_next_minutes(snapshot),
                self._station_command_is_held(snapshot),
            )
            next_text = next_state.text
        except Exception:
            pass
        next_tooltip = next_text
        transition_getter = getattr(self, "_station_command_next_transition", None)
        transition = transition_getter(snapshot) if callable(transition_getter) else {}
        if isinstance(transition, Mapping) and transition.get("state") == "ready":
            target = transition.get("entry") or {}
            next_group, next_band = self._station_command_entry_group_band(target)
            next_text = compact_card_next_text(
                group=next_group, band=next_band,
                frequency=self._station_command_qsy_frequency_text(target),
                current_group=group, current_band=band,
                minutes=self._station_command_next_minutes(snapshot), held=self._station_command_is_held(snapshot))
            next_tooltip += "\n" + " · ".join(str(part) for part in
                (next_group, next_band, self._station_command_qsy_frequency_text(target), transition.get("start_utc")) if part)
        elif isinstance(transition, Mapping) and transition.get("state") == "no_next":
            next_text = "Next: none"
        elif isinstance(transition, Mapping) and transition.get("state") == "pending":
            next_text = "Schedule updating"
        progress_text = str(evidence.get("qsy_progress") or "").strip()
        if not progress_text:
            try:
                progress_text = self._station_command_qsy_progress_text(snapshot)
            except Exception:
                progress_text = ""
        app_rows: tuple[tuple[str, str, str], ...] = ()
        try:
            raw_rows = self._station_command_application_summary(snapshot)
            if isinstance(raw_rows, Sequence):
                app_rows = tuple(
                    (str(row[0]), str(row[1]), str(row[2] if len(row) > 2 else ""))
                    for row in raw_rows
                    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and len(row) >= 2
                )
        except Exception:
            app_rows = ()
        try:
            role, _ = self._station_command_attention_role_for_snapshot(snapshot)
        except Exception:
            role = "info"
        try:
            frequency_controls_available = bool(self._station_command_frequency_controls_available(snapshot))
        except Exception:
            frequency_controls_available = False
        runtime_active = self._station_command_value(snapshot, "runtime_active", None)
        observer = str(self._station_command_value(snapshot, "device_class", "")).lower() == "observer"
        inactive = runtime_active is not None and not self._station_command_bool(runtime_active, default=False)
        if observer or inactive:
            frequency_controls_available = False
            authority = "Inactive" if inactive else "Read-only observer"
            role = "muted"
        hold_remaining_minutes = None
        hold_getter = getattr(self, "_station_command_hold_snapshot_for_radio", None)
        if callable(hold_getter):
            try:
                cached_hold = hold_getter(ident)
                remaining = cached_hold.get("remaining_sec") if cached_hold.get("active") else None
                if remaining is not None:
                    hold_remaining_minutes = max(0, int(math.ceil(float(remaining) / 60)))
            except (AttributeError, TypeError, ValueError, OverflowError):
                pass
        scheduler = getattr(self, "scheduler", None)
        choices = radio_choices({OPERATOR_CONTROLS_KEY: self.settings.get(OPERATOR_CONTROLS_KEY, {}),
                                 "sop_schedule_layer_enabled": self.settings.get("sop_schedule_layer_enabled", True)}, ident)
        lane = self._station_command_lane_for_radio(ident) or {}
        net_active = bool(scheduler is not None and scheduler._manual_net_active_for_radio(ident))
        net_active = net_active or (str(lane.get("current_source") or "").upper() == "NET"
                                    and not self._station_command_is_held(snapshot))
        policy = scheduler.radio_timer_policy(ident) if scheduler is not None else {}
        if net_active:
            authority = "Net · operator control"
            role = "info"
            next_text = "Schedule paused"
            next_tooltip = "Net mode: operator controls tuning; no automatic frequency, mode or offset corrections."
        elif choices.get("routine_schedule_enabled") is False and choices.get("sop_tuning_enabled") is False:
            authority = "Schedule sources off"
            next_text = "Sources off"
            next_tooltip = "Automatic tuning disabled: all schedule sources are off for this radio."
        elif lane.get("schedule_projection_pending"):
            next_text = "Schedule updating"
            next_tooltip = "Schedule updating: automatic tuning waits for the refreshed plan."
        return RadioCardDisplay(
            observed_text=dial_text,
            observed_hz=dial_hz,
            observed_meta=dict(observed_meta) if isinstance(observed_meta, Mapping) else None,
            display_state=state,
            current_group=group,
            current_band=band,
            authority=str(authority or "On schedule"),
            authority_role=str(role or "info"),
            next_text=next_text,
            next_tooltip=next_tooltip,
            notice=notice,
            qsy_progress=progress_text,
            app_rows=app_rows,
            frequency_controls_available=frequency_controls_available,
            hold_remaining_minutes=hold_remaining_minutes,
            net_active=net_active,
            sop_tuning_enabled=choices.get("sop_tuning_enabled") is not False,
            routine_schedule_enabled=choices.get("routine_schedule_enabled") is not False,
            timer_policy=policy,
            cat_verified=bool(dial_hz and dial_current),
            cat_status="CAT delayed" if state == "delayed" else "CAT unavailable",
        )

    def _refresh_radio_control_panel(self, choices: Sequence[object], selected_id: int) -> None:
        panel = getattr(self, "radio_control_panel", None)
        layout = getattr(self, "station_command_radio_summary_layout", None)
        if panel is None or layout is None:
            return
        try:
            theme = resolve_theme(self.settings)
        except Exception:
            theme = {}
        panel.set_theme(theme)
        global_layout = getattr(panel, "global_context_layout", None)
        if global_layout is not None:
            try:
                global_signature = (
                    tuple(self._collect_condition_levels()),
                    self._station_command_source_control_items_signature(self._station_command_saved_mesh_control_items()),
                    str(getattr(self, "_adaptive_station_shell_density", "compact")),
                    repr(theme),
                    panel.font().toString(),
                )
            except Exception:
                global_signature = ((), (), "compact")
            if global_signature != getattr(panel, "_global_context_signature", None):
                panel._global_context_signature = global_signature
                while global_layout.count():
                    item = global_layout.takeAt(0)
                    widget = item.widget()
                    if widget is not None:
                        widget.hide()
                        widget.deleteLater()
                try:
                    global_rail = self._build_adaptive_station_awareness_rail(
                        panel.global_context,
                        [],
                        int(selected_id or 0),
                        density=global_signature[2] if global_signature[2] in {"condensed", "compact", "roomy"} else "compact",
                        theme=theme,
                    )
                    global_layout.addWidget(global_rail)
                    panel._global_awareness_rail = global_rail
                except Exception:
                    panel._global_awareness_rail = None
            # The one-second clock publisher owns formatting. Status polls
            # must not overwrite its minute-only/compact presentation.
        in_dock = bool(panel.property("dockMode"))
        if not in_dock:
            # The retained panel owns its card viewport; avoid a nested
            # outer-summary scrollbar in the full shell.
            try:
                panel.cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
                self.station_command_radio_summary_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            except (AttributeError, RuntimeError):
                pass
        if not in_dock and layout.indexOf(panel) < 0:
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None and widget is not panel:
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()
            layout.addWidget(panel)
        if not panel.isVisible():
            panel.setVisible(True)
        options_by_id: dict[int, Sequence[object]] = {}
        display_by_id: dict[int, RadioCardDisplay] = {}
        held_by_id: dict[int, bool] = {}
        sop_text_by_id: dict[int, str] = {}
        sop_minutes_by_id: dict[int, float | None] = {}
        for snapshot in choices:
            ident = self._station_command_snapshot_id(snapshot)
            if ident <= 0:
                continue
            raw_options = self._station_command_card_plan_options(snapshot)
            options_by_id[ident] = raw_options
            display_by_id[ident] = self._radio_card_display(snapshot, plan_options=raw_options)
            held_by_id[ident] = bool(
                self._station_command_scheduler_manual_qsy_active_for_radio(ident)
                or self._station_command_timed_suspend_active_for_radio(ident)
                or self._station_command_scheduler_suspended_manually_for_radio(ident)
            )
            minutes = self._get_next_sop_action_minutes(snapshot)
            sop_minutes_by_id[ident] = minutes
            sop_text_by_id[ident] = sop_action_text(
                str(getattr(self, "_sop_next_action_label", "") or ""), minutes,
                int(getattr(self, "_sop_next_action_count", 0) or 0),
            )
        if any(display.hold_remaining_minutes is not None for display in display_by_id.values()):
            timer = getattr(self, "_hold_state_timer", None)
            if timer is not None and not timer.isActive():
                timer.start()
        # The shared reminder summary still belongs to the selected radio;
        # projecting every card must not leave it scoped to the last peer.
        selected_snapshot = next((item for item in choices
            if self._station_command_snapshot_id(item) == int(selected_id or 0)), None)
        if selected_snapshot is not None:
            self._get_next_sop_action_minutes(selected_snapshot)
        durations: list[int] = []
        combo = getattr(self, "station_command_duration_combo", None)
        if isinstance(combo, QComboBox):
            for index in range(combo.count()):
                try:
                    value = int(combo.itemData(index) or 0)
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    durations.append(value)
        panel.update_cards(
            list(choices),
            selected_id=int(selected_id or 0),
            display_by_id=display_by_id,
            options_by_id=options_by_id,
            hold_durations=durations,
            held_by_id=held_by_id,
            sop_text_by_id=sop_text_by_id,
            sop_minutes_by_id=sop_minutes_by_id,
        )
        panel.set_minimized(bool(getattr(self, "_station_command_minimized", False)))
        try:
            self._sync_station_command_radio_summary_height(card_mode=True)
        except Exception:
            pass

    def _on_radio_control_panel_minimized_changed(self, minimized: bool) -> None:
        """Keep the legacy host flag aligned with the retained card panel."""

        minimized = bool(minimized)
        if bool(getattr(self, "_station_command_minimized", False)) == minimized:
            return
        self._station_command_minimized = minimized
        self._station_command_radio_summary_signature = None

    def _on_radio_card_qsy(self, radio_id: int, meta: Mapping[str, object] | None, duration: int | None) -> None:
        # A disappearing draft must not fall back to the legacy selected-radio
        # combo, which can still refer to a peer radio's destination.
        if int(radio_id or 0) <= 0 or not isinstance(meta, Mapping):
            return
        if isinstance(meta, Mapping):
            payload = dict(meta)
            payload["target_device_profile_id"] = int(radio_id or 0)
        else:
            payload = None
        if not self._activate_station_command_radio(int(radio_id or 0)):
            return
        self._dismiss_radio_off_schedule_prompt(radio_id)
        if duration is None:
            if payload is not None:
                payload["manual_hold_policy"] = "until_resume"
            self._on_station_command_qsy_now_clicked(payload)
        else:
            self._on_station_command_qsy_hold_clicked(payload, duration)

    def _on_radio_card_hold(self, radio_id: int, duration: int | None) -> None:
        if int(radio_id or 0) <= 0:
            return
        if not self._activate_station_command_radio(int(radio_id or 0)):
            return
        self._dismiss_radio_off_schedule_prompt(radio_id)
        if duration is None:
            self._on_station_command_pause_clicked(int(radio_id or 0))
        else:
            self._on_station_command_timed_suspend_clicked(int(radio_id or 0), minutes=int(duration))

    def _on_radio_card_resume(self, radio_id: int) -> None:
        if int(radio_id or 0) <= 0 or not self._activate_station_command_radio(radio_id):
            return
        self._dismiss_radio_off_schedule_prompt(radio_id)
        self._on_station_command_resume_clicked(radio_id)

    def _set_station_command_card_duration(self, minutes: int) -> None:
        combo = getattr(self, "station_command_duration_combo", None)
        if not isinstance(combo, QComboBox):
            return
        for index in range(combo.count()):
            if combo.itemData(index) == int(minutes):
                combo.setCurrentIndex(index)
                return

    def _on_radio_card_advanced(self, radio_id: int) -> None:
        self._on_station_command_summary_radio_clicked(int(radio_id or 0))
        self._toggle_adaptive_station_controls()

    def _operator_card_target(self, radio_id: int) -> dict | None:
        """Resolve only published identity; a missing card never targets a peer."""
        ident = int(radio_id or 0)
        if ident <= 0:
            return None
        for profile in getattr(self, "_station_command_profile_cache", ()):
            if int(profile.get("id") or 0) == ident:
                if (str(profile.get("device_class") or "").lower() == "observer"
                        or profile.get("runtime_active") is False or profile.get("runtime_active") == 0):
                    return None
                return dict(profile)
        return None

    def _save_operator_card_choices(self, radio_id: int, changes: Mapping[str, object]) -> None:
        if self._operator_card_target(radio_id) is None:
            return
        raw = self.settings.get(OPERATOR_CONTROLS_KEY, {})
        rows = dict(raw) if isinstance(raw, Mapping) else {}
        row = radio_choices({OPERATOR_CONTROLS_KEY: rows,
                             "sop_schedule_layer_enabled": self.settings.get("sop_schedule_layer_enabled", True)}, radio_id)
        row.update(changes)
        rows[str(radio_id)] = row
        # Invalidate before persistence: already queued work must not race the
        # operator's click. This does not invalidate any peer's authority stamp.
        self.scheduler._invalidate_command_authority(radio_id)
        self._dismiss_radio_off_schedule_prompt(radio_id)
        self.settings.set(OPERATOR_CONTROLS_KEY, rows)
        self.scheduler.operator_choices_changed(radio_id, choices=row, sources_changed=bool(
            set(changes).intersection({"sop_tuning_enabled", "routine_schedule_enabled"})))
        self._station_command_radio_summary_signature = None
        self._invalidate_station_command_lane_cache()
        self._refresh_station_command_bar(force=False)

    def _on_radio_card_sop(self, radio_id: int) -> None:
        if self._operator_card_target(radio_id) is None:
            return
        current = self.scheduler.radio_operator_choices(radio_id)
        self._save_operator_card_choices(radio_id, {"sop_tuning_enabled": current.get("sop_tuning_enabled") is False})

    def _on_radio_card_sources(self, radio_id: int) -> None:
        profile = self._operator_card_target(radio_id)
        if profile is None:
            return
        menu = QMenu(self)
        menu.addSection(f"Schedule sources · {profile.get('name') or radio_id}")
        review_action = menu.addAction('Review & enable scheduling…')
        def open_review():
            self.open_settings_section('operator_station_setup', radio_id=radio_id, settings_nav_context='main')
            self.settings_tab._operator_station_setup.set_step('review')
        review_action.triggered.connect(open_review)
        disable_action = menu.addAction('Disable scheduling')
        disable_action.triggered.connect(lambda: self.settings_tab._operator_station_disable_requested(radio_id))
        menu.addSeparator()

        current = self.scheduler.radio_operator_choices(radio_id)
        for key, label in (("routine_schedule_enabled", "Daily / Net schedule"), ("sop_tuning_enabled", "SOP tuning")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(current.get(key) is not False)
            action.triggered.connect(lambda checked, k=key, ident=radio_id: self._save_operator_card_choices(ident, {k: checked}))
        note = menu.addAction("SOP profiles and reminders remain active")
        note.setEnabled(False)
        card = self.radio_control_panel._cards.get(radio_id)
        if card is not None:
            menu.exec(card.sources_button.mapToGlobal(card.sources_button.rect().bottomLeft()))

    def _on_radio_card_policy(self, radio_id: int) -> None:
        profile = self._operator_card_target(radio_id)
        if profile is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Change policy · {profile.get('name') or radio_id}")
        layout = QVBoxLayout(dialog)
        note = QLabel("On Schedule Change applies the plan. Prompt reviews off-schedule differences at the chosen interval; it is not approval before every write. Net mode suspends corrections.", dialog)
        note.setWordWrap(True)
        layout.addWidget(note)
        grid = QGridLayout()
        layout.addLayout(grid)
        current = self.scheduler.radio_timer_policy(radio_id)
        combos = {}
        for row, (prefix, label) in enumerate((("freq", "Frequency"), ("fldigi", "FLDigi mode / offset"), ("js8", "JS8 offset"))):
            grid.addWidget(QLabel(label, dialog), row, 0)
            for column, (suffix, options, default) in enumerate((("enforcement_mode", POLICY_MODES, "On Schedule Change"), ("prompt_interval", POLICY_INTERVALS, "Hourly")), 1):
                key = prefix + "_" + suffix
                combo = QComboBox(dialog)
                combo.addItems(options)
                combo.setCurrentText(current.get(key, default))
                combo.setAccessibleName(label + (" policy" if column == 1 else " reminder interval"))
                grid.addWidget(combo, row, column)
                combos[key] = combo
        actions = QHBoxLayout()
        layout.addLayout(actions)
        defaults = QPushButton("Use profile defaults", dialog)
        actions.addWidget(defaults)
        def restore_profile_policy():
            self._save_operator_card_choices(radio_id, {"timer_policy": {}})
            dialog.reject()
        defaults.clicked.connect(restore_profile_policy)
        actions.addStretch()
        cancel = QPushButton("Cancel", dialog)
        save = QPushButton("Save policy", dialog)
        actions.addWidget(cancel)
        actions.addWidget(save)
        cancel.clicked.connect(dialog.reject)
        save.clicked.connect(dialog.accept)
        if dialog.exec() == QDialog.Accepted:
            self._save_operator_card_choices(radio_id, {"timer_policy": validated_timer_policy({k: combo.currentText() for k, combo in combos.items()})})

    def _open_net_tuning_dialog_for_radio(self, radio_id: int) -> None:
        """Open the cache-only NET FLDigi action for exactly one radio."""
        ident = int(radio_id or 0)
        profile = self._operator_card_target(ident)
        scheduler = getattr(self, "scheduler", None)
        if profile is None or scheduler is None:
            return
        show_net_tuning_dialog(
            self,
            scheduler,
            ident,
            radio_name=str(profile.get("name") or profile.get("label") or f"Radio {ident}"),
        )

    def _add_net_tuning_menu_action(self, menu: QMenu, radio_id: int, anchor: QWidget) -> QAction:
        """Add the configured label from the scheduler cache without widening the menu."""
        label = "Center + Narrow"
        getter = getattr(getattr(self, "scheduler", None), "net_tuning_snapshot", None)
        if callable(getter):
            try:
                snapshot = getter(int(radio_id or 0))
                if isinstance(snapshot, Mapping):
                    label = str(snapshot.get("label") or label).strip() or label
            except Exception:
                pass
        full_text = f"{label}…"
        font_metrics = anchor.fontMetrics()
        width = max(
            font_metrics.horizontalAdvance("Center + Narrow…"),
            font_metrics.horizontalAdvance("M") * 28,
        )
        text = font_metrics.elidedText(full_text, Qt.ElideRight, width)
        action = menu.addAction(text)
        action.setToolTip(full_text)
        action.triggered.connect(
            lambda _checked=False, ident=int(radio_id or 0): self._open_net_tuning_dialog_for_radio(ident)
        )
        return action

    def _on_radio_card_net(self, radio_id: int) -> None:
        profile = self._operator_card_target(radio_id)
        if profile is None:
            return
        sessions = getattr(self.scheduler, "_manual_net_sessions", {})
        token = sessions.get((radio_id, "OPERATOR"))
        ncs_active = any(key[0] == radio_id and key[1] in {"FLDIGI", "JS8"} for key in sessions)
        if ncs_active:
            menu = QMenu(self)
            menu.addSection(f"NCS session active · {profile.get('name') or radio_id}")
            note = menu.addAction("End the NCS session in its workspace to change net ownership")
            note.setEnabled(False)
            card = self.radio_control_panel._cards.get(radio_id)
            if card is not None:
                tune = self._add_net_tuning_menu_action(menu, radio_id, card.net_button)
                menu.exec(card.net_button.mapToGlobal(card.net_button.rect().bottomLeft()))
            return
        lane = self._station_command_lane_for_radio(radio_id) or {}
        self._dismiss_radio_off_schedule_prompt(radio_id)
        if token or str(lane.get("current_source") or "").upper() == "NET":
            menu = QMenu(self)
            menu.addSection(f"{'End operator net' if token else 'Scheduled net'} · {profile.get('name') or radio_id}")
            card = self.radio_control_panel._cards.get(radio_id)
            if card is None:
                return
            tune = self._add_net_tuning_menu_action(menu, radio_id, card.net_button)
            menu.addSeparator()
            resume = menu.addAction("Resume current schedule")
            paused = menu.addAction("Remain paused")
            chosen = menu.exec(card.net_button.mapToGlobal(card.net_button.rect().bottomLeft()))
            if chosen not in (resume, paused):
                return
            # Pause first so releasing ownership cannot dispatch in between.
            self._station_command_set_scheduler_suspended_manual(True, target_device_profile_id=radio_id)
            if token:
                self.scheduler.set_manual_net_active("OPERATOR", False, radio_id=radio_id, session_id=token)
            if chosen is resume:
                self._on_station_command_resume_clicked(radio_id)
        else:
            # A durable pause survives a process restart as a safe interrupted
            # net state. The live Net token grants operator freedom immediately.
            self._station_command_set_scheduler_suspended_manual(True, target_device_profile_id=radio_id)
            self.scheduler.set_manual_net_active("OPERATOR", True, radio_id=radio_id, session_id=f"control-bar:{radio_id}:{time.monotonic_ns()}")
        self._station_command_radio_summary_signature = None
        self._invalidate_station_command_lane_cache()
        self._refresh_station_command_bar(force=False)

    def _on_radio_card_health(self, radio_id: int) -> None:
        if getattr(self, "_radio_control_panel_dock_window", None) is not None:
            self._restore_radio_control_panel_from_dock()
        self._show_station_command_health_menu(device_profile_id=int(radio_id or 0))

    def _on_radio_card_plan(self, radio_id: int) -> None:
        ident = int(radio_id or 0)
        if ident <= 0:
            return
        plan = self._station_command_assigned_plan_for_radio(ident)
        plan_id = int(plan.get("id") or 0) if isinstance(plan, Mapping) else 0
        if getattr(self, "_radio_control_panel_dock_window", None) is not None:
            self._restore_radio_control_panel_from_dock()
        index = self._screen_index_by_label.get("FreqPlanner", -1)
        if index < 0:
            return
        self._set_screen(index)
        epoch = int(getattr(self, "_navigation_epoch", 0))
        def focus_plan() -> None:
            opener = getattr(getattr(self, "freq_planner_tab", None), "focus_frequency_plan", None)
            if callable(opener):
                opener(plan_id, device_profile_id=ident)
        QTimer.singleShot(0, lambda: self._run_if_screen_current(index, epoch, focus_plan))

    def _on_radio_card_launch(self, radio_id: int) -> None:
        ident = int(radio_id or 0)
        if ident <= 0:
            return
        if getattr(self, "_radio_control_panel_dock_window", None) is not None:
            self._restore_radio_control_panel_from_dock()
        self.open_settings_section(
            "launch_control",
            radio_id=ident,
            settings_nav_context="radios",
        )

    def _on_radio_card_js8_offset(self, radio_id: int) -> None:
        if getattr(self, "_radio_control_panel_dock_window", None) is not None:
            self._restore_radio_control_panel_from_dock()
        snapshot = self._station_command_current_snapshot(int(radio_id or 0))
        if snapshot is not None:
            self._show_station_js8_offset_editor(snapshot)

    def _on_radio_card_navigation(self, route: str) -> None:
        if getattr(self, "_radio_control_panel_dock_window", None) is not None:
            self._restore_radio_control_panel_from_dock()
        route_map = {"Ops Center": "ControlFreq", "Messages": "Messages", "BBS": "Managed BBS", "Schedules": "HF Schedule", "Settings": "Settings"}
        index = self._screen_index_by_label.get(route_map.get(str(route), str(route)))
        if index is not None:
            self._set_screen(index)

    def _on_radio_card_dock(self) -> None:
        """Dock request hook; the host owns geometry and tab restoration."""
        self._open_radio_control_dock()

    def _open_radio_control_dock(self) -> None:
        """Temporarily move the same card widgets into an ordinary Dock window."""
        panel = getattr(self, "radio_control_panel", None)
        if panel is None:
            return
        dock = getattr(self, "_radio_control_panel_dock_window", None)
        if dock is not None:
            try:
                dock.show()
                dock.raise_()
                dock.activateWindow()
                return
            except RuntimeError:
                self._radio_control_panel_dock_window = None
        try:
            self._radio_control_panel_main_geometry = self.saveGeometry()
        except Exception:
            self._radio_control_panel_main_geometry = None
        # Keep the Dock top-level so hiding the main workspace cannot hide the
        # operator's active control surface with it.
        dock = RadioControlDockWindow()
        dock.setObjectName("radioControlDockWindow")
        dock.setWindowTitle("Radio controls")
        dock.setMinimumSize(320, 260)
        dock.closed.connect(self._restore_radio_control_panel_from_dock)
        self._radio_control_panel_dock_window = dock
        panel.setParent(dock)
        dock.setCentralWidget(panel)
        panel.set_dock_mode(True)
        panel.show()
        dock.resize(panel.preferred_dock_width(), max(360, min(760, int(self.height() * 0.72 or 520))))
        dock_geometry = getattr(self, "_radio_control_panel_dock_geometry", None)
        if dock_geometry:
            dock.restoreGeometry(dock_geometry)
        shell = getattr(self, "_right_shell_container", None)
        if shell is not None:
            shell.setVisible(False)
        dock.show()
        dock.raise_()
        dock.activateWindow()
        # The dock owns the command surface while open, so the full workspace
        # disappears instead of leaving a blank shell behind it.
        self.hide()

    def _restore_radio_control_panel_from_dock(self) -> None:
        panel = getattr(self, "radio_control_panel", None)
        dock = getattr(self, "_radio_control_panel_dock_window", None)
        if panel is None:
            return
        if bool(getattr(self, "_shutting_down", False)):
            return
        if dock is not None:
            self._radio_control_panel_dock_geometry = dock.saveGeometry()
        if dock is not None and dock.centralWidget() is panel:
            dock.takeCentralWidget()
        summary_parent = getattr(self, "station_command_radio_summary_widget", None)
        if summary_parent is not None:
            panel.setParent(summary_parent)
            layout = getattr(self, "station_command_radio_summary_layout", None)
            if layout is not None and layout.indexOf(panel) < 0:
                layout.addWidget(panel)
        panel.set_dock_mode(False)
        panel.show()
        shell = getattr(self, "_right_shell_container", None)
        if shell is not None:
            shell.setVisible(True)
        self.show()
        if dock is not None:
            try:
                dock.blockSignals(True)
                dock.hide()
                dock.deleteLater()
            except RuntimeError:
                pass
        self._radio_control_panel_dock_window = None
        geometry = getattr(self, "_radio_control_panel_main_geometry", None)
        if geometry:
            try:
                self.restoreGeometry(geometry)
            except Exception:
                pass
        QTimer.singleShot(0, self._schedule_station_command_summary_geometry)

    def _refresh_station_command_radio_summary(self, choices: list[object], selected_id: int) -> None:
        layout = getattr(self, "station_command_radio_summary_layout", None)
        if layout is None:
            return
        visible_choices = list(choices)
        if bool(getattr(self, "_adaptive_station_shell_enabled", False)):
            self._refresh_adaptive_station_command_shell(visible_choices, selected_id)
            return
        if not visible_choices:
            self._station_command_radio_tile_controls = {}
            signature = ("empty",)
            if signature == getattr(self, "_station_command_radio_summary_signature", None):
                return
            self._station_command_radio_summary_signature = signature
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()
            empty = QLabel("No configured radios", getattr(self, "station_command_radio_summary_widget", None))
            empty.setObjectName("stationCommandRadioSummaryEmpty")
            empty.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            layout.addWidget(empty)
            layout.addStretch(1)
            return
        page_choices = visible_choices
        card_choices = self._station_command_card_choices_for_layout(page_choices, selected_id)
        mesh_chips = self._station_command_mesh_source_chips()
        mesh_items = self._station_command_saved_mesh_control_items()
        signature = (
            "tiles",
            int(selected_id or 0),
            len(visible_choices),
            tuple(self._station_command_snapshot_id(snapshot) for snapshot in card_choices),
            self._station_command_radio_card_width(len(card_choices)),
            self._station_command_source_control_items_signature(mesh_items),
            int(getattr(getattr(getattr(self, "station_command_radio_summary_scroll", None), "viewport", lambda: None)(), "width", lambda: 0)() or 0),
            tuple(
                (
                    self._station_command_snapshot_id(snapshot),
                    self._station_command_snapshot_name(snapshot),
                    self._station_command_now_text_for_summary(snapshot, selected_id),
                    self._station_command_compact_state_text(self._station_command_state_text(snapshot)),
                    self._station_command_next_text(snapshot),
                    self._station_command_plan_name_for_snapshot(snapshot),
                    tuple(
                        sorted(
                            (
                                str(meta.get("group") or "").strip().upper(),
                                str(meta.get("band") or "").strip().upper(),
                                f"{float(meta.get('freq') or 0.0):.6f}",
                            )
                            for meta in self._station_command_plan_qsy_options(snapshot).values()
                        )
                    ),
                    str(self._station_command_health_summary_for_profile(snapshot).get("state", "")),
                    self._station_command_scheduler_manual_qsy_active_for_radio(
                        self._station_command_snapshot_id(snapshot)
                    ),
                    self._station_command_scheduler_suspended_manually_for_radio(
                        self._station_command_snapshot_id(snapshot)
                    ),
                    self._station_command_timed_suspend_active_for_radio(
                        self._station_command_snapshot_id(snapshot)
                    ),
                )
                for snapshot in page_choices
            ),
        )
        if signature == getattr(self, "_station_command_radio_summary_signature", None):
            return
        self._station_command_radio_summary_signature = signature
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._station_command_radio_tile_controls = {}
        self._refresh_station_command_radio_tiles(page_choices, selected_id, mesh_chips=mesh_chips)

    def _sync_station_command_radio_summary_height(self, *, card_mode: bool) -> None:
        scroll = getattr(self, "station_command_radio_summary_scroll", None)
        if scroll is None:
            return
        if not card_mode:
            scroll.setFixedHeight(control_height_for_font(scroll, vertical_padding=18, floor=42))
            return
        parent = getattr(self, "station_command_radio_summary_widget", None)
        if bool(getattr(self, "_adaptive_station_shell_enabled", False)) and parent is not None:
            try:
                parent_layout = parent.layout()
                content_height = int(
                    (parent_layout.sizeHint().height() if parent_layout is not None else parent.sizeHint().height()) or 0
                )
            except Exception:
                content_height = 0
            # Measure the finished layout, including native font/control floors.
            # Keep the bounded selected-radio context and its actions visible.
            # The advanced utility row owns any local overflow.
            row_height = control_height_for_font(scroll, vertical_padding=10, floor=34)
            frame_extra = 2 * scroll.frameWidth()
            try:
                available_height = self.centralWidget().height()
            except (AttributeError, RuntimeError):
                available_height = self.height()
            desired = max(row_height * 2, content_height + frame_extra + 8)
            panel_floor = 0
            # The retained card panel owns the card viewport. In the full
            # shell the outer summary scrollbar is disabled, and its height
            # must expose one complete card before the remaining cards
            # continue below the panel viewport. Derive the floor from the
            # actual retained card and named panel chrome so this stays
            # font/theme aware without constructing a runtime worker.
            panel = getattr(self, "radio_control_panel", None)
            if panel is not None and not bool(panel.property("dockMode")):
                parent.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                try:
                    panel.cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
                except (AttributeError, RuntimeError):
                    pass
                cards = getattr(panel, "cards", {})
                first_card = next((card for card in cards.values() if not card.isHidden()), None) if cards else None
                card_height = max(int(first_card.sizeHint().height() or 0), first_card.minimumSizeHint().height(),
                    first_card.heightForWidth(first_card.width()) if first_card.hasHeightForWidth() else 0) if first_card is not None else 0
                panel_layout = panel.layout()
                # Subtract the viewport's hint from the finished layout so
                # hidden rows contribute neither height nor phantom spacing.
                viewport_item = panel_layout.itemAt(panel_layout.indexOf(panel.cards_scroll))
                viewport_hint_height = viewport_item.sizeHint().height() if viewport_item is not None else 0
                chrome_height = max(0, panel_layout.sizeHint().height() - viewport_hint_height)
                margins = parent.layout().contentsMargins()
                panel_floor = max(row_height * 2, chrome_height + card_height
                    + margins.top() + margins.bottom() + frame_extra + 2)
                desired = panel_floor + max(4, panel.fontMetrics().lineSpacing() // 2)
            critical = parent.findChild(QFrame, "stationCommandPrimaryContext")
            # The bounded production shell keeps critical context above its
            # local utility scroller. Other/oversized content still obeys the
            # window budget rather than forcing a laptop window to grow.
            budget = max(row_height * 2, int(max(0, available_height) * 0.45))
            if panel_floor and not bool(panel.property("dockMode")):
                budget = max(budget, desired)
                # Optional utility/navigation rows may reduce the card viewport,
                # but must not increase the native window's minimum height.
                # Reserve useful task workspace and account for real shell
                # banners/margins instead of applying a screen-percentage guess.
                shell = getattr(self, "_right_shell_container", None) or self.centralWidget()
                shell_layout = shell.layout() if shell is not None else None
                bar = getattr(self, "station_command_bar", None)
                workspace = getattr(self, "stack", None) or getattr(self, "tab_workspace", None)
                if shell_layout is not None and bar is not None and workspace is not None:
                    shell_margins = shell_layout.contentsMargins()
                    shell_chrome = shell_margins.top() + shell_margins.bottom()
                    visible_items = 0
                    for index in range(shell_layout.count()):
                        item = shell_layout.itemAt(index)
                        widget = item.widget()
                        if widget is not None and widget.isHidden():
                            continue
                        visible_items += 1
                        if widget is not bar and widget is not workspace:
                            shell_chrome += max(0, item.sizeHint().height())
                    shell_chrome += max(0, visible_items - 1) * max(0, shell_layout.spacing())
                    bar_layout = bar.layout()
                    bar_item = bar_layout.itemAt(bar_layout.indexOf(scroll)) if bar_layout is not None else None
                    bar_chrome = max(0, bar_layout.sizeHint().height() - bar_item.sizeHint().height()) if bar_item is not None else 0
                    task_height = max(80, panel.fontMetrics().lineSpacing() * 4)
                    available_summary = max(row_height * 2, shell.height() - shell_chrome - bar_chrome - task_height)
                    budget = min(budget, available_summary)
            height = desired if critical is not None else min(desired, budget)
            panel_owns_scroll = bool(panel_floor and panel is not None and not bool(panel.property("dockMode")))
            scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarAlwaysOff if critical is not None or panel_owns_scroll else Qt.ScrollBarAsNeeded
            )
            if scroll.height() != height:
                scroll.setFixedHeight(height)
            return
        tiles = parent.findChildren(QFrame, "stationCommandRadioTile") if parent is not None else []
        tile_height = max((int(tile.sizeHint().height()) for tile in tiles), default=0)
        rail = parent.findChild(QFrame, "stationCommandSourceRail") if parent is not None else None
        rail_height = int(rail.sizeHint().height() or 0) if rail is not None else 0
        horizontal_extra = 0
        try:
            viewport_width = int(scroll.viewport().width() or 0)
            content_width = int(parent.minimumWidth() if parent is not None else 0)
            if viewport_width > 0 and content_width > viewport_width:
                horizontal_extra = int(scroll.horizontalScrollBar().sizeHint().height() or 14)
        except Exception:
            horizontal_extra = 0
        height = max(132, min(280, tile_height + rail_height + horizontal_extra + 14))
        scroll.setFixedHeight(height)

    def _station_command_set_qsy_combo_to_meta(self, meta: Mapping[str, object] | None) -> None:
        combo = getattr(self, "station_command_freq_combo", None)
        if not isinstance(combo, QComboBox) or not isinstance(meta, Mapping):
            return
        target_key = self._station_command_qsy_option_key(meta)
        if not target_key:
            return
        for index in range(combo.count()):
            data = combo.itemData(index)
            try:
                if isinstance(data, Mapping) and self._station_command_qsy_option_key(data) == target_key:
                    combo.setCurrentIndex(index)
                    return
            except Exception:
                continue

    @staticmethod
    def _station_command_combo_selected_key(combo: QComboBox) -> str:
        try:
            meta = selected_qsy_meta(combo)
            if isinstance(meta, Mapping):
                return MainWindow._station_command_qsy_option_key(meta)
        except Exception:
            pass
        return ""

    def _station_command_for_radio_qsy(
        self,
        device_profile_id: int,
        meta: Mapping[str, object] | QComboBox | None,
        callback: Callable[..., None],
        duration_combo: QComboBox | None = None,
    ) -> Callable[..., None]:
        def run(*_args: object) -> None:
            current_meta: Mapping[str, object] | None
            if isinstance(meta, QComboBox):
                selected_meta = selected_qsy_meta(meta)
                current_meta = selected_meta if isinstance(selected_meta, Mapping) else None
            else:
                current_meta = meta
            ident = int(device_profile_id or 0)
            direct_meta = dict(current_meta) if isinstance(current_meta, Mapping) else None
            if direct_meta is not None and ident > 0:
                direct_meta["target_device_profile_id"] = ident
            duration_minutes = None
            if isinstance(duration_combo, QComboBox) and isinstance(getattr(self, "station_command_duration_combo", None), QComboBox):
                try:
                    value = duration_combo.currentData()
                    duration_minutes = int(value)
                    for index in range(self.station_command_duration_combo.count()):
                        if self.station_command_duration_combo.itemData(index) == value:
                            self.station_command_duration_combo.setCurrentIndex(index)
                            break
                except Exception:
                    pass
            if duration_combo is not None:
                callback(direct_meta, duration_minutes)
            else:
                callback(direct_meta)

        return run

    def _station_command_populate_card_frequency_combo(self, combo: QComboBox, snapshot: object | None) -> bool:
        meta_map = self._station_command_plan_qsy_options(snapshot)
        preferred_key = self._station_command_preferred_qsy_key(snapshot)
        has_options = False
        previous_block = combo.blockSignals(True)
        try:
            combo.clear()
            for key, meta in sorted(
                meta_map.items(),
                key=lambda item: (
                    str(item[1].get("group") or "").upper(),
                    str(item[1].get("band") or "").upper(),
                    float(item[1].get("freq") or 0.0),
                ),
            ):
                combo.addItem(self._station_command_qsy_label(meta), meta)
                combo.setItemData(combo.count() - 1, self._station_command_qsy_tooltip(meta), Qt.ToolTipRole)
                has_options = True
            if has_options:
                combo.setCurrentIndex(-1)
            if not has_options:
                plan_name = ""
                if snapshot is not None:
                    plan_name = self._station_command_assigned_plan_name_for_radio(self._station_command_snapshot_id(snapshot))
                combo.addItem("No alternate QSY targets" if plan_name else "No assigned plan", None)
            elif preferred_key:
                for index in range(combo.count()):
                    data = combo.itemData(index)
                    try:
                        if isinstance(data, Mapping) and self._station_command_qsy_option_key(data) == preferred_key:
                            combo.setCurrentIndex(index)
                            break
                    except Exception:
                        continue
            preferred_is_catalogued = False
            if preferred_key:
                try:
                    preferred_is_catalogued = any(
                        self._station_command_qsy_option_key(row) == preferred_key
                        for row in self._station_command_operating_group_qsy_rows()
                    )
                except Exception:
                    preferred_is_catalogued = False
            if selected_qsy_meta(combo) is None:
                if len(meta_map) == 1 and (not preferred_key or preferred_is_catalogued):
                    combo.setCurrentIndex(0)
                else:
                    combo.setCurrentIndex(-1)
        finally:
            combo.blockSignals(previous_block)
        return has_options and selected_qsy_meta(combo) is not None

    def _station_command_operating_group_qsy_lookup(self) -> dict[tuple[str, str], dict[str, object]]:
        cached = getattr(self, "_station_command_group_qsy_lookup_cache", None)
        if cached is not None and cached[0] is self.settings:
            return cached[1]
        lookup: dict[tuple[str, str], dict[str, object]] = {}
        try:
            buckets: dict[tuple[str, str], list[dict[str, object]]] = {}
            for configured in self._station_command_operating_group_qsy_rows():
                meta = dict(configured)
                meta["freq"] = parse_frequency_mhz(meta.get("frequency"))
                if meta["freq"] is None:
                    continue
                group = self._station_command_group_display_name(meta.get("group"))
                band = str(meta.get("band") or "").strip().upper()
                if group and band:
                    buckets.setdefault((group.upper(), band), []).append(meta)
            for key, rows in buckets.items():
                if len(rows) == 1:
                    lookup[key] = dict(rows[0])
        except Exception:
            return lookup
        self._station_command_group_qsy_lookup_cache = (self.settings, lookup)
        return lookup

    def _station_command_operating_group_qsy_rows(self) -> list[dict[str, object]]:
        cached = getattr(self, "_station_command_group_qsy_rows_cache", None)
        if cached is not None and cached[0] is self.settings:
            return [dict(row) for row in cached[1]]
        try:
            rows = ensure_operating_group_frequency_keys(load_operating_groups(self.settings))
        except Exception:
            rows = []
        self._station_command_group_qsy_rows_cache = (self.settings, rows)
        return [dict(row) for row in rows]

    def _station_command_plan_qsy_options(self, snapshot: object | None) -> dict[str, dict[str, object]]:
        ident = self._station_command_snapshot_id(snapshot) if snapshot is not None else 0
        lookup = self._station_command_operating_group_qsy_lookup()
        configured = self._station_command_operating_group_qsy_rows()
        options: dict[str, dict[str, object]] = {}

        def add_group_band(ref: Mapping[str, object]) -> None:
            group = self._station_command_group_display_name(ref.get("group_name") or ref.get("group"))
            band = str(ref.get("band") or ref.get("band_name") or "").strip().upper()
            if not group or not band:
                return
            family = resolve_operating_group_frequency(configured, group=group)
            explicit_key = bool(
                str(ref.get("operating_group_frequency_key") or "").strip()
                or str(ref.get("operating_group_key") or "").strip()
            )
            if not family.candidates and not explicit_key:
                # Explicit legacy plan refs remain usable for groups that are
                # absent from today's catalog; there is no variant to guess.
                meta = self._station_command_qsy_meta_from_plan_ref(ref, lookup)
                if meta:
                    option_key = self._station_command_qsy_option_key(meta)
                    if option_key:
                        options.setdefault(option_key, meta)
                return
            resolution = resolve_operating_group_frequency(
                configured,
                group=group,
                frequency_key=ref.get("operating_group_frequency_key"),
                band=band,
                mode=ref.get("mode"),
                frequency=ref.get("frequency") or ref.get("freq") or ref.get("frequency_mhz"),
            )
            # A stale explicit schedule reference must remain unresolved.
            explicit_reference = bool(
                str(ref.get("operating_group_frequency_key") or "").strip()
                or ref.get("frequency") or ref.get("freq") or ref.get("frequency_mhz")
            )
            if explicit_reference and not resolution.resolved:
                return
            for row in configured:
                if str(row.get("group") or "").strip().upper() != group.upper():
                    continue
                if str(row.get("band") or "").strip().upper() != band:
                    continue
                meta = self._station_command_qsy_meta_from_plan_ref(row, lookup)
                if meta:
                    option_key = self._station_command_qsy_option_key(meta)
                    if option_key:
                        options.setdefault(option_key, meta)

        if ident > 0:
            for item in self._station_command_assigned_plan_refs_for_radio(ident):
                ref = self._station_command_ref_mapping(item)
                if ref:
                    add_group_band(ref)
            if options:
                return self._station_command_alternate_qsy_options(options, snapshot)
        lane_rows = self._station_command_lane_schedule_rows(ident) if ident > 0 else []
        for item in lane_rows:
            ref = self._station_command_ref_mapping(item)
            if ref:
                add_group_band(ref)
        if not options:
            scheduler = getattr(self, "scheduler", None)
            entry = getattr(scheduler, "current_schedule_entry", {}) if scheduler is not None else {}
            if isinstance(entry, Mapping):
                entry_radio_id = self._station_command_scheduler_entry_radio_id(entry)
                if ident <= 0 or entry_radio_id <= 0 or entry_radio_id == ident:
                    seed = self._station_command_qsy_meta_from_plan_ref(entry, lookup)
                    if seed:
                        seed_group = str(seed.get("group") or "").strip().upper()
                        for row in configured:
                            if str(row.get("group") or "").strip().upper() != seed_group:
                                continue
                            meta = self._station_command_qsy_meta_from_plan_ref(row, lookup)
                            if meta:
                                options[self._station_command_qsy_option_key(meta)] = meta
        return self._station_command_alternate_qsy_options(options, snapshot)

    def _station_command_qsy_meta_from_plan_ref(
        self,
        ref: object,
        lookup: Mapping[tuple[str, str], Mapping[str, object]],
    ) -> dict[str, object] | None:
        if isinstance(ref, Mapping):
            group = self._station_command_group_display_name(ref.get("group_name") or ref.get("group") or ref.get("label"))
            band = str(ref.get("band") or ref.get("band_name") or "").strip().upper()
            freq_value = (
                ref.get("frequency")
                or ref.get("freq")
                or ref.get("frequency_mhz")
                or ref.get("name")
            )
            mode_value = ref.get("mode")
            vfo_value = ref.get("vfo")
            fldigi_mode_value = ref.get("fldigi_mode")
            fldigi_offset_value = ref.get("fldigi_offset")
            auto_tune_value = ref.get("auto_tune")
        else:
            text = str(ref or "").strip()
            group = ""
            band = ""
            freq_value = text
            mode_value = ""
            vfo_value = "A"
            fldigi_mode_value = ""
            fldigi_offset_value = ""
            auto_tune_value = None
            band_match = re.search(r"\b(160|80|60|40|30|20|17|15|12|10|6|2)\s*M\b", text, flags=re.IGNORECASE)
            if band_match:
                band = f"{band_match.group(1).upper()}M"
            group_match = re.search(r"\b([A-Z][A-Z0-9_-]{2,})\b", text.upper())
            if group_match:
                candidate = group_match.group(1)
                if candidate != band:
                    group = self._station_command_group_display_name(candidate)
        freq = parse_frequency_mhz(freq_value)
        if not band and freq is not None:
            band = self._station_command_band_from_frequency(freq)
        if not group or not band or freq is None:
            return None
        rows = self._station_command_operating_group_qsy_rows()
        resolution = resolve_operating_group_frequency(
            rows,
            group=group,
            frequency_key=(ref.get("operating_group_frequency_key") if isinstance(ref, Mapping) else ""),
            band=band,
            mode=mode_value,
            frequency=freq,
        )
        if not resolution.resolved or resolution.row is None:
            family = resolve_operating_group_frequency(rows, group=group)
            explicit_key = bool(
                isinstance(ref, Mapping)
                and (ref.get("operating_group_frequency_key") or ref.get("operating_group_key"))
            )
            if family.candidates or explicit_key or not isinstance(ref, Mapping):
                return None
            return {
                "group": group,
                "band": band,
                "freq": freq,
                "frequency": format_frequency_mhz(freq_value),
                "mode": str(mode_value or "").strip(),
                "vfo": str(vfo_value or "A").strip().upper() or "A",
                "fldigi_mode": str(fldigi_mode_value or "").strip(),
                "fldigi_offset": str(fldigi_offset_value or "").strip(),
                "auto_tune": bool(auto_tune_value) if auto_tune_value is not None else False,
            }
        resolved = dict(resolution.row)
        return {
            "group": self._station_command_group_display_name(resolved.get("group")),
            "band": str(resolved.get("band") or "").strip().upper(),
            "freq": parse_frequency_mhz(resolved.get("frequency")),
            "frequency": str(resolved.get("frequency") or ""),
            "operating_group_key": str(resolved.get("operating_group_key") or ""),
            "operating_group_frequency_key": str(resolved.get("operating_group_frequency_key") or ""),
            "mode": str(mode_value or resolved.get("mode") or "").strip(),
            "vfo": str(resolved.get("vfo") or vfo_value or "A").strip().upper() or "A",
            "fldigi_mode": str(resolved.get("fldigi_mode") or fldigi_mode_value or "").strip(),
            "fldigi_offset": str(resolved.get("fldigi_offset") or fldigi_offset_value or "").strip(),
            "auto_tune": bool(resolved.get("auto_tune", False) if auto_tune_value is None else auto_tune_value),
        }

    @staticmethod
    def _station_command_band_from_frequency(freq_mhz: object) -> str:
        try:
            value = float(freq_mhz)
        except Exception:
            return ""
        bands = (
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
        )
        for band, lo, hi in bands:
            if lo <= value <= hi:
                return band
        return ""

    def _refresh_station_command_radio_tiles(
        self,
        choices: list[object],
        selected_id: int,
        *,
        mesh_chips: Sequence[Mapping[str, object]] | None = None,
        show_source_rail: bool = True,
    ) -> None:
        layout = getattr(self, "station_command_radio_summary_layout", None)
        parent = getattr(self, "station_command_radio_summary_widget", None)
        if layout is None or parent is None:
            return
        tile_controls: dict[int, dict[str, object]] = {}
        try:
            theme = resolve_theme(self.settings)
        except Exception:
            theme = {}
        pending_qsy_keys = getattr(self, "_station_command_card_qsy_pending_keys", None)
        if not isinstance(pending_qsy_keys, dict):
            pending_qsy_keys = {}
            self._station_command_card_qsy_pending_keys = pending_qsy_keys
        card_choices = self._station_command_card_choices_for_layout(choices, selected_id)
        card_width = self._station_command_radio_card_width(len(card_choices))
        rail_min_width = 0
        if show_source_rail:
            rail_min_width = self._add_station_command_source_rail(
                layout,
                parent,
                choices,
                selected_id,
                theme,
                mesh_chips=mesh_chips,
            )
        try:
            spacing = max(0, int(layout.spacing()))
            card_row_width = card_width * max(1, len(card_choices)) + spacing * max(0, len(card_choices) - 1)
            row_min_width = max(card_row_width, rail_min_width + spacing)
            parent.setMinimumWidth(row_min_width)
            parent.setMaximumWidth(16777215)
        except Exception:
            pass
        card_row = QFrame(parent)
        card_row.setObjectName("stationCommandRadioCardRow")
        card_row.setFrameShape(QFrame.NoFrame)
        card_row_layout = QHBoxLayout(card_row)
        card_row_layout.setContentsMargins(0, 0, 0, 0)
        card_row_layout.setSpacing(6)
        card_row.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        for snapshot in card_choices:
            ident = self._station_command_snapshot_id(snapshot)
            selected = ident > 0 and ident == int(selected_id or 0)
            state = self._station_command_compact_state_text(self._station_command_state_text(snapshot))
            manual_qsy_active = self._station_command_scheduler_manual_qsy_active_for_radio(ident)
            radio_hold_snapshot = self._station_command_hold_snapshot_for_radio(ident)
            radio_hold_active = bool(radio_hold_snapshot.get("active"))
            timed_qsy_active = bool(radio_hold_active and manual_qsy_active)
            timed_suspend_active = self._station_command_timed_suspend_active_for_radio(ident)
            scheduler_suspended_manual = self._station_command_scheduler_suspended_manually_for_radio(ident)
            now = self._station_command_now_text_for_summary(snapshot, selected_id)
            next_text = self._station_command_next_text(snapshot)
            plan_text = self._station_command_display_plan_name(self._station_command_plan_name_for_snapshot(snapshot))
            frequency_control_available = self._station_command_frequency_controls_available(snapshot)
            health_summary = self._station_command_health_summary_for_profile(snapshot)
            health_state = str(health_summary.get("state", "warn") or "warn").strip().lower()
            tile = QFrame(card_row)
            tile.setObjectName("stationCommandRadioTile")
            tile.setProperty("selected", "true" if selected else "false")
            tile.setFrameShape(QFrame.StyledPanel)
            tile.setMinimumWidth(card_width)
            tile.setMaximumWidth(card_width)
            tile.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            tile_layout = QGridLayout(tile)
            tile_layout.setContentsMargins(8, 7, 8, 7)
            tile_layout.setHorizontalSpacing(8)
            tile_layout.setVerticalSpacing(5)
            compact_card = card_width < 360

            name_btn = QPushButton(self._station_command_snapshot_name(snapshot), tile)
            name_btn.setObjectName("stationCommandRadioTileName")
            name_btn.setCheckable(True)
            name_btn.setChecked(selected)
            name_btn.setToolTip(self._station_command_radio_summary_tooltip(snapshot, selected_id))
            name_btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            self._style_station_command_radio_summary_button(name_btn, selected=selected, state_text=state)
            if ident > 0:
                name_btn.clicked.connect(lambda _checked=False, profile_id=ident: self._on_station_command_summary_radio_clicked(profile_id))

            freq_combo = QComboBox(tile)
            freq_combo.setObjectName("stationCommandRadioTileFrequency")
            freq_combo.setToolTip(
                "Select the operating group and band for this radio."
                if frequency_control_available
                else "VarAC-only radio: VarAC handles frequency scheduling; CommsPlan monitors messages and BBS."
            )
            freq_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            freq_combo.setEnabled(frequency_control_available)
            has_card_qsy_target = self._station_command_populate_card_frequency_combo(freq_combo, snapshot)
            preferred_key_for_card = self._station_command_preferred_qsy_key(snapshot)
            pending_key = str(pending_qsy_keys.get(ident, "") or "")
            if pending_key:
                for index in range(freq_combo.count()):
                    data = freq_combo.itemData(index)
                    try:
                        if isinstance(data, Mapping) and f"{float(data.get('freq')):.6f}" == pending_key:
                            freq_combo.setCurrentIndex(index)
                            break
                    except Exception:
                        continue
            freq_combo.setProperty(
                "stationCommandSelectionDirty",
                bool(pending_key and pending_key != preferred_key_for_card),
            )
            freq_combo.setProperty("stationCommandPreferredKey", preferred_key_for_card)
            now_font = freq_combo.font()
            now_font.setBold(True)
            now_font.setPointSize(max(now_font.pointSize(), 13))
            freq_combo.setFont(now_font)

            uses_js8call = self._station_command_bool(
                self._station_command_value(snapshot, "use_js8call", False)
            )
            js8_offset_widget = QWidget(tile)
            js8_offset_widget.setObjectName("stationCommandRadioTileJs8Offset")
            js8_offset_layout = QHBoxLayout(js8_offset_widget)
            js8_offset_layout.setContentsMargins(0, 0, 0, 0)
            js8_offset_layout.setSpacing(6)
            js8_offset_label = QLabel("JS8 offset", js8_offset_widget)
            js8_offset_spin = QSpinBox(js8_offset_widget)
            js8_offset_spin.setObjectName("stationCommandRadioTileJs8OffsetValue")
            js8_offset_spin.setRange(1800, 2700)
            js8_offset_spin.setSingleStep(50)
            js8_offset_spin.setSuffix(" Hz")
            try:
                persisted_offset = int(
                    self._station_command_value(snapshot, "js8_offset_hz", 2050) or 2050
                )
            except Exception:
                persisted_offset = 2050
            js8_offset_spin.setValue(max(1800, min(2700, persisted_offset)))
            js8_offset_spin.setAccessibleName(
                f"{self._station_command_snapshot_name(snapshot)} JS8 default offset"
            )
            js8_offset_spin.setToolTip(
                "Default JS8Call audio offset for this radio. CommsPlan holds live changes while TX or JS8 activity is present."
            )
            js8_offset_save = QPushButton("Save Default", js8_offset_widget)
            js8_offset_save.setObjectName("stationCommandRadioTileJs8OffsetSave")
            js8_offset_save.setToolTip(
                "Save this radio's default JS8 offset. Application is deferred until radio safety checks allow it."
            )
            try:
                expected_js8_instance_id = int(
                    self._station_command_value(snapshot, "js8_instance_id", 0) or 0
                ) or None
            except Exception:
                expected_js8_instance_id = None
            js8_offset_save.clicked.connect(
                lambda _checked=False,
                radio_id=ident,
                spin=js8_offset_spin,
                instance_id=expected_js8_instance_id: self._save_station_command_js8_offset(
                    radio_id,
                    spin.value(),
                    expected_instance_id=instance_id,
                )
            )
            js8_offset_layout.addWidget(js8_offset_label)
            js8_offset_layout.addWidget(js8_offset_spin)
            js8_offset_layout.addWidget(js8_offset_save)
            js8_offset_layout.addStretch(1)
            js8_offset_widget.setVisible(bool(uses_js8call and ident > 0))
            next_label = ElidedLabel(f"Next: {next_text} | Plan: {plan_text}", tile)
            next_label.setObjectName("stationCommandRadioTileNext")
            next_label.setMinimumWidth(0)
            next_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            next_label.setToolTip(f"Next: {next_text}\nPlan: {plan_text}\nClick Change Plan to manage assignment.")

            qsy_btn = QPushButton("QSY", tile)
            qsy_btn.setMinimumWidth(64 if compact_card else 76)
            qsy_btn.setMaximumWidth(88 if compact_card else 96)
            qsy_btn.setToolTip(
                f"Select {self._station_command_snapshot_name(snapshot)} and send the selected manual QSY target."
                if frequency_control_available
                else "Frequency control is not offered for VarAC-only radios."
            )
            qsy_btn.clicked.connect(
                self._station_command_for_radio_qsy(
                    ident,
                    freq_combo,
                    self._on_station_command_qsy_now_clicked,
                )
            )
            duration_combo = QComboBox(tile)
            duration_combo.setObjectName("stationCommandRadioTileDuration")
            refresh_hold_duration_combo(duration_combo, self.settings, getattr(self, "_active_runtime_profile", None))
            duration_combo.setVisible(False)
            timer_btn = QToolButton(tile)
            timer_btn.setObjectName("stationCommandRadioTileTimedSuspend")
            timer_btn.setText("Extend" if compact_card and timed_qsy_active else "Hold" if compact_card else timed_qsy_text(timed_qsy_active=timed_qsy_active))
            timer_btn.setPopupMode(QToolButton.MenuButtonPopup)
            timer_btn.setMinimumWidth(82 if compact_card else 128)
            timer_btn.setMaximumWidth(108 if compact_card else 150)
            timer_btn.setToolTip(
                f"Select {self._station_command_snapshot_name(snapshot)} and QSY with a timed scheduler suspend."
                if frequency_control_available
                else "Timed QSY is not offered for VarAC-only radios."
            )
            timer_btn.clicked.connect(
                self._station_command_for_radio_qsy(
                    ident,
                    freq_combo,
                    self._on_station_command_qsy_hold_clicked,
                    duration_combo,
                )
            )
            duration_menu = QMenu(timer_btn)
            duration_menu.setObjectName("stationCommandTimedSuspendMenu")
            manual_qsy_action = QAction("Indefinite", duration_menu)
            manual_qsy_action.setToolTip("QSY and keep scheduler control suspended until Resume.")
            manual_qsy_action.triggered.connect(
                lambda _checked=False,
                radio_id=ident,
                combo=freq_combo: self._station_command_for_radio_qsy(
                    radio_id,
                    combo,
                    self._on_station_command_qsy_now_clicked,
                )()
            )
            duration_menu.addAction(manual_qsy_action)
            duration_menu.addSeparator()
            for duration_index in range(duration_combo.count()):
                label = duration_combo.itemText(duration_index)
                value = duration_combo.itemData(duration_index)
                action = QAction(label, duration_menu)
                action.triggered.connect(
                    lambda _checked=False,
                    combo=duration_combo,
                    duration_value=value,
                    button=timer_btn: (
                        combo.setCurrentIndex(
                            next(
                                (idx for idx in range(combo.count()) if combo.itemData(idx) == duration_value),
                                combo.currentIndex(),
                            )
                        ),
                        button.click(),
                    )
                )
                duration_menu.addAction(action)
            timer_btn.setMenu(duration_menu)
            suspend_btn = QToolButton(tile)
            suspend_btn.setObjectName("stationCommandRadioTileSchedulerSuspend")
            scheduler_actions = scheduler_action_state(
                manual_qsy_active=manual_qsy_active,
                timed_qsy_active=timed_qsy_active,
                timed_suspend_active=timed_suspend_active,
                scheduler_suspended_manual=scheduler_suspended_manual,
                scheduler_state_text=state,
            )
            if compact_card:
                suspend_text = scheduler_actions.timed_suspend_text
                if suspend_text == "Indefinite Suspend":
                    suspend_text = "Indef."
                elif suspend_text == "Extend Suspend":
                    suspend_text = "Extend"
                elif suspend_text == "Timed Suspend":
                    suspend_text = "Suspend"
                suspend_btn.setText(suspend_text)
            else:
                suspend_btn.setText(scheduler_actions.timed_suspend_text)
            suspend_btn.setPopupMode(QToolButton.MenuButtonPopup)
            suspend_btn.setMinimumWidth(92 if compact_card else 128)
            suspend_btn.setMaximumWidth(118 if compact_card else 150)
            suspend_btn.setToolTip(
                f"Suspend scheduler control for {self._station_command_snapshot_name(snapshot)}."
                if frequency_control_available
                else "CommsPlan scheduler suspend is not offered for VarAC-only radios."
            )
            suspend_btn.setEnabled(ident > 0 and frequency_control_available)
            suspend_btn.clicked.connect(
                lambda _checked=False, radio_id=ident: self._on_station_command_timed_suspend_clicked(radio_id)
            )
            suspend_menu = QMenu(suspend_btn)
            suspend_menu.setObjectName("stationCommandSchedulerSuspendMenu")
            manual_suspend_action = QAction("Indefinite", suspend_menu)
            manual_suspend_action.setToolTip("Suspend scheduler control until Resume.")
            manual_suspend_action.triggered.connect(
                lambda _checked=False, radio_id=ident: self._on_station_command_pause_clicked(radio_id)
            )
            suspend_menu.addAction(manual_suspend_action)
            suspend_menu.addSeparator()
            for duration_index in range(duration_combo.count()):
                label = duration_combo.itemText(duration_index)
                value = duration_combo.itemData(duration_index)
                action = QAction(label, suspend_menu)
                action.triggered.connect(
                    lambda _checked=False,
                    combo=duration_combo,
                    duration_value=value,
                    button=suspend_btn: (
                        combo.setCurrentIndex(
                            next(
                                (idx for idx in range(combo.count()) if combo.itemData(idx) == duration_value),
                                combo.currentIndex(),
                            )
                        ),
                        button.click(),
                    )
                )
                suspend_menu.addAction(action)
            suspend_btn.setMenu(suspend_menu)
            resume_btn = QPushButton("Resume", tile)
            resume_btn.setToolTip(
                f"Resume scheduled control for {self._station_command_snapshot_name(snapshot)}."
                if frequency_control_available
                else "VarAC-only radios use VarAC's scheduler; there is no CommsPlan schedule to resume."
            )
            resume_btn.setEnabled(ident > 0 and frequency_control_available)
            resume_btn.clicked.connect(
                lambda _checked=False, radio_id=ident: self._on_station_command_resume_clicked(radio_id)
            )
            health_btn = QPushButton("Health", tile)
            health_btn.setObjectName("stationCommandRadioTileHealth")
            health_btn.setToolTip(self._station_command_radio_summary_tooltip(snapshot, selected_id))
            health_btn.setEnabled(ident > 0)
            health_btn.clicked.connect(
                lambda _checked=False, profile_id=ident, snap=snapshot, button=health_btn: self._show_station_command_health_menu(
                    device_profile_id=profile_id,
                    snapshot=snap,
                    anchor=button,
                )
            )
            assign_btn = QPushButton("Change Plan", tile)
            assign_btn.setObjectName("stationCommandRadioTileAssign")
            if compact_card:
                assign_btn.setText("Plan")
            assign_btn.setToolTip(f"Assign or change the Frequency Plan for {self._station_command_snapshot_name(snapshot)}.")
            assign_btn.setEnabled(ident > 0)
            assign_btn.clicked.connect(lambda _checked=False, profile_id=ident: self._open_schedule_assignment_for_radio(profile_id))
            def _update_card_qsy_buttons(
                _index: int = -1,
                *,
                radio_id: int = ident,
                qsy_button: QPushButton = qsy_btn,
                timer_button: QPushButton = timer_btn,
                combo: QComboBox = freq_combo,
                manual_active: bool = manual_qsy_active,
                timed_qsy: bool = timed_qsy_active,
                card_snapshot: object = snapshot,
                controls_available: bool = frequency_control_available,
            ) -> None:
                if not controls_available:
                    qsy_button.setText("QSY")
                    qsy_button.setEnabled(False)
                    timer_button.setEnabled(False)
                    qsy_button.setStyleSheet(button_style("muted", theme))
                    timer_button.setStyleSheet(button_style("muted", theme))
                    return
                preferred_key = self._station_command_preferred_qsy_key(card_snapshot)
                stable_preferred_key = str(combo.property("stationCommandPreferredKey") or preferred_key or "")
                selected_key = self._station_command_combo_selected_key(combo)
                action_state = qsy_action_state(
                    selected_meta=selected_qsy_meta(combo),
                    preferred_key=stable_preferred_key,
                    radio_id=radio_id,
                    selection_changed=bool(
                        selected_key
                        and (
                            (stable_preferred_key and selected_key != stable_preferred_key)
                            or (not stable_preferred_key and combo.property("stationCommandSelectionDirty"))
                        )
                    ),
                    manual_qsy_active=manual_active,
                    timed_qsy_active=timed_qsy,
                )
                qsy_button.setText("QSY")
                qsy_button.setEnabled(action_state.qsy_enabled)
                timer_button.setEnabled(action_state.timed_qsy_enabled)
                qsy_button.setStyleSheet(button_style(action_state.qsy_role, theme))
                timer_button.setStyleSheet(button_style(action_state.timed_qsy_role, theme))

            def _on_card_frequency_changed(
                _index: int = -1,
                *,
                combo: QComboBox = freq_combo,
                radio_id: int = ident,
                update_buttons: Callable[[int], None] = _update_card_qsy_buttons,
            ) -> None:
                selected_key = self._station_command_combo_selected_key(combo)
                if selected_key:
                    pending_qsy_keys[radio_id] = selected_key
                else:
                    pending_qsy_keys.pop(radio_id, None)
                combo.setProperty("stationCommandSelectionDirty", True)
                update_buttons(_index)

            freq_combo.currentIndexChanged.connect(_on_card_frequency_changed)
            for btn, role in (
                (qsy_btn, "muted"),
                (timer_btn, "warning" if timed_qsy_active else "muted"),
                (suspend_btn, scheduler_actions.timed_suspend_role),
                (resume_btn, scheduler_actions.resume_role),
                (
                    health_btn,
                    "danger"
                    if health_state == "error"
                    else "warning"
                    if health_state in {"warn", "warning", "review"}
                    else "success",
                ),
                (assign_btn, "muted"),
            ):
                btn.setStyleSheet(button_style(role, theme))
                btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            _update_card_qsy_buttons()

            if compact_card:
                tile_layout.addWidget(name_btn, 0, 0, 1, 2)
                tile_layout.addWidget(health_btn, 0, 2)
                tile_layout.addWidget(freq_combo, 1, 0, 1, 3)
                tile_layout.addWidget(qsy_btn, 2, 0)
                tile_layout.addWidget(timer_btn, 2, 1)
                tile_layout.addWidget(suspend_btn, 2, 2)
                tile_layout.addWidget(js8_offset_widget, 3, 0, 1, 3)
                tile_layout.addWidget(next_label, 4, 0, 1, 3)
                tile_layout.addWidget(resume_btn, 5, 0, 1, 2)
                tile_layout.addWidget(assign_btn, 5, 2)
            else:
                tile_layout.addWidget(name_btn, 0, 0, 1, 3)
                tile_layout.addWidget(health_btn, 0, 3)
                tile_layout.addWidget(freq_combo, 1, 0, 1, 4)
                tile_layout.addWidget(qsy_btn, 2, 0)
                tile_layout.addWidget(timer_btn, 2, 1)
                tile_layout.addWidget(suspend_btn, 2, 2)
                tile_layout.addWidget(js8_offset_widget, 3, 0, 1, 4)
                tile_layout.addWidget(next_label, 4, 0, 1, 4)
                tile_layout.addWidget(resume_btn, 5, 0, 1, 2)
                tile_layout.addWidget(assign_btn, 5, 2, 1, 2)
            tile_layout.setColumnStretch(0, 0)
            tile_layout.setColumnStretch(1, 0)
            tile_layout.setColumnStretch(2, 0)
            tile_layout.setColumnStretch(3, 1)
            tile_layout.setColumnMinimumWidth(0, 64 if compact_card else 76)
            tile_layout.setColumnMinimumWidth(1, 82 if compact_card else 128)
            tile_layout.setColumnMinimumWidth(2, 92 if compact_card else 128)
            if ident > 0:
                tile_controls[ident] = {
                    "qsy_btn": qsy_btn,
                    "timer_btn": timer_btn,
                    "suspend_btn": suspend_btn,
                    "resume_btn": resume_btn,
                    "freq_combo": freq_combo,
                    "js8_offset_spin": js8_offset_spin,
                    "js8_offset_save": js8_offset_save,
                    "frequency_controls_available": frequency_control_available,
                    "compact_card": compact_card,
                }
            try:
                tile.style().unpolish(tile)
                tile.style().polish(tile)
            except Exception:
                pass
            card_row_layout.addWidget(tile)
        card_row_layout.addStretch(1)
        layout.addWidget(card_row)
        layout.addStretch(1)
        try:
            parent.adjustSize()
            self._sync_station_command_radio_summary_height(card_mode=True)
        except Exception:
            pass
        self._station_command_radio_tile_controls = tile_controls

    def _update_station_command_radio_tile_hold_controls(self, hold_snapshot: Mapping[str, object]) -> None:
        controls_by_radio = getattr(self, "_station_command_radio_tile_controls", {})
        if not isinstance(controls_by_radio, Mapping):
            return
        try:
            theme = resolve_theme(self.settings)
        except Exception:
            theme = {}
        for raw_radio_id, controls in list(controls_by_radio.items()):
            if not isinstance(controls, Mapping):
                continue
            try:
                radio_id = int(raw_radio_id or 0)
            except Exception:
                radio_id = 0
            radio_hold_snapshot = self._station_command_hold_snapshot_for_radio(radio_id)
            radio_hold_active = bool(radio_hold_snapshot.get("active"))
            countdown = self._station_command_countdown_text(radio_hold_snapshot.get("remaining_sec")) if radio_hold_active else ""
            timed_qsy_active = bool(radio_hold_active and self._station_command_scheduler_manual_qsy_active_for_radio(radio_id))
            timed_suspend_active = self._station_command_timed_suspend_active_for_radio(radio_id)
            scheduler_suspended_manual = self._station_command_scheduler_suspended_manually_for_radio(radio_id)
            manual_qsy_active = self._station_command_scheduler_manual_qsy_active_for_radio(radio_id)
            qsy_btn = controls.get("qsy_btn")
            timer_btn = controls.get("timer_btn")
            suspend_btn = controls.get("suspend_btn")
            resume_btn = controls.get("resume_btn")
            combo = controls.get("freq_combo")
            frequency_control_available = bool(controls.get("frequency_controls_available", True))
            compact_card = bool(controls.get("compact_card", False))
            if isinstance(qsy_btn, QPushButton) and isinstance(combo, QComboBox):
                if not frequency_control_available:
                    set_widget_value(qsy_btn, "text", "QSY")
                    set_widget_value(qsy_btn, "enabled", False)
                    set_widget_value(qsy_btn, "styleSheet", button_style("muted", theme))
                    set_widget_value(combo, "enabled", False)
                    if isinstance(timer_btn, QToolButton):
                        set_widget_value(timer_btn, "enabled", False)
                        set_widget_value(timer_btn, "styleSheet", button_style("muted", theme))
                    if isinstance(suspend_btn, QToolButton):
                        set_widget_value(suspend_btn, "enabled", False)
                        set_widget_value(suspend_btn, "styleSheet", button_style("muted", theme))
                    if isinstance(resume_btn, QPushButton):
                        set_widget_value(resume_btn, "enabled", False)
                        set_widget_value(resume_btn, "styleSheet", button_style("muted", theme))
                    continue
                preferred_key = str(combo.property("stationCommandPreferredKey") or "")
                selected_key = self._station_command_combo_selected_key(combo)
                action_state = qsy_action_state(
                    selected_meta=selected_qsy_meta(combo),
                    preferred_key=preferred_key,
                    radio_id=radio_id,
                    selection_changed=bool(
                        selected_key
                        and (
                            (preferred_key and selected_key != preferred_key)
                            or (not preferred_key and combo.property("stationCommandSelectionDirty"))
                        )
                    ),
                    manual_qsy_active=manual_qsy_active,
                    timed_qsy_active=timed_qsy_active,
                )
                set_widget_value(qsy_btn, "text", "QSY")
                set_widget_value(qsy_btn, "enabled", action_state.qsy_enabled)
                set_widget_value(qsy_btn, "styleSheet", button_style(action_state.qsy_role, theme))
            if isinstance(timer_btn, QToolButton):
                if compact_card:
                    set_widget_value(timer_btn, "text", f"{countdown}" if timed_qsy_active and countdown else "Extend" if timed_qsy_active else "Hold")
                else:
                    set_widget_value(timer_btn, "text", f"{countdown} | Extend" if timed_qsy_active and countdown else timed_qsy_text(timed_qsy_active=timed_qsy_active))
                if isinstance(combo, QComboBox):
                    preferred_key = str(combo.property("stationCommandPreferredKey") or "")
                    selected_key = self._station_command_combo_selected_key(combo)
                    action_state = qsy_action_state(
                        selected_meta=selected_qsy_meta(combo),
                        preferred_key=preferred_key,
                        radio_id=radio_id,
                        selection_changed=bool(
                            selected_key
                            and (
                                (preferred_key and selected_key != preferred_key)
                                or (not preferred_key and combo.property("stationCommandSelectionDirty"))
                            )
                        ),
                        manual_qsy_active=manual_qsy_active,
                        timed_qsy_active=timed_qsy_active,
                    )
                    set_widget_value(timer_btn, "enabled", action_state.timed_qsy_enabled)
                    set_widget_value(timer_btn, "styleSheet", button_style(action_state.timed_qsy_role, theme))
                else:
                    set_widget_value(timer_btn, "styleSheet", button_style("warning" if timed_qsy_active else "muted", theme))
            if isinstance(suspend_btn, QToolButton):
                scheduler_actions = scheduler_action_state(
                    manual_qsy_active=manual_qsy_active,
                    timed_qsy_active=timed_qsy_active,
                    timed_suspend_active=timed_suspend_active,
                    scheduler_suspended_manual=scheduler_suspended_manual,
                    scheduler_state_text="Scheduler Suspended" if scheduler_suspended_manual else "",
                )
                suspend_text = (
                    f"{countdown} | Extend"
                    if timed_suspend_active and countdown
                    else scheduler_actions.timed_suspend_text
                )
                if compact_card:
                    if timed_suspend_active and countdown:
                        suspend_text = countdown
                    elif suspend_text == "Indefinite Suspend":
                        suspend_text = "Indef."
                    elif suspend_text == "Extend Suspend":
                        suspend_text = "Extend"
                    elif suspend_text == "Timed Suspend":
                        suspend_text = "Suspend"
                set_widget_value(suspend_btn, "text", suspend_text)
                set_widget_value(suspend_btn, "styleSheet", button_style(scheduler_actions.timed_suspend_role, theme))
            if isinstance(resume_btn, QPushButton):
                resume_active = manual_qsy_active or timed_qsy_active or timed_suspend_active or scheduler_suspended_manual
                set_widget_value(resume_btn, "enabled", radio_id > 0 and resume_active)
                set_widget_value(resume_btn, "styleSheet", button_style("warning" if resume_active else "muted", theme))

    def _clear_station_command_admin_layout(self) -> None:
        layout = getattr(self, "station_command_radio_admin_layout", None)
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _station_command_plan_name_for_snapshot(self, snapshot: object) -> str:
        ident = self._station_command_snapshot_id(snapshot)
        if ident > 0:
            assigned_name = self._station_command_assigned_plan_name_for_radio(ident)
            if assigned_name:
                return assigned_name
        for key in (
            "frequency_plan_name",
            "assigned_frequency_plan_name",
            "assigned_schedule_name",
        ):
            text = str(self._station_command_value(snapshot, key, "") or "").strip()
            if text:
                return text
        return "Unassigned"

    def _refresh_station_command_radio_admin(self, choices: list[object], selected_id: int) -> None:
        panel = getattr(self, "station_command_radio_admin_panel", None)
        layout = getattr(self, "station_command_radio_admin_layout", None)
        if panel is None or layout is None:
            return
        expanded = bool(getattr(self, "_station_command_radio_admin_expanded", False))
        panel.setVisible(expanded)
        admin_btn = getattr(self, "station_command_radio_admin_btn", None)
        if admin_btn is not None:
            admin_btn.setText("Hide Radios" if expanded else "All Radios")
        self._clear_station_command_admin_layout()
        if not expanded:
            return
        if not choices:
            layout.addWidget(QLabel("No configured radios are available for station control.", panel))
            return
        for snapshot in choices:
            ident = self._station_command_snapshot_id(snapshot)
            name = self._station_command_snapshot_name(snapshot)
            now = self._station_command_now_text_for_summary(snapshot, selected_id)
            state = self._station_command_compact_state_text(self._station_command_state_text(snapshot))
            plan_name = self._station_command_display_plan_name(self._station_command_plan_name_for_snapshot(snapshot))
            row = QFrame(panel)
            row.setObjectName("stationCommandRadioAdminRow")
            row.setFrameShape(QFrame.StyledPanel)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 4, 8, 4)
            row_layout.setSpacing(8)
            title = QLabel(name, row)
            title.setMinimumWidth(110)
            title.setStyleSheet("font-weight: 800;")
            title.setToolTip(self._station_command_radio_summary_tooltip(snapshot, selected_id))
            now_label = QLabel(now, row)
            now_label.setMinimumWidth(160)
            now_label.setToolTip(self._station_command_frequency_text(snapshot))
            state_label = QLabel(state, row)
            state_label.setMinimumWidth(120)
            plan_label = QLabel(f"Plan: {plan_name}", row)
            plan_label.setMinimumWidth(180)
            plan_label.setToolTip(plan_name)
            select_btn = QPushButton("Select", row)
            select_btn.setEnabled(ident > 0 and ident != int(selected_id or 0))
            select_btn.clicked.connect(lambda _checked=False, profile_id=ident: self._on_station_command_summary_radio_clicked(profile_id))
            assign_btn = QPushButton("Assign Plan", row)
            assign_btn.setEnabled(ident > 0)
            assign_btn.clicked.connect(lambda _checked=False, profile_id=ident: self._open_schedule_assignment_for_radio(profile_id))
            health_btn = QPushButton("Health", row)
            health_btn.setEnabled(ident > 0)
            health_btn.clicked.connect(lambda _checked=False, profile_id=ident: self._open_station_health_detail(device_profile_id=profile_id))
            row_layout.addWidget(title)
            row_layout.addWidget(now_label)
            row_layout.addWidget(state_label)
            row_layout.addWidget(plan_label, 1)
            row_layout.addWidget(select_btn)
            row_layout.addWidget(assign_btn)
            row_layout.addWidget(health_btn)
            layout.addWidget(row)
        layout.addStretch(1)

    def _toggle_station_command_radio_admin(self) -> None:
        self._station_command_radio_admin_expanded = not bool(getattr(self, "_station_command_radio_admin_expanded", False))
        self._refresh_station_command_bar(force=True)

    def _open_schedule_assignment_for_radio(self, device_profile_id: int) -> None:
        ident = int(device_profile_id or 0)
        if ident <= 0:
            return
        self.open_settings_section("schedule_assignments", radio_id=ident, settings_nav_context="radios")
        settings_tab = getattr(self, "settings_tab", None)
        opener = getattr(settings_tab, "open_schedule_assignment_editor", None)
        if callable(opener):
            expected = self._screen_index_by_label.get("Settings", -1)
            epoch = int(getattr(self, "_navigation_epoch", 0))
            QTimer.singleShot(0, lambda profile_id=ident: self._run_if_screen_current(
                expected, epoch, lambda: opener(device_profile_id=profile_id)
            ))

    def _on_station_command_summary_radio_clicked(self, device_profile_id: int) -> None:
        ident = int(device_profile_id or 0)
        if ident <= 0:
            return
        snapshot = self._station_command_current_snapshot(ident)
        read_only = snapshot is not None and (
            str(self._station_command_value(snapshot, "device_class", "")).lower() == "observer"
            or (self._station_command_value(snapshot, "runtime_active", None) is not None
                and not self._station_command_bool(self._station_command_value(snapshot, "runtime_active", None), default=False))
        )
        # Observer/inactive cards are inspectable without activating a control
        # context. Their command controls remain unavailable.
        if read_only or self._activate_station_command_radio(ident):
            self._station_command_selected_profile_id = ident
        self._refresh_station_command_bar(force=True)

    def _refresh_station_command_bar(self, *, force: bool = False) -> None:
        if not hasattr(self, "station_command_radio_combo"):
            return
        if force:
            self._invalidate_station_command_plan_cache()
            self._invalidate_station_command_lane_cache()
        manager = getattr(self, "station_runtime_manager", None)
        snapshots: list[object] = []
        if manager is not None:
            try:
                try:
                    snapshots = list(manager.get_runtime_snapshots(force=force, cache_only=True))
                except TypeError:
                    snapshots = list(manager.get_runtime_snapshots(force=force))
            except Exception:
                snapshots = []
        scheduler = getattr(self, "scheduler", None)
        operational = {}
        # A transient cache-getter failure must not blank every visible radio.
        # This is raw safety evidence; GUI retention is projected separately.
        if not isinstance(getattr(self, "_station_command_endpoint_status_cache", None), dict):
            self._station_command_endpoint_status_cache = {}
        if scheduler is not None and hasattr(scheduler, "get_endpoint_status_summaries"):
            try:
                self._station_command_endpoint_status_cache = scheduler.get_endpoint_status_summaries() or {}
            except Exception:
                pass
        if scheduler is not None and hasattr(scheduler, "get_endpoint_operational_summaries"):
            try:
                operational = scheduler.get_endpoint_operational_summaries() or {}
            except Exception:
                operational = {}
        settings_tab = getattr(self, "settings_tab", None)
        publish_status = getattr(settings_tab, "set_radio_operational_status_cache", None)
        if callable(publish_status):
            publish_status(operational)
        for snapshot in snapshots:
            profile_id = self._station_command_snapshot_id(snapshot)
            endpoint_summary = operational.get(profile_id) if isinstance(operational, Mapping) else None
            if not isinstance(endpoint_summary, Mapping):
                continue
            apply_endpoint_operational_summary(snapshot, endpoint_summary)
        snapshot_by_id = {self._station_command_snapshot_id(snapshot): snapshot for snapshot in snapshots}
        choices: list[object] = []
        seen_ids: set[int] = set()
        for profile in self._station_command_configured_profiles():
            ident = self._station_command_snapshot_id(profile)
            if ident <= 0 or ident in seen_ids:
                continue
            choices.append(snapshot_by_id.get(ident, profile))
            seen_ids.add(ident)
        for snapshot in snapshots:
            ident = self._station_command_snapshot_id(snapshot)
            if ident > 0 and ident not in seen_ids and (self._station_command_is_controllable_profile(snapshot)
                    or str(self._station_command_value(snapshot, "device_class", "")).lower() == "observer"):
                choices.append(snapshot)
                seen_ids.add(ident)
        self._station_command_last_choices = list(choices)
        active_ids = {self._station_command_snapshot_id(item) for item in choices}
        self._station_command_endpoint_status_cache = {
            key: value for key, value in self._station_command_endpoint_status_cache.items()
            if key in active_ids
        }

        selected = self._station_command_promoted_snapshot(choices, self._station_command_selected_snapshot(choices))
        selected_id = self._station_command_snapshot_id(selected) if selected is not None else 0
        card_mode = len(choices) >= 1
        multi_active = len(choices) >= 2
        if card_mode and not force and bool(getattr(self, "_station_command_multi_mode_active", False)):
            if selected is not None and selected_id > 0:
                self._station_command_selected_profile_id = int(selected_id)
            for btn, direction, label in (
                (getattr(self, "station_command_radio_prev_btn", None), -1, "Prev"),
                (getattr(self, "station_command_radio_next_btn", None), 1, "Next"),
            ):
                if btn is None:
                    continue
                btn.setVisible(False)
                btn.setEnabled(False)
                if btn.text() != label:
                    btn.setText(label)
            self._refresh_station_command_radio_summary(choices, selected_id)
            try:
                snapshot = suspend_snapshot(self.settings, allow_reload=False)
            except Exception:
                snapshot = {}
            self._update_station_command_radio_tile_hold_controls(snapshot if isinstance(snapshot, Mapping) else {})
            return
        self._station_command_bar_loading = True
        combo = self.station_command_radio_combo
        previous_block = combo.blockSignals(True)
        try:
            combo.clear()
            for snapshot in choices:
                ident = self._station_command_snapshot_id(snapshot)
                role = "SDR" if str(self._station_command_value(snapshot, "device_class", "") or "").strip().lower() == "observer" else "HF"
                combo.addItem(f"{self._station_command_snapshot_name(snapshot)} ({role})", ident)
            if combo.count() <= 0:
                combo.addItem("No configured radios", 0)
            for index in range(combo.count()):
                try:
                    if int(combo.itemData(index) or 0) == int(selected_id):
                        combo.setCurrentIndex(index)
                        break
                except Exception:
                    continue
        finally:
            combo.blockSignals(previous_block)
            self._station_command_bar_loading = False

        if selected is not None and selected_id > 0:
            self._station_command_selected_profile_id = int(selected_id)
            self.station_command_now_label.setToolTip(self._station_command_now_tooltip(selected))
            self.station_command_now_label.setText(self._station_command_now_text(selected))
            state_text = self._station_command_state_text(selected)
            if state_text.strip().lower() in {"ok", "ready", "on schedule"}:
                self.station_command_state_label.setText("")
                self.station_command_state_label.setVisible(False)
            else:
                self.station_command_state_label.setText(state_text)
                self.station_command_state_label.setVisible(True)
            self.station_command_next_label.setText(f"Next: {self._station_command_next_text(selected)}")
            target_name = self._station_command_snapshot_name(selected)
            tooltip = f"Command target: {target_name}."
        else:
            self._station_command_selected_profile_id = None
            self.station_command_now_label.setToolTip("No configured radio is available for station commands.")
            self.station_command_now_label.setText("Now: unavailable")
            self.station_command_state_label.setText("No configured radio")
            self.station_command_state_label.setVisible(True)
            self.station_command_next_label.setText("Next: none")
            tooltip = "No configured radio is available for station commands."
        self._refresh_station_command_health(selected, selected_id)
        self._station_command_multi_mode_active = card_mode
        if getattr(self, "station_command_radio_summary_label", None) is not None:
            self.station_command_radio_summary_label.setText("Active Radios" if card_mode else "Radios")
            self.station_command_radio_summary_label.setVisible(False)
        if getattr(self, "station_command_radio_summary_scroll", None) is not None:
            self._sync_station_command_radio_summary_height(card_mode=card_mode)
            self.station_command_radio_summary_scroll.setVisible(True)
        self._apply_station_command_bar_layout(force=False)
        for btn, direction, label in (
            (getattr(self, "station_command_radio_prev_btn", None), -1, "Prev"),
            (getattr(self, "station_command_radio_next_btn", None), 1, "Next"),
        ):
            if btn is None:
                continue
            btn.setVisible(False)
            btn.setEnabled(False)
            btn.setText(label)
        if getattr(self, "station_command_radio_admin_btn", None) is not None:
            self.station_command_radio_admin_btn.setVisible(False)
        if card_mode and getattr(self, "station_command_radio_admin_panel", None) is not None:
            self._station_command_radio_admin_expanded = False
            self.station_command_radio_admin_panel.setVisible(False)
        for widget in (
            getattr(self, "station_command_radio_label", None),
            getattr(self, "station_command_radio_combo", None),
            getattr(self, "station_command_radio_separator", None),
            getattr(self, "station_command_now_caption", None),
            getattr(self, "station_command_now_label", None),
            getattr(self, "station_command_state_label", None),
            getattr(self, "station_command_now_separator", None),
            getattr(self, "station_command_action_label", None),
            getattr(self, "station_command_freq_combo", None),
            getattr(self, "station_command_qsy_btn", None),
            getattr(self, "station_command_duration_combo", None),
            getattr(self, "station_command_hold_btn", None),
            getattr(self, "station_command_suspend_btn", None),
            getattr(self, "station_command_resume_btn", None),
            getattr(self, "station_command_health_label", None),
            getattr(self, "station_command_health_widget", None),
            getattr(self, "station_command_next_label", None),
        ):
            if widget is not None:
                widget.setVisible(not card_mode)
        self._refresh_station_command_radio_summary(choices, selected_id)
        self._refresh_station_command_radio_admin(choices, selected_id)
        try:
            refresh_hold_duration_combo(
                self.station_command_duration_combo,
                self.settings,
                getattr(self, "_active_runtime_profile", None),
            )
        except Exception:
            pass
        has_qsy_target = self._refresh_station_command_frequency_combo(selected)
        has_radio = selected is not None and selected_id > 0
        hold_snapshot = suspend_snapshot(self.settings, allow_reload=False)
        manual_qsy_active = self._station_command_scheduler_manual_qsy_active()
        scheduler_suspended_manual = self._station_command_scheduler_suspended_manually()
        can_qsy = bool(has_radio and has_qsy_target)
        self.station_command_qsy_btn.setEnabled(can_qsy)
        self.station_command_hold_btn.setEnabled(can_qsy)
        self.station_command_suspend_btn.setEnabled(bool(has_radio))
        self.station_command_resume_btn.setEnabled(
            bool(has_radio and (hold_snapshot.get("active") or manual_qsy_active or scheduler_suspended_manual))
        )
        if has_radio and manual_qsy_active:
            self.station_command_state_label.setText("Manual QSY")
            self.station_command_state_label.setVisible(True)
        elif has_radio and scheduler_suspended_manual:
            self.station_command_state_label.setText("Scheduler Suspended")
            self.station_command_state_label.setVisible(True)
        self.station_command_duration_combo.setEnabled(bool(has_radio))
        self.station_command_freq_combo.setEnabled(bool(has_radio))
        for btn in (
            self.station_command_qsy_btn,
            self.station_command_hold_btn,
            self.station_command_suspend_btn,
            self.station_command_resume_btn,
        ):
            btn.setToolTip(tooltip)
        if can_qsy:
            self.station_command_qsy_btn.setToolTip(f"{tooltip} Send the selected manual QSY now and suspend scheduled changes until Resume Schedule.")
            mins = self._selected_station_command_hold_minutes()
            self.station_command_hold_btn.setToolTip(f"{tooltip} Send the selected manual QSY and suspend the scheduler for {mins} minutes.")
        self.station_command_suspend_btn.setToolTip(f"{tooltip} Suspend scheduled frequency changes until Resume Schedule without changing the radio.")
        self.station_command_resume_btn.setToolTip(f"{tooltip} Resume scheduled frequency changes.")
        self._update_station_command_hold_button_labels(hold_snapshot)
        if card_mode:
            for widget in (
                getattr(self, "station_command_radio_label", None),
                getattr(self, "station_command_radio_combo", None),
                getattr(self, "station_command_radio_separator", None),
                getattr(self, "station_command_now_caption", None),
                getattr(self, "station_command_now_label", None),
                getattr(self, "station_command_state_label", None),
                getattr(self, "station_command_now_separator", None),
                getattr(self, "station_command_action_label", None),
                getattr(self, "station_command_freq_combo", None),
                getattr(self, "station_command_qsy_btn", None),
                getattr(self, "station_command_duration_combo", None),
                getattr(self, "station_command_hold_btn", None),
                getattr(self, "station_command_suspend_btn", None),
                getattr(self, "station_command_resume_btn", None),
                getattr(self, "station_command_health_label", None),
                getattr(self, "station_command_health_widget", None),
                getattr(self, "station_command_next_label", None),
            ):
                if widget is not None:
                    widget.setVisible(False)
        try:
            self._style_station_command_bar(resolve_theme(self.settings))
        except Exception:
            pass

    def _on_station_command_radio_changed(self, _index: int) -> None:
        if bool(getattr(self, "_station_command_bar_loading", False)):
            return
        try:
            ident = int(self.station_command_radio_combo.currentData() or 0)
        except Exception:
            ident = 0
        if ident > 0:
            self._on_station_command_summary_radio_clicked(ident)
            return
        self._station_command_selected_profile_id = None
        self._refresh_station_command_bar(force=False)

    def _on_station_health_settings_saved(self) -> None:
        self._refresh_station_health_scope_map()
        try:
            tab = getattr(self, "station_health_tab", None)
            if tab is not None:
                tab.refresh_from_registry()
        except Exception:
            pass
        self._station_health_alert_signature = None
        self._refresh_station_health_alert()

    def _refresh_station_health_scope_map(self) -> None:
        scope_map: dict[str, str] = {}
        endpoint_owners: dict[str, set[str]] = {}
        try:
            profiles = list(self.multi_radio_store.list_runtime_active_device_profiles())
        except Exception:
            profiles = []

        def _profile_name(profile: dict) -> str:
            name = str(profile.get("name", "") or profile.get("label", "") or "").strip()
            if name:
                return name
            ident = profile.get("id", "")
            return f"Radio {ident}" if ident not in (None, "") else "Station-wide"

        def _host(value: object, default: str = "127.0.0.1") -> str:
            return str(value or default or "127.0.0.1").strip().lower() or str(default or "127.0.0.1")

        def _port(value: object, default: int) -> int:
            try:
                return int(value if value not in (None, "") else default)
            except Exception:
                return int(default)

        def _add(service: str, host: object, port: object, name: str, *, default_port: int) -> None:
            host_text = _host(host)
            if host_text in {"localhost", "loopback", "::1"}:
                host_text = "127.0.0.1"
            port_text = str(_port(port, default_port))
            key = f"{service.lower()}:{host_text}:{port_text}"
            owners = endpoint_owners.setdefault(key, set())
            owners.add(name)
            label = name if len(owners) == 1 else "Shared · " + ", ".join(sorted(owners))
            scope_map[key] = label
            if host_text == "127.0.0.1":
                for alias in ("loopback", "localhost", "::1"):
                    scope_map[f"{service.lower()}:{alias}:{port_text}"] = label

        for raw_profile in profiles:
            if not isinstance(raw_profile, dict):
                continue
            name = _profile_name(raw_profile)
            if raw_profile.get("id") not in (None, ""):
                scope_map[f"radio_{raw_profile['id']}"] = name
            if (raw_profile.get("js8_instance_id") or raw_profile.get("js8_install_path")
                    or raw_profile.get("js8_profile_path") or raw_profile.get("control_backend") == "js8call"):
                _add("JS8CALL", raw_profile.get("js8_host"), raw_profile.get("js8_port"), name, default_port=2442)
            _add("FLRIG", raw_profile.get("flrig_host"), raw_profile.get("flrig_port"), name, default_port=12345)
            fldigi_host = raw_profile.get("fldigi_host") or raw_profile.get("flrig_host")
            _add("FLDIGI", fldigi_host, raw_profile.get("fldigi_port"), name, default_port=7362)
            backend = str(raw_profile.get("control_backend", "") or "").strip().lower()
            if backend == "rigctld":
                _add("RIGCTLD", raw_profile.get("rig_host"), raw_profile.get("rig_port"), name, default_port=4532)
            if str(raw_profile.get("device_class", "") or "").strip().lower() == "observer":
                _add("OBSERVER", raw_profile.get("sdr_host"), raw_profile.get("sdr_port"), name, default_port=0)
        self._station_health_scope_map = scope_map

    def _station_health_scope_resolver(self, key: str, metadata: dict[str, object]) -> str:
        scope_map = getattr(self, "_station_health_scope_map", {}) or {}
        radio_id = metadata.get("device_profile_id", metadata.get("radio_id"))
        identity = str(metadata.get("radio_profile_id") or radio_id or "")
        if identity and not identity.startswith("radio_"):
            identity = f"radio_{identity}"
        if identity:
            return scope_map.get(identity, identity.replace("radio_", "Radio "))
        endpoint = str(metadata.get("endpoint") or "").strip().lower()
        if endpoint and "js8" in str(key).lower():
            endpoint_scope = scope_map.get(f"js8call:{endpoint}")
            if endpoint_scope:
                return endpoint_scope
        for meta_key in ("scope", "radio_name", "radio", "profile_name", "device_name"):
            text = str(metadata.get(meta_key, "") or "").strip()
            if text:
                return text
        normalized = str(key or "").strip().lower()
        scope_map = getattr(self, "_station_health_scope_map", {}) or {}
        if normalized in scope_map:
            return scope_map[normalized]
        parts = normalized.split(":")
        if len(parts) >= 3:
            compact = ":".join(parts[:3])
            if compact in scope_map:
                return scope_map[compact]
        return ""

    def _runtime_client_signature_for_settings(self) -> tuple[object, ...]:
        manager = getattr(self, "station_runtime_manager", None)
        if manager is not None and hasattr(manager, "primary_runtime_signature"):
            try:
                signature = manager.primary_runtime_signature()
                if signature:
                    return tuple(signature)
            except Exception:
                pass

        def _text(key: str, default: str = "") -> str:
            try:
                return str(self.settings.get(key, default) or "").strip()
            except Exception:
                return str(default or "").strip()

        def _int(key: str, default: int) -> int:
            try:
                value = self.settings.get(key, default)
                return int(value if value not in (None, "") else default)
            except Exception:
                return int(default)

        return (
            _text("control_via", "FLRig").upper(),
            _text("rig_host", "127.0.0.1"),
            _int("rig_port", 4532),
            _text("flrig_host", "127.0.0.1"),
            _int("flrig_port", 12345),
            _text("fldigi_host", ""),
            _int("fldigi_port", 7362),
            _text("js8_host", "127.0.0.1"),
            _int("js8_port", 2442),
        )

    def _new_js8_control_client(self) -> object:
        js8_host = str(self.settings.get("js8_host", "") or "").strip() or None
        js8_port = int(self.settings.get("js8_port", 2442) or 2442)
        for kwargs in (
            {"host": js8_host, "port": js8_port, "settings": self.settings},
            {"host": js8_host, "port": js8_port},
            {"host": js8_host},
            {},
        ):
            try:
                return JS8ControlClient(**kwargs)
            except TypeError:
                continue
        return JS8ControlClient()

    def _new_varac_status_client(self) -> object:
        for kwargs in ({"settings": self.settings}, {}):
            try:
                return VarACStatusClient(**kwargs)
            except TypeError:
                continue
        return VarACStatusClient()

    def _rebuild_runtime_clients(self, *, force: bool = False) -> None:
        old_signature = self._runtime_client_signature
        station_runtime_manager = getattr(self, "station_runtime_manager", None)
        try:
            self.settings.reload()
        except Exception:
            pass
        if station_runtime_manager is not None:
            try:
                station_runtime_manager.sync_with_store()
            except Exception as e:
                log.debug("MainWindow: station runtime sync failed: %s", e)
        signature = self._runtime_client_signature_for_settings()
        if not force and signature == self._runtime_client_signature:
            self._refresh_station_overview(force=False)
            return
        self._runtime_client_signature = signature

        old_js8 = getattr(self, "js8_control", None)
        if old_signature is not None and signature != old_signature:
            try:
                JS8RxHub.shutdown_all()
            except Exception:
                pass

        primary_runtime = station_runtime_manager.get_primary_runtime() if station_runtime_manager is not None else None
        try:
            self.rig_client = (
                primary_runtime.rig_client
                if primary_runtime is not None and primary_runtime.rig_client is not None
                else rig_control_client_from_settings(self.settings)
            )
        except Exception as e:
            log.debug("MainWindow: rig backend rebuild failed: %s", e)
            self.rig_client = None

        try:
            self.js8_control = (
                primary_runtime.js8_control_client
                if primary_runtime is not None and primary_runtime.js8_control_client is not None
                else self._new_js8_control_client()
            )
        except Exception as e:
            log.debug("MainWindow: JS8 control rebuild failed: %s", e)
            self.js8_control = self._new_js8_control_client()

        try:
            self.varac_status = (
                primary_runtime.varac_status_client
                if primary_runtime is not None and primary_runtime.varac_status_client is not None
                else self._new_varac_status_client()
            )
        except Exception as e:
            log.debug("MainWindow: VarAC status rebuild failed: %s", e)
            self.varac_status = self._new_varac_status_client()

        try:
            self.fldigi_log_status = FldigiLogStatusClient()
        except Exception as e:
            log.debug("MainWindow: FLDigi log status rebuild failed: %s", e)

        try:
            if old_js8 is not None and old_js8 is not self.js8_control and hasattr(old_js8, "stop"):
                old_js8.stop()
        except Exception:
            pass

        if hasattr(self, "scheduler") and self.scheduler is not None:
            try:
                self.scheduler.rig = self.rig_client
                self.scheduler.js8 = self.js8_control
                self.scheduler.varac = self.varac_status
                self.scheduler.fldigi_log = self.fldigi_log_status
            except Exception:
                pass
        self._refresh_station_overview(force=True)

    @staticmethod
    def _failed_receiver_verification(
        profile: Mapping[str, object],
        detail: str,
    ) -> dict[str, object]:
        try:
            port = int(profile.get("sdr_port") or 0)
        except (TypeError, ValueError):
            port = 0
        return {
            "schema_version": 1,
            "tested_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "adapter": str(profile.get("sdr_adapter") or "").strip().lower(),
            "application": str(profile.get("sdr_application") or "").strip(),
            "host": str(profile.get("sdr_host") or "").strip(),
            "port": port,
            "target": str(profile.get("sdr_target") or "").strip(),
            "tune_readback_verified": False,
            "restore_readback_verified": False,
            "failure_detail": str(detail or "Receiver qualification failed.")[:240],
        }

    def _publish_receiver_qualification_result(
        self,
        profile: Mapping[str, object],
        *,
        verification_state: str,
        detail: str,
        verification: object = None,
    ) -> None:
        try:
            profile_id = int(profile.get("id") or profile.get("device_profile_id") or 0)
        except (TypeError, ValueError):
            profile_id = 0
        evidence = (
            dict(verification)
            if isinstance(verification, Mapping)
            else self._failed_receiver_verification(profile, detail)
        )
        payload = {
            "profile_id": profile_id,
            QUALIFICATION_REQUEST_ID_FIELD: str(
                profile.get(QUALIFICATION_REQUEST_ID_FIELD) or ""
            ).strip(),
            "verification_state": str(verification_state or "failed").strip().lower(),
            "detail": str(detail or "").strip(),
            "verification": evidence,
        }
        signal = getattr(self.settings_tab, "receiver_control_test_completed", None)
        if signal is not None:
            signal.emit(payload)

    def _on_receiver_control_test_requested(self, profile: object) -> None:
        """Queue explicit SDR qualification; this UI slot performs no endpoint I/O."""

        if self._shutting_down or not isinstance(profile, Mapping):
            return
        snapshot = dict(profile)
        request_id = str(snapshot.get(QUALIFICATION_REQUEST_ID_FIELD) or "").strip()
        if not request_id:
            request_id = uuid.uuid4().hex
        snapshot[QUALIFICATION_REQUEST_ID_FIELD] = request_id
        # A repeated queued signal is not a second operator action.  Keeping
        # the original snapshot avoids a duplicate completion racing its own
        # correlation token on the UI thread.
        if request_id in self._receiver_qualification_profiles:
            return
        self._receiver_qualification_profiles[request_id] = snapshot
        try:
            submission = self.receiver_qualification.request(
                snapshot,
                self._receiver_qualification_finished.emit,
                qualification_request_id=request_id,
            )
        except Exception as exc:
            self._receiver_qualification_profiles.pop(request_id, None)
            detail = str(exc).strip() or "Receiver qualification could not start."
            self._publish_receiver_qualification_result(
                snapshot,
                verification_state="failed",
                detail=detail,
            )
            return
        if not submission.accepted:
            self._receiver_qualification_profiles.pop(request_id, None)
            detail = f"Receiver qualification was not started ({submission.disposition}); manual tuning remains available."
            self._publish_receiver_qualification_result(
                snapshot,
                verification_state="failed",
                detail=detail,
            )

    def _on_receiver_qualification_finished(self, result: object) -> None:
        """Marshal an immutable endpoint result onto the Qt thread."""

        if not isinstance(result, EndpointResult):
            return
        actual = result.actual_state()
        request_id = str(actual.get(QUALIFICATION_REQUEST_ID_FIELD) or "").strip()
        if not request_id:
            # An endpoint result without the captured token is not safe to
            # associate with an unsaved draft or an in-flight newer request.
            return
        profile = self._receiver_qualification_profiles.pop(request_id, None)
        if profile is None:
            # The request already completed, was rejected, or was superseded.
            # Ignore duplicate/stale lane callbacks rather than changing UI
            # evidence for a newer configuration.
            return
        verification = actual.get("verification")
        verified = bool(
            result.status in {"applied_and_verified", "applied_unverified"}
            and str(actual.get("verification_state") or "").strip().lower() == "verified"
            and isinstance(verification, Mapping)
            and bool(verification.get("tune_readback_verified"))
            and bool(verification.get("restore_readback_verified"))
        )
        self._publish_receiver_qualification_result(
            profile,
            verification_state="verified" if verified else "failed",
            detail=result.detail or ("Receiver control verified." if verified else result.reason_code),
            verification=verification if isinstance(verification, Mapping) else None,
        )

    def _on_runtime_settings_saved(self) -> None:
        self._station_command_qsy_requests = {}
        self._station_command_qsy_dispositions = {}
        self._station_command_endpoint_status_cache = {}
        self._rebuild_runtime_clients()
        try:
            self._station_command_profile_cache = list(
                self.multi_radio_store.list_runtime_active_device_profiles()
            )
        except Exception:
            pass
        self._refresh_station_command_launch_monitor_cache()
        self._apply_runtime_profile_state()
        self._refresh_plan_context_labels("runtime_settings_saved")
        try:
            if self.stations_map_tab is not None and hasattr(self.stations_map_tab, "_start_js8_rx_listener"):
                self.stations_map_tab._start_js8_rx_listener()
        except Exception:
            pass
        try:
            if hasattr(self, "scheduler") and self.scheduler is not None:
                self.scheduler.force_refresh()
        except Exception:
            pass

    def _on_runtime_device_profiles_changed(self) -> None:
        self._station_command_qsy_requests = {}
        self._station_command_qsy_dispositions = {}
        self._station_command_endpoint_status_cache = {}
        self._rebuild_runtime_clients()
        self._sync_settings_runtime_status(refresh_store=False)
        try:
            self._station_command_profile_cache = list(
                self.multi_radio_store.list_runtime_active_device_profiles()
            )
        except Exception:
            pass
        self._refresh_station_command_launch_monitor_cache()
        self._apply_runtime_profile_state()
        self._refresh_plan_context_labels("runtime_device_profiles_changed")
        self._on_settings_saved_for_lazy_tabs()
        try:
            self.background_ingest.refresh_runtime_settings()
        except Exception:
            log.debug("MainWindow: radio-profile ingest refresh failed", exc_info=True)
        try:
            self._on_station_health_settings_saved()
        except Exception:
            log.debug("MainWindow: radio-profile station-health refresh failed", exc_info=True)
        try:
            if self.stations_map_tab is not None and hasattr(self.stations_map_tab, "_start_js8_rx_listener"):
                self.stations_map_tab._start_js8_rx_listener()
        except Exception:
            pass
        try:
            if hasattr(self, "scheduler") and self.scheduler is not None:
                self.scheduler.force_refresh()
        except Exception:
            pass

    def _refresh_freq_planner_if_loaded(self) -> None:
        try:
            if self.freq_planner_tab is not None:
                refresh_sources = getattr(self.freq_planner_tab, "on_schedule_sources_changed", None)
                if callable(refresh_sources):
                    refresh_sources()
                elif self.stack.currentWidget() is self.freq_planner_tab:
                    self.freq_planner_tab.rebuild_table()
                elif hasattr(self.freq_planner_tab, "mark_schedule_dirty"):
                    self.freq_planner_tab.mark_schedule_dirty()
        except Exception:
            pass

    def _start_launch_control_startup(self) -> None:
        try:
            if hasattr(self, "launch_orchestrator"):
                self.launch_orchestrator.start_startup_sequence()
        except Exception as e:
            log.debug("MainWindow: launch-control startup sequence failed to start: %s", e)

    @staticmethod
    def _launch_sequence_radio_ids(payload: object) -> set[int]:
        data = payload if isinstance(payload, Mapping) else {}
        queue = data.get("queue") if isinstance(data, Mapping) else ()
        radio_ids: set[int] = set()
        if not isinstance(queue, Sequence) or isinstance(queue, (str, bytes)):
            return radio_ids
        for item in queue:
            if not isinstance(item, Mapping):
                continue
            values = item.get("radio_ids", ())
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                values = (values,)
            for value in values:
                try:
                    radio_id = int(value)
                except (TypeError, ValueError):
                    continue
                if radio_id > 0:
                    radio_ids.add(radio_id)
        return radio_ids

    @staticmethod
    def _launch_radio_ids_after_progress(
        current_ids: set[int],
        payload: object,
    ) -> set[int]:
        data = payload if isinstance(payload, Mapping) else {}
        remaining = set(current_ids)
        status = str(data.get("status", "") or "").strip()
        if bool(data.get("counts_toward_total", True)) or status not in {
            "ready",
            "unavailable",
        }:
            return remaining
        values = data.get("radio_ids", ())
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            values = (values,)
        for value in values:
            try:
                remaining.discard(int(value))
            except (TypeError, ValueError):
                continue
        return remaining

    def _on_launch_sequence_started(self, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        queue = data.get("queue") if isinstance(data, dict) else []
        queue_count = len(queue) if isinstance(queue, list) else 0
        trigger = str(data.get("trigger", "")).strip().capitalize() or "Launch"
        launch_radio_ids = self._launch_sequence_radio_ids(payload)
        self._launch_initializing_radio_ids = set(launch_radio_ids)
        try:
            self.scheduler.set_launch_initializing_radios(launch_radio_ids)
        except Exception:
            log.debug("MainWindow: could not apply launch scheduler hold", exc_info=True)
        self._launch_progress_total = max(queue_count, 1)
        self._launch_progress_done = 0
        try:
            self.statusBar().showMessage(f"{trigger}: launching {queue_count} application(s)...")
        except Exception:
            pass
        try:
            if self._launch_progress_dialog is not None:
                self._launch_progress_dialog.close()
        except Exception:
            pass
        dlg = QProgressDialog(
            f"{trigger}: launching applications...",
            "Stop",
            0,
            self._launch_progress_total,
            self,
        )
        dlg.setWindowTitle("Launch Control")
        dlg.setWindowModality(Qt.NonModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        try:
            dlg.canceled.connect(self.launch_orchestrator.stop_sequence)
        except Exception:
            pass
        dlg.show()
        self._launch_progress_dialog = dlg

    def _on_launch_sequence_progress(self, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        name = str(data.get("name", "")).strip() or "Application"
        status = str(data.get("status", "")).strip() or "status"
        detail = str(data.get("detail", "")).strip()
        if (
            not bool(data.get("counts_toward_total", True))
            and status in {"ready", "unavailable"}
        ):
            active_ids = self._launch_radio_ids_after_progress(
                set(getattr(self, "_launch_initializing_radio_ids", set())),
                data,
            )
            self._launch_initializing_radio_ids = active_ids
            try:
                self.scheduler.set_launch_initializing_radios(active_ids)
                if status == "ready":
                    for radio_id in data.get("radio_ids", []):
                        self.scheduler.begin_launch_schedule_apply(int(radio_id))
            except Exception:
                log.debug("MainWindow: could not release launch scheduler hold", exc_info=True)
        if bool(data.get("counts_toward_total", True)):
            self._launch_progress_done = min(self._launch_progress_total, self._launch_progress_done + 1)
        try:
            self.statusBar().showMessage(f"Launch: {name} {status}" + (f" ({detail})" if detail else ""))
        except Exception:
            pass
        if self._launch_progress_dialog is not None:
            try:
                label = f"{name}: {status}"
                if detail:
                    label = f"{label} ({detail})"
                self._launch_progress_dialog.setLabelText(label)
                self._launch_progress_dialog.setValue(self._launch_progress_done)
            except Exception:
                pass

    def _on_launch_sequence_finished(self, payload: object) -> None:
        self._launch_initializing_radio_ids = set()
        try:
            self.scheduler.set_launch_initializing_radios(())
            self.scheduler.end_launch_schedule_apply()
            self.scheduler.force_refresh()
        except Exception:
            log.debug("MainWindow: could not clear launch scheduler hold", exc_info=True)
        try:
            data = payload if isinstance(payload, dict) else {}
            trigger = str(data.get("trigger", "")).strip().lower()
            launched = int(data.get("launched", 0) or 0)
            running = int(data.get("already_running", 0) or 0)
            failed = int(data.get("failed", 0) or 0)
            timeout = int(data.get("timeout", 0) or 0)
            blocked_self = int(data.get("blocked_self", 0) or 0)
            blocked_radio_control = int(data.get("blocked_radio_control", 0) or 0)
            cancelled = bool(data.get("cancelled", False))
            summary = (
                f"Launch {trigger or 'sequence'} complete: "
                f"launched={launched}, running={running}, failed={failed}, timeout={timeout}, "
                f"blocked={blocked_self}, radio-control-skipped={blocked_radio_control}"
            )
            if cancelled:
                summary = f"{summary}, cancelled=true"
            try:
                self.statusBar().showMessage(summary, 12000)
            except Exception:
                pass
            if self._launch_progress_dialog is not None:
                try:
                    self._launch_progress_dialog.setValue(self._launch_progress_total)
                    self._launch_progress_dialog.close()
                except Exception:
                    pass
                self._launch_progress_dialog = None
            log.info(
                "LaunchControl summary (%s): launched=%s running=%s failed=%s timeout=%s "
                "radio_control_skipped=%s cancelled=%s",
                trigger or "unknown",
                launched,
                running,
                failed,
                timeout,
                blocked_radio_control,
                cancelled,
            )
        except Exception:
            pass

    def _set_window_icon(self):
        asset_roots = []
        bundle_root = str(getattr(sys, "_MEIPASS", "") or "").strip()
        if bundle_root:
            asset_roots.append(Path(bundle_root) / "assets")
        asset_roots.append(Path(__file__).resolve().parents[2] / "assets")
        icon = QIcon()
        if sys.platform == "win32":
            candidates = ["CommsPlan.ico", "CommsPlan-desktop.png", "FreqInOut.ico", "FreqInOut-desktop.png"]
        elif sys.platform == "darwin":
            candidates = ["CommsPlan.icns", "CommsPlan-desktop.png", "CommsPlan.ico", "FreqInOut-desktop.png", "FreqInOut.ico"]
        else:
            candidates = ["CommsPlan-desktop.png", "CommsPlan.ico", "FreqInOut-desktop.png", "FreqInOut.ico"]
        for assets_dir in asset_roots:
            for name in candidates:
                icon_path = assets_dir / name
                if not icon_path.exists():
                    continue
                candidate = QIcon(str(icon_path))
                if candidate.isNull():
                    continue
                icon = candidate
                break
            if not icon.isNull():
                break
        if icon.isNull():
            log.warning("CommsPlan application icon could not be loaded from packaged or source assets.")
            return
        self.setWindowIcon(icon)
        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(icon)

    def _set_logo_pixmap(self):
        if not hasattr(self, "logo_label"):
            return
        theme = resolve_theme(self.settings)
        assets_dir = Path(__file__).resolve().parents[2] / "assets"
        logo_names = (
            ["CommsPlan-dark.png", "CommsPlan_logo.png", "FreqInOut-dark.png", "FreqInOut_logo.png"]
            if theme.get("bg") == "#0F1216"
            else ["CommsPlan_logo.png", "FreqInOut_logo.png"]
        )
        logo_path = next((assets_dir / name for name in logo_names if (assets_dir / name).exists()), None)
        if logo_path is None:
            self.logo_label.clear()
            return
        pix = QPixmap(str(logo_path))
        if pix.isNull():
            self.logo_label.clear()
            return
        pix = pix.scaledToWidth(96, Qt.SmoothTransformation)
        self.logo_label.setPixmap(pix)

    def _load_nav_group_states(self) -> dict[str, bool]:
        # Keep main navigation compact on startup. Screen changes and Quick
        # Search expand the relevant group when navigation intent is explicit.
        defaults = {
            "Messages": False,
            "NCS": False,
            "Operators": False,
            "Resources": False,
            "Plan Builder": False,
            "Station": False,
            "Configuration": False,
        }
        try:
            raw = self.settings.get("main_nav_group_states", {}) or {}
        except Exception:
            raw = {}
        if isinstance(raw, dict):
            # Backward compatibility for prior key name.
            if "FreqPlanner" not in raw and "Schedules" in raw:
                raw["FreqPlanner"] = raw.get("Schedules")
            if "FreqPlanner" not in raw and "Schedule" in raw:
                raw["FreqPlanner"] = raw.get("Schedule")
            if "Plan Builder" not in raw and "FreqPlanner" in raw:
                raw["Plan Builder"] = raw.get("FreqPlanner")
            if "Configuration" not in raw and "Settings" in raw:
                raw["Configuration"] = raw.get("Settings")
            for key in defaults:
                if key in raw:
                    defaults[key] = bool(raw.get(key))
        return defaults

    def _persist_nav_group_states(self) -> None:
        try:
            self.settings.set("main_nav_group_states", dict(self._nav_group_states))
        except Exception:
            pass

    @staticmethod
    def _nav_group_for_label(button_label: str, screen_label: str = "") -> str:
        screen = str(screen_label or "").strip()
        if screen in {"NCS-FLDigi/SSB", "NCS-JS8", "NCS-Local"}:
            return "NCS"
        if screen in {"Station Overview", "Station Health"}:
            return "Station"
        if screen in {"FreqPlanner", "SOP", "HF Schedule", "Net Schedule", "Local Nets", "Peer Schedules"}:
            return "Plan Builder"
        if screen == "Messages":
            return "Messages"
        if screen in {"HF Operators", "Local Operators", "Local Reports", "Call Finder"}:
            return "Operators"
        if screen in {"Resources", "Shortwave"}:
            return "Resources"
        if screen == "Settings":
            return "Configuration"
        txt = str(button_label or "").strip()
        if txt.startswith("NCS -"):
            return "NCS"
        if txt.startswith("Schedule -"):
            return "Plan Builder"
        if txt.startswith("Operators -"):
            return "Operators"
        return ""

    def _nav_group_for_screen_label(self, screen_label: str) -> str:
        return self._nav_group_for_label("", screen_label)

    def _expand_nav_group_for_screen(self, screen_label: str) -> None:
        key = self._nav_group_for_screen_label(screen_label)
        if not key:
            return
        body = self._nav_group_bodies.get(key)
        header = self._nav_group_headers.get(key)
        if body is not None:
            body.setVisible(True)
        if header is not None and not header.isChecked():
            header.blockSignals(True)
            try:
                header.setChecked(True)
            finally:
                header.blockSignals(False)
            self._set_nav_group_header_visual_state(header, True)
        self._nav_group_states[key] = True

    def _ensure_nav_group_layout(self, group_key: str, nav_layout: QVBoxLayout) -> QVBoxLayout:
        key = str(group_key or "").strip()
        existing = self._nav_group_layouts.get(key)
        if existing is not None:
            return existing

        section = QWidget(self.nav_content)
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(2)

        header = QPushButton(section)
        # Keep the stable persisted route key while presenting the operator's
        # broader task language in the navigation rail.
        header.setText("Plans" if key == "Plan Builder" else key)
        header.setCheckable(True)
        expanded = bool(self._nav_group_states.get(key, True))
        header.setChecked(expanded)
        header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._sync_nav_group_header_font(header)
        self._set_nav_group_header_visual_state(header, expanded)
        header.toggled.connect(lambda checked, g=key: self._on_nav_group_toggled(g, checked))
        section_layout.addWidget(header)

        body = QWidget(section)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(8, 0, 0, 0)
        body_layout.setSpacing(3)
        body.setVisible(expanded)
        section_layout.addWidget(body)

        self._nav_group_headers[key] = header
        self._nav_group_bodies[key] = body
        self._nav_group_layouts[key] = body_layout
        self._nav_group_sections[key] = section
        nav_layout.addWidget(section)
        return body_layout

    def _on_nav_group_toggled(self, group_key: str, expanded: bool) -> None:
        key = str(group_key or "").strip()
        body = self._nav_group_bodies.get(key)
        header = self._nav_group_headers.get(key)
        if body is not None:
            body.setVisible(bool(expanded))
        if header is not None:
            self._set_nav_group_header_visual_state(header, expanded)
        self._nav_group_states[key] = bool(expanded)
        self._persist_nav_group_states()
        try:
            self._update_nav_group_header_styles(resolve_theme(self.settings))
        except Exception:
            pass
        self._update_nav_layout_metrics()
        # A group can change the rail width without a window resize. Reflow the
        # cache-only station command bar immediately so compact controls stay
        # reachable rather than waiting for a later resize event.
        try:
            QTimer.singleShot(0, self._reflow_adaptive_station_shell)
        except Exception:
            pass

    def _update_nav_group_header_styles(self, theme: dict) -> None:
        align_style = self._nav_button_alignment_style()
        ncs_tooltip = self._active_ncs_session_tooltip()
        for key in self._nav_group_order:
            header = self._nav_group_headers.get(key)
            if header is None:
                continue
            self._sync_nav_group_header_font(header)
            expanded = bool(self._nav_group_states.get(key, True))
            role = "secondary" if expanded else "muted"
            # If NCS group is collapsed while any net is active, keep an explicit
            # reminder on the accordion header.
            if key == "NCS" and (not expanded) and any(bool(v) for v in self._ncs_net_active.values()):
                role = "warning"
                header.setToolTip(ncs_tooltip)
            elif key == "NCS":
                header.setToolTip(ncs_tooltip)
            if key == "Station" and not expanded:
                issue_count, severity = self._station_health_alert_counts()
                if issue_count > 0:
                    role = "danger" if severity == "danger" else "warning"
                    header.setToolTip(
                        f"Station Health: {issue_count} responsiveness issue"
                        f"{'s' if issue_count != 1 else ''}. Expand Station or open Health Details."
                    )
                else:
                    header.setToolTip("")
            self._set_nav_group_header_visual_state(header, expanded)
            try:
                set_widget_value(header, "styleSheet", button_style(role, theme) + align_style)
            except Exception:
                pass

    def _sync_nav_group_header_font(self, header: QPushButton) -> None:
        try:
            source = self.nav_buttons[0] if getattr(self, "nav_buttons", None) else self
            if header.font() != source.font():
                header.setFont(source.font())
        except Exception:
            pass

    def _set_nav_group_header_visual_state(self, header: QPushButton, expanded: bool) -> None:
        try:
            style = header.style() or QApplication.style()
            if style is not None:
                icon_kind = QStyle.SP_ArrowDown if expanded else QStyle.SP_ArrowRight
                if header.property("fioExpandedIcon") != bool(expanded):
                    header.setIcon(style.standardIcon(icon_kind))
                    header.setProperty("fioExpandedIcon", bool(expanded))
        except Exception:
            pass

    def _group_has_active_nav_context(self, key: str) -> bool:
        key_txt = str(key or "").strip()
        if key_txt == "NCS":
            return any(bool(v) for v in self._ncs_net_active.values())
        if key_txt == "Station":
            issue_count, _severity = self._station_health_alert_counts()
            return issue_count > 0
        return False

    def _auto_collapse_inactive_nav_groups(self) -> None:
        if not hasattr(self, "nav_widget"):
            return
        if not hasattr(self, "status_dock_widget"):
            return
        nav_layout = self.nav_widget.layout()
        if nav_layout is None:
            return
        try:
            margins = nav_layout.contentsMargins()
            avail_h = int(self.nav_widget.height() - margins.top() - margins.bottom())
        except Exception:
            avail_h = int(self.nav_widget.height())
        if avail_h <= 0:
            return
        try:
            logo_h = int(self.logo_label.sizeHint().height()) if self.logo_label.isVisible() else 0
        except Exception:
            logo_h = 0
        try:
            status_h = int(self.status_dock_widget.sizeHint().height())
        except Exception:
            status_h = 0
        spacing = int(nav_layout.spacing()) if nav_layout is not None else 0
        # Keep a minimal nav-scroll footprint so status cards can remain visible.
        min_nav_zone_h = 24
        required_h = logo_h + status_h + min_nav_zone_h + (spacing * 2)
        if avail_h >= required_h:
            return

        # Collapse expanded groups that are currently inactive until status
        # sections can remain fully visible.
        changed = False
        collapse_order = [k for k in self._nav_group_order if k != "NCS"] + ["NCS"]
        for key in collapse_order:
            if avail_h >= required_h:
                break
            if not bool(self._nav_group_states.get(key, False)):
                continue
            if self._group_has_active_nav_context(key):
                continue
            header = self._nav_group_headers.get(key)
            if header is None:
                continue
            header.blockSignals(True)
            try:
                header.setChecked(False)
            finally:
                header.blockSignals(False)
            body = self._nav_group_bodies.get(key)
            if body is not None:
                body.setVisible(False)
            header.setArrowType(Qt.RightArrow)
            self._nav_group_states[key] = False
            changed = True
            try:
                status_h = int(self.status_dock_widget.sizeHint().height())
            except Exception:
                pass
            required_h = logo_h + status_h + min_nav_zone_h + (spacing * 2)
        if changed:
            try:
                self._update_nav_group_header_styles(resolve_theme(self.settings))
            except Exception:
                pass

    def _style_condition_levels_edit_action(self, theme: dict) -> None:
        if not hasattr(self, "condition_levels_edit_btn"):
            return
        normal = theme.get("accent", theme.get("info", "#1E88E5"))
        hover = theme.get("info", normal)
        self.condition_levels_edit_btn.setStyleSheet(
            "QToolButton {"
            f"color: {normal}; border: none; background: transparent; padding: 0px; text-align: left; "
            "text-decoration: underline;"
            "}"
            "QToolButton:hover {"
            f"color: {hover};"
            "}"
        )

    @staticmethod
    def _condition_level_palette(level: int) -> tuple[str, str]:
        palette = {
            1: ("#C62828", "#FFFFFF"),  # Red
            2: ("#EF6C00", "#111111"),  # Orange
            3: ("#F9A825", "#111111"),  # Yellow
            4: ("#1565C0", "#FFFFFF"),  # Blue
            5: ("#2E7D32", "#FFFFFF"),  # Green
        }
        return palette.get(int(level), ("#455A64", "#FFFFFF"))

    def _collect_condition_levels(self) -> list[tuple[str, int]]:
        try:
            rows = self.settings.get("operating_groups", []) or []
        except Exception:
            rows = []
        if not isinstance(rows, list):
            return []
        by_group: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            group = str(row.get("group", "") or "").strip().upper()
            if not group:
                continue
            if not bool(row.get("use_condition_levels", False)):
                continue
            try:
                level = int(row.get("condition_level", 0) or 0)
            except Exception:
                level = 0
            if level < 1 or level > 5:
                continue
            prev = by_group.get(group)
            if prev is None or level < prev:
                by_group[group] = level
        out = sorted(by_group.items(), key=lambda x: x[0])
        return [(g, lvl) for g, lvl in out]

    def _condition_sop_automation_status(self) -> tuple[str, str]:
        db_path = get_config_dir() / "config" / "freqinout_nets.db"
        cache = getattr(self, "_condition_audit_read_cache", None)
        if cache is None:
            cache = self._condition_audit_read_cache = LatestReadCache("fio-condition-audit")
        def reader_factory():
            def read():
                if not db_path.exists():
                    return "", "none"
                display = condition_sop_audit_display(condition_sop_audit_summary(db_path, limit=10))
                return display.text, display.severity
            return read
        return cache.read(str(db_path), reader_factory, refresh_after_s=5.0) or ("", "none")

    def _style_condition_sop_automation_label(self, severity: str) -> None:
        label = getattr(self, "condition_sop_automation_label", None)
        if label is None:
            return
        theme = resolve_theme(self.settings)
        sev = str(severity or "none").strip().lower()
        if sev == "warning":
            border = theme.get("warning", "#C99700")
            bg = theme.get("warning_bg", "#FFF4CC")
        elif sev == "ok":
            border = theme.get("success", "#2E7D32")
            bg = theme.get("success_bg", "#DFF3E3")
        elif sev == "review":
            border = theme.get("accent", "#2a6fd3")
            bg = theme.get("surface", "#FFFFFF")
        else:
            border = theme.get("border", "#CCCCCC")
            bg = theme.get("surface", "#FFFFFF")
        text = theme.get("text", "#222222")
        label.setStyleSheet(
            "QLabel {"
            f" background-color: {bg}; color: {text}; border: 1px solid {border};"
            " border-radius: 6px; padding: 3px 6px;"
            "}"
        )

    def _clear_layout_widgets(self, layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _refresh_condition_level_panel(self) -> None:
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty("condition_level_panel")
            return
        if not hasattr(self, "condition_levels_rows_layout"):
            return
        adaptive_shell = bool(getattr(self, "_adaptive_station_shell_enabled", False))
        if adaptive_shell and hasattr(self, "condition_level_container"):
            self.condition_level_container.setVisible(False)
        levels = self._collect_condition_levels()
        audit_text, audit_severity = self._condition_sop_automation_status()
        audit_signature = (audit_text, audit_severity)
        if not levels:
            self._condition_levels_signature = tuple()
            self._clear_layout_widgets(self.condition_levels_rows_layout)
            if hasattr(self, "condition_sop_automation_label"):
                self.condition_sop_automation_label.setVisible(False)
            if hasattr(self, "condition_level_container"):
                self.condition_level_container.setVisible(False)
            if adaptive_shell:
                self._station_command_radio_summary_signature = None
                self._schedule_station_command_bar_refresh("condition_levels", force=True)
            self._auto_collapse_inactive_nav_groups()
            return
        if hasattr(self, "condition_level_container"):
            self.condition_level_container.setVisible(not adaptive_shell)
        signature = tuple((g, int(level)) for g, level in levels) + (("__audit__", audit_signature),)
        if signature == getattr(self, "_condition_levels_signature", tuple()):
            # If signature is unchanged, still verify rows are rendered as button widgets.
            # This prevents stale row formats from persisting across iterative UI updates.
            rows_current = True
            try:
                if self.condition_levels_rows_layout.count() != len(levels):
                    rows_current = False
                else:
                    for i in range(self.condition_levels_rows_layout.count()):
                        item = self.condition_levels_rows_layout.itemAt(i)
                        row_widget = item.widget() if item is not None else None
                        if row_widget is None:
                            rows_current = False
                            break
                        chips = row_widget.findChildren(QPushButton)
                        # Current format is exactly one button row and no standalone labels.
                        labels = row_widget.findChildren(QLabel)
                        if len(chips) != 1:
                            rows_current = False
                            break
                        if labels:
                            rows_current = False
                            break
            except Exception:
                rows_current = False
            if rows_current:
                return
        self._condition_levels_signature = signature
        self._clear_layout_widgets(self.condition_levels_rows_layout)
        for group, level in levels:
            row = QWidget(self.condition_levels_rows)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            chip = QPushButton(f"{group}  Level {level}")
            chip.setObjectName("conditionLevelChip")
            bg, fg = self._condition_level_palette(level)
            chip.setCheckable(False)
            chip.setEnabled(True)
            chip.setFocusPolicy(Qt.NoFocus)
            chip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            chip.setStyleSheet(
                f"QPushButton#conditionLevelChip {{"
                f" font-weight: 600; border-radius: 6px; padding: 2px 8px;"
                f" text-align: left; background-color: {bg}; color: {fg}; border: 1px solid {bg};"
                f"}}"
                f"QPushButton#conditionLevelChip:hover {{ background-color: {bg}; color: {fg}; border: 1px solid {bg}; }}"
                f"QPushButton#conditionLevelChip:pressed {{ background-color: {bg}; color: {fg}; border: 1px solid {bg}; }}"
            )
            row_layout.addWidget(chip, 1)
            self.condition_levels_rows_layout.addWidget(row)
        if hasattr(self, "condition_sop_automation_label"):
            self.condition_sop_automation_label.setText(audit_text)
            self.condition_sop_automation_label.setVisible(bool(audit_text))
            self._style_condition_sop_automation_label(audit_severity)
        if adaptive_shell:
            self._station_command_radio_summary_signature = None
            self._schedule_station_command_bar_refresh("condition_levels", force=True)
        self._auto_collapse_inactive_nav_groups()

    def _open_condition_levels_editor(self) -> None:
        groups_data = self.settings.get("operating_groups", []) or []
        if not isinstance(groups_data, list) or not groups_data:
            QMessageBox.information(self, "Condition Levels", "No HF Operating Groups found.")
            return
        grouped: dict[str, dict[str, object]] = {}
        for row in groups_data:
            if not isinstance(row, dict):
                continue
            group = str(row.get("group", "") or "").strip().upper()
            if not group:
                continue
            use_level = bool(row.get("use_condition_levels", False))
            try:
                level = int(row.get("condition_level", 5) or 5)
            except Exception:
                level = 5
            if level < 1 or level > 5:
                level = 5
            if group not in grouped:
                grouped[group] = {"use": use_level, "level": level}
            else:
                grouped[group]["use"] = bool(grouped[group].get("use")) or use_level
                grouped[group]["level"] = min(int(grouped[group].get("level", level)), level)
        if not grouped:
            QMessageBox.information(self, "Condition Levels", "No HF Operating Groups found.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Condition Levels")
        dlg_layout = QVBoxLayout(dlg)
        rows_holder = QWidget(dlg)
        rows_layout = QVBoxLayout(rows_holder)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(6)
        editors: dict[str, tuple[QCheckBox, QComboBox]] = {}
        for group in sorted(grouped.keys()):
            row_meta = grouped[group]
            row = QWidget(rows_holder)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            group_lbl = QLabel(group)
            group_lbl.setMinimumWidth(120)
            use_chk = QCheckBox("Use Condition Levels")
            use_chk.setChecked(bool(row_meta.get("use", False)))
            level_combo = QComboBox()
            for n in range(1, 6):
                level_combo.addItem(str(n), n)
            level_combo.setCurrentText(str(int(row_meta.get("level", 5))))
            level_combo.setEnabled(use_chk.isChecked())
            use_chk.toggled.connect(level_combo.setEnabled)
            row_layout.addWidget(group_lbl)
            row_layout.addWidget(use_chk)
            row_layout.addWidget(QLabel("Level:"))
            row_layout.addWidget(level_combo)
            row_layout.addStretch(1)
            rows_layout.addWidget(row)
            editors[group] = (use_chk, level_combo)
        dlg_layout.addWidget(rows_holder)
        buttons = QHBoxLayout()
        save_btn = QPushButton("Save")
        cancel_btn = QPushButton("Cancel")
        buttons.addStretch(1)
        buttons.addWidget(save_btn)
        buttons.addWidget(cancel_btn)
        dlg_layout.addLayout(buttons)

        def _save() -> None:
            updated: list[dict] = []
            for raw in groups_data:
                if not isinstance(raw, dict):
                    continue
                row = dict(raw)
                group = str(row.get("group", "") or "").strip().upper()
                editor = editors.get(group)
                if editor is not None:
                    use_chk, level_combo = editor
                    row["use_condition_levels"] = bool(use_chk.isChecked())
                    try:
                        level = int(level_combo.currentData() or level_combo.currentText() or 5)
                    except Exception:
                        level = 5
                    if level < 1 or level > 5:
                        level = 5
                    row["condition_level"] = level
                updated.append(row)
            try:
                self.settings.set("operating_groups", updated)
                self.settings.reload()
            except Exception as e:
                QMessageBox.warning(self, "Condition Levels", f"Failed to save condition levels:\n{e}")
                return
            try:
                if hasattr(self, "settings_tab") and self.settings_tab is not None:
                    self.settings_tab.operating_groups = [dict(r) for r in updated if isinstance(r, dict)]
                    if hasattr(self.settings_tab, "_refresh_operating_groups_table"):
                        self.settings_tab._refresh_operating_groups_table()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._refresh_condition_level_panel()
            dlg.accept()
            QTimer.singleShot(0, self._queue_condition_levels_changed)

        save_btn.clicked.connect(_save)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()

    def _style_ledge_clock(self, theme: dict[str, str] | None = None) -> None:
        if not hasattr(self, "ledge_clock_widget"):
            return
        palette = theme or resolve_theme(self.settings)
        border = palette.get("border", "#D0D7DE")
        panel = palette.get("surface", palette.get("bg", "#FFFFFF"))
        text = palette.get("text", "#202124")
        muted = palette.get("text_muted", "#5F6368")
        self.ledge_clock_widget.setStyleSheet(
            f"""
            QFrame#mainLedgeClock {{
                background: {panel};
                border: 1px solid {border};
                border-radius: 4px;
            }}
            QLabel#ledgeLocalTime {{
                color: {text};
                font-weight: 600;
            }}
            QLabel#ledgeUtcTime {{
                color: {muted};
                font-weight: 600;
            }}
            """
        )

    def _set_screen(self, index: int) -> None:
        with perf_span(
            "main_window.set_screen",
            settings=self.settings,
            meta={"index": index},
            min_ms=5.0,
        ):
            if 0 <= index < self.stack.count():
                label = self._screens[index][0]
                if self._screen_is_runtime_suppressed(label):
                    fallback_index = self._runtime_fallback_screen_index()
                    if fallback_index != index:
                        self._set_screen(fallback_index)
                    return
                if label == "Map":
                    self._open_map_window()
                    self._restore_nav_selection_to_active_tab()
                    self._sync_compact_navigation_selection(self._current_screen_label())
                    return
                try:
                    if label == "Settings":
                        nav_idx = self._settings_nav_button_indices.get(
                            str(getattr(self, "_settings_nav_context", "main") or "main")
                        )
                    elif label == "Messages":
                        nav_idx = self._messages_nav_button_indices.get(
                            str(getattr(self, "_messages_nav_context", "inbox") or "inbox")
                        )
                    elif label == "Resources":
                        nav_idx = self._resources_nav_button_indices.get(
                            str(getattr(self, "_resources_nav_context", "frequency_catalog") or "frequency_catalog")
                        )
                    else:
                        nav_idx = self._nav_screen_index_map.get(index)
                    if bool(getattr(self, "_suppress_initial_nav_group_auto_expand", False)):
                        self._suppress_initial_nav_group_auto_expand = False
                    else:
                        self._expand_nav_group_for_screen(label)
                    if nav_idx is not None and 0 <= nav_idx < len(self.nav_buttons):
                        btn = self.nav_buttons[nav_idx]
                        if not btn.isChecked():
                            btn.setChecked(True)
                except Exception:
                    pass
                prev_index = self._active_tab_index
                if prev_index is not None and 0 <= prev_index < self.stack.count():
                    try:
                        prev_widget = self.stack.widget(prev_index)
                        if hasattr(prev_widget, "set_tab_active"):
                            prev_widget.set_tab_active(False)
                    except Exception:
                        pass

                self._ensure_lazy_tab_loaded(label, index)
                self._navigation_epoch = int(getattr(self, "_navigation_epoch", 0)) + 1
                navigation_epoch = self._navigation_epoch
                self.stack.setCurrentIndex(index)
                self._active_tab_index = index
                self._sync_compact_navigation_selection(label)
                try:
                    fit_child_combo_boxes(self.stack.widget(index))
                except Exception:
                    pass
                try:
                    widget_active = self.stack.widget(index)
                    if label == "Messages":
                        self._apply_messages_nav_context()
                    elif label == "Resources" and hasattr(widget_active, "open_section"):
                        widget_active.open_section(self._resources_nav_context)
                    if hasattr(widget_active, "set_tab_active"):
                        widget_active.set_tab_active(True)
                    if label == "SOP" and bool(getattr(self, "_sop_settings_refresh_pending", False)):
                        self._sop_settings_refresh_pending = False
                        if hasattr(widget_active, "on_settings_saved"):
                            QTimer.singleShot(
                                0,
                                lambda target=widget_active: self._run_timed_ui_refresh(
                                    "settings_saved.sop.activate", target.on_settings_saved
                                ),
                            )
                except Exception:
                    pass
                self._update_map_filters_visibility(index)
                self._update_ncs_nav_button_styles()
                QTimer.singleShot(0, self._schedule_status_refresh)
                try:
                    widget = self.stack.widget(index)
                    if hasattr(widget, "show_loading_toast"):
                        widget.show_loading_toast()
                    if hasattr(widget, "on_tab_activated"):
                        QTimer.singleShot(
                            0,
                            lambda target=widget, expected=index, epoch=navigation_epoch: self._run_if_screen_current(
                                expected,
                                epoch,
                                target.on_tab_activated,
                            ),
                        )
                except Exception:
                    pass
                QTimer.singleShot(
                    0,
                    lambda expected=index, epoch=navigation_epoch: self._settle_active_screen_layout(
                        expected,
                        epoch,
                    ),
                )

    def _on_ncs_net_status_changed(self, kind: str, active: bool) -> None:
        kind_key = (kind or "").strip().upper()
        if kind_key in self._ncs_net_active:
            self._ncs_net_active[kind_key] = bool(active)
        self._update_ncs_nav_button_styles()
        self._refresh_scheduler_status_panel()

    def _on_ncs_scoped_net_status_changed(
        self, kind: str, active: bool, radio_id: int, session_id: str
    ) -> None:
        kind_key = (kind or "").strip().upper()
        try:
            if kind_key in {"FLDIGI", "JS8"} and int(radio_id or 0) > 0 and hasattr(self, "scheduler"):
                if active:
                    self._dismiss_radio_off_schedule_prompt(int(radio_id))
                self.scheduler.set_manual_net_active(
                    kind_key,
                    bool(active),
                    radio_id=int(radio_id or 0) or None,
                    session_id=str(session_id or ""),
                )
        except Exception:
            pass

    def _active_ncs_session_flags_from_settings(self) -> dict[str, bool]:
        try:
            return active_ncs_session_flags(self.settings)
        except Exception as exc:
            log.debug("Main shell: failed to read NCS session snapshots: %s", exc)
            return {"FLDIGI": False, "JS8": False, "LOCAL": False}

    def _clear_stale_ncs_activity_on_startup(self) -> None:
        try:
            cleared = clear_persisted_active_ncs_sessions(self.settings, timing_state="interrupted")
            if cleared:
                log.info("Main shell: cleared %s stale persisted NCS session snapshot(s) on startup.", cleared)
        except Exception as exc:
            log.debug("Main shell: failed to clear stale NCS session snapshots on startup: %s", exc)

    def _refresh_ncs_activity_from_snapshots(self) -> None:
        try:
            snapshot_flags = active_ncs_session_flags(self.settings)
        except Exception as exc:
            log.debug("Main shell: failed to refresh NCS session snapshots: %s", exc)
            return
        for kind_key, active in snapshot_flags.items():
            if kind_key in self._ncs_net_active:
                self._ncs_net_active[kind_key] = bool(active)

    def _active_ncs_session_tooltip(self, kind: str | None = None) -> str:
        try:
            by_kind = active_ncs_session_summaries_by_kind(self.settings)
        except Exception as exc:
            log.debug("Main shell: failed to read NCS session summaries: %s", exc)
            by_kind = {}
        if kind:
            summaries = list(by_kind.get(str(kind or "").strip().upper(), []))
        else:
            summaries = [label for labels in by_kind.values() for label in labels]
        if not summaries:
            return ""
        visible = summaries[:6]
        suffix = "" if len(summaries) <= len(visible) else f"\n...and {len(summaries) - len(visible)} more"
        plural = "s" if len(summaries) != 1 else ""
        return f"Active NCS session{plural}:\n" + "\n".join(visible) + suffix

    @staticmethod
    def _nav_button_alignment_style() -> str:
        return (
            "QPushButton { text-align: left; padding-left: 12px; }"
            "QToolButton { text-align: left; padding-left: 12px; }"
        )

    def _update_ncs_nav_button_styles(self) -> None:
        if not getattr(self, "nav_buttons", None):
            return
        try:
            theme = resolve_theme(self.settings)
        except Exception:
            theme = {}
        align_style = self._nav_button_alignment_style()
        ncs_by_index = {idx: kind for kind, idx in self._ncs_nav_indices.items()}
        health_index = getattr(self, "_station_health_nav_index", None)
        issue_count, severity = self._station_health_alert_counts()
        ncs_tooltips = {}
        for kind in ncs_by_index.values():
            try:
                ncs_tooltips[kind] = self._active_ncs_session_tooltip(kind)
            except Exception:
                ncs_tooltips[kind] = ""
        try:
            aggregate_ncs_tooltip = self._active_ncs_session_tooltip()
        except Exception:
            aggregate_ncs_tooltip = ""

        def font_signature(widget: object | None) -> tuple[object, ...] | None:
            if widget is None:
                return None
            try:
                font = widget.font()
                metrics = widget.fontMetrics()
                return (
                    font.toString(),
                    int(metrics.height()),
                    int(metrics.lineSpacing()),
                    int(metrics.horizontalAdvance("M")),
                )
            except Exception:
                return None

        nav_group_headers = getattr(self, "_nav_group_headers", {})
        if not isinstance(nav_group_headers, Mapping):
            nav_group_headers = {}
        nav_group_states = getattr(self, "_nav_group_states", {})
        if not isinstance(nav_group_states, Mapping):
            nav_group_states = {}
        nav_group_order = tuple(str(key) for key in (getattr(self, "_nav_group_order", ()) or ()))
        nav_group_signature = tuple(
            (
                str(key),
                id(header),
                bool(nav_group_states.get(key, True)),
                font_signature(header),
            )
            for key, header in sorted(nav_group_headers.items(), key=lambda item: str(item[0]))
        )
        theme_signature = tuple(sorted((str(key), repr(value)) for key, value in theme.items()))
        signature = (
            theme_signature,
            align_style,
            tuple((id(btn), bool(btn.isChecked())) for btn in self.nav_buttons),
            font_signature(self.nav_buttons[0] if self.nav_buttons else self),
            tuple(sorted((int(index), str(kind)) for index, kind in ncs_by_index.items())),
            tuple(sorted((str(kind), bool(self._ncs_net_active.get(kind)), str(tip))
                         for kind, tip in ncs_tooltips.items())),
            str(aggregate_ncs_tooltip),
            nav_group_order,
            tuple(sorted((str(key), bool(value)) for key, value in nav_group_states.items())),
            nav_group_signature,
            health_index,
            issue_count,
            severity,
        )
        if signature == getattr(self, "_ncs_nav_projection_signature", None):
            return

        self._update_nav_group_header_styles(theme)
        # Resolve overlays before publication: resetting a warning to base and
        # back on every health tick repolishes the entire sidebar twice.
        for idx, btn in enumerate(self.nav_buttons):
            role = "primary" if btn.isChecked() else "muted"
            kind = ncs_by_index.get(idx)
            try:
                if kind is not None:
                    set_widget_value(btn, "toolTip", ncs_tooltips.get(kind, ""))
                    if bool(self._ncs_net_active.get(kind)):
                        role = "warning"
                if idx == health_index and issue_count > 0:
                    role = "danger" if severity == "danger" else "warning"
                set_widget_value(btn, "styleSheet", button_style(role, theme) + align_style)
            except Exception:
                pass
        self._apply_station_health_nav_alert(theme, align_style)
        self._ncs_nav_projection_signature = signature

    def _station_health_nav_label(self, issue_count: int = 0) -> str:
        idx = getattr(self, "_station_health_nav_index", None)
        if idx is None or idx < 0 or idx >= len(getattr(self, "_nav_base_labels", [])):
            return "Station Health"
        label = str(self._nav_base_labels[idx] or "Station Health")
        try:
            callsign = str(self.settings.get("callsign", "") or "").strip().upper()
        except Exception:
            callsign = ""
        if callsign:
            label = f"{label} [{callsign}]"
        if issue_count > 0:
            label = f"{label} ({issue_count})"
        return label

    def _station_health_runtime_items(self) -> list[Mapping[str, object]]:
        return self._station_health_extra_items(
            include_assigned_schedules=True,
            include_runtime_sources=True,
            include_sop_audit=True,
        )

    def _station_health_extra_items(
        self,
        *,
        include_assigned_schedules: bool = True,
        include_runtime_sources: bool = True,
        include_sop_audit: bool = True,
    ) -> list[Mapping[str, object]]:
        items: list[Mapping[str, object]] = []
        station_poll_metrics = None
        scheduler_poll_metrics = None
        scheduler_companion_status = None
        background_job_status = None
        assigned_schedule_status = []
        runtime_source_rows = []
        try:
            manager = getattr(self, "station_runtime_manager", None)
            if manager is not None and hasattr(manager, "get_status_poll_metrics"):
                station_poll_metrics = manager.get_status_poll_metrics()
        except Exception:
            station_poll_metrics = None
        try:
            scheduler = getattr(self, "scheduler", None)
            if scheduler is not None and hasattr(scheduler, "get_status_poll_metrics"):
                scheduler_poll_metrics = scheduler.get_status_poll_metrics()
            if scheduler is not None and hasattr(scheduler, "get_status_summary"):
                scheduler_companion_status = scheduler.get_status_summary(live=False, refresh=False)
        except Exception:
            scheduler_poll_metrics = None
            scheduler_companion_status = None
        try:
            background = getattr(self, "background_ingest", None)
            if background is not None and hasattr(background, "job_status_snapshot"):
                background_job_status = background.job_status_snapshot()
        except Exception:
            background_job_status = None
        try:
            js8_registry_status = JS8ApiClientRegistry.status_dicts()
        except Exception:
            js8_registry_status = []
        if include_assigned_schedules:
            assigned_schedule_status = self._station_health_assigned_schedule_status_rows()
        if include_runtime_sources:
            runtime_source_rows = self._station_health_runtime_source_rows()
        items.extend(
            runtime_observability_items(
                station_poll_metrics=station_poll_metrics,
                scheduler_poll_metrics=scheduler_poll_metrics,
                scheduler_companion_status=scheduler_companion_status,
                assigned_schedule_status=assigned_schedule_status,
                background_job_status=background_job_status,
                js8_registry_status=js8_registry_status,
                runtime_source_rows=runtime_source_rows,
            )
        )
        if include_sop_audit:
            try:
                sop_audit_item = condition_sop_audit_observability_item(get_config_dir() / "config" / "freqinout_nets.db")
            except Exception:
                sop_audit_item = None
            if sop_audit_item:
                items.append(sop_audit_item)
        return items

    def _station_health_alert_extra_items(self) -> list[Mapping[str, object]]:
        return self._station_health_extra_items(
            include_assigned_schedules=False,
            include_runtime_sources=False,
            include_sop_audit=False,
        )

    def _station_health_assigned_schedule_status_rows(self) -> list[Mapping[str, object]]:
        try:
            store = getattr(self, "multi_radio_store", None) or MultiRadioStore()
            assignments = [dict(row) for row in store.list_effective_assigned_plans()]
            if not assignments:
                return []
            devices = {
                int(row.get("id", 0) or 0): dict(row)
                for row in store.list_device_profiles()
                if isinstance(row, Mapping)
            }
            plans = {
                int(row.get("id", 0) or 0): dict(row)
                for row in store.list_frequency_plans()
                if isinstance(row, Mapping)
            }
        except Exception:
            return []
        rows: list[Mapping[str, object]] = []
        for assignment in assignments:
            try:
                device_id = int(assignment.get("device_profile_id", 0) or 0)
                plan_id = int(assignment.get("frequency_plan_id", 0) or 0)
            except Exception:
                continue
            device = devices.get(device_id, {})
            plan = plans.get(plan_id, {})
            row = dict(assignment)
            row["device_name"] = str(device.get("name") or row.get("device_name") or f"Radio {device_id}")
            row["frequency_plan_name"] = str(plan.get("name") or row.get("frequency_plan_name") or f"Frequency Plan {plan_id}")
            rows.append(row)
        return rows

    def _station_health_runtime_source_rows(self) -> list[Mapping[str, object]]:
        rows: list[dict[str, object]] = []
        try:
            rows = [dict(getattr(row, "__dict__", {}) or {}) for row in active_runtime_source_view_rows()]
        except Exception:
            rows = []
        known_source_ids = {str(row.get("source_id", "") or "").strip() for row in rows}
        try:
            background = getattr(self, "background_ingest", None)
            status = background.job_status_snapshot() if background is not None and hasattr(background, "job_status_snapshot") else {}
            source_skips = status.get("source_skip_reasons", {}) if isinstance(status, Mapping) else {}
            if isinstance(source_skips, Mapping) and source_skips:
                for row in runtime_source_view_rows_from_skip_reasons(source_skips):
                    payload = dict(getattr(row, "__dict__", {}) or {})
                    source_id = str(payload.get("source_id", "") or "").strip()
                    if source_id and source_id in known_source_ids:
                        continue
                    rows.append(payload)
                    if source_id:
                        known_source_ids.add(source_id)
        except Exception:
            pass
        for row in rows:
            metadata = dict(row["metadata"]) if isinstance(row.get("metadata"), Mapping) else {}
            radio_id = row.get("radio_id")
            row["radio_scope"] = (self._station_health_scope_resolver("ingest:source", {"device_profile_id": radio_id})
                                  if radio_id else "Shared" if metadata.get("attribution") == "shared" else "Unattributed")
            candidates = metadata.get("candidate_radio_ids", ())
            metadata["candidate_radio_names"] = tuple(
                self._station_health_scope_resolver("ingest:source", {"device_profile_id": value})
                for value in candidates
            )
            row["metadata"] = metadata
        return rows

    @staticmethod
    def _station_health_alert_source_signature(
        registry_snapshot: Mapping[str, object],
        extra_items: Sequence[Mapping[str, object]],
        *,
        now_monotonic: float | None = None,
    ) -> tuple[object, ...]:
        """Stable key for health inputs, including time-based freshness edges."""
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)

        def number(value: object) -> float:
            try:
                return float(value or 0.0)
            except (TypeError, ValueError):
                return 0.0

        registry_rows = []
        for key, raw in sorted(registry_snapshot.items(), key=lambda pair: str(pair[0])):
            if not isinstance(raw, Mapping):
                continue
            try:
                failures = int(raw.get("consecutive_failures", 0) or 0)
                slow = int(raw.get("consecutive_slow", 0) or 0)
                last_checked = float(raw.get("last_checked_ts", 0.0) or 0.0)
                cooldown_active = float(raw.get("cooldown_remaining_sec", 0.0) or 0.0) > 0.0
            except (TypeError, ValueError):
                failures, slow, last_checked, cooldown_active = 0, 0, 0.0, False
            service = str(key or "").split(":", 1)[0].strip().lower().replace("-", "_")
            stale_ok = (
                service != "scheduler"
                and last_checked > 0.0
                and failures <= 0
                and slow <= 0
                and now - last_checked > 600.0
            )
            expired_transient = (
                str(key or "").strip().lower().replace("_", "-") == "scheduler:fldigi-busy-check"
                and last_checked > 0.0
                and now - last_checked > 300.0
            )
            registry_rows.append(
                (
                    str(key),
                    str(raw.get("owner", "") or ""),
                    failures,
                    slow,
                    number(raw.get("last_success_ts")),
                    number(raw.get("last_failure_ts")),
                    number(raw.get("issue_started_ts")),
                    last_checked,
                    number(raw.get("last_duration_ms")),
                    cooldown_active,
                    stale_ok,
                    expired_transient,
                    str(raw.get("last_error", "") or ""),
                    repr(raw.get("metadata", {})),
                )
            )
        return tuple(registry_rows), tuple(repr(dict(item)) for item in extra_items)

    def _refresh_station_health_alert(self) -> None:
        if not self._ui_refresh_allowed():
            self._mark_ui_refresh_dirty("station_health_alert")
            return
        try:
            registry_snapshot = get_dependency_health_registry().snapshot()
            extra_items = self._station_health_alert_extra_items()
            input_signature = self._station_health_alert_source_signature(
                registry_snapshot if isinstance(registry_snapshot, Mapping) else {},
                extra_items,
            )
            if input_signature == getattr(self, "_station_health_alert_inputs_signature", None):
                # The cached summary already represents the same source state.
                # Still run the nav projection guard so theme/selection changes
                # can publish independently of health input changes.
                self._update_ncs_nav_button_styles()
                return
        except Exception:
            registry_snapshot = {}
            extra_items = []
            input_signature = None
        try:
            summary = summarize_station_health(
                registry_snapshot=registry_snapshot,
                include_ok=False,
                extra_items=extra_items,
            )
        except Exception:
            summary = {"issue_count": 0, "severity": "ok", "issue_items": []}
            input_signature = None
        if input_signature is not None:
            self._station_health_alert_inputs_signature = input_signature
        issue_count = int(summary.get("issue_count", 0) or 0)
        severity = str(summary.get("severity", "ok") or "ok")
        signature = (issue_count, severity)
        if signature != getattr(self, "_station_health_alert_signature", None):
            self._station_health_alert_signature = signature
            idx = getattr(self, "_station_health_nav_index", None)
            if idx is not None and 0 <= idx < len(getattr(self, "nav_buttons", [])):
                try:
                    self.nav_buttons[idx].setText(self._station_health_nav_label(issue_count))
                    self._update_nav_layout_metrics()
                except Exception:
                    pass
        self._station_health_alert_summary = dict(summary)
        self._update_ncs_nav_button_styles()

    def _station_health_alert_counts(self) -> tuple[int, str]:
        try:
            summary = getattr(self, "_station_health_alert_summary", None)
            if not isinstance(summary, dict):
                summary = summarize_station_health(include_ok=False, extra_items=self._station_health_alert_extra_items())
            issue_count = int(summary.get("issue_count", 0) or 0)
            severity = str(summary.get("severity", "ok") or "ok")
            return issue_count, severity
        except Exception:
            return 0, "ok"

    def _apply_station_health_nav_alert(self, theme: dict[str, str], align_style: str) -> None:
        idx = getattr(self, "_station_health_nav_index", None)
        if idx is None or idx < 0 or idx >= len(getattr(self, "nav_buttons", [])):
            return
        btn = self.nav_buttons[idx]
        try:
            issue_count, severity = self._station_health_alert_counts()
            if issue_count <= 0:
                btn.setToolTip("Station Health: no known external software responsiveness issues.")
                return
            role = "danger" if severity == "danger" else "warning"
            set_widget_value(btn, "styleSheet", button_style(role, theme) + align_style)
            btn.setToolTip(
                f"Station Health: {issue_count} responsiveness issue"
                f"{'s' if issue_count != 1 else ''}. Open Station Health for details."
            )
        except Exception:
            pass
