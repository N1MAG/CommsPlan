"""Detached map/inspector read snapshots and bounded latest-request lanes.

Only copied values cross this boundary. The adapter deliberately reuses the
existing domain builders through an explicit allowlist, never a QWidget or its
bound methods. Unknown dependencies fail closed instead of reaching the GUI.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import time
import datetime
import weakref

from PySide6.QtCore import QObject, Signal

from freqinout.core.config_paths import get_config_dir
from freqinout.core.map_read_budget import map_read_budget
from freqinout.core.sqlite_utils import connect_sqlite_readonly

# Values only: no settings manager, Qt object, connection, executor or host.
INPUT_FIELDS = (
    "_app_active",
    "_initial_data_loaded",
    "_last_map_auto_fit_sig",
    "_last_map_config",
    "_last_map_payload_sig",
    "_last_map_render_input_sig",
    "_last_map_view",
    "_last_prop_region_filter",
    "_map_dirty",
    "_map_initialized",
    "_map_last_link_all_time_count",
    "_map_last_link_missing_position_rows",
    "_map_last_link_source_rows",
    "_map_link_count",
    "_map_link_display_limited_count",
    "_map_link_loaded_count",
    "_map_link_status_detail",
    "_map_marker_count",
    "_map_page_loading",
    "_map_read_cache_revision",
    "_map_selected_payload",
    "_map_visible",
    "_mesh_nodes_auto_fit_pending",
    "_now_reachable_callsigns",
    "_now_reachable_enabled",
    "_now_reachable_meta",
    "_paths_focus_station",
    "_observation_focus_enabled",
    "_observation_focus_mode",
    "_operator_groups",
    "_operator_regions",
    "_render_requested_during_load",
    "_sitrep_report_groups",
    "_sitrep_status_only_enabled",
    "_stations_revision",
    "city_pop_min",
    "link_mode",
    "link_value",
    "operator_index",
    "operator_rows",
    "prop_adaptive_enabled",
    "prop_overlay_enabled",
    "prop_window_hours",
    "recency_seconds",
    "relay_target",
    "show_alert_reports",
    "show_callsigns",
    "show_city_labels",
    "show_grid_labels",
    "show_grids",
    "show_infrastructure_reports",
    "show_link_paths",
    "show_regions",
    "show_states",
    "show_station_markers",
    "show_weather_reports",
    "stations",
)
SELECTORS = (
    "_current_link_selection",
    "_current_scheduler_frequency_for_map",
    "_current_map_mode_key",
    "_effective_map_observation_focus_enabled",
    "_effective_map_observation_focus_mode",
    "_effective_map_report_focus_mode",
    "_effective_prop_mode",
    "_implicit_map_observation_focus_enabled",
    "_map_advanced_scope_filter",
    "_map_advanced_source_filter",
    "_map_advanced_state_filter",
    "_map_advanced_status_filter",
    "_map_advanced_trust_filter",
    "_selected_map_group_filter",
    "_selected_map_intel_sensitivity",
    "_selected_map_region_filter",
    "_selected_map_search_text",
    "_selected_map_topic_filter",
)
# Audited dependency closure of map payload and inspector domain builders.
DOMAIN_METHODS = frozenset("""
_active_sitrep_status_key
_adaptive_adjustment
_adaptive_adjustment_state
_advanced_filters_allow_reports
_advanced_filters_allow_stations
_best_band_for_region
_best_band_for_state
_best_band_for_target
_best_band_overall_states
_blend_settings_snapshot
_build_map_payload
_build_map_report_focus_events
_build_spotter_operational_events
_build_weather_map_events
_cached_map_value
_canonical_map_source_family
_classify_alert_text
_classify_infrastructure_text
_classify_weather_text
_commstat_artifact_metadata_lookup
_compact_report_status_line
_compute_now_reachable_snapshot
_compute_region_scores
_compute_state_scores
_current_path_scope_label
_day_to_index
_daily_schedule_freqs
_decode_source_summary
_display_links_for_mode
_effective_city_label_config
_effective_path_recency_seconds
_encode_source_chips
_expand_observed_path_directions
_extract_state_grid_from_map_text
_get_origin_grid6
_get_user_latlon
_haversine_km
_hex_to_rgba
_include_legacy_spotter_report_layers
_is_usa_canada
_links_active
_load_commstat_reporter_activity
_load_fldigi_presence
_load_js8_direct_contact_summary
_load_js8_links
_load_js8_presence
_load_message_metadata_operational_reports
_load_my_active_schedule_freqs
_load_observation_operational_reports
_load_observed_band_presence
_load_operator_activity_summary
_load_operator_history
_load_peer_schedule_presence
_load_recent_calls
_load_recent_calls_by_band
_load_sitrep_state_rollup
_load_spotter_alert_reports
_load_spotter_infrastructure_reports
_load_spotter_layer_reports
_load_spotter_map_activity
_load_spotter_station_status
_load_spotter_weather_reports
_load_varac_links
_load_varac_stats
_map_advanced_filters_signature
_map_auto_fit_requested
_map_band_filter_signature
_map_callsign_base
_map_callsign_from_value
_map_commstat_scope_note
_map_compact_tooltip_html
_map_context_callsign
_map_context_group
_map_context_query_text
_map_detail_clean_text
_map_detail_first_value
_map_detail_hop_count
_map_detail_list_row_html
_map_detail_location_confidence_text
_map_detail_optional_bool
_map_detail_routing_context
_map_detail_routing_section_html
_map_detail_row_html
_map_detail_shell_html
_map_event_matches_advanced_filters
_map_event_matches_primary_filters
_map_event_topic_and_icon
_map_event_within_recency
_map_filter_combo_signature
_map_filters_active
_map_grid_looks_usable
_map_icon_for_topics
_map_link_direction_markers_enabled
_map_link_status_text
_map_mesh_node_labels
_map_message_context_age_label
_map_message_file_location
_map_message_snapshot
_map_observation_has_direct_topic_evidence
_map_path_snapshot
_map_payload_is_mesh_node
_map_payload_latlon
_map_payload_rows
_map_payload_source_family
_map_preferred_topic_for_values
_map_recency_display_label
_map_recency_options
_map_report_age_text
_map_report_cache_signature
_map_report_detail_lines
_map_report_group_values
_map_report_location_line
_map_report_refinement_active
_map_report_source_kind
_map_report_source_label
_map_report_topic_values
_map_report_unique_key
_map_reports_allowed_for_current_view
_map_selected_action_callsign
_map_selected_detail_html
_map_selected_latlon
_map_selected_message_context
_map_selected_messages_html
_map_selected_paths_html
_map_selected_source_label
_map_selected_station_callsign
_map_selected_station_is_self
_map_selected_status_html
_map_source_family_matches_filter
_map_state_filter_values
_map_station_activity_snapshot
_map_station_summary_is_noise
_map_status_matches_filter
_map_text_matches_query
_map_tool_list_text
_map_topic_icon
_map_trust_matches_filter
_map_values_match_group_filter
_marker_station_matches_filters
_message_file_metadata_lookup
_message_metadata_source_allowed
_metadata_for_observation
_metadata_matches_map_scope
_metadata_matches_map_search
_metadata_matches_topic_filter
_modeled_band_score
_nets_db_fingerprint
_normalize_map_group_value
_normalize_map_search_text
_normalize_peer_callsign
_normalize_state_abbr
_observation_file_path
_observation_focus_scopes_station_markers
_observation_focus_sources
_observation_matches_advanced_filters
_observation_matches_map_scope
_observation_matches_map_search
_observation_matches_topic_filter
_observation_station_scope_calls
_observation_ts
_operator_callsign_for_map_actions
_operator_target_point
_parse_frequency_mhz
_parse_hhmm_minutes
_path_endpoint_markers
_path_stat_fingerprint
_path_target_from_search_text
_path_to_planning_rows
_path_to_propagation_hint
_peer_schedule_hint_for_callsign
_points_for_region_lower48
_presence_band_weights
_prop_target_context
_prop_window_seconds
_query_cache_get
_query_cache_set
_rebuild_operator_index
_region_centroid
_regional_evidence_lines
_regional_intelligence_density_events
_regional_intelligence_payload
_regional_source_mix
_regional_source_mix_text
_regional_topic_rows_text
_report_position
_resolve_prop_band_colors
_safe_float
_schedule_day_indices
_schedule_minutes_to_end
_score_level
_selected_station_paths_active
_set_station_points
_settings_bool
_sitrep_age_text
_sitrep_conflict
_sitrep_status_label
_spotter_alert_form_codes_for_map
_spotter_form_codes_for_flag
_spotter_infrastructure_form_codes_for_map
_spotter_weather_form_codes_for_map
_state_centroid
_station_detected_capability_text
_station_matches_advanced_filters
_station_matches_map_search
_station_points_for_map
_stations_for_region
_stations_for_state
_summarize_operational_text
_summarize_weather_text
""".split())
OUTPUT_FIELDS = (
    'stations', 'operator_rows', 'operator_index', '_operator_groups',
    '_operator_regions', '_sitrep_report_groups', '_stations_revision',
    '_now_reachable_meta', '_now_reachable_callsigns', '_map_schedule_freqs',
    '_last_map_auto_fit_sig', '_last_map_config', '_last_map_render_input_sig',
    '_last_prop_region_filter', '_mesh_nodes_auto_fit_pending',
    '_map_marker_count', '_map_link_count', '_map_link_status_detail',
    '_map_link_display_limited_count', '_map_link_loaded_count',
    '_map_last_link_source_rows', '_map_last_link_missing_position_rows',
    '_map_last_link_all_time_count',
)
COMBO_FIELDS = ('band_combo', '_map_source_filter_combo', '_map_state_filter_combo',
                '_map_status_filter_combo', '_map_scope_filter_combo', '_map_trust_filter_combo')


@dataclass(frozen=True)
class MapReadRequest:
    generation: int
    kind: str
    state: dict
    settings: dict
    selectors: dict
    combo_signatures: dict
    theme: dict
    band: object
    mesh_recent: bool
    preserve_view: object = True


@dataclass(frozen=True)
class MapReadResult:
    generation: int
    kind: str
    projection: object = None
    state: dict = field(default_factory=dict)
    inspector: tuple[str, ...] = ()
    badge: tuple = ()
    error: str = ''
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class _ValueControl:
    value: object

    def currentData(self):
        return self.value

    def isChecked(self):
        return bool(self.value)


class _DomainContext:
    def __getattr__(self, name):
        if name in DOMAIN_METHODS:
            from freqinout.gui.stations_map_tab import StationsMapTab
            return StationsMapTab.__dict__[name].__get__(self, StationsMapTab)
        raise AttributeError(f'Uncaptured map worker dependency: {name}')

    def _queue_native_map_projection(self, payload):
        self.payload = payload

    def _update_prop_badge(self, *values):
        self.badge = values

    def _theme_snapshot(self, **kwargs):
        return self.theme

    def _map_filter_combo_signature(self, attr):
        return self.combo_signatures.get(attr, '')

    def _application_window(self):
        return None

    def _emit_map_event(self, *args, **kwargs):
        pass

    def _maybe_start_map_ingest(self):
        pass

    def _update_now_reachable_summary(self):
        pass


class MapReadEngine:
    """One instance per serial worker lane; caches never leave its thread."""
    def __init__(self):
        self._cache_key = None
        self._query_cache = {}
        self._map_query_cache = {}
        self._prop_service = None
        self._prop_month = None

    def __call__(self, request):
        started = time.monotonic()
        try:
            with map_read_budget() as budget:
                from freqinout.gui.stations_map_tab import StationsMapTab, PROP_DEFAULT_PROFILES
                from freqinout.core.propagation_service import PropagationService
                db_path = get_config_dir() / 'config' / 'freqinout_nets.db'
                # Missing/corrupt databases must not become successful empty maps.
                with connect_sqlite_readonly(db_path, timeout=0.1) as conn:
                    conn.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
                    from freqinout.core.observation_store import _validate_observation_read_schema
                    _validate_observation_read_schema(conn, db_path)
                ctx = _DomainContext()
                ctx.__dict__.update(deepcopy(request.state))
                ctx.settings = request.settings
                ctx.theme = request.theme
                ctx.combo_signatures = request.combo_signatures
                for name, value in request.selectors.items():
                    setattr(ctx, name, lambda value=value: value)
                ctx.band_combo = _ValueControl(request.band)
                ctx._mesh_recent_chk = _ValueControl(request.mesh_recent)
                ctx._native_map_renderer = None
                ctx._query_cache_ttl_sec = 3.0
                ctx._presence_weights_cache = None
                ctx._presence_weights_ts = 0.0
                cache_key = (ctx._nets_db_fingerprint(), request.state.get('_map_read_cache_revision', 0))
                if cache_key != self._cache_key:
                    self._query_cache.clear()
                    self._map_query_cache.clear()
                    self._cache_key = cache_key
                ctx._query_cache = self._query_cache
                ctx._map_query_cache = self._map_query_cache
                month = datetime.datetime.now(datetime.timezone.utc).month
                if self._prop_service is None or month != self._prop_month:
                    self._prop_month = month
                    base = Path(__file__).resolve().parents[2]
                    self._prop_service = PropagationService(
                        default_profiles=PROP_DEFAULT_PROFILES,
                        profiles_path=base / 'config/propagation/prop_profiles.json',
                        climatology_db_path=base / 'config/propagation/prop_climatology.db',
                        outcome_db_path=db_path, db_index_mode='floor5', climatology_month=month)
                ctx._prop_service = self._prop_service
                ctx.badge = ()
                ctx.payload = None
                if not request.state.get('_initial_data_loaded', False):
                    ctx._load_operator_history()
                if ctx._now_reachable_enabled:
                    ctx._now_reachable_meta = ctx._compute_now_reachable_snapshot()
                    ctx._now_reachable_callsigns = set(ctx._now_reachable_meta)
                if request.kind == 'inspector':
                    payload = ctx._map_selected_payload
                    summary = ctx._map_detail_clean_text(payload.get('summary') or '', multiline=True)
                    inspector = (ctx._map_selected_detail_html(payload, summary=summary),
                                 ctx._map_selected_status_html(payload, summary=summary),
                                 ctx._map_selected_paths_html(payload), ctx._map_selected_messages_html(payload))
                    budget.raise_if_failed()
                    return MapReadResult(request.generation, request.kind, inspector=inspector,
                                         elapsed_ms=(time.monotonic()-started)*1000)
                ctx._map_schedule_freqs = ctx._daily_schedule_freqs()
                ctx._build_map_payload(request.preserve_view)
                budget.raise_if_failed()
                if ctx.payload is None:
                    raise RuntimeError('Map builder returned no projection')
                projection = StationsMapTab._serialize_map_projection_snapshot(request.generation, ctx.payload)
                budget.raise_if_failed()
                if projection.error:
                    raise RuntimeError(projection.error)
                if budget.capped:
                    ctx._map_link_status_detail += " Recent evidence capped: " + ", ".join(sorted(budget.capped)) + "."
                state = {name: deepcopy(getattr(ctx, name)) for name in OUTPUT_FIELDS if hasattr(ctx, name)}
                return MapReadResult(request.generation, request.kind, projection=deepcopy(projection),
                                     state=state, badge=ctx.badge, elapsed_ms=(time.monotonic()-started)*1000)
        except Exception as exc:
            # Do not retain partial/empty cache entries from a failed loader.
            self._query_cache.clear()
            self._map_query_cache.clear()
            self._prop_service = None
            self._prop_month = None
            return MapReadResult(request.generation, request.kind, error=str(exc),
                                 elapsed_ms=(time.monotonic()-started)*1000)


class MapReadLane(QObject):
    """GUI-owned mailbox: one running job and one replaceable pending value."""
    ready = Signal(object)
    _completed = Signal(object)

    def __init__(self, parent=None, engine=None):
        super().__init__(parent)
        self._engine = engine or MapReadEngine()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='fio-map-read')
        self._running = False
        self._pending = None
        self._closed = False
        self._completed.connect(self._finish)

    def request(self, snapshot):
        if self._closed:
            return
        if self._running:
            self._pending = snapshot
        else:
            self._start(snapshot)

    def _start(self, snapshot):
        self._running = True
        future = self._executor.submit(self._engine, snapshot)
        ref = weakref.ref(self)
        def completed(done):
            lane = ref()
            if lane is None or done.cancelled():
                return
            try:
                result = done.result()
            except Exception as exc:
                result = MapReadResult(snapshot.generation, snapshot.kind, error=str(exc))
            try:
                lane._completed.emit(result)
            except RuntimeError:
                pass  # The Qt owner was destroyed during a bounded read.
        future.add_done_callback(completed)

    def _finish(self, result):
        self._running = False
        if self._closed:
            return
        pending, self._pending = self._pending, None
        if pending is not None:
            self._start(pending)
        self.ready.emit(result)

    def shutdown(self):
        self._closed = True
        self._pending = None
        self._executor.shutdown(wait=False, cancel_futures=True)
