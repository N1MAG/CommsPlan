from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from PySide6.QtCore import QObject, QTimer, Signal

from freqinout.core.condition_alerts import CONDITION_ALERT_RULES_SETTING_KEY
from freqinout.core.condition_sop_execution import execute_condition_sop_invocation_plans
from freqinout.core.condition_sop_invocation import (
    ConditionSopInvocationPlan,
    plan_condition_sop_invocations,
    schedule_layer_rows_for_condition_decision,
)
from freqinout.core.condition_sop_policy import AUTO_SOP_INVOCATION_SETTING_KEY
from freqinout.core.dependency_health import get_dependency_health_registry
from freqinout.core.ingest_health import source_health_key
from freqinout.core.ingest_refresh_planner import ingest_sources_fingerprint, plan_ingest_refresh
from freqinout.core.js8_expect_runtime import ExpectAutomationCoordinator, GuardPreflightCallback
from freqinout.core.js8_storage_reconciliation import reconcile_js8_storage_profile
from freqinout.core.ingest_runtime_status import active_runtime_ingest_inventory
from freqinout.core.ingest_source_model import IngestSourceDescriptor, IngestSourceInventory, js8_ingest_sources
from freqinout.core.logger import log
from freqinout.core.worker_lifecycle import CancellationToken, OperationCancelled
from freqinout.core.message_ingest import MessageIngestor
from freqinout.core.multi_rig_runtime_status import (
    SCOPE_ALL_ACTIVE_RUNTIME,
    build_multi_rig_runtime_status,
)
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.observation_backfill import backfill_observations
from freqinout.core.observation_queries import ObservationQuery, query_observations
from freqinout.core.peer_schedule_infer import infer_peer_schedules
from freqinout.core.propagation_outcome_ingest import ingest_propagation_outcomes
from freqinout.core.subspace_hearing import HearingRuntime
from freqinout.core.subspace_files import SubspaceFileRuntime
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.sqlite_utils import connect_sqlite
from freqinout.core.sitrep_fusion import fuse_sitreps
from freqinout.core.sitrep_ingest import ingest_sitreps
from freqinout.core.sop_manager import SOPManager
from freqinout.core.schedule_source_sets import assigned_plan_rf_guard_impacts_for_sop_update
from freqinout.core.varac_ingest import ingest_varac
from freqinout.core.varac_bbs_vault import (
    VaracBbsVaultRunResult,
    build_varac_bbs_vault_activity_signature,
    run_varac_bbs_vault,
)
from freqinout.core.varac_bbs_library_store import (
    bbs_library_db_path_from_settings,
    ensure_bbs_library_schema,
    import_legacy_station_bbs_profiles,
    reconcile_bbs_publications,
)
from freqinout.core.varac_guard import run_varac_guard
from freqinout.core.js8_log_link_indexer import JS8LogLinkIndexer


class _DeviceProfileVaultSettings:
    def __init__(self, profile: Dict[str, object], fallback_settings: SettingsManager, store: MultiRadioStore) -> None:
        self.profile = dict(profile)
        self.fallback_settings = fallback_settings
        self.store = store

    def _profile_value(self, *keys: str) -> object:
        for key in keys:
            value = self.profile.get(key)
            if value not in (None, ""):
                return value
        return ""

    def get(self, key: str, default=None):
        if key == "varac_path":
            return self._profile_value("varac_path", "varac_install_path") or self.fallback_settings.get(key, default)
        if key == "varac_bbs_vault_flamp_relay_dir":
            return self._profile_value(key) or self.fallback_settings.get(key, default)
        if key == "message_paths":
            merged = dict(self.fallback_settings.get("message_paths", {}) or {})
            flmsg = str(self._profile_value("flmsg_message_path") or "").strip()
            if flmsg:
                merged["flmsg"] = flmsg
            flamp = str(self._profile_value("flamp_message_path") or "").strip()
            if flamp:
                merged["flamp"] = flamp
            incoming = str(self._profile_value("varac_incoming_path") or "").strip()
            if incoming:
                merged["varac"] = incoming
            return merged or default
        if key in self.profile:
            return self.profile.get(key, default)
        return self.fallback_settings.get(key, default)

    def set(self, key: str, value) -> None:
        if str(key or "").startswith(("spotter_directed_offset", "expect_directed_offset")):
            # Tail checkpoint failure must reach the ingest owner. Swallowing
            # it reports success while the next poll replays the old offset.
            writer = getattr(self.fallback_settings, "set_runtime_checkpoint", self.fallback_settings.set)
            writer(key, value)
            if hasattr(self.fallback_settings, "save"):
                self.fallback_settings.save()
            return
        self.profile[key] = value
        if key in {"varac_bbs_vault_runtime_state_v1", "varac_bbs_vault_last_summary"}:
            try:
                profile_id = int(self.profile.get("id", 0) or 0)
                if profile_id > 0:
                    self.store.save_device_profile({"id": profile_id, key: value})
            except Exception as exc:
                log.debug("BackgroundIngest: failed to persist VarAC vault profile state: %s", exc)


class _DeviceProfileGuardSettings(_DeviceProfileVaultSettings):
    """Keep inbound guard policy/path/state scoped to one active radio."""
    @property
    def db_path(self):
        return self.fallback_settings.db_path

    def get(self, key, default=None):
        if key in {"varac_guard_state_v1", "varac_guard_last_summary"}:
            return self.fallback_settings.get(f"{key}:radio:{self.profile['id']}", default)
        if key in {"varac_guard_allow_bbs_allowed_callsigns", "varac_guard_allow_operator_trusted"}:
            value = self.profile.get(key)
            if value is None:
                return self.fallback_settings.get(key, default)
            return str(value).strip().casefold() not in {"", "0", "false", "off", "no"}
        if key == "varac_guard_access_policy_v1":
            return self.profile.get(key) or ""
        return super().get(key, default)

    def set(self, key, value):
        if key in {"varac_guard_state_v1", "varac_guard_last_summary"}:
            self.fallback_settings.set(f"{key}:radio:{self.profile['id']}", value)
            return
        super().set(key, value)


class BackgroundIngestController(QObject):
    """
    Background, non-UI data ingest to keep DBs warm for fast tab activation.
    """

    _VARAC_VAULT_ACTIVITY_INTERVAL_MS = 5_000
    _VARAC_VAULT_ACTIVE_INTERVAL_MS = 5_000
    _VARAC_VAULT_WARM_IDLE_INTERVAL_MS = 30_000
    _VARAC_VAULT_IDLE_INTERVAL_MS = 120_000
    _VARAC_VAULT_DEGRADED_INTERVAL_MS = 60_000
    _VARAC_VAULT_DISABLED_INTERVAL_MS = 30_000
    _controller_thread_call = Signal(object)
    job_finished = Signal(str)
    condition_sop_invocation_audited = Signal(object)
    condition_sop_invocation_applied = Signal(object)

    def __init__(self, settings: SettingsManager, *, expect_guard_preflight: Optional[GuardPreflightCallback] = None):
        super().__init__()
        self.settings = settings
        self._runtime_store_local = threading.local()
        self._runtime_settings_db_path = getattr(settings, "db_path", None)
        self.expect_guard_preflight = expect_guard_preflight
        self._js8_links_timer: Optional[QTimer] = None
        self._messages_timer: Optional[QTimer] = None
        self._dynamic_expect_timer: Optional[QTimer] = None
        self._dynamic_flamp_projection_timer: Optional[QTimer] = None
        self._varac_timer: Optional[QTimer] = None
        self._varac_vault_timer: Optional[QTimer] = None
        self._varac_vault_activity_timer: Optional[QTimer] = None
        self._varac_guard_timer: Optional[QTimer] = None
        self._sitrep_timer: Optional[QTimer] = None
        self._prop_outcome_timer: Optional[QTimer] = None
        self._peer_sched_timer: Optional[QTimer] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._executor_lock = threading.RLock()
        self._job_futures: Dict[str, Future] = {}
        self._realtime_executor: Optional[ThreadPoolExecutor] = None
        self._realtime_executor_lock = threading.RLock()
        self._realtime_job_futures: Dict[str, Future] = {}
        self._job_skipped_counts: Dict[str, int] = {}
        self._job_started_at: Dict[str, float] = {}
        self._job_timeout_warned: set[str] = set()
        self._job_refresh_fingerprints: Dict[str, tuple[object, ...]] = {}
        self._job_refresh_last_run_ts: Dict[str, float] = {}
        self._job_refresh_skip_reasons: Dict[str, str] = {}
        self._job_refresh_decisions: Dict[str, Dict[str, object]] = {}
        self._job_skip_reasons: Dict[str, str] = {}
        self._source_skip_reasons: Dict[str, Dict[str, object]] = {}
        self._runtime_inventory_cache: Optional[IngestSourceInventory] = None
        self._runtime_inventory_cache_ts: float = 0.0
        self._runtime_inventory_cache_ttl_sec: float = 5.0
        # Linked device/profile resolution is comparatively expensive and is
        # shared by the JS8, FLAMP, and VarAC background jobs.  The cache is
        # fenced by an explicit configuration generation, incremented by the
        # Settings-saved path, rather than relying on an unbounded TTL.
        self._runtime_profile_cache_lock = threading.RLock()
        self._runtime_profile_generation: int = 0
        self._runtime_profile_cache_generation: int = -1
        self._runtime_profile_cache: tuple[Dict[str, object], ...] = ()
        self._job_watchdog_timer: Optional[QTimer] = None
        self._health = get_dependency_health_registry()
        self._running = False
        self._subspace_runtime = HearingRuntime()
        self._subspace_files_runtime = SubspaceFileRuntime()
        self._subspace_lock = threading.RLock()
        self._subspace_timer = None
        self._cancel_token = CancellationToken()
        self._varac_vault_activity_signature: Optional[object] = None
        # Timer callbacks must stay cache-only. Runtime-profile discovery can
        # touch SQLite, so eligibility is refreshed on the realtime worker and
        # published back to this controller thread.
        self._varac_vault_enabled_cached: bool = self._truthy(
            self.settings.get("varac_bbs_vault_enabled", False), False
        )
        self._varac_vault_config_refresh_pending: bool = False
        self._varac_vault_no_change_runs: int = 0
        self._varac_vault_full_interval_ms: int = self._VARAC_VAULT_ACTIVE_INTERVAL_MS
        self._varac_vault_refresh_pending: bool = False
        self._condition_sop_seen_observation_ids: set[str] = set()
        self._dynamic_flamp_projection_ready = threading.Event()
        self._controller_thread_call.connect(self._run_controller_thread_call)

    def _run_controller_thread_call(self, callback: object) -> None:
        if not callable(callback):
            return
        try:
            callback()
        except Exception as e:
            log.debug("BackgroundIngest: queued controller-thread callback failed: %s", e)

    def _queue_controller_thread_call(self, callback: Callable[[], None]) -> None:
        self._controller_thread_call.emit(callback)

    def start(self, *, initial_stagger: bool = True) -> None:
        if self._running:
            return
        if self._cancel_token.is_cancelled:
            self._cancel_token = CancellationToken()
        self._dynamic_flamp_projection_ready.clear()
        self._running = True
        self._ensure_executor()
        self._refresh_varac_vault_enabled_async()
        self._subspace_timer = QTimer(self)
        self._subspace_timer.setInterval(30_000)
        self._subspace_timer.timeout.connect(self._refresh_subspace_hearing)
        self._subspace_timer.timeout.connect(self._refresh_subspace_files)
        self._subspace_timer.start()
        self._refresh_subspace_hearing()
        self._refresh_subspace_files()
        # JS8 links/background ingest: low cadence
        self._js8_links_timer = QTimer(self)
        self._js8_links_timer.setInterval(5 * 60 * 1000)  # 5 minutes
        self._js8_links_timer.timeout.connect(self._ingest_js8_links)
        self._js8_links_timer.start()

        # Messages/spotter ingest: moderate cadence
        self._messages_timer = QTimer(self)
        self._messages_timer.setInterval(90 * 1000)  # 90 seconds
        self._messages_timer.timeout.connect(self._ingest_messages)
        self._messages_timer.start()

        # Dynamic Expect is a lightweight tail of DIRECTED.TXT, separate from
        # the broader 90-second message/projection pass.  This keeps on-air
        # FLAMP queries responsive without repeatedly rebuilding message views.
        self._dynamic_expect_timer = QTimer(self)
        self._dynamic_expect_timer.setInterval(3 * 1000)
        self._dynamic_expect_timer.timeout.connect(self._ingest_dynamic_expect)
        self._dynamic_expect_timer.start()

        # FLAMP relay/RX reconciliation is a bounded filesystem projection,
        # intentionally separate from the three-second directed-query tail.
        self._dynamic_flamp_projection_timer = QTimer(self)
        self._dynamic_flamp_projection_timer.setInterval(30 * 1000)
        self._dynamic_flamp_projection_timer.timeout.connect(self._ingest_dynamic_flamp_projection)
        self._dynamic_flamp_projection_timer.start()

        # VarAC ingest: moderate cadence
        self._varac_timer = QTimer(self)
        self._varac_timer.setInterval(2 * 60 * 1000)  # 2 minutes
        self._varac_timer.timeout.connect(self._ingest_varac)
        self._varac_timer.start()

        self._varac_vault_timer = QTimer(self)
        self._varac_vault_timer.timeout.connect(self._ingest_varac_vault)
        self._update_varac_vault_timer_state()

        self._varac_vault_activity_timer = QTimer(self)
        self._varac_vault_activity_timer.setInterval(self._VARAC_VAULT_ACTIVITY_INTERVAL_MS)
        self._varac_vault_activity_timer.timeout.connect(self._probe_varac_vault_activity)
        if self._varac_vault_enabled():
            self._varac_vault_activity_timer.start()

        self._varac_guard_timer = QTimer(self)
        self._varac_guard_timer.setInterval(90 * 1000)  # 90 seconds
        self._varac_guard_timer.timeout.connect(self._ingest_varac_guard)
        self._varac_guard_timer.start()

        # Unified SitRep source ingest: moderate cadence
        self._sitrep_timer = QTimer(self)
        self._sitrep_timer.setInterval(2 * 60 * 1000)  # 2 minutes
        self._sitrep_timer.timeout.connect(self._ingest_sitreps)
        self._sitrep_timer.start()

        # Propagation outcome ingest: checkpointed incremental backfill.
        self._prop_outcome_timer = QTimer(self)
        self._prop_outcome_timer.setInterval(3 * 60 * 1000)  # 3 minutes
        self._prop_outcome_timer.timeout.connect(self._ingest_prop_outcomes)
        self._prop_outcome_timer.start()

        # Inferred peer schedule generation: lower cadence.
        self._peer_sched_timer = QTimer(self)
        self._peer_sched_timer.setInterval(6 * 60 * 1000)  # 6 minutes
        self._peer_sched_timer.timeout.connect(self._infer_peer_schedules)
        self._peer_sched_timer.start()

        self._job_watchdog_timer = QTimer(self)
        self._job_watchdog_timer.setInterval(5000)
        self._job_watchdog_timer.timeout.connect(self._check_long_running_jobs)
        self._job_watchdog_timer.start()

        # Initial staggered ingest
        if initial_stagger:
            # Seed the dedicated Expect checkpoint before the broader message
            # pass can advance its compatibility checkpoint.
            QTimer.singleShot(1500, self._ingest_dynamic_expect)
            QTimer.singleShot(2000, self._ingest_js8_links)
            QTimer.singleShot(4000, self._ingest_messages)
            QTimer.singleShot(6000, self._ingest_varac)
            if self._varac_vault_enabled():
                QTimer.singleShot(6500, self._ingest_varac_vault)
            QTimer.singleShot(7000, self._ingest_varac_guard)
            QTimer.singleShot(8000, self._ingest_sitreps)
            QTimer.singleShot(9000, self._ingest_prop_outcomes)
            QTimer.singleShot(10000, self._infer_peer_schedules)

    def stop(self) -> None:
        with self._subspace_lock:
            self._subspace_runtime.stop()
            self._subspace_files_runtime.stop()
            self._running = False
        self._cancel_token.cancel()
        for attr in (
            "_subspace_timer",
            "_js8_links_timer",
            "_messages_timer",
            "_dynamic_expect_timer",
            "_dynamic_flamp_projection_timer",
            "_varac_timer",
            "_varac_vault_timer",
            "_varac_vault_activity_timer",
            "_varac_guard_timer",
            "_sitrep_timer",
            "_prop_outcome_timer",
            "_peer_sched_timer",
            "_job_watchdog_timer",
        ):
            t = getattr(self, attr, None)
            if t:
                try:
                    t.stop()
                    t.deleteLater()
                except Exception:
                    pass
                setattr(self, attr, None)
        self._shutdown_executor()
        self._shutdown_realtime_executor()
        self._dynamic_flamp_projection_ready.clear()

    def _ensure_executor(self) -> ThreadPoolExecutor:
        with self._executor_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="freqinout-ingest",
                )
            return self._executor

    def _shutdown_executor(self) -> None:
        with self._executor_lock:
            futures = list(self._job_futures.values())
            executor = self._executor
            self._executor = None
        for future in futures:
            try:
                future.cancel()
            except Exception:
                pass
        if executor is None:
            return
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)
        except Exception as e:
            log.debug("BackgroundIngest: executor shutdown failed: %s", e)

    def _ensure_realtime_executor(self) -> ThreadPoolExecutor:
        with self._realtime_executor_lock:
            if self._realtime_executor is None:
                self._realtime_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="freqinout-realtime",
                )
            return self._realtime_executor

    def _shutdown_realtime_executor(self) -> None:
        with self._realtime_executor_lock:
            futures = list(self._realtime_job_futures.values())
            executor = self._realtime_executor
            self._realtime_executor = None
        for future in futures:
            try:
                future.cancel()
            except Exception:
                pass
        if executor is None:
            return
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)
        except Exception as e:
            log.debug("BackgroundIngest: realtime executor shutdown failed: %s", e)

    def _new_worker_settings(self) -> SettingsManager:
        # The application startup owner has already created and migrated the
        # settings database. Background jobs use a lightweight thread-owned
        # view so recurring ingest cannot contend with the GUI by rerunning
        # schema, migration, or launch-bundle adoption work.
        return SettingsManager(runtime_worker=True)

    def _runtime_store(self) -> MultiRadioStore:
        # Connections remain operation-local; only schema readiness is retained
        # per worker. MultiRadioStore checks file/schema identity on each open.
        store = getattr(self._runtime_store_local, "store", None)
        if store is None:
            store = MultiRadioStore(self._runtime_settings_db_path)
            self._runtime_store_local.store = store
        return store

    def _runtime_active_profiles(self) -> list[Dict[str, object]]:
        """Return one configuration-generation-fenced linked-profile snapshot.

        This helper is called only by background workers.  It keeps recurring
        JS8/FLAMP/VarAC eligibility checks from independently resolving the
        same active radios and linked application profiles.  ``refresh_runtime_settings``
        invalidates it immediately after a committed settings change.
        """

        with self._runtime_profile_cache_lock:
            generation = self._runtime_profile_generation
            if self._runtime_profile_cache_generation == generation:
                return [dict(profile) for profile in self._runtime_profile_cache]
        try:
            store = self._runtime_store()
            runtime_status = build_multi_rig_runtime_status(store)
            if runtime_status.background_ingest_scope != SCOPE_ALL_ACTIVE_RUNTIME:
                profiles: tuple[Dict[str, object], ...] = ()
            else:
                profiles = tuple(
                    dict(row) for row in store.list_runtime_active_device_profiles()
                )
        except Exception:
            # A transient database/read failure is not evidence that the
            # station has no active radios.  Leave this generation uncached
            # so the next ingest pass retries instead of suppressing all
            # profile-scoped ingestion until another settings save.
            return []
        with self._runtime_profile_cache_lock:
            # A settings-save can race the read.  Do not publish an older
            # snapshot into the newer generation; its caller can safely use
            # the result once, and the next request will reload.
            if generation == self._runtime_profile_generation:
                self._runtime_profile_cache = profiles
                self._runtime_profile_cache_generation = generation
        return [dict(profile) for profile in profiles]

    def _active_varac_vault_profiles(self) -> list[Dict[str, object]]:
        profiles = self._runtime_active_profiles()
        return [
            profile
            for profile in profiles
            if self._truthy(profile.get("use_varac", False), False)
            and self._truthy(profile.get("varac_bbs_vault_enabled", False), False)
            and str(profile.get("varac_bbs_dir", "") or "").strip()
        ]

    @staticmethod
    def _normalized_bbs_dir(value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            return str(Path(raw).expanduser().resolve()).lower()
        except Exception:
            return str(Path(raw).expanduser()).lower()

    @staticmethod
    def _truthy(value: object, default: bool = False) -> bool:
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

    def _compute_varac_vault_enabled(self) -> bool:
        try:
            if self._active_varac_vault_profiles():
                return True
            return self._truthy(self.settings.get("varac_bbs_vault_enabled", False), False)
        except Exception:
            return False

    def _varac_vault_enabled(self) -> bool:
        """Return the worker-published eligibility snapshot without I/O."""

        return bool(getattr(self, "_varac_vault_enabled_cached", False))

    def _refresh_varac_vault_enabled_async(self) -> None:
        if not self._running:
            return
        with self._realtime_executor_lock:
            future = self._realtime_job_futures.get("varac_vault_config")
            if future is not None and not future.done():
                self._varac_vault_config_refresh_pending = True
                return
        self._varac_vault_config_refresh_pending = False
        self._submit_realtime_job("varac_vault_config", self._compute_varac_vault_enabled)

    def _on_varac_vault_config_result(self, enabled: object) -> None:
        previous = self._varac_vault_enabled()
        self._varac_vault_enabled_cached = bool(enabled)
        self._update_varac_vault_timer_state()
        if self._varac_vault_config_refresh_pending:
            self._varac_vault_config_refresh_pending = False
            self._refresh_varac_vault_enabled_async()
            return
        refresh_requested = bool(self._varac_vault_refresh_pending)
        self._varac_vault_refresh_pending = False
        if self._varac_vault_enabled() and (not previous or refresh_requested):
            self._ingest_varac_vault()

    def _update_varac_vault_timer_state(self) -> None:
        timer = self._varac_vault_timer
        if timer is None:
            return
        enabled = self._varac_vault_enabled()
        health = self._health.snapshot(self._job_health_key("varac_vault"))
        degraded = bool(health.get("degraded")) and float(health.get("cooldown_remaining_sec") or 0.0) > 0
        timer.setInterval(
            self._VARAC_VAULT_DEGRADED_INTERVAL_MS
            if enabled and degraded
            else self._varac_vault_full_interval_ms
            if enabled
            else self._VARAC_VAULT_DISABLED_INTERVAL_MS
        )
        if not self._running:
            return
        if enabled and not timer.isActive():
            timer.start()
        elif not enabled and timer.isActive():
            timer.stop()
        activity_timer = self._varac_vault_activity_timer
        if activity_timer is not None:
            if enabled and self._running and not activity_timer.isActive():
                activity_timer.start()
            elif not enabled and activity_timer.isActive():
                activity_timer.stop()

    def _refresh_subspace_hearing(self) -> None:
        if not self._running:
            return
        with self._runtime_profile_cache_lock:
            generation = self._runtime_profile_generation

        def configure():
            profiles = self._runtime_active_profiles()
            with self._runtime_profile_cache_lock:
                if self._runtime_profile_cache_generation != generation:
                    return  # Read failed/raced; an empty result is not configuration.
            local_call = self._new_worker_settings().get("operator_callsign", "")
            with self._subspace_lock:
                if self._running and generation == self._runtime_profile_generation:
                    self._subspace_runtime.configure(profiles, local_call)

        self._submit_job("subspace_hearing_config", configure)

    def _refresh_subspace_files(self) -> None:
        if not self._running:
            return
        with self._runtime_profile_cache_lock:
            generation = self._runtime_profile_generation

        def configure():
            profiles = self._runtime_active_profiles()
            with self._runtime_profile_cache_lock:
                if self._runtime_profile_cache_generation != generation:
                    return
            if self._running and generation == self._runtime_profile_generation:
                worker_settings = self._new_worker_settings()
                self._subspace_files_runtime.configure(
                    profiles, worker_settings.config_dir / "freqinout_nets.db",
                    should_continue=lambda: self._running and generation == self._runtime_profile_generation)

        self._submit_job("subspace_files_config", configure)

    def refresh_runtime_settings(self) -> None:
        self._runtime_inventory_cache = None
        self._runtime_inventory_cache_ts = 0.0
        with self._runtime_profile_cache_lock:
            self._runtime_profile_generation += 1
            self._runtime_profile_cache_generation = -1
            self._runtime_profile_cache = ()
        with self._subspace_lock:
            self._subspace_runtime.stop()
            self._subspace_files_runtime.stop()
        self._refresh_subspace_hearing()
        self._refresh_subspace_files()
        self.request_varac_vault_refresh("settings_saved")

    def is_running(self) -> bool:
        return bool(self._running)

    def is_stopped(self) -> bool:
        """Return True once timers are stopped and submitted jobs have drained."""
        if self._running:
            return False
        with self._executor_lock:
            background_pending = any(not future.done() for future in self._job_futures.values())
        with self._realtime_executor_lock:
            realtime_pending = any(not future.done() for future in self._realtime_job_futures.values())
        return not background_pending and not realtime_pending and self._subspace_runtime.is_stopped() and self._subspace_files_runtime.is_stopped()

    def has_inflight_jobs(self, *job_names: str) -> bool:
        """Return whether any named background job is queued or running.

        This lightweight snapshot lets downstream projection avoid competing
        with the source writer that is creating its work.  A queued future is
        deliberately considered in flight: the background executor is serial,
        so starting projection while a message scan is waiting would recreate
        the same SQLite convoy a few moments later.
        """

        wanted = {
            str(name or "").strip().lower()
            for name in job_names
            if str(name or "").strip()
        }
        if not wanted:
            return False
        with self._executor_lock:
            return any(
                str(name).strip().lower() in wanted and not future.done()
                for name, future in self._job_futures.items()
            )

    def _cancel_checkpoint(self) -> None:
        self._cancel_token.checkpoint()

    def job_status_snapshot(self, *, now_ts: Optional[float] = None) -> Dict[str, object]:
        now = time.time() if now_ts is None else float(now_ts)
        with self._executor_lock:
            queued_jobs = {
                str(name): {
                    "done": bool(future.done()),
                    "running_for_sec": max(0.0, now - float(self._job_started_at.get(name, now) or now)),
                }
                for name, future in self._job_futures.items()
            }
            worker_active = self._executor is not None
        with self._realtime_executor_lock:
            realtime_jobs = {
                str(name): {
                    "done": bool(future.done()),
                    "running_for_sec": max(0.0, now - float(self._job_started_at.get(name, now) or now)),
                }
                for name, future in self._realtime_job_futures.items()
            }
            realtime_worker_active = self._realtime_executor is not None
        return {
            "running": bool(self._running),
            "queued_jobs": queued_jobs,
            "realtime_jobs": realtime_jobs,
            "skipped_counts": dict(self._job_skipped_counts),
            "skip_reasons": dict(self._job_skip_reasons),
            "source_skip_reasons": dict(self._source_skip_reasons),
            "refresh_skip_reasons": dict(self._job_refresh_skip_reasons),
            "refresh_decisions": dict(self._job_refresh_decisions),
            "timeout_warned": tuple(sorted(self._job_timeout_warned)),
            "worker_active": worker_active,
            "realtime_worker_active": realtime_worker_active,
            "runtime_inventory_cached": self._runtime_inventory_cache is not None,
            "runtime_inventory_cache_age_sec": (
                max(0.0, time.monotonic() - float(self._runtime_inventory_cache_ts or 0.0))
                if self._runtime_inventory_cache is not None
                else 0.0
            ),
        }

    def request_refresh(self, *kinds: str) -> None:
        requested = {str(kind or "").strip().lower() for kind in kinds if str(kind or "").strip()}
        force = bool(requested.intersection({"force", "forced", "manual"}))
        requested.difference_update({"force", "forced", "manual"})
        if not requested:
            requested = {"js8_links", "messages", "varac", "sitreps", "propagation"}
        if "js8" in requested:
            requested.add("js8_links")
            requested.add("messages")
        if "dynamic_flamp" in requested or "flamp_projection" in requested:
            self._dynamic_flamp_projection_ready.clear()
            self._ingest_dynamic_flamp_projection()
        if "js8_links" in requested:
            self._ingest_js8_links(force=force)
        if "message_cache" in requested:
            self._ingest_messages(include_observation_backfill=False, force=force)
        elif "messages" in requested:
            self._ingest_messages(include_observation_backfill=True, force=force)
        if "varac" in requested:
            self._ingest_varac(force=force)
        if "sitreps" in requested:
            self._ingest_sitreps(force=force)
        if "propagation" in requested or "prop_outcomes" in requested:
            self._ingest_prop_outcomes()

    def _submit_job(self, job_name: str, job_func: Callable[[], None]) -> None:
        if not self._running:
            return
        health_key = self._job_health_key(job_name)
        may_run, health = self._health.may_run(health_key, owner="BackgroundIngest")
        if not may_run:
            self._record_job_skip(job_name, "backoff")
            log.debug(
                "BackgroundIngest: backing off %s for %.1fs",
                job_name,
                float(health.get("cooldown_remaining_sec") or 0.0),
            )
            return
        with self._executor_lock:
            future = self._job_futures.get(job_name)
            if future is not None and not future.done():
                self._record_job_skip(job_name, "already_running")
                log.debug("BackgroundIngest: job already running, skipping trigger: %s", job_name)
                return
            executor = self._ensure_executor()
            self._job_started_at[job_name] = time.time()
            self._job_timeout_warned.discard(job_name)
            future = executor.submit(self._run_job, job_name, job_func)
            self._job_futures[job_name] = future
        future.add_done_callback(lambda done, name=job_name: self._on_job_done(name, done))

    def _run_job(self, job_name: str, job_func: Callable[[], object]) -> object:
        started_at = time.time()
        failed = False
        was_cancelled = False
        result: object = None
        try:
            self._cancel_checkpoint()
            result = job_func()
            self._cancel_checkpoint()
        except OperationCancelled:
            was_cancelled = True
            return None
        except Exception as e:
            failed = True
            log.debug("BackgroundIngest: %s worker failed: %s", job_name, e)
        finally:
            elapsed = time.time() - started_at
            elapsed_ms = elapsed * 1000.0
            health_key = self._job_health_key(job_name)
            if was_cancelled:
                log.debug("BackgroundIngest: %s cancelled during shutdown", job_name)
            elif failed:
                self._health.record_failure(
                    health_key,
                    owner="BackgroundIngest",
                    error="worker failed",
                    duration_ms=elapsed_ms,
                )
            else:
                self._health.record_success(
                    health_key,
                    owner="BackgroundIngest",
                    duration_ms=elapsed_ms,
                    slow_ms=self._job_success_slow_ms(job_name),
                )
            if elapsed >= 1.0:
                log.debug("BackgroundIngest: %s completed in %.2fs", job_name, elapsed)
        return result

    def _on_job_done(self, job_name: str, future: Future) -> None:
        with self._executor_lock:
            current = self._job_futures.get(job_name)
            if current is future:
                self._job_futures.pop(job_name, None)
                self._job_started_at.pop(job_name, None)
                self._job_timeout_warned.discard(job_name)
        try:
            future.result()
        except Exception as e:
            log.debug("BackgroundIngest: %s future failed: %s", job_name, e)
        if self._running:
            self._queue_controller_thread_call(lambda name=job_name: self.job_finished.emit(name))

    def _submit_realtime_job(self, job_name: str, job_func: Callable[[], None]) -> None:
        if not self._running:
            return
        health_key = self._job_health_key(job_name)
        may_run, health = self._health.may_run(health_key, owner="BackgroundIngest")
        if not may_run:
            self._record_job_skip(job_name, "backoff")
            if job_name == "varac_vault":
                self._update_varac_vault_timer_state()
            log.debug(
                "BackgroundIngest: backing off realtime %s for %.1fs",
                job_name,
                float(health.get("cooldown_remaining_sec") or 0.0),
            )
            return
        with self._realtime_executor_lock:
            future = self._realtime_job_futures.get(job_name)
            if future is not None and not future.done():
                self._record_job_skip(job_name, "already_running")
                return
            executor = self._ensure_realtime_executor()
            self._job_started_at[job_name] = time.time()
            self._job_timeout_warned.discard(job_name)
            future = executor.submit(self._run_job, job_name, job_func)
            self._realtime_job_futures[job_name] = future
        future.add_done_callback(lambda done, name=job_name: self._on_realtime_job_done(name, done))

    def _on_realtime_job_done(self, job_name: str, future: Future) -> None:
        with self._realtime_executor_lock:
            current = self._realtime_job_futures.get(job_name)
            if current is future:
                self._realtime_job_futures.pop(job_name, None)
                self._job_started_at.pop(job_name, None)
                self._job_timeout_warned.discard(job_name)
        try:
            result = future.result()
        except Exception as e:
            log.debug("BackgroundIngest: realtime %s future failed: %s", job_name, e)
            result = None
        if not self._running:
            return
        if job_name == "varac_vault":
            self._queue_controller_thread_call(lambda result=result: self._on_varac_vault_result(result))
        elif job_name == "varac_vault_probe":
            self._queue_controller_thread_call(lambda result=result: self._on_varac_vault_activity_result(result))
        elif job_name == "varac_vault_config":
            self._queue_controller_thread_call(lambda result=result: self._on_varac_vault_config_result(result))

    def _job_timeout_seconds(self, job_name: str) -> float:
        if job_name in {"varac_vault", "varac_guard"}:
            return 30.0
        return 90.0

    def _job_success_slow_ms(self, job_name: str) -> float:
        if job_name in {"varac_vault", "varac_guard"}:
            return max(5000.0, self._job_timeout_seconds(job_name) * 2000.0)
        return 5000.0

    def _check_long_running_jobs(self) -> None:
        now = time.time()
        for job_name, started_at in list(self._job_started_at.items()):
            threshold = self._job_timeout_seconds(job_name)
            elapsed = max(0.0, now - float(started_at or 0.0))
            if elapsed < threshold or job_name in self._job_timeout_warned:
                continue
            self._job_timeout_warned.add(job_name)
            self._health.record_failure(
                self._job_health_key(job_name),
                owner="BackgroundIngest",
                error=f"job running longer than {int(threshold)}s",
                duration_ms=elapsed * 1000.0,
            )
            log.warning("BackgroundIngest: %s has been running for %.1fs", job_name, elapsed)

    def _record_job_skip(self, job_name: str, reason: str) -> None:
        name = str(job_name or "").strip() or "unknown"
        reason_txt = str(reason or "").strip() or "skipped"
        self._job_skipped_counts[name] = self._job_skipped_counts.get(name, 0) + 1
        self._job_skip_reasons[name] = reason_txt

    def _record_source_skip(
        self,
        health_key: str,
        source: IngestSourceDescriptor,
        reason: str,
        health: Optional[Dict[str, object]] = None,
        *,
        source_type: str = "",
        path: str = "",
    ) -> None:
        key = str(health_key or "").strip() or source_health_key(source)
        health_data = dict(health or {})
        self._source_skip_reasons[key] = {
            "reason": str(reason or "skipped").strip() or "skipped",
            "label": source.label,
            "family": source.family,
            "source_type": str(source_type or source.source_type or "").strip(),
            "source_id": source.source_id,
            "radio_id": source.radio_id,
            "app_instance_id": source.app_instance_id,
            "path": str(path or source.path or "").strip(),
            "endpoint": source.endpoint,
            "cooldown_remaining_sec": float(health_data.get("cooldown_remaining_sec") or 0.0),
            "skipped_at_ts": time.time(),
        }

    def _clear_source_skip(self, health_key: str) -> None:
        key = str(health_key or "").strip()
        if key:
            self._source_skip_reasons.pop(key, None)

    def _ingest_js8_links(self, *, force: bool = False) -> None:
        def job() -> None:
            decision = self._source_backed_refresh_decision(
                job_name="js8_links",
                family="js8call",
                source_types=("file",),
                force=force,
                max_quiet_sec=900.0,
            )
            if not decision.should_run:
                self._job_refresh_decisions["js8_links"] = decision.as_dict()
                self._record_job_skip("js8_links", decision.reason)
                self._job_refresh_skip_reasons["js8_links"] = decision.reason
                log.debug("BackgroundIngest: skipping JS8 links ingest; refresh fingerprint %s", decision.reason)
                return

            self._run_js8_links_job()
            self._job_refresh_fingerprints["js8_links"] = decision.fingerprint
            self._job_refresh_last_run_ts["js8_links"] = time.time()
            self._job_refresh_decisions["js8_links"] = decision.as_dict()
            self._job_refresh_skip_reasons.pop("js8_links", None)

        self._submit_job("js8_links", job)

    def _run_js8_links_job(self) -> None:
        worker_settings = self._new_worker_settings()
        try:
            db_path = worker_settings.config_dir / "freqinout_nets.db"
            indexer = JS8LogLinkIndexer(worker_settings, db_path)
            last_ts = float(worker_settings.get("js8_links_last_load_utc", 0) or 0)
            count = self._run_js8_links_for_sources(indexer, last_ts=last_ts)
            latest_ts = max(indexer._ensure_latest_ts(last_default=time.time()), time.time())
            try:
                worker_settings.set("js8_links_last_load_utc", latest_ts)
            except Exception:
                pass
            if count:
                log.debug("BackgroundIngest: js8_links ingested=%s", count)
        except Exception as e:
            log.debug("BackgroundIngest: js8_links ingest failed: %s", e)
        finally:
            worker_settings.close()

    def _run_js8_links_for_sources(self, indexer: JS8LogLinkIndexer, *, last_ts: float = 0.0) -> int:
        inventory = self._runtime_ingest_inventory()
        instances = [instance for instance in inventory.app_instances if instance.family == "js8call"]
        was_empty = indexer.link_count() == 0
        if not instances:
            count = indexer.update(since_ts=last_ts if last_ts > 0 else None)
            if count <= 0 and was_empty:
                log.info("BackgroundIngest: rebuilding empty js8_links from legacy source")
                count = indexer.update(since_ts=None, force_rebuild=True)
            return count
        total = 0
        for instance in instances:
            source_by_role = {
                str(source.metadata.get("role", "") or ""): source
                for source in js8_ingest_sources(instance)
                if source.source_type == "file"
            }
            directed_source = source_by_role.get("directed")
            if directed_source is None or not directed_source.path:
                continue
            health_key = source_health_key(directed_source)
            may_run, health = self._health.may_run(health_key, owner="BackgroundIngest")
            if not may_run:
                self._record_source_skip(health_key, directed_source, "backoff", health)
                continue
            started_at = time.time()
            try:
                counts = indexer.update_from_ingest_sources(
                    source_by_role.values(),
                    since_ts=last_ts if last_ts > 0 else None,
                )
                inserted = sum(int(value or 0) for value in counts.values())
                if inserted <= 0 and was_empty:
                    log.info(
                        "BackgroundIngest: rebuilding empty js8_links for %s",
                        directed_source.label,
                    )
                    counts = indexer.update_from_ingest_sources(
                        source_by_role.values(),
                        since_ts=None,
                        force_rebuild=True,
                    )
                    inserted = sum(int(value or 0) for value in counts.values())
                total += inserted
                self._health.record_success(
                    health_key,
                    owner="BackgroundIngest",
                    duration_ms=(time.time() - started_at) * 1000.0,
                    slow_ms=5000.0,
                    metadata={
                        "label": directed_source.label,
                        "family": directed_source.family,
                        "source_type": directed_source.source_type,
                        "path": directed_source.path,
                        "inserted": inserted,
                    },
                )
                self._clear_source_skip(health_key)
            except Exception as exc:
                self._health.record_failure(
                    health_key,
                    owner="BackgroundIngest",
                    error=str(exc),
                    duration_ms=(time.time() - started_at) * 1000.0,
                    metadata={
                        "label": directed_source.label,
                        "family": directed_source.family,
                        "source_type": directed_source.source_type,
                        "path": directed_source.path,
                    },
                )
                log.debug(
                    "BackgroundIngest: js8_links source ingest failed for %s: %s",
                    directed_source.label,
                    exc,
                )
        return total

    def _runtime_ingest_inventory(self) -> IngestSourceInventory:
        now = time.monotonic()
        cached = self._runtime_inventory_cache
        if cached is not None and (now - float(self._runtime_inventory_cache_ts or 0.0)) < self._runtime_inventory_cache_ttl_sec:
            return cached
        inventory = active_runtime_ingest_inventory(self._runtime_store())
        self._runtime_inventory_cache = inventory
        self._runtime_inventory_cache_ts = now
        return inventory

    def _ingest_messages(self, *, include_observation_backfill: bool = True, force: bool = False) -> None:
        def job() -> None:
            decision = self._message_ingest_refresh_decision(
                include_observation_backfill=include_observation_backfill,
                force=force,
            )
            if not decision.should_run:
                self._job_refresh_decisions["messages"] = decision.as_dict()
                self._record_job_skip("messages", decision.reason)
                self._job_refresh_skip_reasons["messages"] = decision.reason
                log.debug("BackgroundIngest: skipping messages ingest; refresh fingerprint %s", decision.reason)
                return

            self._run_messages_job(include_observation_backfill=include_observation_backfill)
            self._job_refresh_fingerprints["messages"] = decision.fingerprint
            self._job_refresh_last_run_ts["messages"] = time.time()
            self._job_refresh_decisions["messages"] = decision.as_dict()
            self._job_refresh_skip_reasons.pop("messages", None)

        self._submit_job("messages", job)

    def _message_ingest_refresh_decision(
        self,
        *,
        include_observation_backfill: bool,
        force: bool,
    ):
        inventory = self._runtime_ingest_inventory()
        sources = tuple(source for source in inventory.sources_for_family("js8call") if source.source_type in {"file", "api"})
        realtime_present = any(source.source_type == "api" for source in sources)
        fingerprint = ingest_sources_fingerprint(sources, families=("js8call",), source_types=("file", "api"))
        if len(fingerprint) <= 1:
            # Legacy single-profile settings may still have JS8 paths even when the multi-rig
            # inventory is empty, so do not suppress the old ingest path on that basis.
            return plan_ingest_refresh(
                fingerprint,
                previous_fingerprint=None,
                force=force,
                realtime_source_present=realtime_present,
            )
        quiet_sec = 300.0 if include_observation_backfill else 0.0
        return plan_ingest_refresh(
            fingerprint,
            previous_fingerprint=self._job_refresh_fingerprints.get("messages"),
            last_run_ts=float(self._job_refresh_last_run_ts.get("messages", 0.0) or 0.0),
            force=force,
            max_quiet_sec=quiet_sec,
            realtime_source_present=realtime_present,
        )

    def _ingest_dynamic_expect(self, *, force: bool = False) -> None:
        def job() -> None:
            sources = tuple(
                source
                for source in self._runtime_ingest_inventory().sources_for_family("js8call")
                if source.source_type == "file"
                and str(source.metadata.get("role", "") or "") == "directed"
            )
            fingerprint = ingest_sources_fingerprint(
                sources,
                families=("js8call",),
                source_types=("file",),
            )
            decision = plan_ingest_refresh(
                fingerprint,
                previous_fingerprint=self._job_refresh_fingerprints.get("dynamic_expect"),
                last_run_ts=float(self._job_refresh_last_run_ts.get("dynamic_expect", 0.0) or 0.0),
                force=force,
            )
            if not decision.should_run:
                return

            self._run_dynamic_expect_job()
            self._job_refresh_fingerprints["dynamic_expect"] = decision.fingerprint
            self._job_refresh_last_run_ts["dynamic_expect"] = time.time()

        self._submit_realtime_job("dynamic_expect", job)

    def _ingest_dynamic_flamp_projection(self) -> None:
        """Refresh saved FLAMP relay/RX state without touching the GUI thread."""

        self._submit_job("dynamic_flamp_projection", self._run_dynamic_flamp_projection_job)

    def _run_dynamic_flamp_projection_job(
        self, *, profiles: Optional[Sequence[Mapping[str, object]]] = None
    ) -> None:
        selected_profiles = list(profiles) if profiles is not None else self._active_js8_spotter_profiles()
        store = self._runtime_store()
        profile_fallback = self._new_worker_settings()
        try:
            for profile_row in selected_profiles:
                self._cancel_checkpoint()
                profile = dict(profile_row)
                radio_id = int(profile.get("id", 0) or 0)
                if radio_id <= 0:
                    continue
                profile_settings = _DeviceProfileVaultSettings(profile, profile_fallback, store)
                try:
                    if not self._truthy(
                        profile_settings.get("js8_expect_dynamic_flamp_enabled", False), False
                    ):
                        continue
                    js8_instance_id = str(
                        profile.get("js8_instance_id", "")
                        or profile.get("name", "")
                        or radio_id
                    )
                    MessageIngestor(profile_settings).refresh_dynamic_flamp_state(  # type: ignore[arg-type]
                        source_radio_id=radio_id,
                        js8_instance_id=js8_instance_id,
                    )
                except Exception as exc:
                    log.debug(
                        "BackgroundIngest: FLAMP transfer projection failed for %s: %s",
                        str(profile.get("name", "") or radio_id),
                        exc,
                    )
        finally:
            profile_fallback.close()
            self._dynamic_flamp_projection_ready.set()

    def _ensure_initial_dynamic_flamp_projection(
        self, profiles: Sequence[Mapping[str, object]]
    ) -> None:
        """Run one projection before this process consumes its first Q request."""

        if self._dynamic_flamp_projection_ready.is_set():
            return
        self._run_dynamic_flamp_projection_job(profiles=profiles)

    def _run_dynamic_expect_job(self) -> None:
        profiles = self._active_js8_spotter_profiles()
        if not profiles:
            return
        # The initial projection is the startup phase of this worker, not part
        # of the per-request tail. It prevents yesterday's saved relay snapshot
        # from answering before completed FLAMP/rx output is reconciled.
        self._ensure_initial_dynamic_flamp_projection(profiles)
        inventory = self._runtime_ingest_inventory()
        directed_sources_by_radio = {
            str(source.radio_id or ""): source
            for source in inventory.sources_for_family("js8call")
            if source.source_type == "file"
            and str(source.metadata.get("role", "") or "") == "directed"
        }
        worker_settings = self._new_worker_settings()
        coordinator = ExpectAutomationCoordinator(
            worker_settings,
            profiles=profiles,
            guard_preflight=self.expect_guard_preflight,
        )
        store = self._runtime_store()
        try:
            for profile in profiles:
                self._cancel_checkpoint()
                radio_id = int(profile.get("id", 0) or 0)
                source = directed_sources_by_radio.get(str(radio_id))
                directed = str(
                    (source.path if source is not None else "")
                    or profile.get("js8_directed_path", "")
                    or ""
                ).strip()
                if radio_id <= 0 or not directed:
                    continue
                source_id = str(getattr(source, "source_id", "") or "").strip()
                standard_offset = (
                    f"spotter_directed_offset_{source_id}"
                    if source_id
                    else f"spotter_directed_offset_radio_{radio_id}"
                )
                expect_offset = (
                    f"expect_directed_offset_{source_id}"
                    if source_id
                    else f"expect_directed_offset_radio_{radio_id}"
                )
                profile_fallback = self._new_worker_settings()
                profile_settings = _DeviceProfileVaultSettings(profile, profile_fallback, store)
                try:
                    js8_instance_id = str(
                        profile.get("js8_instance_id", "")
                        or profile.get("name", "")
                        or radio_id
                    )
                    MessageIngestor(
                        profile_settings,  # type: ignore[arg-type]
                        expect_dispatch_client_factory=coordinator.client_factory_for_ingest(),
                        expect_auto_reply_enabled=coordinator.runtime_unattended_enabled(),
                    ).ingest_dynamic_flamp_from_directed(
                        directed_path=Path(directed).expanduser(),
                        source_radio_id=radio_id,
                        js8_instance_id=js8_instance_id,
                        source_key=source_id,
                        offset_key=expect_offset,
                        fallback_offset_key=standard_offset,
                    )
                finally:
                    profile_fallback.close()
        finally:
            coordinator.close()
            worker_settings.close()

    def _run_messages_job(self, *, include_observation_backfill: bool = True) -> None:
        worker_settings = self._new_worker_settings()
        try:
            self._cancel_checkpoint()
            msg_ingest = MessageIngestor(worker_settings)
            has_runtime_js8 = bool([instance for instance in self._runtime_ingest_inventory().app_instances if instance.family == "js8call"])
            if has_runtime_js8:
                try:
                    self._run_multi_radio_js8_message_ingest()
                except Exception as e:
                    log.debug("BackgroundIngest: multi-radio JS8 inbox ingest failed: %s", e)
            else:
                self._cancel_checkpoint()
                try:
                    msg_ingest.ingest_js8_messages()
                except Exception as e:
                    log.debug("BackgroundIngest: JS8 inbox ingest failed: %s", e)
                try:
                    self._cancel_checkpoint()
                    msg_ingest.ingest_spotter_from_directed()
                except Exception as e:
                    log.debug("BackgroundIngest: spotter ingest failed: %s", e)
            try:
                self._cancel_checkpoint()
                self._run_multi_radio_spotter_ingest()
            except Exception as e:
                log.debug("BackgroundIngest: multi-radio spotter ingest failed: %s", e)
            if include_observation_backfill:
                try:
                    self._cancel_checkpoint()
                    self._run_observation_backfill(worker_settings)
                except Exception as e:
                    log.debug("BackgroundIngest: observation backfill failed: %s", e)
                try:
                    self._cancel_checkpoint()
                    self._run_condition_sop_invocation(worker_settings)
                except Exception as e:
                    log.debug("BackgroundIngest: condition SOP invocation failed: %s", e)
        finally:
            worker_settings.close()

    def _run_multi_radio_js8_message_ingest(self) -> None:
        active_profiles = self._active_js8_spotter_profiles()
        reconcile_store = self._runtime_store()
        mapping_changed = False
        for profile in active_profiles:
            try:
                outcome = reconcile_js8_storage_profile(reconcile_store, profile)
                mapping_changed = mapping_changed or outcome.state == "verified"
                if outcome.state == "mismatch":
                    log.warning(
                        "BackgroundIngest: JS8 storage needs attention for %s: %s",
                        profile.get("name", "JS8Call"),
                        outcome.detail,
                    )
            except Exception as exc:
                log.debug("BackgroundIngest: JS8 storage reconciliation failed: %s", exc)
        if mapping_changed:
            self._runtime_inventory_cache = None
            self._runtime_inventory_cache_ts = 0.0
        inventory = self._runtime_ingest_inventory()
        inbox_sources = [
            source
            for source in inventory.sources_for_family("js8call")
            if source.source_type == "sqlite"
            and str((source.metadata or {}).get("role", "") or "") == "inbox"
        ]
        if not inbox_sources:
            # Preserve the legacy diagnostic contract: a configured DIRECTED
            # source with no discovered sibling inbox is visible as a missing
            # inbox source, even though no speculative SQLite source is built.
            for directed_source in inventory.sources_for_family("js8call"):
                if (
                    directed_source.source_type != "file"
                    or str((directed_source.metadata or {}).get("role", "") or "") != "directed"
                ):
                    continue
                health_key = f"{source_health_key(directed_source)}:inbox"
                self._record_source_skip(
                    health_key,
                    directed_source,
                    "missing",
                    source_type="js8-inbox",
                    path=str(directed_source.path or ""),
                )
            return
        store = reconcile_store
        profiles = {str(profile.get("id", "") or profile.get("system_key", "") or ""): profile for profile in active_profiles}
        for inbox_source in inbox_sources:
            self._cancel_checkpoint()
            metadata = dict(inbox_source.metadata or {})
            radio_id = str(inbox_source.radio_id or "").strip()
            candidate_radios = tuple(
                str(value or "").strip()
                for value in metadata.get("candidate_radio_ids", ())
                if str(value or "").strip()
            )
            profile = profiles.get(radio_id) or next(
                (profiles[value] for value in candidate_radios if value in profiles),
                {},
            )
            health_key = f"{source_health_key(inbox_source)}:inbox"
            inbox_path = Path(str(inbox_source.path or "")).expanduser()
            if not inbox_path.is_file():
                log.debug(
                    "BackgroundIngest: skipping JS8 inbox ingest for %s; source path missing",
                    inbox_source.label,
                )
                self._health.record_failure(
                    health_key,
                    owner="BackgroundIngest",
                    error="source path missing",
                    metadata={
                        "label": inbox_source.label,
                        "family": inbox_source.family,
                        "source_type": "js8-inbox",
                        "path": str(inbox_path),
                    },
                )
                self._record_source_skip(
                    health_key,
                    inbox_source,
                    "missing",
                    source_type="js8-inbox",
                    path=str(inbox_path),
                )
                continue
            may_run, health = self._health.may_run(health_key, owner="BackgroundIngest")
            if not may_run:
                self._record_source_skip(
                    health_key,
                    inbox_source,
                    "backoff",
                    health,
                    source_type="js8-inbox",
                    path=str(inbox_path),
                )
                continue
            started_at = time.time()
            profile_settings = _DeviceProfileVaultSettings(profile, self._new_worker_settings(), store)
            try:
                ingestor = MessageIngestor(profile_settings)  # type: ignore[arg-type]
                ingestor.ingest_js8_messages(
                    inbox_path=inbox_path,
                    source_radio_id=radio_id,
                    js8_instance_id=str(metadata.get("js8_instance_id", "") or "") if radio_id else "",
                    source_key=str(inbox_source.app_instance_id or inbox_source.source_id),
                )
                self._health.record_success(
                    health_key,
                    owner="BackgroundIngest",
                    duration_ms=(time.time() - started_at) * 1000.0,
                    slow_ms=5000.0,
                    metadata={
                        "label": inbox_source.label,
                        "family": inbox_source.family,
                        "source_type": "js8-inbox",
                        "path": str(inbox_path),
                    },
                )
                self._clear_source_skip(health_key)
            except Exception as exc:
                self._health.record_failure(
                    health_key,
                    owner="BackgroundIngest",
                    error=str(exc),
                    duration_ms=(time.time() - started_at) * 1000.0,
                    metadata={
                        "label": inbox_source.label,
                        "family": inbox_source.family,
                        "source_type": "js8-inbox",
                        "path": str(inbox_path),
                    },
                )
                log.debug("BackgroundIngest: JS8 inbox source ingest failed for %s: %s", inbox_source.label, exc)
            finally:
                try:
                    profile_settings.fallback_settings.close()
                except Exception:
                    pass

    def _run_observation_backfill(self, worker_settings: SettingsManager) -> None:
        try:
            limit = int(worker_settings.get("observation_backfill_batch_limit", 100) or 100)
        except Exception:
            limit = 100
        limit = max(1, min(500, limit))
        db_path = worker_settings.config_dir / "freqinout_nets.db"
        condition_alert_rules = worker_settings.get(CONDITION_ALERT_RULES_SETTING_KEY, None)
        result = backfill_observations(
            db_path,
            batch_limit=limit,
            condition_alert_rules=condition_alert_rules,
        )
        total = sum(int(value or 0) for value in result.values())
        if total:
            log.debug("BackgroundIngest: observation backfill projected=%s detail=%s", total, result)

    def _run_condition_sop_invocation(self, worker_settings: SettingsManager) -> None:
        """Audit condition-alert SOP decisions from background ingest.

        This path is intentionally conservative: condition-alert auto-apply
        must pass the explicit operator gate and assigned-plan RF Guard preflight
        before any setting is changed.
        """
        auto_apply_enabled = self._truthy(worker_settings.get(AUTO_SOP_INVOCATION_SETTING_KEY, False), False)
        db_path = worker_settings.config_dir / "freqinout_nets.db"
        observations = tuple(
            observation
            for observation in query_observations(
                db_path,
                ObservationQuery(source_family="condition_alert", limit=25),
            )
            if observation.observation_id not in self._condition_sop_seen_observation_ids
        )
        if not observations:
            return

        sop_profiles = self._active_condition_sop_profiles(db_path)
        if not sop_profiles:
            return

        provisional_plans = plan_condition_sop_invocations(
            observations,
            settings_data={"operating_groups": worker_settings.get("operating_groups", [])},
            sop_profiles=sop_profiles,
            auto_apply_enabled=auto_apply_enabled,
            rf_guard_state_by_profile={},
        )
        if not provisional_plans:
            return

        plans = plan_condition_sop_invocations(
            observations,
            settings_data={"operating_groups": worker_settings.get("operating_groups", [])},
            sop_profiles=sop_profiles,
            auto_apply_enabled=auto_apply_enabled,
            rf_guard_state_by_profile=self._condition_sop_rf_guard_state_by_profile(sop_profiles, provisional_plans),
        )
        if not plans:
            return

        result = execute_condition_sop_invocation_plans(worker_settings, db_path, plans, apply_limit=1)
        deferred_ids = {record.observation_id for record in result.records if record.status == "deferred"}
        for observation in observations:
            if observation.observation_id not in deferred_ids:
                self._condition_sop_seen_observation_ids.add(observation.observation_id)
        if result.audited_count:
            log.info(
                "BackgroundIngest condition SOP decisions audited=%s applied=%s failed=%s",
                result.audited_count,
                result.applied_count,
                result.failed_count,
            )
            self._queue_controller_thread_call(
                lambda result=result: self.condition_sop_invocation_audited.emit(result)
            )
        if result.applied_count:
            self._queue_controller_thread_call(
                lambda result=result: self.condition_sop_invocation_applied.emit(result)
            )

    def _active_condition_sop_profiles(self, db_path: Path) -> tuple[Mapping[str, Any], ...]:
        manager = SOPManager(db_path=db_path)
        try:
            profiles: list[Mapping[str, Any]] = []
            for summary in manager.list_profiles():
                if not self._truthy(summary.get("active", False), False):
                    continue
                try:
                    profile_id = int(summary.get("id", 0) or 0)
                except Exception:
                    profile_id = 0
                if profile_id <= 0:
                    continue
                profile = manager.get_profile(profile_id)
                if not profile:
                    continue
                if not self._condition_sop_profile_layers(profile):
                    continue
                profiles.append(dict(profile))
            return tuple(profiles)
        finally:
            try:
                manager.settings.close()
            except Exception:
                pass

    @staticmethod
    def _condition_sop_profile_layers(profile: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        raw_layers = profile.get("schedule_layer") or profile.get("layers") or ()
        if isinstance(raw_layers, (str, bytes)) or not isinstance(raw_layers, Sequence):
            return ()
        return tuple(layer for layer in raw_layers if isinstance(layer, Mapping))

    def _condition_sop_rf_guard_state_by_profile(
        self,
        profiles: Sequence[Mapping[str, Any]],
        plans: Sequence[ConditionSopInvocationPlan],
    ) -> Mapping[str, Mapping[str, object]]:
        profile_by_id = {
            str(profile.get("id") or profile.get("profile_id") or profile.get("sop_profile_id") or "").strip(): profile
            for profile in profiles
        }
        states: dict[str, Mapping[str, object]] = {}
        for plan in plans:
            profile_id = str(plan.decision.sop_profile_id or "").strip()
            if not profile_id:
                continue
            if profile_id in states:
                continue
            profile = profile_by_id.get(profile_id)
            if not profile:
                states[profile_id] = {
                    "state": "blocked",
                    "messages": ["RF Guard preflight could not find the matching SOP profile."],
                }
                continue
            rows = schedule_layer_rows_for_condition_decision(profile, plan.decision)
            if not rows:
                states[profile_id] = {
                    "state": "blocked",
                    "messages": ["RF Guard preflight found no enabled SOP schedule rows for this condition level."],
                }
                continue
            try:
                impacts = assigned_plan_rf_guard_impacts_for_sop_update(int(profile_id), [dict(row) for row in rows])
            except Exception as exc:
                states[profile_id] = {
                    "state": "blocked",
                    "messages": [f"RF Guard preflight failed: {exc}"],
                }
                continue
            if impacts:
                states[profile_id] = {
                    "state": "blocked",
                    "messages": list(self._condition_sop_rf_guard_messages(impacts)),
                }
                continue
            states[profile_id] = {
                "state": "ok",
                "messages": ["RF Guard preflight passed for assigned plans."],
            }
        return states

    @staticmethod
    def _condition_sop_rf_guard_messages(impacts: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        messages: list[str] = []
        for impact in impacts:
            plan = impact.get("plan") if isinstance(impact, Mapping) else {}
            device = impact.get("device") if isinstance(impact, Mapping) else {}
            validation = impact.get("validation") if isinstance(impact, Mapping) else {}
            plan_name = str((plan or {}).get("name") or (plan or {}).get("plan_name") or "Frequency Plan").strip()
            radio_name = str((device or {}).get("radio_name") or (device or {}).get("name") or "Radio").strip()
            prefix = f"{radio_name} / {plan_name}: "
            if not isinstance(validation, Mapping):
                messages.append(prefix + "RF Guard reported a schedule conflict.")
                continue
            raw_messages = []
            for key in ("blocked", "warnings", "messages"):
                value = validation.get(key)
                if isinstance(value, str):
                    raw_messages.append(value)
                elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    raw_messages.extend(str(item) for item in value if str(item or "").strip())
            if not raw_messages:
                raw_messages.append("RF Guard reported a schedule conflict.")
            for message in raw_messages:
                clean = str(message or "").strip()
                if clean:
                    messages.append(prefix + clean)
        deduped = list(dict.fromkeys(messages))
        return tuple(deduped[:6] or ["RF Guard reported a condition-alert SOP conflict."])

    def _active_js8_spotter_profiles(self) -> list[Dict[str, object]]:
        profiles = self._runtime_active_profiles()
        out: list[Dict[str, object]] = []
        for profile in profiles:
            if not self._truthy(profile.get("use_js8call", False), False) and not self._truthy(profile.get("use_js8spotter", False), False):
                continue
            out.append(profile)
        return out

    def _run_multi_radio_spotter_ingest(self) -> None:
        profiles = self._active_js8_spotter_profiles()
        if not profiles:
            return
        inventory = self._runtime_ingest_inventory()
        directed_sources = tuple(
            source
            for source in inventory.sources_for_family("js8call")
            if source.source_type == "file"
            and str(source.metadata.get("role", "") or "") == "directed"
            and source.path
        )
        profiles_by_id = {
            str(profile.get("id", "") or profile.get("system_key", "") or "").strip(): profile
            for profile in profiles
        }
        store = self._runtime_store()
        worker_settings = self._new_worker_settings()
        coordinator = ExpectAutomationCoordinator(
            worker_settings,
            profiles=profiles,
            guard_preflight=self.expect_guard_preflight,
        )
        for directed_source in directed_sources:
            metadata = dict(directed_source.metadata or {})
            radio_id_text = str(directed_source.radio_id or "").strip()
            candidate_radios = tuple(
                str(value or "").strip()
                for value in metadata.get("candidate_radio_ids", ())
                if str(value or "").strip()
            )
            profile = profiles_by_id.get(radio_id_text) or next(
                (profiles_by_id[value] for value in candidate_radios if value in profiles_by_id),
                {},
            )
            directed = str(directed_source.path or "").strip()
            directed_source_id = str(directed_source.app_instance_id or directed_source.source_id or "").strip()
            radio_id = int(radio_id_text) if radio_id_text.isdigit() else 0
            health_key = f"{source_health_key(directed_source)}:spotter"
            may_run, health = self._health.may_run(health_key, owner="BackgroundIngest")
            if not may_run:
                self._record_source_skip(
                    health_key,
                    directed_source,
                    "backoff",
                    health,
                    source_type="spotter-directed",
                    path=directed,
                )
                continue
            if not Path(directed).expanduser().exists():
                self._health.record_failure(
                    health_key,
                    owner="BackgroundIngest",
                    error="source path missing",
                    metadata={
                        "label": directed_source.label,
                        "family": directed_source.family,
                        "source_type": "spotter-directed",
                        "path": directed_source.path,
                    },
                )
                self._record_source_skip(
                    health_key,
                    directed_source,
                    "missing",
                    source_type="spotter-directed",
                    path=directed,
                )
                continue
            started_at = time.time()
            profile_fallback = self._new_worker_settings()
            profile_settings = _DeviceProfileVaultSettings(profile, profile_fallback, store)
            try:
                ingestor = MessageIngestor(
                    profile_settings,  # type: ignore[arg-type]
                    expect_dispatch_client_factory=coordinator.client_factory_for_ingest(),
                    expect_auto_reply_enabled=coordinator.runtime_unattended_enabled(),
                )
                js8_instance_id = str(metadata.get("js8_instance_id", "") or "") if radio_id_text else ""
                inserted = ingestor.ingest_spotter_from_directed(
                    directed_path=Path(directed).expanduser(),
                    source_radio_id=radio_id,
                    js8_instance_id=js8_instance_id,
                    source_key=directed_source_id,
                    offset_key=f"spotter_directed_offset_{directed_source_id}" if directed_source_id else f"spotter_directed_offset_radio_{radio_id}",
                    evaluate_expect=False,
                )
                if health_key and directed_source is not None:
                    self._health.record_success(
                        health_key,
                        owner="BackgroundIngest",
                        duration_ms=(time.time() - started_at) * 1000.0,
                        slow_ms=5000.0,
                        metadata={
                            "label": directed_source.label,
                            "family": directed_source.family,
                            "source_type": "spotter-directed",
                            "path": directed_source.path,
                            "inserted": int(inserted or 0),
                        },
                    )
                    self._clear_source_skip(health_key)
            except Exception as exc:
                if health_key and directed_source is not None:
                    self._health.record_failure(
                        health_key,
                        owner="BackgroundIngest",
                        error=str(exc),
                        duration_ms=(time.time() - started_at) * 1000.0,
                        metadata={
                            "label": directed_source.label,
                            "family": directed_source.family,
                            "source_type": "spotter-directed",
                            "path": directed_source.path,
                        },
                    )
                log.debug("BackgroundIngest: spotter ingest failed for %s: %s", directed_source.label, exc)
            finally:
                profile_fallback.close()
        try:
            coordinator.close()
        finally:
            try:
                worker_settings.close()
            except Exception:
                pass

    def _ingest_varac(self, *, force: bool = False) -> None:
        def job() -> None:
            decision = self._source_backed_refresh_decision(
                job_name="varac",
                family="varac",
                source_types=("sqlite",),
                force=force,
                max_quiet_sec=600.0,
            )
            if not decision.should_run:
                self._job_refresh_decisions["varac"] = decision.as_dict()
                self._record_job_skip("varac", decision.reason)
                self._job_refresh_skip_reasons["varac"] = decision.reason
                log.debug("BackgroundIngest: skipping VarAC ingest; refresh fingerprint %s", decision.reason)
                return

            self._run_varac_job()
            self._job_refresh_fingerprints["varac"] = decision.fingerprint
            self._job_refresh_last_run_ts["varac"] = time.time()
            self._job_refresh_decisions["varac"] = decision.as_dict()
            self._job_refresh_skip_reasons.pop("varac", None)

        self._submit_job("varac", job)

    def _source_backed_refresh_decision(
        self,
        *,
        job_name: str,
        family: str,
        source_types: tuple[str, ...],
        force: bool,
        max_quiet_sec: float,
    ):
        inventory = self._runtime_ingest_inventory()
        sources = tuple(source for source in inventory.sources_for_family(family) if source.source_type in source_types)
        fingerprint = ingest_sources_fingerprint(sources, families=(family,), source_types=source_types)
        if len(fingerprint) <= 1:
            return plan_ingest_refresh(fingerprint, previous_fingerprint=None, force=force)
        return plan_ingest_refresh(
            fingerprint,
            previous_fingerprint=self._job_refresh_fingerprints.get(job_name),
            last_run_ts=float(self._job_refresh_last_run_ts.get(job_name, 0.0) or 0.0),
            force=force,
            max_quiet_sec=max_quiet_sec,
        )

    def _ingest_varac_vault(self) -> None:
        self._update_varac_vault_timer_state()
        if not self._varac_vault_enabled():
            return
        self._submit_realtime_job("varac_vault", self._run_varac_vault_job)

    def request_varac_vault_refresh(self, reason: str = "manual") -> None:
        self._varac_vault_no_change_runs = 0
        self._varac_vault_full_interval_ms = self._VARAC_VAULT_ACTIVE_INTERVAL_MS
        if str(reason or "").strip().lower() == "settings_saved":
            self._varac_vault_refresh_pending = True
            self._refresh_varac_vault_enabled_async()
            return
        self._update_varac_vault_timer_state()
        log.debug("VARAC_VAULT_CADENCE|refresh_requested|reason=%s", str(reason or "manual"))
        if not self._running or not self._varac_vault_enabled():
            return
        with self._realtime_executor_lock:
            future = self._realtime_job_futures.get("varac_vault")
            if future is not None and not future.done():
                self._varac_vault_refresh_pending = True
                return
        self._ingest_varac_vault()

    def _probe_varac_vault_activity(self) -> None:
        if not self._running or not self._varac_vault_enabled():
            return
        self._submit_realtime_job("varac_vault_probe", self._run_varac_vault_activity_probe)

    def _run_varac_vault_activity_probe(self) -> object:
        worker_settings = self._new_worker_settings()
        try:
            profiles = self._active_varac_vault_profiles()
            if not profiles:
                return build_varac_bbs_vault_activity_signature(worker_settings)
            store = self._runtime_store()
            signatures = []
            for profile in profiles:
                profile_id = str(profile.get("id", "") or profile.get("system_key", "") or profile.get("name", "") or "")
                profile_settings = _DeviceProfileVaultSettings(profile, worker_settings, store)
                signatures.append((profile_id, build_varac_bbs_vault_activity_signature(profile_settings)))
            return tuple(signatures)
        except Exception as e:
            log.debug("VARAC_VAULT_CADENCE|activity_probe_failed|error=%s", e)
            return None
        finally:
            worker_settings.close()

    def _on_varac_vault_activity_result(self, signature: object) -> None:
        if signature is None:
            return
        previous = self._varac_vault_activity_signature
        self._varac_vault_activity_signature = signature
        if previous is None:
            return
        if signature != previous:
            log.debug(
                "VARAC_VAULT_CADENCE|activity_changed|reason=signature|full_interval_ms=%s",
                self._varac_vault_full_interval_ms,
            )
            self.request_varac_vault_refresh("activity_changed")

    def _on_varac_vault_result(self, result: object) -> None:
        results = (
            [item for item in result if isinstance(item, VaracBbsVaultRunResult)]
            if isinstance(result, (list, tuple))
            else [result] if isinstance(result, VaracBbsVaultRunResult)
            else []
        )
        if not results:
            log.debug("VARAC_VAULT_CADENCE|full_job_result_missing|idle_backoff_skipped=true")
            self._update_varac_vault_timer_state()
            if self._varac_vault_refresh_pending:
                self._varac_vault_refresh_pending = False
                self._ingest_varac_vault()
            return
        processed = sum(int(item.processed_events or 0) for item in results)
        publish_changed = any(bool(item.publish_changed or item.published) for item in results)
        state_changed = any(bool(item.state_changed or item.unmanaged_live_files_changed) for item in results)
        active_session = any(bool(item.active_session or item.current_session_callsign) for item in results)
        changed = bool(processed or publish_changed or state_changed)
        if changed or active_session:
            self._varac_vault_no_change_runs = 0
            self._varac_vault_full_interval_ms = self._VARAC_VAULT_ACTIVE_INTERVAL_MS
        else:
            self._varac_vault_no_change_runs += 1
            self._varac_vault_full_interval_ms = (
                self._VARAC_VAULT_IDLE_INTERVAL_MS
                if self._varac_vault_no_change_runs >= 3
                else self._VARAC_VAULT_WARM_IDLE_INTERVAL_MS
            )
            log.debug(
                "VARAC_VAULT_CADENCE|idle_backoff|no_change_runs=%s|next_interval_ms=%s",
                self._varac_vault_no_change_runs,
                self._varac_vault_full_interval_ms,
            )
        log.debug(
            "VARAC_VAULT_CADENCE|full_job_result|profiles=%s|processed=%s|publish_changed=%s|state_changed=%s|active_session=%s|next_interval_ms=%s",
            len(results),
            processed,
            publish_changed,
            state_changed,
            active_session,
            self._varac_vault_full_interval_ms,
        )
        self._update_varac_vault_timer_state()
        if self._varac_vault_refresh_pending:
            self._varac_vault_refresh_pending = False
            self._ingest_varac_vault()

    @staticmethod
    def _job_health_key(job_name: str) -> str:
        return f"background-ingest:{str(job_name or '').strip().lower() or 'unknown'}"

    def _ingest_varac_guard(self) -> None:
        self._submit_job("varac_guard", self._run_varac_guard_job)

    def _run_varac_job(self) -> None:
        worker_settings = self._new_worker_settings()
        try:
            inventory = self._runtime_ingest_inventory()
            varac_sources = list(inventory.sources_for_family("varac"))
            if not varac_sources:
                ingest_varac(worker_settings)
                return
            store = self._runtime_store()
            profiles_by_id = {str(profile.get("id", "") or profile.get("system_key", "") or ""): profile for profile in self._active_varac_profiles()}
            for source in varac_sources:
                profile = profiles_by_id.get(str(source.radio_id or ""))
                if profile is None:
                    continue
                profile_settings = _DeviceProfileVaultSettings(profile, worker_settings, store)
                health_key = source_health_key(source)
                may_run, health = self._health.may_run(health_key, owner="BackgroundIngest")
                if not may_run:
                    self._record_source_skip(health_key, source, "backoff", health)
                    continue
                started_at = time.time()
                try:
                    success = ingest_varac(
                        profile_settings,
                        ingest_source_key=source.source_id,
                        ingest_scope="runtime-active",
                        ingest_source_label=source.label,
                    )
                    if success:
                        self._health.record_success(
                            health_key,
                            owner="BackgroundIngest",
                            duration_ms=(time.time() - started_at) * 1000.0,
                            slow_ms=5000.0,
                            metadata={
                                "label": source.label,
                                "family": source.family,
                                "source_type": source.source_type,
                                "path": source.path,
                            },
                        )
                        self._clear_source_skip(health_key)
                    else:
                        self._health.record_failure(
                            health_key,
                            owner="BackgroundIngest",
                            error="VarAC source ingest did not complete",
                            duration_ms=(time.time() - started_at) * 1000.0,
                            metadata={
                                "label": source.label,
                                "family": source.family,
                                "source_type": source.source_type,
                                "path": source.path,
                            },
                        )
                except Exception as exc:
                    self._health.record_failure(
                        health_key,
                        owner="BackgroundIngest",
                        error=str(exc),
                        duration_ms=(time.time() - started_at) * 1000.0,
                        metadata={
                            "label": source.label,
                            "family": source.family,
                            "source_type": source.source_type,
                            "path": source.path,
                        },
                    )
                    log.debug("BackgroundIngest: VarAC source ingest failed for %s: %s", source.label, exc)
        except Exception as e:
            log.debug("BackgroundIngest: VarAC ingest failed: %s", e)
        finally:
            worker_settings.close()

    def _active_varac_profiles(self) -> list[Dict[str, object]]:
        profiles = self._runtime_active_profiles()
        return [
            profile
            for profile in profiles
            if self._truthy(profile.get("use_varac", False), False)
            and str(profile.get("varac_db_path", "") or profile.get("varac_path", "") or "").strip()
        ]

    def _run_varac_vault_job(self) -> object:
        worker_settings = self._new_worker_settings()
        try:
            profiles = self._active_varac_vault_profiles()
            if profiles:
                store = self._runtime_store()
                try:
                    catalog_db_path = bbs_library_db_path_from_settings(worker_settings)
                    with connect_sqlite(catalog_db_path) as catalog_conn:
                        ensure_bbs_library_schema(catalog_conn)
                        with catalog_conn:
                            import_legacy_station_bbs_profiles(
                                catalog_conn,
                                list(store.list_device_profiles()),
                            )
                            reconcile_result = reconcile_bbs_publications(catalog_conn)
                    if reconcile_result.missing or reconcile_result.restored or reconcile_result.expired:
                        log.info(
                            "BBS_RECONCILE|checked=%s|missing=%s|restored=%s|expired=%s|remaining=%s",
                            reconcile_result.checked,
                            reconcile_result.missing,
                            reconcile_result.restored,
                            reconcile_result.expired,
                            reconcile_result.remaining,
                        )
                except Exception as exc:
                    log.warning("BackgroundIngest: station BBS catalog preparation failed: %s", exc)
                results: list[VaracBbsVaultRunResult] = []
                by_live_dir: Dict[str, list[Dict[str, object]]] = {}
                for profile in profiles:
                    key = self._normalized_bbs_dir(profile.get("varac_bbs_dir", ""))
                    if key:
                        by_live_dir.setdefault(key, []).append(profile)
                duplicate_dirs = {key for key, rows in by_live_dir.items() if len(rows) > 1}
                warning_signature = tuple(sorted(
                    (key, tuple(sorted((str(row.get("id", "")), str(row.get("name", ""))) for row in by_live_dir[key])))
                    for key in duplicate_dirs
                ))
                warn_duplicates = warning_signature != getattr(self, "_duplicate_bbs_warning_signature", None)
                self._duplicate_bbs_warning_signature = warning_signature
                for profile in profiles:
                    profile_name = str(profile.get("name", "") or profile.get("system_key", "") or profile.get("id", "") or "radio").strip()
                    live_key = self._normalized_bbs_dir(profile.get("varac_bbs_dir", ""))
                    if live_key in duplicate_dirs:
                        summary = (
                            "Managed BBS Library skipped: duplicate live BBS directory is configured on more than one active radio."
                        )
                        try:
                            profile_id = int(profile.get("id", 0) or 0)
                            # Runtime discovery may return a cached profile;
                            # compare the persisted warning on this worker.
                            saved_profile = store.get_device_profile(profile_id) if profile_id > 0 else None
                            # This job repeats every few seconds. An unchanged
                            # warning must not rewrite radio configuration and
                            # invalidate every schedule/Plan Builder projection.
                            if saved_profile and saved_profile.get("varac_bbs_vault_last_summary") != summary:
                                store.save_device_profile(
                                    {
                                        "id": profile_id,
                                        "varac_bbs_vault_last_summary": summary,
                                    }
                                )
                        except Exception as exc:
                            log.debug("BackgroundIngest: failed to persist duplicate BBS warning for %s: %s", profile_name, exc)
                        if warn_duplicates:
                            log.warning("BackgroundIngest: VarAC vault skipped duplicate live BBS directory for %s", profile_name)
                        continue
                    vault_result = run_varac_bbs_vault(_DeviceProfileVaultSettings(profile, worker_settings, store))
                    results.append(vault_result)
                    if bool(vault_result.enabled) and (
                        int(vault_result.processed_events or 0) > 0 or bool(vault_result.published)
                    ):
                        log.debug("BackgroundIngest: VarAC vault [%s] %s", profile_name, vault_result.summary)
                return results
            else:
                self._duplicate_bbs_warning_signature = ()
                vault_result = run_varac_bbs_vault(worker_settings)
                if bool(vault_result.enabled) and (
                    int(vault_result.processed_events or 0) > 0 or bool(vault_result.published)
                ):
                    log.debug("BackgroundIngest: VarAC vault %s", vault_result.summary)
                return vault_result
        except Exception as e:
            log.debug("BackgroundIngest: VarAC vault failed: %s", e)
            return None
        finally:
            worker_settings.close()

    def _run_varac_guard_job(self) -> None:
        worker_settings = self._new_worker_settings()
        try:
            profiles = self._active_varac_profiles()
            # Preserve legacy station behavior when no active radio source exists.
            if not profiles:
                run_varac_guard(worker_settings)
                return
            store = self._runtime_store()
            from freqinout.core.varac_bbs_sync import _station_values
            try:
                station_callsigns = ",".join(_station_values(store)["allowed_callsigns"])
            except Exception:
                station_callsigns = None
            for profile in profiles:
                if not str(profile.get("varac_incoming_path") or "").strip():
                    continue  # never act on another radio's fallback inbound folder
                if station_callsigns is not None:
                    profile = dict(profile, varac_bbs_allowed_callsigns=station_callsigns)
                try:
                    result = run_varac_guard(_DeviceProfileGuardSettings(profile, worker_settings, store))
                    if int(result.scanned_events or 0) > 0:
                        log.debug("BackgroundIngest: VarAC guard [%s] %s", profile.get("name"), result.summary)
                except Exception as exc:
                    log.debug("BackgroundIngest: VarAC guard [%s] failed: %s", profile.get("name"), exc)
        except Exception as e:
            log.debug("BackgroundIngest: VarAC guard failed: %s", e)
        finally:
            worker_settings.close()

    def _ingest_sitreps(self, *, force: bool = False) -> None:
        self._submit_job("sitreps", lambda: self._run_sitreps_job(force=force))

    def _run_sitreps_job(self, *, force: bool = False) -> None:
        worker_settings = self._new_worker_settings()
        try:
            if self._should_run_legacy_sitrep_ingest(force=force):
                stats = ingest_sitreps(worker_settings, max_rows_per_source=500)
                if int(stats.get("events_inserted", 0)) > 0:
                    log.debug(
                        "BackgroundIngest: sitrep ingest scanned=%s inserted=%s errors=%s",
                        stats.get("rows_scanned", 0),
                        stats.get("events_inserted", 0),
                        stats.get("errors", 0),
                    )
            else:
                self._record_job_skip("sitreps:legacy", "unchanged")
                self._job_refresh_skip_reasons["sitreps:legacy"] = "unchanged"
            self._run_commstat_source_sitrep_ingest(worker_settings, force=force)
            fused = fuse_sitreps(worker_settings, max_rows=1000)
            if int(fused.get("events_upserted", 0)) > 0 or int(fused.get("latest_updated", 0)) > 0:
                log.debug(
                    "BackgroundIngest: sitrep fusion scanned=%s upserted=%s latest=%s errors=%s",
                    fused.get("rows_scanned", 0),
                    fused.get("events_upserted", 0),
                    fused.get("latest_updated", 0),
                    fused.get("errors", 0),
                )
        except Exception as e:
            log.debug("BackgroundIngest: sitrep ingest failed: %s", e)
        finally:
            worker_settings.close()

    def _should_run_legacy_sitrep_ingest(self, *, force: bool = False) -> bool:
        decision = self._source_backed_refresh_decision(
            job_name="sitreps:legacy",
            family="js8call",
            source_types=("file",),
            force=force,
            max_quiet_sec=900.0,
        )
        if decision.should_run:
            self._job_refresh_fingerprints["sitreps:legacy"] = decision.fingerprint
            self._job_refresh_last_run_ts["sitreps:legacy"] = time.time()
            self._job_refresh_decisions["sitreps:legacy"] = decision.as_dict()
            self._job_refresh_skip_reasons.pop("sitreps:legacy", None)
            return True
        self._job_refresh_decisions["sitreps:legacy"] = decision.as_dict()
        return False

    @staticmethod
    def _commstat_source_ingest_batch_limit(settings: SettingsManager) -> int:
        try:
            raw = settings.get("commstat_source_ingest_batch_limit", 50000)
        except Exception:
            raw = 50000
        try:
            return max(1000, min(250000, int(raw or 50000)))
        except Exception:
            return 50000

    def _run_commstat_source_sitrep_ingest(self, worker_settings: SettingsManager, *, force: bool = False) -> None:
        inventory = self._runtime_ingest_inventory()
        commstat_sources = list(inventory.sources_for_family("commstat"))
        if not commstat_sources:
            return
        decision = self._source_backed_refresh_decision(
            job_name="sitreps:commstat",
            family="commstat",
            source_types=("sqlite",),
            force=force,
            max_quiet_sec=900.0,
        )
        if not decision.should_run:
            self._job_refresh_decisions["sitreps:commstat"] = decision.as_dict()
            self._record_job_skip("sitreps:commstat", decision.reason)
            self._job_refresh_skip_reasons["sitreps:commstat"] = decision.reason
            return
        store = self._runtime_store()
        profiles_by_id = {str(profile.get("id", "") or profile.get("system_key", "") or ""): profile for profile in store.list_runtime_active_device_profiles()}
        batch_limit = self._commstat_source_ingest_batch_limit(worker_settings)
        for source in commstat_sources:
            profile = dict(profiles_by_id.get(str(source.radio_id or ""), {}) or {})
            if not profile:
                continue
            profile.update(
                {
                    "commstat_db_path": source.path,
                    "commstat3_db_path": source.path,
                    "sitrep_ingest_js8spotter_enabled": False,
                    "sitrep_ingest_commstat3_enabled": True,
                    "sitrep_ingest_commstat23_enabled": False,
                }
            )
            health_key = source_health_key(source)
            may_run, health = self._health.may_run(health_key, owner="BackgroundIngest")
            if not may_run:
                self._record_source_skip(health_key, source, "backoff", health)
                continue
            started_at = time.time()
            profile_settings = _DeviceProfileVaultSettings(profile, worker_settings, store)
            try:
                stats = ingest_sitreps(
                    profile_settings,
                    max_rows_per_source=batch_limit,
                    ingest_scope_key=source.source_id,
                )
                log.info(
                    "BackgroundIngest: CommStat source ingest path=%s label=%s scanned=%s inserted=%s batch_limit=%s",
                    source.path,
                    source.label,
                    int(stats.get("rows_scanned", 0) or 0),
                    int(stats.get("events_inserted", 0) or 0),
                    batch_limit,
                )
                self._health.record_success(
                    health_key,
                    owner="BackgroundIngest",
                    duration_ms=(time.time() - started_at) * 1000.0,
                    slow_ms=5000.0,
                    metadata={
                        "label": source.label,
                        "family": source.family,
                        "source_type": source.source_type,
                        "path": source.path,
                        "rows_scanned": int(stats.get("rows_scanned", 0) or 0),
                        "events_inserted": int(stats.get("events_inserted", 0) or 0),
                    },
                )
                self._clear_source_skip(health_key)
            except Exception as exc:
                self._health.record_failure(
                    health_key,
                    owner="BackgroundIngest",
                    error=str(exc),
                    duration_ms=(time.time() - started_at) * 1000.0,
                    metadata={
                        "label": source.label,
                        "family": source.family,
                        "source_type": source.source_type,
                        "path": source.path,
                    },
                )
                log.debug("BackgroundIngest: CommStat source SitRep ingest failed for %s: %s", source.label, exc)
        self._job_refresh_fingerprints["sitreps:commstat"] = decision.fingerprint
        self._job_refresh_last_run_ts["sitreps:commstat"] = time.time()
        self._job_refresh_decisions["sitreps:commstat"] = decision.as_dict()
        self._job_refresh_skip_reasons.pop("sitreps:commstat", None)

    def _ingest_prop_outcomes(self) -> None:
        self._submit_job("prop_outcomes", self._run_prop_outcomes_job)

    def _run_prop_outcomes_job(self) -> None:
        worker_settings = self._new_worker_settings()
        try:
            stats = ingest_propagation_outcomes(worker_settings, max_rows_per_source=500)
            if int(stats.get("events_inserted", 0)) > 0:
                log.debug(
                    "BackgroundIngest: propagation outcomes scanned=%s inserted=%s stats=%s",
                    stats.get("rows_scanned", 0),
                    stats.get("events_inserted", 0),
                    stats.get("stats_updated", 0),
                )
        except Exception as e:
            log.debug("BackgroundIngest: propagation outcome ingest failed: %s", e)
        finally:
            worker_settings.close()

    def _infer_peer_schedules(self) -> None:
        self._submit_job("peer_schedules", self._run_peer_schedule_job)

    def _run_peer_schedule_job(self) -> None:
        worker_settings = self._new_worker_settings()
        try:
            stats = infer_peer_schedules(worker_settings, lookback_days=56, bucket_minutes=15)
            if int(stats.get("rows_inferred", 0)) > 0:
                log.debug(
                    "BackgroundIngest: peer schedule inference scanned=%s inferred=%s callsigns=%s",
                    stats.get("rows_scanned", 0),
                    stats.get("rows_inferred", 0),
                    stats.get("callsigns_inferred", 0),
                )
        except Exception as e:
            log.debug("BackgroundIngest: peer schedule inference failed: %s", e)
        finally:
            worker_settings.close()
