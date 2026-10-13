from __future__ import annotations

import atexit
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from PySide6.QtCore import Qt, QObject, QCoreApplication, QTimer, Signal, Slot

from freqinout.core.logger import log
from freqinout.core.software_status_service import (
    STATUS_KEYS,
    SoftwareStatusService,
)
from freqinout.core.worker_lifecycle import CancellationToken, OperationCancelled


LEGACY_PRIMARY_DEPENDENCY_SCOPE = "legacy_primary"
PROCESS_STATUS_CADENCE_SEC = 10.0
PROCESS_STATUS_STALE_AFTER_SEC = 30.0
SCOPED_STATUS_STALE_AFTER_SEC = 15.0


def _status_program_name(status_key: str) -> str:
    return "JS8Call" if str(status_key or "") == "JS8Call_API" else str(status_key or "")


@dataclass(frozen=True)
class DependencyStatus:
    key: str
    state: str
    value: Optional[str] = None
    checked_at: float = 0.0
    updated_at: float = 0.0
    stale_after_sec: float = PROCESS_STATUS_STALE_AFTER_SEC
    source: str = "process"
    tooltip: str = ""
    running: bool = False
    reachable: Optional[bool] = None
    last_success_at: Optional[float] = None
    last_error_at: Optional[float] = None
    last_error: str = ""
    backoff_until: Optional[float] = None
    next_check_at: Optional[float] = None
    duration_ms: Optional[float] = None
    slow: bool = False
    meta: Mapping[str, Any] = field(default_factory=dict)

    def age_sec(self, *, now: Optional[float] = None) -> Optional[float]:
        if not self.checked_at:
            return None
        return max(0.0, float(now if now is not None else time.time()) - float(self.checked_at))

    def is_fresh_enough(self, max_age_sec: Optional[float] = None, *, now: Optional[float] = None) -> bool:
        state = str(self.state or "").strip().lower()
        if state in {"stale", "unknown", "error"}:
            return False
        age = self.age_sec(now=now)
        if age is None:
            return False
        limit = float(max_age_sec if max_age_sec is not None else self.stale_after_sec)
        return age <= limit

    def to_software_status_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "state": self.state,
            "tooltip": self.tooltip or ("Running" if self.running else "Not running"),
            "running": bool(self.running),
            "reachable": self.reachable,
            "checked_at": self.checked_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "stale": not self.is_fresh_enough(),
            "slow": bool(self.slow),
        }
        if self.value:
            out["value"] = self.value
        if self.last_error:
            out["last_error"] = self.last_error
        if self.backoff_until:
            out["backoff_until"] = self.backoff_until
        if self.next_check_at:
            out["next_check_at"] = self.next_check_at
        if self.duration_ms is not None:
            out["duration_ms"] = self.duration_ms
        if self.meta:
            out.update(dict(self.meta))
        return out


@dataclass(frozen=True)
class DependencySnapshot:
    generated_at: float
    scope: str = LEGACY_PRIMARY_DEPENDENCY_SCOPE
    station_id: Optional[str] = None
    radio_id: Optional[str] = None
    process: Mapping[str, DependencyStatus] = field(default_factory=dict)
    # Launch-owned snapshots retain the exact immutable process records that
    # produced their family status.  Launch authorization must not consult the
    # mutable station-wide cache later because an unrelated timer refresh can
    # replace that cache while a multi-application startup sequence is still
    # running.
    process_records: tuple[Mapping[str, object], ...] = ()
    reason: str = ""
    sequence: int = 0

    def is_fresh_enough(self, max_age_sec: Optional[float] = None, *, now: Optional[float] = None) -> bool:
        if not self.generated_at:
            return False
        age = max(0.0, float(now if now is not None else time.time()) - float(self.generated_at))
        return age <= float(max_age_sec if max_age_sec is not None else PROCESS_STATUS_STALE_AFTER_SEC)

    def to_software_status_snapshot(self) -> Dict[str, Dict[str, object]]:
        out: Dict[str, Dict[str, object]] = {}
        for key in dict.fromkeys((*STATUS_KEYS, *self.process)):
            status = self.process.get(key)
            if status is None:
                out[key] = _unknown_status_dict(key, self.generated_at)
                continue
            out[key] = status.to_software_status_dict()
        return out


def _unknown_status_dict(key: str, checked_at: float = 0.0) -> Dict[str, object]:
    return {
        "state": "idle",
        "tooltip": f"{key}: status not checked yet",
        "running": False,
        "reachable": None,
        "checked_at": checked_at,
        "source": "process",
        "stale": True,
    }


def _initial_snapshot() -> DependencySnapshot:
    now = time.time()
    return DependencySnapshot(
        generated_at=now,
        process={
            key: DependencyStatus(
                key=key,
                state="idle",
                checked_at=0.0,
                updated_at=now,
                tooltip=f"{key}: status not checked yet",
            )
            for key in STATUS_KEYS
        },
    )


def _logger_handlers_available() -> bool:
    for handler in getattr(log, "handlers", []) or []:
        stream = getattr(handler, "stream", None)
        if stream is not None and bool(getattr(stream, "closed", False)):
            return False
    return True


class DependencyStatusService(QObject):
    """
    Main-thread owner for shared dependency status snapshots.

    Slow or potentially blocking probes run in a plain executor worker. UI code
    reads the latest complete immutable snapshot and never performs routine
    status polling itself.
    """

    snapshot_changed = Signal(object)
    _snapshot_ready = Signal(object)

    def __init__(self, settings: Any, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._lock = threading.RLock()
        self._latest_snapshot = _initial_snapshot()
        self._scoped_snapshots: Dict[str, DependencySnapshot] = {}
        self._scoped_pending: set[str] = set()
        self._active_futures: set[Future] = set()
        self._sequence = 0
        self._worker_pending = False
        self._queued_process_refresh_reason = ""
        self._stopped = False
        self._cancel_token = CancellationToken()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fio-dependency-status")
        self._snapshot_ready.connect(self._publish_snapshot, Qt.QueuedConnection)
        self._timer = QTimer(self)
        self._timer.setInterval(int(PROCESS_STATUS_CADENCE_SEC * 1000))
        self._timer.timeout.connect(lambda: self.refresh_now(reason="timer"))
        self._timer.start()
        app = QCoreApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.connect(self.stop)
            except Exception:
                pass
        QTimer.singleShot(0, lambda: self.refresh_now(reason="startup"))

    def latest_snapshot(self) -> DependencySnapshot:
        with self._lock:
            return self._latest_snapshot

    def software_status_snapshot(self) -> Dict[str, Dict[str, object]]:
        return self.latest_snapshot().to_software_status_snapshot()

    def is_stopped(self) -> bool:
        with self._lock:
            return bool(self._stopped and not any(not future.done() for future in self._active_futures))

    def status_snapshot(
        self,
        *,
        force: bool = False,
        force_process_snapshot: bool = True,
        port_override: Optional[int] = None,
        host_override: Optional[str] = None,
        flrig_port_override: Optional[int] = None,
        flrig_host_override: Optional[str] = None,
        rigctld_port_override: Optional[int] = None,
        rigctld_host_override: Optional[str] = None,
        fldigi_port_override: Optional[int] = None,
        fldigi_host_override: Optional[str] = None,
        instance_identities: Optional[Mapping[str, Mapping[str, object]]] = None,
        verify_control_readback: bool = False,
        control_backend: str = "",
        control_probe_only: bool = False,
    ) -> Dict[str, Dict[str, object]]:
        """Return the latest endpoint-scoped snapshot and refresh it asynchronously.

        Settings and other UI surfaces use this compatibility-shaped API instead
        of calling :class:`SoftwareStatusService` directly. The first request
        returns the shared process snapshot while the endpoint-specific probe is
        queued; subsequent calls return the completed immutable scoped snapshot.
        """
        overrides: Dict[str, object] = {
            "port_override": port_override,
            "host_override": host_override,
            "flrig_port_override": flrig_port_override,
            "flrig_host_override": flrig_host_override,
            "rigctld_port_override": rigctld_port_override,
            "rigctld_host_override": rigctld_host_override,
            "fldigi_port_override": fldigi_port_override,
            "fldigi_host_override": fldigi_host_override,
            "verify_control_readback": bool(verify_control_readback),
            "control_backend": str(control_backend or "").strip().lower(),
            "control_probe_only": bool(control_probe_only),
        }
        exact_identities = {
            str(name): dict(identity)
            for name, identity in (instance_identities or {}).items()
            if isinstance(identity, Mapping)
        }
        if exact_identities:
            overrides["instance_identities"] = exact_identities
        scope = self._endpoint_scope(overrides)
        with self._lock:
            cached = self._scoped_snapshots.get(scope)
            stopped = self._stopped
            pending = scope in self._scoped_pending
        if stopped:
            return cached.to_software_status_snapshot() if cached is not None else self.software_status_snapshot()
        if cached is not None and cached.is_fresh_enough(SCOPED_STATUS_STALE_AFTER_SEC) and not force:
            return cached.to_software_status_snapshot()
        if not pending:
            with self._lock:
                if scope not in self._scoped_pending and not self._stopped:
                    self._scoped_pending.add(scope)
                    self._sequence += 1
                    sequence = self._sequence
                else:
                    sequence = 0
            if sequence:
                future = self._executor.submit(
                    self._build_endpoint_snapshot,
                    sequence,
                    scope,
                    overrides,
                    bool(force),
                    bool(force_process_snapshot),
                )
                with self._lock:
                    self._active_futures.add(future)
                future.add_done_callback(lambda done, scoped=scope: self._on_scoped_worker_done(scoped, done))
        if cached is not None:
            return cached.to_software_status_snapshot()
        fallback = self.software_status_snapshot()
        # The shared snapshot intentionally answers only "is any process in
        # this family running?"  Do not present that answer as selected-radio
        # state while the exact executable+argument probe is pending.
        for program_name in (instance_identities or {}):
            status_key = "JS8Call_API" if str(program_name) == "JS8Call" else str(program_name)
            if status_key not in fallback:
                continue
            fallback[status_key] = {
                **dict(fallback[status_key]),
                "state": "idle",
                "running": False,
                "reachable": False,
                "tooltip": f"Checking the selected radio's {program_name} instance…",
                "stale": True,
            }
        return fallback

    def refresh_now(self, *, reason: str = "manual", force: bool = False) -> DependencySnapshot:
        requested_reason = str(reason or "manual")
        with self._lock:
            if self._stopped:
                return self._latest_snapshot
            # A forced refresh bypasses freshness, not the single-flight rule.
            # Queueing another whole process walk behind an in-flight one only
            # makes the returned snapshot older and delays scoped requests.
            # Launch safety is the exception: an unrelated timer/startup walk
            # is not launch-owned evidence, so retain one coalesced dedicated
            # walk behind it rather than letting the caller accept that result.
            if self._worker_pending:
                if force and requested_reason.startswith("launch-preflight:"):
                    self._queued_process_refresh_reason = requested_reason
                return self._latest_snapshot
            self._worker_pending = True
            self._sequence += 1
            sequence = self._sequence
        future = self._executor.submit(
            self._build_process_snapshot,
            sequence,
            requested_reason,
        )
        with self._lock:
            self._active_futures.add(future)
        future.add_done_callback(self._on_worker_done)
        return self.latest_snapshot()

    @Slot()
    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._worker_pending = False
            self._queued_process_refresh_reason = ""
            self._scoped_pending.clear()
            futures = tuple(self._active_futures)
        self._cancel_token.cancel()
        for future in futures:
            future.cancel()
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._executor.shutdown(wait=False)
        except Exception:
            pass

    def _on_worker_done(self, future: Future) -> None:
        with self._lock:
            self._active_futures.discard(future)
            stopped = self._stopped
        if stopped:
            return
        try:
            snapshot = future.result()
        except OperationCancelled:
            self._release_failed_process_refresh()
            return
        except Exception as exc:
            log.warning("DEPENDENCY_STATUS|refresh_failed|error=%s", exc)
            self._release_failed_process_refresh()
            return
        self._snapshot_ready.emit(snapshot)

    def _release_failed_process_refresh(self) -> None:
        queued_reason = ""
        with self._lock:
            self._worker_pending = False
            if not self._stopped:
                queued_reason = self._queued_process_refresh_reason
            self._queued_process_refresh_reason = ""
        if queued_reason:
            self.refresh_now(reason=queued_reason, force=True)

    def _on_scoped_worker_done(self, scope: str, future: Future) -> None:
        with self._lock:
            self._active_futures.discard(future)
            stopped = self._stopped
        if stopped:
            return
        try:
            snapshot = future.result()
        except OperationCancelled:
            with self._lock:
                self._scoped_pending.discard(scope)
            return
        except Exception as exc:
            log.warning("DEPENDENCY_STATUS|scoped_refresh_failed|scope=%s|error=%s", scope, exc)
            with self._lock:
                self._scoped_pending.discard(scope)
            return
        self._snapshot_ready.emit(snapshot)

    @Slot(object)
    def _publish_snapshot(self, snapshot: DependencySnapshot) -> None:
        queued_reason = ""
        with self._lock:
            if self._stopped:
                return
            if snapshot.scope == LEGACY_PRIMARY_DEPENDENCY_SCOPE:
                self._latest_snapshot = snapshot
                self._worker_pending = False
                queued_reason = self._queued_process_refresh_reason
                self._queued_process_refresh_reason = ""
            else:
                self._scoped_snapshots[snapshot.scope] = snapshot
                self._scoped_pending.discard(snapshot.scope)
        if queued_reason:
            self.refresh_now(reason=queued_reason, force=True)
        self.snapshot_changed.emit(snapshot)

    @staticmethod
    def _endpoint_scope(overrides: Mapping[str, object]) -> str:
        parts = []
        for key in sorted(overrides):
            value = overrides.get(key)
            parts.append(f"{key}={'' if value is None else value}")
        return "software_endpoint:" + "|".join(parts)

    def _build_endpoint_snapshot(
        self,
        sequence: int,
        scope: str,
        overrides: Mapping[str, object],
        force: bool,
        force_process_snapshot: bool,
    ) -> DependencySnapshot:
        started = time.perf_counter()
        self._cancel_token.checkpoint()
        checked_at = time.time()
        probe = SoftwareStatusService(self.settings)
        kwargs = {str(key): value for key, value in overrides.items()}
        rows = probe.status_snapshot(
            force=bool(force),
            force_process_snapshot=bool(force_process_snapshot),
            **kwargs,
        )
        self._cancel_token.checkpoint()
        statuses: Dict[str, DependencyStatus] = {}
        for key in dict.fromkeys((*STATUS_KEYS, *rows)):
            self._cancel_token.checkpoint()
            row = dict(rows.get(key, {}) or {})
            state = str(row.pop("state", "idle") or "idle")
            tooltip = str(row.pop("tooltip", f"{key}: status not checked yet") or "")
            running = bool(row.pop("running", False))
            reachable = row.pop("reachable", None)
            statuses[key] = DependencyStatus(
                key=key,
                state=state,
                value=str(row.pop("value", "") or "") or None,
                checked_at=checked_at,
                updated_at=checked_at,
                stale_after_sec=SCOPED_STATUS_STALE_AFTER_SEC,
                source="endpoint",
                tooltip=tooltip,
                running=running,
                reachable=bool(reachable) if reachable is not None else None,
                last_success_at=checked_at if state != "error" else None,
                last_error_at=checked_at if state == "error" else None,
                last_error=str(row.pop("last_error", "") or ""),
                meta=row,
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms > 250.0 and _logger_handlers_available():
            log.info(
                "DEPENDENCY_STATUS|slow_endpoint_snapshot|scope=%s|duration_ms=%.1f",
                scope,
                elapsed_ms,
            )
        return DependencySnapshot(
            generated_at=checked_at,
            scope=scope,
            process=statuses,
            reason="endpoint",
            sequence=sequence,
        )

    def _build_process_snapshot(self, sequence: int, reason: str) -> DependencySnapshot:
        started = time.perf_counter()
        checked_at = time.time()
        probe = SoftwareStatusService(self.settings)
        # Launch authorization needs complete command-line attribution. Keep
        # routine timer/UI inventories cheap, but make the launch-owned walk
        # inspect every process off the GUI thread before absence can authorize
        # a spawn.
        probe._refresh_process_snapshot(
            force=True,
            inspect_all=str(reason or "").startswith("launch-preflight:"),
        )
        process_records = tuple(dict(record) for record in probe._proc_records)
        statuses: Dict[str, DependencyStatus] = {}
        for status_key in STATUS_KEYS:
            self._cancel_token.checkpoint()
            program_name = _status_program_name(status_key)
            item_started = time.perf_counter()
            try:
                running = bool(probe.program_is_running(program_name))
                capability: Dict[str, object] = {}
                value = self._status_value(status_key, running, capability)
                state = self._status_state(status_key, running, capability)
                tooltip = self._process_tooltip(status_key, program_name, running, probe, capability)
                duration_ms = (time.perf_counter() - item_started) * 1000.0
                statuses[status_key] = DependencyStatus(
                    key=status_key,
                    state=state,
                    value=value,
                    checked_at=checked_at,
                    updated_at=checked_at,
                    stale_after_sec=PROCESS_STATUS_STALE_AFTER_SEC,
                    source="process",
                    tooltip=tooltip,
                    running=running,
                    reachable=None,
                    last_success_at=checked_at,
                    duration_ms=duration_ms,
                    slow=duration_ms > 250.0,
                    meta=self._status_meta(status_key, program_name, capability),
                )
            except Exception as exc:
                duration_ms = (time.perf_counter() - item_started) * 1000.0
                statuses[status_key] = DependencyStatus(
                    key=status_key,
                    state="error",
                    value="unknown",
                    checked_at=checked_at,
                    updated_at=checked_at,
                    stale_after_sec=PROCESS_STATUS_STALE_AFTER_SEC,
                    source="process",
                    tooltip=f"{program_name}: process status check failed",
                    running=False,
                    reachable=None,
                    last_error_at=checked_at,
                    last_error=str(exc or "process status check failed"),
                    duration_ms=duration_ms,
                    slow=duration_ms > 250.0,
                    meta={"program": program_name},
                )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            stopped = self._stopped
        if elapsed_ms > 250.0 and not stopped and _logger_handlers_available():
            log.info(
                "DEPENDENCY_STATUS|slow_process_snapshot|scope=%s|duration_ms=%.1f|reason=%s",
                LEGACY_PRIMARY_DEPENDENCY_SCOPE,
                elapsed_ms,
                reason,
            )
        return DependencySnapshot(
            generated_at=checked_at,
            scope=LEGACY_PRIMARY_DEPENDENCY_SCOPE,
            station_id=None,
            radio_id=None,
            process=statuses,
            process_records=process_records,
            reason=reason,
            sequence=sequence,
        )

    def _process_tooltip(
        self,
        status_key: str,
        program_name: str,
        running: bool,
        probe: SoftwareStatusService,
        capability: Optional[Mapping[str, object]] = None,
    ) -> str:
        if status_key == "JS8Call_API":
            if not capability:
                return (
                    "JS8Call process is running. Endpoint readiness refreshes separately."
                    if running
                    else "JS8Call is not running."
                )
            return self._js8_capability_tooltip(capability or {}, running=running)
        if status_key in {"FLRig", "FLDigi"}:
            return (
                f"{program_name} process is running. Routine UI status avoids repeated XML-RPC probes."
                if running
                else f"{program_name} is not running."
            )
        if status_key == "VarAC" and running:
            exe = probe.find_process_exe("VarAC")
            if exe:
                return f"Running: {exe}"
        return "Running" if running else "Not running"

    @staticmethod
    def _status_value(status_key: str, running: bool, capability: Mapping[str, object]) -> str:
        if status_key == "JS8Call_API":
            if not capability:
                return "running_unverified" if running else "offline"
            return str(capability.get("mode", "offline") or "offline")
        return "running" if running else "not_running"

    @staticmethod
    def _status_state(status_key: str, running: bool, capability: Mapping[str, object]) -> str:
        if status_key == "JS8Call_API":
            if not capability:
                return "warn" if running else "idle"
            mode = str(capability.get("mode", "offline") or "offline")
            if mode in {"api_full", "api_basic", "file_fallback"}:
                return "ok"
            return "warn" if running else "idle"
        return "ok" if running else "idle"

    @staticmethod
    def _status_meta(status_key: str, program_name: str, capability: Mapping[str, object]) -> Dict[str, object]:
        meta: Dict[str, object] = {"program": program_name}
        if status_key == "JS8Call_API" and capability:
            meta.update(
                {
                    "capability_mode": str(capability.get("mode", "") or ""),
                    "version": str(capability.get("version", "") or ""),
                    "endpoint": str(capability.get("endpoint", "") or ""),
                    "supported": dict(capability.get("supported", {}) or {}),
                }
            )
        return meta

    @staticmethod
    def _js8_capability_tooltip(capability: Mapping[str, object], *, running: bool) -> str:
        endpoint = str(capability.get("endpoint", "") or "").strip()
        version = str(capability.get("version", "") or "").strip()
        mode = str(capability.get("mode", "offline") or "offline")
        version_part = f" Version: {version}." if version else ""
        if mode == "api_full":
            return f"JS8Call API is ready at {endpoint}.{version_part}"
        if mode == "api_basic":
            return f"JS8Call API is reachable at {endpoint}; CommsPlan will keep compatibility fallbacks available.{version_part}"
        if mode == "file_fallback":
            return f"JS8Call API support is limited at {endpoint}; CommsPlan will use log/database fallbacks.{version_part}"
        if running:
            return f"JS8Call is running, but CommsPlan could not verify the TCP API at {endpoint}."
        return "JS8Call is not running."


_SERVICE_LOCK = threading.Lock()
_SERVICE: Optional[DependencyStatusService] = None


def get_dependency_status_service(settings: Any, parent: Optional[QObject] = None) -> DependencyStatusService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None or bool(getattr(_SERVICE, "_stopped", False)):
            _SERVICE = DependencyStatusService(settings, parent=None)
        elif settings is not None:
            _SERVICE.settings = settings
        return _SERVICE


def shutdown_dependency_status_service() -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        service = _SERVICE
        _SERVICE = None
    if service is None:
        return
    try:
        service.stop()
    except Exception:
        pass
    try:
        service.deleteLater()
    except Exception:
        pass


atexit.register(shutdown_dependency_status_service)
